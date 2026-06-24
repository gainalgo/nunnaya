# ============================================================
# File: app/api/strategy_contrarian_router.py
# Extracted from strategy_router.py — Phase 1-E (file diet)
#
# CONTRARIAN strategy scan/setup/stop endpoints
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
    _get_cached, _set_cached, _build_cache_key,
    _check_manual_overflow, _generate_coin_warnings,
    _sync_policy_tp_sl, StrategyStopRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ============================================================
# CONTRARIAN Scanner API
# [CREATED 2026-01-26]
# ============================================================

@router.get(
    "/contrarian/list",
    summary="List active contrarian positions",
    responses={
        200: {"description": "List of active contrarian strategy positions"},
    },
)
def contrarian_list(request: Request):
    """
    Get list of markets with active CONTRARIAN strategy.
    """
    system = request.app.state.system

    def _get_price_safe(market: str) -> float:
        """Look up price from price_store."""
        return price_store.get_price(market) or 0

    items = []
    try:
        oma = system.oma_registry
        for market, entry in oma._markets.items():
            ctx = system.coordinator.contexts.get(market)
            if not ctx:
                continue

            # Check strategy_mode attribute or controls.strategy.mode
            strategy_mode = str(getattr(ctx, "strategy_mode", "")).upper()
            ctrls = getattr(ctx, "controls", {}) or {}
            strat_ctrl = ctrls.get("strategy", {}) or {}
            ctrl_mode = str(strat_ctrl.get("mode", "")).upper()

            if strategy_mode == "CONTRARIAN" or ctrl_mode == "CONTRARIAN":
                pos = getattr(ctx, "position", None) or {}
                pnl_amount = 0
                pnl_pct = 0
                entry_price = 0
                current_value = 0

                if pos:
                    qty = float(pos.get("qty", 0) or 0)
                    entry_price = float(pos.get("entry", 0) or pos.get("entry_price", 0) or 0)
                    current_px = _get_price_safe(market) or entry_price
                    if qty > 0 and entry_price > 0:
                        current_value = qty * current_px
                        cost = qty * entry_price
                        pnl_amount = current_value - cost
                        pnl_pct = (pnl_amount / cost * 100) if cost > 0 else 0

                params = strat_ctrl.get("params", {}) or {}

                # entry is a dict: {"state": MarketState, "reason": [], "budget_usdt": float}
                entry_state = entry.get("state")
                entry_budget = entry.get("budget_usdt") or 0

                items.append({
                    "market": market,
                    "state": str(entry_state.value) if entry_state else "UNKNOWN",
                    "strategy": "CONTRARIAN",
                    "active": True,
                    "budget": entry_budget,
                    "position": {
                        "qty": pos.get("qty", 0) if pos else 0,
                        "entry": entry_price,
                        "usdt": current_value,
                    },
                    "pnl": {
                        "amount": pnl_amount,
                        "pct": pnl_pct,
                        "value": current_value,
                    },
                    "params": params,
                    "last_meta": {"score": getattr(ctx, "contrarian_score", 0)},
                })
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("strategy_contrarian_router._get_price_safe L107: %s", e)
        import logging
        logging.getLogger("strategy_router").warning(f"[contrarian/list] error: {e}")

    return {
        "ok": True,
        "items": items,
        "count": len(items),
    }

