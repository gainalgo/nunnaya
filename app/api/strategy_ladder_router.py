# ============================================================
# File: app/api/strategy_ladder_router.py
# Extracted from strategy_router.py — Phase 1-B (file diet)
#
# LADDER 전략 셋업/조회/중지 엔드포인트
# [FROZEN] 코드 내용 변경 없이 이동만
# ============================================================

from fastapi import APIRouter, Request, Query, Body
from typing import Dict, Any, List, Optional
import logging
from pydantic import BaseModel
from app.manager.oma_market_registry import MarketState
from app.core.hyper_price_store import price_store
from app.core.constants import (
    BYBIT_MARKET_TICKERS,
    bybit_v5_rest_category,
    parse_bybit_list,
    normalize_bybit_ticker,
)
from app.core.currency import Q
from app.core.rate_limiter import bybit_get
from app.api.strategy_utils import _sync_policy_tp_sl, StrategyStopRequest

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================
# Pydantic Models
# ============================================================
class LadderSetupRequest(BaseModel):
    market: str
    budget_usdt: float
    order_usdt: Optional[float] = None
    step_pct: float = 1.0
    steps: Optional[int] = None
    max_steps: int = 10
    martingale: float = 1.0
    tp_pct: float = 2.0
    buy_now: bool = False
    step_gap_atr_enabled: bool = False
    step_gap_atr_mult: float = 1.0
    grid_auto_sync: bool = True
    auto_center: bool = True
    max_down_buys: int = 3
    reversal_pct: float = 1.5
    profit_borrow_enabled: bool = False
    profit_borrow_max: int = 3
    spacing_mode: Optional[str] = None
    spacing_value: Optional[float] = None
    emergency_last_step_enabled: bool = True
    emergency_last_step_gap_mult: float = 2.0
    emergency_last_step_buy_mult: float = 0.5
    tune_mode: Optional[str] = None

    def __init__(self, **data):
        if "budget" in data and "budget_usdt" not in data:
            data["budget_usdt"] = data["budget"]
        if "tp" in data and "tp_pct" not in data:
            data["tp_pct"] = data["tp"]
        if "steps" in data and "max_steps" not in data:
            data["max_steps"] = data["steps"]
        if "use_atr" in data:
            data.setdefault("step_gap_atr_enabled", data["use_atr"])
        super().__init__(**data)

    @property
    def budget(self) -> float:
        return self.budget_usdt or 0.0


# ============================================================
# Ladder 실현손익/수수료/매수/매도 리셋 API
# ============================================================
@router.post(
    "/ladder/reset_stats",
    summary="Reset realized PnL, fee, buy/sell count for LADDER markets",
    responses={200: {"description": "LADDER stats reset"}},
)
def reset_ladder_stats(request: Request, market: Optional[str] = Body(None, embed=True)):
    """
    실현손익/수수료/매수/매도 카운트 리셋 (market 지정 시 단일, 미지정 시 전체)
    """
    system = request.app.state.system
    mgr = getattr(system, "ladder_manager", None)
    if mgr is None:
        from app.manager.ladder_manager import LadderManager
        mgr = LadderManager(system)
        system.ladder_manager = mgr
    if market:
        ok = mgr.reset_stats_for_market(market)
        return {"ok": ok, "market": market}
    else:
        count = mgr.reset_stats_for_all()
        return {"ok": True, "reset_count": count}


