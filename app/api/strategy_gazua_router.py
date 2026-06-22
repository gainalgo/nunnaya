# ============================================================
# File: app/api/strategy_gazua_router.py
# Extracted from strategy_router.py — Phase 1-D (file diet)
#
# GAZUA 전략 셋업/조회/중지 엔드포인트
# ============================================================

from fastapi import APIRouter, Request, Query
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
from app.api.strategy_utils import (
    _check_manual_overflow, _generate_coin_warnings,
    _sync_policy_tp_sl, StrategyStopRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ============================================================
# Pydantic Models
# ============================================================
class GazuaSetupRequest(BaseModel):
    market: str
    budget_usdt: Optional[float] = None
    budget_usdt: Optional[float] = None  # deprecated: use budget_usdt
    tp_pct: float = 15.0
    tp_price: float = 0.0
    sl_pct: float = -10.0
    sl_price: float = 0.0
    hold_sell: bool = False  # True면 TP 도달해도 자동 매도 안 함
    user_sell_only: bool = False  # [2026-01-26] True면 사용자만 매도 가능 (TP/SL 완전 비활성화)
    deep_sl: bool = False  # [2026-02-01] True면 SL을 -50%로 설정 (장기 보유용)
    buy_now: bool = False    # True면 즉시 매수, False면 AI 판단
    buy_limit: bool = False
    trail_tp_enabled: bool = True   # Trailing TP: TP 도달 후 최고가 추적
    trail_dist_pct: float = 4.0     # Trail 거리 (%): 최고가 대비 하락 허용폭

    @property
    def budget(self) -> float:
        return self.budget_usdt or self.budget_usdt or 0.0

# ============================================================
# GAZUA Setup
# ============================================================
@router.post(
    "/gazua/setup",
    summary="Setup a market with GAZUA strategy",
    responses={
        200: {"description": "Market configured with GAZUA strategy"},
    },
)
def setup_gazua_market(req: GazuaSetupRequest, request: Request):
    """
    Register a market with GAZUA (moon-shot) strategy.

    - Sets OMA state to ACTIVE with specified budget
    - Configures TP/SL percentages or absolute prices
    - Optionally executes immediate buy (buy_now/buy_limit)
    """
    try:
        system = request.app.state.system
        market = req.market.strip().upper()

        # [2026-03-07] 수동 주문 슬롯 초과 체크 (+2 한도)
        overflow_check = _check_manual_overflow(system, "GAZUA", market)
        coin_warnings = _generate_coin_warnings(system, market, "GAZUA")
        if not overflow_check["allowed"]:
            return {"ok": False, "error": "slot_overflow", "detail": overflow_check["message"],
                    "overflow": overflow_check, "warnings": coin_warnings}

        # 1. Set OMA State to ACTIVE with Budget
        system.oma_set_market(
            market=market,
            state=MarketState.ACTIVE,
            reason=["gazua_factory_setup"],
            budget_usdt=req.budget
        )

        # 2. Configure Strategy Controls (GAZUA)
        from app.manager.market_controls import apply_engine_controls
        ctrls = apply_engine_controls(system, market, "GAZUA")

        # Apply custom TP
        ctx = system.coordinator.ensure_market(market)
        patch = {
            "strategy": { "params": {
                "tp": req.tp_pct,
                "tp_price": req.tp_price,
                "sl": req.sl_pct,
                "sl_price": req.sl_price,
                "hold_sell": req.hold_sell,
                "user_sell_only": req.user_sell_only,
                "buy_now": req.buy_now,
                "sell_fraction": 0.5,  # Fixed to 50% as requested
                "trail_tp_enabled": req.trail_tp_enabled,
                "trail_dist_pct": req.trail_dist_pct,
            }}
        }
        ctx.update_controls(patch)
        _sync_policy_tp_sl(ctx, tp=req.tp_pct, sl=req.sl_pct)
        system._save_context_state()

        buy_result = {}

        # 3. Buy Now (Optional)
        # [2026-01-28] 이미 포지션이 있으면 buy_now 무시 (중복 매수 방지)
        has_position = False
        try:
            pos = getattr(ctx, "position", None)
            if pos and float(pos.get("qty", 0.0) or 0.0) > 0:
                has_position = True
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[strategy_gazua_router] %s: %s", '[2026-01-28] 이미 포지션이 있으면 buy_now 무시 (중복 매수 방지)', exc, exc_info=True)

        if req.buy_now and req.budget > 0 and not has_position:
            current_price = price_store.get_price(market) or 0.0

            # If price is missing, try to fetch it quickly (best effort)
            if current_price <= 0:
                try:
                    # Market format (e.g., "BTC/USDT" → "BTCUSDT")
                    if "/" in market:
                        base = market.split("/")[0]
                        exchange_market = Q.market(base)
                    elif not market.startswith(Q.config.market_prefix):
                        exchange_market = Q.market(market)
                    else:
                        exchange_market = market
                    resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=1.0)
                    if resp.status_code == 200:
                        for _t in parse_bybit_list(resp.json()):
                            if isinstance(_t, dict):
                                _tc = normalize_bybit_ticker(_t)
                                if _tc.get("market", "").upper() == exchange_market.upper():
                                    current_price = float(_tc.get("trade_price") or 0.0)
                                    if current_price > 0:
                                        ctx.record_price(current_price)
                                    break
                except Exception as exc:
                    logger.warning("[strategy_gazua_router] %s: %s", 'Market format (e.g., "BTC/USDT" → "BTCUSDT")', exc, exc_info=True)

            # Ensure order_fsm is available
            if hasattr(system, "order_fsm") and system.order_fsm:
                if req.buy_limit:
                    # Submit limit buy at current price
                    ok, msg = system.order_fsm.submit_limit_buy(
                        ctx=ctx,
                        market=market,
                        quote_amount=req.budget,  # quote_amount는 레거시 이름, 실제로는 USDT
                        limit_price=current_price,
                        reason="gazua:buy_now_limit_ui"
                    )
                else:
                    # Submit market buy
                    ok, msg = system.order_fsm.submit_market_buy(
                        ctx=ctx,
                        market=market,
                        quote_amount=req.budget,  # quote_amount는 레거시 이름, 실제로는 USDT
                        expected_price=current_price,
                        reason="gazua:buy_now_ui"
                    )
                buy_result = {"ok": ok, "msg": str(msg)}

                # 주문 성공 시 포지션 즉시 기록 (폴링 전에 UI 반영용)
                if ok and current_price > 0:
                    try:
                        qty = req.budget / current_price
                        ctx.open_position(entry_price=current_price, usdt_amount=req.budget, source="gazua_buy_now")
                        system._save_context_state()
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                        logger.warning("strategy_gazua_router.setup_gazua_market L180: %s", e)
                        # [FIX] 포지션 기록 실패해도 매수는 성공했으므로 경고만 기록
                        import logging
                        logging.getLogger("strategy_router").warning(f"[gazua/setup] position record failed: {e}")
            elif system.trading_mode == "PAPER":
                if current_price > 0:
                    ctx.open_position(entry_price=current_price, usdt_amount=req.budget, source="paper")
                    system.ledger.append("PAPER_BUY_NOW", market=market, price=current_price, usdt=req.budget)
                    system._save_context_state()
                    buy_result = {"ok": True, "msg": "paper_filled"}
                else:
                    buy_result = {"ok": False, "msg": "no_price_for_paper"}

        return {"ok": True, "market": market, "setup": req.dict(), "buy_now_result": buy_result,
                "overflow": overflow_check, "warnings": coin_warnings}
    except Exception as exc:
        logger.warning("strategy_gazua_router.setup_gazua_market L195: %s", exc)
        import traceback
        tb = traceback.format_exc()
        return {"ok": False, "error": str(exc), "traceback": tb}