@router.get(
    "/contrarian/scan",
    summary="Get contrarian coin candidates",
    responses={
        200: {"description": "Contrarian scanner results with candidates"},
    },
)
def contrarian_scan(
    request: Request,
    force: bool = Query(False, description="Force rescan ignoring cache"),
    benchmark: str = Query("BTC", description="Benchmark type: BTC, ETH, MARKET_AVG, FEAR_GREED"),
):
    """
    Scan all exchange markets for contrarian (counter-trend) coins.

    Returns coins that are rising while benchmark/market is falling.
    - RS (Relative Strength): coin return / benchmark return
    - Correlation: price movement correlation with benchmark
    - Score: 0-3 based on contrarian signals

    Benchmark types:
    - BTC: Bitcoin (default)
    - ETH: Ethereum (high correlation with altcoins)
    - MARKET_AVG: average return across the whole market
    - FEAR_GREED: Fear & Greed Index (<= 40 = market falling)
    """
    from app.core.contrarian_scanner import get_contrarian_scanner

    benchmark = benchmark.upper()
    cache_key = _build_cache_key("contrarian_scan", force=str(force), benchmark=benchmark)
    if not force:
        cached = _get_cached(cache_key, ttl=10)
        if cached:
            return cached

    system = request.app.state.system
    scanner = get_contrarian_scanner()

    # Build the market list
    markets = []
    try:
        if hasattr(system, "coordinator") and hasattr(system.coordinator, "_contexts"):
            markets = list(system.coordinator._contexts.keys())
        if not markets:
            markets = [Q.market(c) for c in ["BTC", "ETH", "XRP", "SOL", "DOGE", "ADA"]]
    except (KeyError, AttributeError, TypeError):
        logger.warning("strategy_contrarian_router.contrarian_scan L163 except", exc_info=True)
        markets = [Q.market("BTC"), Q.market("ETH"), Q.market("XRP")]

    result = scanner.scan(markets=markets, force=force, benchmark_type=benchmark)
    response = scanner.to_dict()
    response["ok"] = True

    _set_cached(cache_key, response)
    return response

@router.get(
    "/contrarian/candidate/{market}",
    summary="Get contrarian status for a specific market",
    responses={
        200: {"description": "Contrarian candidate info for the market"},
    },
)
def contrarian_candidate(
    request: Request,
    market: str,
):
    """
    Get contrarian analysis for a specific market.
    """
    from app.core.contrarian_scanner import get_contrarian_scanner

    scanner = get_contrarian_scanner()
    candidate = scanner.get_candidate(market)

    if candidate:
        return {
            "ok": True,
            "market": market,
            "is_contrarian": True,
            "coin_ret_pct": round(candidate.coin_ret_pct, 2),
            "btc_ret_pct": round(candidate.btc_ret_pct, 2),
            "rs": round(candidate.rs, 2) if candidate.rs else None,
            "rs_diff": round(candidate.rs_diff, 2),
            "corr": round(candidate.corr, 2) if candidate.corr else None,
            "score": candidate.score,
            "rank": candidate.rank,
            "market_down": scanner._cache.market_down if scanner._cache else False,
        }
    else:
        return {
            "ok": True,
            "market": market,
            "is_contrarian": False,
            "score": 0,
            "market_down": scanner._cache.market_down if scanner._cache else False,
        }

# ============================================================
# CONTRARIAN Setup API
# ============================================================

class ContrarianSetupRequest(BaseModel):
    """Request body for CONTRARIAN strategy setup."""
    market: str
    budget_usdt: Optional[float] = None
    budget_usdt: Optional[float] = None
    tp_pct: float = 15.0
    sl_pct: float = -50.0
    min_score: int = 1
    cooldown_sec: int = 300
    trail_tp: bool = False
    trail_dist_pct: float = 0.3
    use_atr: bool = False
    rsi_filter: bool = False
    buy_now: bool = False
    hold_sell: bool = False      # disable stop-loss (keep holding even on decline)
    user_sell_only: bool = False # allow manual sells only (block automatic system sells)

    @property
    def budget(self) -> float:
        return self.budget_usdt or self.budget_usdt or 0.0