# ============================================================
# LADDER Setup
# ============================================================
@router.post(
    "/ladder/setup",
    summary="Setup a market with LADDER strategy",
    responses={
        200: {"description": "Market configured with LADDER strategy"},
    },
)
def setup_ladder_market(req: LadderSetupRequest, request: Request):
    """
    Register or update a market with LADDER strategy.

    - Sets OMA state to ACTIVE with specified budget
    - Configures step_pct, max_steps, martingale, and TP parameters
    - Persists configuration immediately
    """
    system = request.app.state.system
    market = req.market.strip().upper()

    # 1. Set OMA State to ACTIVE with Budget
    if req.budget_usdt is None or req.budget_usdt <= 0:
        raise ValueError("budget_usdt must be specified and > 0 for LADDER setup.")
    system.oma_set_market(
        market=market,
        state=MarketState.ACTIVE,
        reason=["ladder_factory_setup"],
        budget_usdt=req.budget_usdt
    )

    # 2. Configure Strategy Controls (LADDER)
    # We construct the control patch manually to ensure params are set correctly
    ctx = system.coordinator.ensure_market(market)

    fields_set = getattr(req, "__fields_set__", None)
    if fields_set is None:
        fields_set = getattr(req, "model_fields_set", set())
    fields_set = set(fields_set or [])
    tune_mode_raw = str(req.tune_mode or "").upper()
    tune_mode = tune_mode_raw if tune_mode_raw in ("AUTO", "MANUAL") else "AUTO"
    auto_center = bool(getattr(req, "auto_center", False))
    tune_mode_explicit = ("tune_mode" in fields_set) or bool(tune_mode_raw)
    if not tune_mode_explicit:
        explicit_manual = (
            auto_center
            or (req.steps is not None)
            or (req.spacing_mode is not None)
            or (req.spacing_value is not None)
            or (req.order_usdt is not None)
        )
        if explicit_manual:
            tune_mode = "MANUAL"
    if tune_mode not in ("AUTO", "MANUAL"):
        tune_mode = "AUTO"
    order_override = None
    try:
        if req.order_usdt is not None and float(req.order_usdt) > 0:
            order_override = float(req.order_usdt)
    except (TypeError, ValueError):
        logger.warning("strategy_ladder_router.setup_ladder_market L155 except", exc_info=True)
        order_override = None
    if order_override is not None and order_override < Q.min_order:
        order_override = float(Q.min_order)
    spacing_mode = str(req.spacing_mode or "PERCENT").upper()
    if spacing_mode not in ("PERCENT", Q.symbol):
        spacing_mode = "PERCENT"
    spacing_value = None
    try:
        if req.spacing_value is not None and float(req.spacing_value) > 0:
            spacing_value = float(req.spacing_value)
    except (TypeError, ValueError):
        logger.warning("strategy_ladder_router.setup_ladder_market L166 except", exc_info=True)
        spacing_value = None
    if spacing_value is None:
        if spacing_mode == "PERCENT":
            spacing_value = float(req.step_pct or 0.5)
        else:
            spacing_value = 0.0
    step_pct_for_params = float(req.step_pct or 0.5)
    if spacing_mode == Q.symbol:
        try:
            cur_for_pct = float(price_store.get_price(market) or 0.0)
            if cur_for_pct > 0 and spacing_value > 0:
                step_pct_for_params = (spacing_value / cur_for_pct) * 100.0
        except (TypeError, ValueError) as exc:
            logger.warning("[LADDER_STRAT_API] strategy_ladder_router fallback: %s", exc, exc_info=True)
    patch = {
        "strategy": {
            "enabled": True,
            "mode": "LADDER",
            "params": {
                "step_pct": step_pct_for_params,
                "spacing_mode": spacing_mode,
                "spacing_value": spacing_value,
                "max_steps": req.max_steps,
                "martingale": req.martingale,
                "tp": req.tp_pct,
                "sl": -5.0,
                "min_order_usdt": Q.min_order,
                "trailing_entry": True,
                "trailing_entry_pct": 0.5,
                "reset_on_exit": True,
                "step_gap_atr_enabled": req.step_gap_atr_enabled,
                "step_gap_atr_mult": req.step_gap_atr_mult,
                "grid_auto_sync": req.grid_auto_sync,
                "auto_center": auto_center,
                "max_down_buys": req.max_down_buys,
                "reversal_pct": req.reversal_pct,
                "profit_borrow_enabled": req.profit_borrow_enabled,
                "profit_borrow_max": req.profit_borrow_max,
                "emergency_last_step_enabled": req.emergency_last_step_enabled,
                "emergency_last_step_gap_mult": req.emergency_last_step_gap_mult,
                "emergency_last_step_buy_mult": req.emergency_last_step_buy_mult,
                "tune_mode": tune_mode,
            }
        }
    }

    ctx.update_controls(patch)
    _sync_policy_tp_sl(ctx, tp=req.tp_pct, sl=-5.0)

    # 3. 즉시 첫 매수 (포지션 없을 때만)
    buy_result = None
    has_position = False
    try:
        pos = getattr(ctx, "position", None)
        if pos and float(pos.get("qty", 0.0) or 0.0) > 0:
            has_position = True
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[LADDER_STRAT_API] 3. 즉시 첫 매수 (포지션 없을 때만): %s", exc, exc_info=True)

    if bool(getattr(req, "buy_now", False)) and req.budget > 0 and not has_position:
        current_price = price_store.get_price(market) or 0.0
        if current_price <= 0:
            try:
                exchange_market = Q.normalize(market)
                resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=1.0)
                if resp.status_code == 200:
                    for _t in parse_bybit_list(resp.json()):
                        if isinstance(_t, dict):
                            _tc = normalize_bybit_ticker(_t)
                            if _tc.get("market", "").upper() == exchange_market.upper():
                                current_price = float(_tc.get("trade_price") or 0.0)
                                break
            except Exception as exc:
                logger.warning("[LADDER_STRAT_API] 3. 즉시 첫 매수 (포지션 없을 때만): %s", exc, exc_info=True)

        per_step_usdt = order_override if order_override is not None else (req.budget / max(req.max_steps, 1))

        if hasattr(system, "order_fsm") and system.order_fsm and current_price > 0:
            ok, msg = system.order_fsm.submit_market_buy(
                ctx=ctx,
                market=market,
                quote_amount=per_step_usdt,
                expected_price=current_price,
                reason="ladder:deploy_first_buy"
            )
            buy_result = {"ok": ok, "msg": str(msg)}
            if ok:
                try:
                    qty = per_step_usdt / current_price
                    ctx.open_position(entry_price=current_price, usdt_amount=per_step_usdt, source="ladder_deploy")
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[LADDER_STRAT_API] strategy_ladder_router fallback: %s", exc, exc_info=True)
        elif system.trading_mode == "PAPER" and current_price > 0:
            ctx.open_position(entry_price=current_price, usdt_amount=per_step_usdt, source="paper")
            system.ledger.append("PAPER_BUY_NOW", market=market, price=current_price, usdt=per_step_usdt)
            buy_result = {"ok": True, "msg": "paper_filled"}

    # Persist
    system._save_context_state()

    # 4. ladder_config.json 자동 생성 → ICAG V3 sync → 업비트 예약 주문
    grid_sync_result = None
    ladder_cfg_saved = None
    try:
        mgr = getattr(system, "ladder_manager", None)
        if mgr is None:
            from app.manager.ladder_manager import LadderManager
            mgr = LadderManager(system)
            system.ladder_manager = mgr

        existing_cfg = mgr.get_config(market)
        existing_lower = float(existing_cfg.get("lower_bound") or 0)
        existing_upper = float(existing_cfg.get("upper_bound") or 0)

        stats = mgr.get_market_stats(market)
        cur = float(stats.get("last_price") or 0)
        hi = float(stats.get("hi_24h") or 0)
        lo = float(stats.get("lo_24h") or 0)
        if cur <= 0:
            cur = float(price_store.get_price(market) or 0)

        bounds_missing = existing_lower <= 0 or existing_upper <= 0
        bounds_stale = cur > 0 and (
            cur < existing_lower * 0.5 or cur > existing_upper * 2.0
        )
        needs_auto_config = bounds_missing or bounds_stale

        if tune_mode == "MANUAL":
            lower = existing_lower
            upper = existing_upper
            spacing_val = float(spacing_value or 0.0)
            if spacing_mode == Q.symbol:
                if spacing_val <= 0 and cur > 0 and float(req.step_pct or 0) > 0:
                    spacing_val = cur * (float(req.step_pct) / 100.0)
            else:
                if spacing_val <= 0:
                    spacing_val = float(req.step_pct or 0.5)
            if auto_center and cur > 0:
                levels = int(req.max_steps or req.steps or 10)
                levels = max(1, levels)
                per_side = max(1, levels // 2)
                if spacing_mode == Q.symbol:
                    lower = cur - (spacing_val * per_side)
                    upper = cur + (spacing_val * per_side)
                else:
                    lower = cur * (1.0 - (spacing_val / 100.0) * per_side)
                    upper = cur * (1.0 + (spacing_val / 100.0) * per_side)
                if lower <= 0:
                    lower = cur * 0.9
                if upper <= lower:
                    upper = cur * 1.05
            elif needs_auto_config and cur > 0:
                lower = lo * 0.97 if lo > 0 else cur * 0.95
                upper = hi * 1.03 if hi > 0 else cur * 1.05
                lower = min(lower, cur * 0.98)
                upper = max(upper, cur * 1.02)
            order_usdt_val = int(order_override) if order_override is not None else (int(req.budget / max(req.max_steps, 1)) if req.budget > 0 else 10)
            order_usdt_val = max(order_usdt_val, int(Q.min_order))
            cfg_to_save = {
                "market": market,
                "enabled": True,
                "lower_bound": round(lower, 2),
                "upper_bound": round(upper, 2),
                "spacing_mode": spacing_mode,
                "spacing_value": float(spacing_val),
                "order_usdt": order_usdt_val,
                "ladder_fixed_order_usdt": order_usdt_val,
                "max_levels": int(req.max_steps or 10),
                "tune_mode": "MANUAL",
                "auto_center": auto_center,
                "grid_auto_sync": req.grid_auto_sync,
                "emergency_last_step_enabled": req.emergency_last_step_enabled,
                "emergency_last_step_gap_mult": req.emergency_last_step_gap_mult,
                "emergency_last_step_buy_mult": req.emergency_last_step_buy_mult,
            }
            ladder_cfg_saved = mgr.save_config(cfg_to_save)
        elif needs_auto_config:
            if cur > 0:
                lower = lo * 0.97 if lo > 0 else cur * 0.95
                upper = hi * 1.03 if hi > 0 else cur * 1.05
                lower = min(lower, cur * 0.98)
                upper = max(upper, cur * 1.02)

                cfg_spacing_mode = spacing_mode
                if cfg_spacing_mode == Q.symbol and spacing_value and spacing_value > 0:
                    cfg_spacing_value = float(spacing_value)
                else:
                    cfg_spacing_mode = "PERCENT"
                    try:
                        cfg_spacing_value = mgr.auto_set_spacing_value(market)
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                        logger.warning("strategy_ladder_router.setup_ladder_market L357 except", exc_info=True)
                        cfg_spacing_value = float(req.step_pct or 0.5)
                max_levels_needed = mgr._suggest_max_levels(
                    upper, lower, cfg_spacing_mode, cfg_spacing_value, cap=40
                )
                order_usdt_val = int(order_override) if order_override is not None else (int(req.budget / max_levels_needed) if max_levels_needed > 0 and req.budget > 0 else 10)
                order_usdt_val = max(order_usdt_val, int(Q.min_order))

                cfg_to_save = {
                    "market": market,
                    "enabled": True,
                    "lower_bound": round(lower, 2),
                    "upper_bound": round(upper, 2),
                    "spacing_mode": cfg_spacing_mode,
                    "spacing_value": cfg_spacing_value,
                    "order_usdt": order_usdt_val,
                    "ladder_fixed_order_usdt": order_usdt_val,
                    "max_levels": max_levels_needed,
                    "tune_mode": "AUTO",
                    "auto_center": auto_center,
                    "grid_auto_sync": req.grid_auto_sync,
                    "emergency_last_step_enabled": req.emergency_last_step_enabled,
                    "emergency_last_step_gap_mult": req.emergency_last_step_gap_mult,
                    "emergency_last_step_buy_mult": req.emergency_last_step_buy_mult,
                }
                ladder_cfg_saved = mgr.save_config(cfg_to_save)
                logger.info("LADDER auto-config created: %s bounds=[%.2f ~ %.2f] spacing=%.2f%% order_usdt=%d",
                            market, lower, upper, spacing_value, order_usdt_val)
        else:
            if req.budget > 0 and int(req.max_steps or 0) > 0:
                order_usdt_val = int(order_override) if order_override is not None else int(req.budget / req.max_steps)
                order_usdt_val = max(order_usdt_val, int(Q.min_order))
                existing_cfg["order_usdt"] = order_usdt_val
                existing_cfg["ladder_fixed_order_usdt"] = order_usdt_val
                existing_cfg["enabled"] = True
                existing_cfg["tune_mode"] = tune_mode
                existing_cfg["auto_center"] = auto_center
                spacing_val = float(spacing_value or existing_cfg.get("spacing_value") or 0.5)
                if spacing_mode == Q.symbol:
                    if spacing_val <= 0 and cur > 0 and float(req.step_pct or 0) > 0:
                        spacing_val = cur * (float(req.step_pct) / 100.0)
                else:
                    if spacing_val <= 0:
                        spacing_val = float(req.step_pct or 0.5)
                existing_cfg["spacing_mode"] = spacing_mode
                existing_cfg["spacing_value"] = float(spacing_val)
                existing_cfg["max_levels"] = int(req.max_steps or existing_cfg.get("max_levels") or 10)
                existing_cfg["grid_auto_sync"] = req.grid_auto_sync
                existing_cfg["emergency_last_step_enabled"] = req.emergency_last_step_enabled
                existing_cfg["emergency_last_step_gap_mult"] = req.emergency_last_step_gap_mult
                existing_cfg["emergency_last_step_buy_mult"] = req.emergency_last_step_buy_mult
                if auto_center and cur > 0:
                    levels = int(existing_cfg.get("max_levels") or 10)
                    levels = max(1, levels)
                    per_side = max(1, levels // 2)
                    if spacing_mode == Q.symbol:
                        lower = cur - (spacing_val * per_side)
                        upper = cur + (spacing_val * per_side)
                    else:
                        lower = cur * (1.0 - (spacing_val / 100.0) * per_side)
                        upper = cur * (1.0 + (spacing_val / 100.0) * per_side)
                    if lower > 0 and upper > lower:
                        existing_cfg["lower_bound"] = round(lower, 2)
                        existing_cfg["upper_bound"] = round(upper, 2)
                ladder_cfg_saved = mgr.save_config(existing_cfg)

        mgr.reset_stats_for_market(market)
        logger.info("LADDER stats reset on setup: %s", market)

        # ICAG V3 initial sync
        from app.manager.ladder_grid_v3 import LadderGridV3
        grid_v3 = getattr(system, "_ladder_grid_v3", None)
        if grid_v3 is None or grid_v3 is False:
            grid_v3 = LadderGridV3(mgr)
            system._ladder_grid_v3 = grid_v3
        if req.grid_auto_sync:
            grid_sync_result = grid_v3.sync_active_window(market)
        else:
            grid_sync_result = {"skipped": True, "reason": "grid_auto_sync_disabled"}
    except Exception as e:
        logger.error("LADDER setup grid sync failed: %s — %s", market, e)
        grid_sync_result = {"error": str(e)}

    return {
        "ok": True, "market": market, "setup": req.dict(),
        "buy_result": buy_result, "grid_sync": grid_sync_result,
        "ladder_config": ladder_cfg_saved,
    }

@router.get(
    "/ladder/test-sync",
    summary="Test grid sync for a LADDER market (diagnostic)",
)
def test_ladder_sync(request: Request, market: str = Query(...)):
    system = request.app.state.system
    market = market.strip().upper()
    try:
        from app.manager.ladder_grid_v3 import LadderGridV3
        mgr = getattr(system, "ladder_manager", None)
        if mgr is None:
            return {"ok": False, "error": "ladder_manager not found"}
        grid_v3 = getattr(system, "_ladder_grid_v3", None)
        if grid_v3 is None or grid_v3 is False:
            grid_v3 = LadderGridV3(mgr)
            system._ladder_grid_v3 = grid_v3
        result = grid_v3.poll_and_sync(market)
        return {"ok": True, "market": market, "engine": "icag_v3", "result": result}
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("strategy_ladder_router.test_ladder_sync L464: %s", e)
        import traceback
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}

@router.get(
    "/ladder/list",
    summary="List all LADDER strategy markets",
    responses={
        200: {"description": "List of markets running LADDER strategy with status"},
    },
)
def list_ladder_markets(request: Request):
    """
    List all markets currently running LADDER strategy.

    - Includes position, PnL, params, and readiness status
    - Sorted for admin dashboard display
    """
    system = request.app.state.system
    items = []
    contexts = getattr(system.coordinator, "contexts", {}) or {}
    for market, ctx in list(contexts.items()):
        # Check if strategy is enabled and mode is LADDER
        ctrls = getattr(ctx, "controls", {}) or {}
        strat = ctrls.get("strategy", {}) or {}
        if not strat.get("enabled"):
            continue
        mode = str(strat.get("mode") or "").upper()
        if mode != "LADDER":
            continue

        # Collect info
        params = strat.get("params", {})
        pos = getattr(ctx, "position", {}) or {}

        # Calculate PnL
        current_price = price_store.get_price(market)
        entry = float(pos.get("entry") or 0.0)
        qty = float(pos.get("qty") or 0.0)
        pnl = 0.0
        pnl_pct = 0.0
        val = 0.0
        if current_price and qty > 0:
            val = current_price * qty
            pnl = val - (entry * qty)
            if entry > 0:
                pnl_pct = (current_price - entry) / entry * 100.0

        # Stats & Status
        readiness = ctx.readiness_status()
        trade_count = getattr(ctx, "win_count", 0) + getattr(ctx, "loss_count", 0)
        total_profit = getattr(ctx, "total_profit", 0.0)
        last_signal = getattr(ctx, "last_signal", "none")

        # --- 실현손익, 수수료, 매수/매도 횟수 병합 ---
        realized_profit_usdt = 0.0
        total_fee_usdt = 0.0
        buy_count = 0
        sell_count = 0
        mgr = getattr(system, "ladder_manager", None)
        if mgr is not None:
            try:
                stats = mgr.get_market_stats(market)
                realized_profit_usdt = stats.get("realized_pnl", 0.0)
                total_fee_usdt = stats.get("total_fee", 0.0)
                buy_count = stats.get("buy_count", 0)
                sell_count = stats.get("sell_count", 0)
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning("[ladder/list] get_market_stats failed for %s: %s", market, e)

        items.append({
            "market": market,
            "state": getattr(ctx, "market_state", "UNKNOWN"),
            "budget": getattr(ctx, "allocated_capital", 0.0),
            "params": {
                "step_pct": params.get("step_pct", 1.0),
                "max_steps": params.get("max_steps", 10),
                "martingale": params.get("martingale", 1.0),
                "tp": params.get("tp", 2.0),
                "step_gap_atr_enabled": params.get("step_gap_atr_enabled", False),
                "step_gap_atr_mult": params.get("step_gap_atr_mult", 1.0),
                "grid_auto_sync": params.get("grid_auto_sync", True),
                "auto_center": params.get("auto_center", True),
                "spacing_mode": params.get("spacing_mode", "PERCENT"),
                "spacing_value": params.get("spacing_value", params.get("step_pct", 1.0)),
                "emergency_last_step_enabled": params.get("emergency_last_step_enabled", True),
                "emergency_last_step_gap_mult": params.get("emergency_last_step_gap_mult", 2.0),
                "emergency_last_step_buy_mult": params.get("emergency_last_step_buy_mult", 0.5),
            },
            "position": {
                "qty": pos.get("qty", 0.0),
                "entry": pos.get("entry", 0.0),
                "usdt": pos.get("usdt", 0.0),
            },
            "next_step": ctx.get_var("ladder_next_step", 1),
            "active": ctx.get_var("ladder_active", False),
            "pnl": {
                "current_price": current_price,
                "amount": pnl,
                "pct": pnl_pct,
                "value": val
            },
            "readiness": readiness,
            "trade_stats": {
                "count": trade_count,
                "realized_profit": total_profit
            },
            "last_signal": last_signal,
            "realized_profit_usdt": realized_profit_usdt,
            "total_fee_usdt": total_fee_usdt,
            "buy_count": buy_count,
            "sell_count": sell_count,
        })

    mgr = getattr(system, "ladder_manager", None)
    if mgr is None:
        try:
            from app.manager.ladder_manager import LadderManager
            mgr = LadderManager(system)
            system.ladder_manager = mgr
        except (ImportError, AttributeError, TypeError) as e:
            logger.warning("[ladder/list] LadderManager init failed: %s", e)
            mgr = None
    if mgr:
        try:
            configs = mgr.list_configs()
            cfg_map = {c.get("market"): c for c in configs if isinstance(c, dict)}
            for item in items:
                cfg = cfg_map.get(item["market"])
                if cfg:
                    item["realized_profit_usdt"] = cfg.get("realized_profit_usdt", 0.0)
                    item["total_fee_usdt"] = cfg.get("total_fee_usdt", 0.0)
                    item["buy_count"] = cfg.get("buy_count", 0)
                    item["sell_count"] = cfg.get("sell_count", 0)
                    if isinstance(item.get("params"), dict):
                        if cfg.get("spacing_mode"):
                            item["params"]["spacing_mode"] = cfg.get("spacing_mode")
                        if cfg.get("spacing_value") is not None:
                            item["params"]["spacing_value"] = cfg.get("spacing_value")
        except (KeyError, AttributeError, TypeError) as e:
            logger.warning("[ladder/list] list_configs failed: %s", e)

    return {"ok": True, "items": items}

@router.get(
    "/ladder/steps",
    summary="Get ladder step details for a market",
)
def get_ladder_steps(request: Request, market: str = Query(...)):
    """계단 상세 정보 (현재 계단 위치, 각 계단 가격, 체결 여부)."""
    system = request.app.state.system
    market = market.strip().upper()
    ctx = system.coordinator.contexts.get(market)
    if not ctx:
        return {"ok": False, "error": "market_not_found"}

    ctrls = getattr(ctx, "controls", {}) or {}
    strat = ctrls.get("strategy", {}) or {}
    params = strat.get("params", {}) or {}

    max_steps = int(params.get("max_steps", 10))
    step_pct = float(params.get("step_pct", 1.0))
    martingale = float(params.get("martingale", 1.0))
    tp_pct = float(params.get("tp", 2.0))
    cap_alloc = float(getattr(ctx, "allocated_capital", 0.0) or 0.0)

    base_price = float(ctx.get_var("ladder_base_price", 0.0))
    next_step = int(ctx.get_var("ladder_next_step", 1))
    ladder_active = bool(ctx.get_var("ladder_active", False))
    current_price = price_store.get_price(market) or 0.0

    avg_buy = float(getattr(ctx, "avg_buy_price", 0.0) or 0.0)
    holding_qty = float(getattr(ctx, "holding_qty", 0.0) or 0.0)

    steps = []
    for i in range(1, max_steps + 1):
        sp = base_price * (1.0 - step_pct * i / 100.0) if base_price > 0 else 0
        sb = (cap_alloc / max_steps)
        if martingale > 1.0 and i > 1:
            sb = sb * (martingale ** (i - 1))
        sb = min(sb, cap_alloc * 0.3)
        filled = i < next_step
        steps.append({
            "step": i,
            "price": round(sp, 2),
            "budget": round(sb, 0),
            "filled": filled,
            "status": "filled" if filled else ("next" if i == next_step else "waiting"),
        })

    pnl_amount = 0.0
    pnl_pct_val = 0.0
    if holding_qty > 0 and avg_buy > 0 and current_price > 0:
        pnl_amount = (current_price - avg_buy) * holding_qty
        pnl_pct_val = (current_price - avg_buy) / avg_buy * 100.0

    return {
        "ok": True,
        "market": market,
        "active": ladder_active,
        "base_price": base_price,
        "current_price": current_price,
        "next_step": next_step,
        "max_steps": max_steps,
        "step_pct": step_pct,
        "tp_pct": tp_pct,
        "budget": cap_alloc,
        "steps": steps,
        "position": {
            "avg_buy": avg_buy,
            "qty": holding_qty,
            "value": round(holding_qty * current_price, 0),
        },
        "pnl": {
            "amount": round(pnl_amount, 0),
            "pct": round(pnl_pct_val, 2),
        },
    }

@router.post(
    "/ladder/update_params",
    summary="Update LADDER strategy parameters",
    responses={
        200: {"description": "LADDER parameters updated"},
    },
)
def update_ladder_params(req: LadderSetupRequest, request: Request):
    """
    Update parameters for an existing LADDER market.

    - Does not reset OMA state
    - Updates step_pct, max_steps, martingale, and TP settings
    """
    # Reuse setup logic but maybe we don't want to force ACTIVE if it was PAUSED?
    # For now, setup_ladder_market is safe enough as it enforces the desired state.
    return setup_ladder_market(req, request)


# ============================================================
# LADDER Stop
# ============================================================
@router.post(
    "/ladder/stop",
    summary="Stop LADDER strategy for a market",
    responses={
        200: {"description": "LADDER strategy stopped"},
    },
)
def stop_ladder_market(req: StrategyStopRequest, request: Request):
    """
    Disable LADDER strategy for a market.

    - **liquidate**: If true, triggers recovery liquidation
    - **delete**: If true, sets state to DISABLED
    - Otherwise, sets state to WATCH
    """
    system = request.app.state.system
    market = req.market.strip().upper()

    # 1. Determine Target State & Reason
    if req.liquidate:
        target_state = MarketState.RECOVERY
        reason = ["ladder_stop_liquidate"]
    elif req.delete:
        target_state = MarketState.DISABLED
        reason = ["ladder_delete_btn", "user_disabled"]
    else:
        target_state = MarketState.WATCH
        reason = ["ladder_stop_btn"]

    system.oma_set_market(
        market=market,
        state=target_state,
        reason=reason
    )

    # 2. Liquidate if requested
    if req.liquidate:
        # Force sell via recovery mechanism
        system.request_recovery_liquidate(market=market, reason="ladder_stop_liquidate")

    ctx = system.coordinator.ensure_market(market)

    # Disable strategy
    patch = {
        "strategy": {
            "enabled": False
        }
    }
    ctx.update_controls(patch)
    system._save_context_state()

    # Optional cleanup: cancel ladder orders and disable ladder config to prevent re-seeding
    cleanup_requested = bool(req.delete or req.cleanup)
    if cleanup_requested:
        try:
            mgr = getattr(system, "ladder_manager", None)
            if mgr is None:
                from app.manager.ladder_manager import LadderManager
                system.ladder_manager = LadderManager(system=system)
                mgr = system.ladder_manager
            cfg = mgr.get_config(market)
            if isinstance(cfg, dict) and cfg:
                cfg["enabled"] = False
                try:
                    mgr.save_config(cfg)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[LADDER_STRAT_API] Optional cleanup: cancel ladder orders and disable ladder config to prevent re-s: %s", exc, exc_info=True)
                try:
                    mgr.cancel_ladder_orders(cfg)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[LADDER_STRAT_API] Optional cleanup: cancel ladder orders and disable ladder config to prevent re-s: %s", exc, exc_info=True)
        except (KeyError, AttributeError, TypeError) as exc:
            try:
                system.ledger.append("LADDER_STOP_CLEANUP_ERROR", market=market, error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[LADDER_STRAT_API] Optional cleanup: cancel ladder orders and disable ladder config to prevent re-s: %s", exc, exc_info=True)

    return {"ok": True, "market": market, "status": "stopped", "liquidating": req.liquidate}