@router.get(
    "/gazua/list",
    summary="List all GAZUA strategy markets",
    responses={
        200: {"description": "List of markets running GAZUA strategy"},
    },
)
def list_gazua_markets(request: Request):
    """
    List all markets currently running GAZUA strategy.

    - Includes position, PnL, TP/SL params, and trade statistics
    """
    system = request.app.state.system
    items = []

    # ★ [2026-06-02 부모] 진입 근접도 = compute_scope_score 실시간 (5분봉 6-stage rank_score 0~100, 10s 캔들 캐시 → 폴링 부담 적음)
    #   부모님 통찰: 고정/수동 아니라 자동 평가로 점수가 살아 변동해야. market만 주면 매 호출 실시간 계산 (5분봉 따라 변동).
    try:
        from app.api.strategy_router import compute_scope_score as _scope_fn
    except (ImportError, AttributeError):
        _scope_fn = None

    for market, ctx in system.coordinator.contexts.items():
        try:
            ctrls = getattr(ctx, "controls", {}) or {}
            strat = ctrls.get("strategy", {}) or {}
            if not strat.get("enabled"):
                continue
            mode = str(strat.get("mode") or "").upper()
            if mode != "GAZUA":
                continue

            # Extract params
            params = strat.get("params", {})

            # Collect info (similar to lightning)
            pos = getattr(ctx, "position", {}) or {}

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

            trade_count = getattr(ctx, "win_count", 0) + getattr(ctx, "loss_count", 0)
            total_profit = getattr(ctx, "total_profit", 0.0)

            # ★ [2026-06-02 부모] 진입 근접도 실시간 계산 (compute_scope_score 5분봉 rank_score). 0=캔들 부족/실패
            _entry_score = 0.0
            if _scope_fn is not None:
                try:
                    _sc = _scope_fn(market)
                    if _sc:
                        _entry_score = round(float(_sc.get("rank_score") or 0.0), 1)
                except (KeyError, AttributeError, TypeError, ValueError):
                    pass

            items.append({
                "market": market,
                "state": getattr(ctx, "market_state", "UNKNOWN"),
                "budget": getattr(ctx, "allocated_capital", 0.0),
                "params": {
                    "tp": params.get("tp", 15.0),
                    "tp_price": params.get("tp_price", 0.0),
                    "sl": params.get("sl", -10.0),
                    "sl_price": params.get("sl_price", 0.0),
                    "hold_sell": params.get("hold_sell", False),
                    "buy_now": params.get("buy_now", False),
                },
                "position": {
                    "qty": pos.get("qty", 0.0),
                    "entry": pos.get("entry", 0.0),
                    "usdt": pos.get("usdt", 0.0),
                },
                "pnl": {
                    "amount": pnl,
                    "pct": pnl_pct,
                    "value": val
                },
                "trade_stats": {
                    "count": trade_count,
                    "realized_profit": total_profit
                },
                # ★ [2026-06-02 부모] 진입 근접도 (compute_scope_score 실시간 rank_score 0~100). 0=캔들 부족/계산 실패
                "entry_score": _entry_score,
            })
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[strategy_gazua_router] %s: %s", 'strategy_gazua_router except-> continue', exc, exc_info=True)
            continue

    return {"ok": True, "items": items}

@router.post(
    "/gazua/stop",
    summary="Stop GAZUA strategy for a market",
    responses={
        200: {"description": "GAZUA strategy stopped"},
    },
)
def stop_gazua_market(req: StrategyStopRequest, request: Request):
    """
    Disable GAZUA strategy for a market.

    - **liquidate**: If true, triggers recovery liquidation
    - **delete**: If true, sets state to DISABLED
    """
    system = request.app.state.system
    market = req.market.strip().upper()

    if req.liquidate:
        target_state = MarketState.RECOVERY
        reason = ["gazua_stop_liquidate"]
    elif req.delete:
        target_state = MarketState.DISABLED
        reason = ["gazua_delete_btn", "user_disabled"]
    else:
        target_state = MarketState.WATCH
        reason = ["gazua_stop_btn"]

    system.oma_set_market(
        market=market,
        state=target_state,
        reason=reason
    )

    if req.liquidate:
        system.request_recovery_liquidate(market=market, reason="gazua_stop_liquidate")

    ctx = system.coordinator.ensure_market(market)
    patch = {
        "strategy": { "enabled": False }
    }
    ctx.update_controls(patch)
    system._save_context_state()

    return {"ok": True, "market": market, "status": "stopped", "liquidating": req.liquidate}