@router.post(
    "/contrarian/setup",
    summary="Setup a market with CONTRARIAN strategy",
    responses={
        200: {"description": "Market configured with CONTRARIAN strategy"},
    },
)
def setup_contrarian_market(req: ContrarianSetupRequest, request: Request):
    """
    Register a market with CONTRARIAN (counter-trend) strategy.

    - Sets OMA state to ACTIVE with specified budget
    - Configures TP/SL percentages
    - Optionally enables trailing TP and RSI filter
    """
    try:
        from fastapi import HTTPException
        system = request.app.state.system
        market = Q.normalize(req.market.strip().upper())

        # CONTRARIAN uses BTC as its benchmark axis, so exclude it as a tradable target
        if market == Q.market("BTC"):
            raise HTTPException(
                status_code=400,
                detail="BTCUSDT is benchmark-only and cannot be configured as CONTRARIAN.",
            )

        # [2026-03-07] Manual order slot overflow check (+2 limit)
        overflow_check = _check_manual_overflow(system, "CONTRARIAN", market)
        coin_warnings = _generate_coin_warnings(system, market, "CONTRARIAN")
        if not overflow_check["allowed"]:
            return {"ok": False, "error": "slot_overflow", "detail": overflow_check["message"],
                    "overflow": overflow_check, "warnings": coin_warnings}

        # 1. Set OMA State to ACTIVE with Budget
        system.oma_set_market(
            market=market,
            state=MarketState.ACTIVE,
            reason=["contrarian_factory_setup"],
            budget_usdt=req.budget
        )

        # 2. Configure Strategy Controls (CONTRARIAN)
        from app.manager.market_controls import apply_engine_controls
        ctrls = apply_engine_controls(system, market, "CONTRARIAN")

        # Apply custom params
        ctx = system.coordinator.ensure_market(market)

        # Set strategy_mode for list API detection
        ctx.strategy_mode = "CONTRARIAN"

        patch = {
            "strategy": {
                "mode": "CONTRARIAN",
                "enabled": True,
                "params": {
                    "tp": req.tp_pct,
                    "sl": req.sl_pct,
                    "min_score": req.min_score,
                    "cooldown_sec": req.cooldown_sec,
                    "trail_tp_enabled": req.trail_tp,
                    "trail_dist_pct": req.trail_dist_pct,
                    "use_atr": req.use_atr,
                    "rsi_filter": req.rsi_filter,
                    "hold_sell": req.hold_sell,           # HOLD: disable stop-loss
                    "user_sell_only": req.user_sell_only, # LOCK: allow manual sells only
                }
            }
        }
        ctx.update_controls(patch)
        _sync_policy_tp_sl(ctx, tp=req.tp_pct, sl=req.sl_pct)
        system._save_context_state()

        buy_result = {}

        # 3. Buy Now (Optional)
        has_position = False
        try:
            pos = getattr(ctx, "position", None)
            if pos and float(pos.get("qty", 0.0) or 0.0) > 0:
                has_position = True
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[strategy_contrarian_router] %s: %s", '3. Buy Now (Optional)', exc, exc_info=True)

        if req.buy_now and req.budget > 0 and not has_position:
            current_price = price_store.get_price(market) or 0.0

            if current_price <= 0:
                try:
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
                    logger.warning("[strategy_contrarian_router] %s: %s", '3. Buy Now (Optional)', exc, exc_info=True)

            if hasattr(system, "order_fsm") and system.order_fsm:
                ok, msg = system.order_fsm.submit_market_buy(
                    ctx=ctx,
                    market=market,
                    quote_amount=req.budget,
                    expected_price=current_price,
                    reason="contrarian:buy_now_ui"
                )
                buy_result = {"ok": ok, "msg": str(msg)}

                if ok and current_price > 0:
                    try:
                        ctx.open_position(entry_price=current_price, usdt_amount=req.budget, source="contrarian_buy_now")
                        system._save_context_state()
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                        logger.warning("strategy_contrarian_router.setup_contrarian_market L367: %s", e)
                        import logging
                        logging.getLogger("strategy_router").warning(f"[contrarian/setup] position record failed: {e}")
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
        logger.warning("strategy_contrarian_router.setup_contrarian_market L381: %s", exc)
        import traceback
        tb = traceback.format_exc()
        return {"ok": False, "error": str(exc), "traceback": tb}

@router.post(
    "/contrarian/stop",
    summary="Stop CONTRARIAN strategy for a market",
    responses={
        200: {"description": "CONTRARIAN strategy stopped"},
    },
)
def stop_contrarian(
    request: Request,
    market: str = Query(..., description="Market to stop"),
    delete: bool = Query(False, description="If true, set DISABLED and stop OMA watch"),
):
    """Stop CONTRARIAN strategy for a market."""
    try:
        system = request.app.state.system
        market = market.strip().upper()

        target_state = MarketState.DISABLED if delete else MarketState.WATCH
        reason = ["contrarian_delete_btn", "user_disabled"] if delete else ["contrarian_stop_ui"]

        system.oma_set_market(
            market=market,
            state=target_state,
            reason=reason,
        )

        # Clear strategy mode
        ctx = system.coordinator.get_context(market)
        if ctx:
            ctx.update_controls({"strategy": {"enabled": False, "mode": ""}})
            system._save_context_state()

        return {"ok": True, "market": market, "state": target_state.value}
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("strategy_contrarian_router.stop_contrarian L420: %s", exc)
        return {"ok": False, "error": str(exc)}
