# ============================================================
# File: app/api/engine_router.py
# Autocoin OS v3-H — Engine Router (Final)
# ============================================================

from fastapi import APIRouter, Request, Query
from typing import Dict, Any, List, Optional
from app.core.hyper_price_store import price_store
from app.core.currency import Q
from app.manager.oma_market_registry import MarketState

import logging
import time as _time
from collections import defaultdict as _defaultdict

logger = logging.getLogger(__name__)


# ── [2026-04-09 security] Rate limiting for trade endpoints ──────────────
_RATE_LIMIT: Dict[str, list] = _defaultdict(list)  # ip → [timestamp, ...]
_RATE_LIMIT_MAX = 30   # max requests per minute
_RATE_LIMIT_WINDOW = 60  # seconds


def _check_rate_limit(request: Request, limit: int = _RATE_LIMIT_MAX) -> bool:
    """True = allowed, False = blocked"""
    ip = request.client.host if request.client else "unknown"
    now = _time.time()
    window = now - _RATE_LIMIT_WINDOW
    # Drop old records
    _RATE_LIMIT[ip] = [t for t in _RATE_LIMIT[ip] if t > window]
    if len(_RATE_LIMIT[ip]) >= limit:
        return False
    _RATE_LIMIT[ip].append(now)
    return True


router = APIRouter(prefix="/api/engine", tags=["engine"])


def _sync_policy_tp_sl(ctx: Any) -> None:
    """Align engine policy TP/SL with effective strategy params."""
    try:
        ctrls = getattr(ctx, "controls", None) or {}
        st = ctrls.get("strategy", {}) if isinstance(ctrls, dict) else {}
        mode = str(st.get("mode") or "").strip().upper() if isinstance(st, dict) else ""
        sp = st.get("params", {}) if isinstance(st, dict) else {}
        if not isinstance(sp, dict):
            return

        tp = sp.get("tp", sp.get("tp_pct"))
        sl = sp.get("sl", sp.get("sl_pct"))
        if tp is None and sl is None:
            return

        policy = getattr(ctx, "policy", None)
        if not isinstance(policy, dict):
            policy = {"name": "nunnaya", "params": {}}
        pparams = policy.get("params")
        if not isinstance(pparams, dict):
            pparams = {}

        if tp is not None:
            pparams["tp"] = float(tp)
        if sl is not None:
            sl_num = float(sl)
            sl_norm = -abs(sl_num) if sl_num > 0 else sl_num
            if mode == "LADDER" and sl_norm > -5.0:
                sl_norm = -5.0
            pparams["sl"] = sl_norm

        policy["name"] = str(policy.get("name") or "nunnaya")
        policy["params"] = pparams
        if hasattr(ctx, "update_policy"):
            ctx.update_policy(policy)
        else:
            ctx.policy = policy
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[ENGINE_API] _sync_policy_tp_sl fallback: %s", exc, exc_info=True)


# ------------------------------------------------------------
# Execute one tick (price input required)
# ------------------------------------------------------------
@router.post(
    "/tick",
    summary="Execute a single tick for a market",
    responses={
        200: {"description": "Tick executed successfully with engine result"},
    },
)
def tick(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    price: float = Query(..., gt=0, le=1_000_000, description="Current price to inject"),
) -> Dict[str, Any]:
    """
    Execute a single tick with an injected price.

    - Updates price store with the given price
    - Runs the nunnaya engine for the specified market
    - Returns the engine tick result
    """
    system = request.app.state.system

    price_store.set_price(market, price)
    return system.run_tick("nunnaya", market)

# ------------------------------------------------------------
# Engine runtime controls (HANDLE)
# ------------------------------------------------------------
@router.post(
    "/controls",
    summary="Set engine controls for a market",
    responses={
        200: {"description": "Controls updated successfully"},
    },
)
def set_engine_controls(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    controls: Dict[str, Any] = None,
):
    """
    Set engine decision handles (buttons/sliders) for a market.

    - Applied per market
    - Takes effect immediately without server restart
    - Supports baseline, ai, strategy, risk, tp_sl, guards sections
    """
    system = request.app.state.system
    ctx = system.coordinator.get_context(market)

    # --------------------------------------------------------
    # Settings: apply controls from UI/API as the "single source"
    # - ctx.controls is used as-is in both display (STATUS) and execution (tick)
    # - Persisted so it is restored identically after a server restart
    # --------------------------------------------------------
    if hasattr(ctx, "update_controls"):
        # New path (recommended): safe deep-merge + alias(config->params)
        prev_manual_enabled = bool(((ctx.controls or {}).get("manual") or {}).get("enabled"))
        ctx.update_controls(controls)
        new_manual_enabled = bool(((ctx.controls or {}).get("manual") or {}).get("enabled"))

        # Manual mode implies market isolation; ensure OMA state is not ACTIVE
        if (not prev_manual_enabled) and new_manual_enabled:
            try:
                system.oma_registry.set_state(
                    market=market,
                    state=MarketState.WATCH,
                    reason=["manual_mode_on"],
                )
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[ENGINE_API] Manual mode OMA state change: %s", exc, exc_info=True)
    else:
        # Legacy path (compat): overwrite only selected keys
        if "baseline" in controls:
            ctx.controls["baseline"] = controls["baseline"]
        if "ai" in controls:
            ctx.controls["ai"] = controls["ai"]
        if "strategy" in controls:
            ctx.controls["strategy"] = controls["strategy"]
        if "risk" in controls:
            ctx.controls["risk"] = controls["risk"]
        if "tp_sl" in controls:
            ctx.controls["tp_sl"] = controls["tp_sl"]
        if "guards" in controls:
            ctx.controls["guards"] = controls["guards"]

    _sync_policy_tp_sl(ctx)

    # Record the change history
    try:
        system.ledger.append(
            "ENGINE_CONTROLS_SET",
            market=market,
            patch=controls,
        )
    except (AttributeError, TypeError) as exc:
        logger.warning("[ENGINE_API] ledger append controls: %s", exc)

    # Persist immediately (settings preserved even right before a restart)
    try:
        system._save_context_state()  # noqa: SLF001 (intentional call)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[ENGINE_API] save context state after controls: %s", exc, exc_info=True)

    return {
        "ok": True,
        "market": market,
        "controls": ctx.controls,
    }

# ------------------------------------------------------------
# 2026-03-10: API to force OMA state change
# ------------------------------------------------------------

@router.post(
    "/oma/set-state",
    summary="Force OMA market state change",
)
def oma_set_state(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    system = request.app.state.system
    market = str(payload.get("market") or "").strip().upper()
    state_str = str(payload.get("state") or "").strip().upper()
    reason = payload.get("reason") or ["manual_api"]

    if not market:
        return {"ok": False, "error": "market required"}

    state_map = {"ACTIVE": MarketState.ACTIVE, "WATCH": MarketState.WATCH,
                 "DISABLED": MarketState.DISABLED, "RECOVERY": MarketState.RECOVERY}
    target = state_map.get(state_str)
    if target is None:
        return {"ok": False, "error": f"invalid state: {state_str}"}

    if isinstance(reason, str):
        reason = [reason]

    try:
        system.oma_set_market(market=market, state=target, reason=reason)
        if target == MarketState.DISABLED:
            try:
                system.coordinator.remove_market(market)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[ENGINE_API] oma_set_state remove_market: %s", exc, exc_info=True)
        return {"ok": True, "market": market, "state": state_str}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("engine_router.oma_set_state L207: %s", e)
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------
# Engine runtime status
# ------------------------------------------------------------

@router.post(
    "/manual/order",
    summary="Submit a manual order for a market",
    responses={
        200: {"description": "Order submitted or error details returned"},
        400: {"description": "Invalid request parameters"},
    },
)
def manual_order(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Submit an emergency/manual order for a single market.

    **Payload (JSON body):**
    - **market**: Market code (e.g., "BTCUSDT" or "BTCUSDT")
    - **side**: "buy" or "sell" (also accepts "bid"/"ask")
    - **mode**:
        - BUY: "usdt" (absolute USDT) | "pct_alloc" (percent of allocation)
        - SELL: "pct_pos" | "usdt" | "qty" | "all"

    - **value**: Numeric value (interpretation depends on mode)
    - **force**: Optional bool (IGNORED — emergency stop cannot be bypassed)
    - **reconcile_after**: Optional bool; trigger reconcile after order

    Rate-limited: 30 requests/minute per IP.
    """
    # [2026-04-09 security] Trade rate limiting
    if not _check_rate_limit(request):
        return {"ok": False, "error": "rate_limited", "message": "Too many requests. Max 30/min."}
    system = request.app.state.system

    market = str(payload.get("market") or "").strip()
    side = str(payload.get("side") or "").strip().lower()
    mode = str(payload.get("mode") or "").strip().lower()
    value = payload.get("value", None)
    force = bool(payload.get("force") or False)
    reconcile_after = bool(payload.get("reconcile_after") or False)
    use_quick_sell = bool(payload.get("quick") or payload.get("use_quick_sell") or False)
    fallback_to_market = bool(payload.get("fallback") or payload.get("fallback_to_market") or False)

    if not market:
        return {"ok": False, "error": "bad_request", "message": "market is required"}

    if side in ("bid", "buy"):
        side = "buy"
    elif side in ("ask", "sell"):
        side = "sell"
    else:
        return {"ok": False, "error": "bad_request", "message": f"invalid side: {side}"}

    if system is None or getattr(system, "order_fsm", None) is None:
        return {"ok": False, "error": "not_ready", "message": "order_fsm not available"}

    # basic guard: do not stack manual orders on top of pending
    ctx = system.coordinator.get_context(market)
    if getattr(ctx, "order_state", None) is not None:
        return {"ok": False, "error": "pending_order", "message": "order_state is not empty for this market"}

    expected_price = float(price_store.get_price(market) or 0.0)
    if expected_price <= 0 and side == "sell" and mode in ("usdt",):
        return {"ok": False, "error": "no_price", "message": "no price available (cannot convert quote to qty)"}

    # [2026-04-09 security] Emergency stop can never be bypassed
    if side == "buy" and getattr(system, "emergency_stop", False):
        return {"ok": False, "error": "emergency_stop", "message": "emergency_stop is active. Resume via /api/system/emergency/resume first."}

    try:
        if side == "buy":
            if mode not in ("usdt", "pct_alloc", "pct"):
                return {"ok": False, "error": "bad_request", "message": f"invalid buy mode: {mode}"}

            if value is None:
                return {"ok": False, "error": "bad_request", "message": "value is required"}

            if mode in ("pct_alloc", "pct"):
                pct = float(value)
                if pct <= 0:
                    return {"ok": False, "error": "bad_request", "message": "pct must be > 0"}
                alloc = float(getattr(ctx, "allocated_capital", 0.0) or 0.0)
                usable = float(getattr(ctx, "usable_capital", 0.0) or 0.0)
                base = usable if usable > 0 else alloc
                if alloc > 0 and usable > 0:
                    base = min(alloc, usable)
                quote_amount = int(max(0.0, base * (pct / 100.0)))
            else:
                quote_amount = int(float(value))

            if quote_amount < int(getattr(system, "min_order_usdt", 5) or 5):
                return {"ok": False, "error": "min_order", "message": f"quote_amount too small: {quote_amount}"}

            ok, msg = system.order_fsm.submit_market_buy(
                ctx=ctx,
                market=market,
                usdt_amount=quote_amount,
                expected_price=expected_price,
                reason="manual_ui_buy",
            )
            if ok:
                system.ledger.append("MANUAL_ORDER", market=market, side="buy", mode=mode, value=value, quote_amount=quote_amount)
            if reconcile_after:
                system.reconcile(reason="manual_ui_buy")
            return {"ok": bool(ok), "message": msg, "market": market, "side": "buy", "mode": mode, "quote_amount": quote_amount}

        # SELL
        if mode not in ("pct_pos", "pct", "usdt", "qty", "all"):
            return {"ok": False, "error": "bad_request", "message": f"invalid sell mode: {mode}"}

        # Check the system position
        pos = getattr(ctx, "position", None) or {}
        pos_qty = float(pos.get("qty") or 0.0)
        focus_pos = None  # FOCUS position reference (for state cleanup after exit)

        # 🔧 If there is no system position, check the FOCUS position
        if pos_qty <= 0:
            try:
                fm = getattr(system, "focus_manager", None)
                if fm:
                    for fp in (fm.positions or []):
                        if fp.market.upper() == market.upper():
                            pos_qty = float(fp.qty or 0)
                            focus_pos = fp
                            logger.info("[ENGINE_API] Found FOCUS position: %s qty=%.4f", market, pos_qty)
                            break
            except Exception as exc:
                logger.debug("[ENGINE_API] FOCUS position check: %s", exc)

        # 🔧 If still none, query the actual Bybit position
        if pos_qty <= 0:
            try:
                import os, hashlib, hmac, time as _t, requests as _req
                key = os.environ.get('BYBIT_API_KEY', '')
                secret = os.environ.get('BYBIT_API_SECRET', '')
                ts = str(int(_t.time() * 1000))
                recv = '5000'
                params = f'category=linear&symbol={market.upper()}'
                sign_str = ts + key + recv + params
                sig = hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
                headers = {'X-BAPI-API-KEY': key, 'X-BAPI-TIMESTAMP': ts,
                           'X-BAPI-RECV-WINDOW': recv, 'X-BAPI-SIGN': sig}
                r = _req.get('https://api.bybit.com/v5/position/list',
                             params={'category': 'linear', 'symbol': market.upper()},
                             headers=headers, timeout=5)
                bp_list = r.json().get('result', {}).get('list', [])
                for bp in bp_list:
                    sz = float(bp.get('size', 0))
                    if sz > 0:
                        pos_qty = sz
                        logger.info("[ENGINE_API] Found Bybit position: %s size=%.4f side=%s", market, sz, bp.get('side'))
                        break
            except Exception as exc:
                logger.error("[ENGINE_API] Bybit position check for manual sell: %s", exc)

        if mode in ("all",):
            qty = pos_qty
        elif mode in ("pct_pos", "pct"):
            pct = float(value)
            qty = pos_qty * (pct / 100.0)
        elif mode == "qty":
            qty = float(value)
        else:  # usdt
            quote_amount = float(value)
            if expected_price <= 0:
                return {"ok": False, "error": "no_price", "message": "no price available"}
            qty = quote_amount / expected_price

        if qty <= 0:
            return {"ok": False, "error": "no_position", "message": f"qty=0 (system pos={pos.get('qty', 0)}, check exchange balance)"}

        # Quick Sell (IOC) vs Market Sell
        if use_quick_sell and expected_price > 0:
            ok, msg = system.order_fsm.submit_quick_sell(
                ctx=ctx,
                market=market,
                qty=qty,
                price=expected_price,
                reason="manual_ui_quick_sell",
                fallback_to_market=fallback_to_market,
            )
            order_type = "quick_sell"
        else:
            ok, msg = system.order_fsm.submit_market_sell(
                ctx=ctx,
                market=market,
                qty=qty,
                expected_price=expected_price,
                reason="manual_ui_sell",
            )
            order_type = "market_sell"
            
        if ok:
            system.ledger.append("MANUAL_ORDER", market=market, side="sell", mode=mode, value=value, qty=qty, order_type=order_type)
            # On a manual full sell from a Precision Scope slot, release the slot immediately so it returns to the recommendation pool.
            try:
                is_full_exit = (
                    mode == "all"
                    or (mode in ("pct_pos", "pct") and float(value or 0.0) >= 99.0)
                )
            except (TypeError, ValueError):
                logger.warning("engine_router.manual_order L373 except", exc_info=True)
                is_full_exit = (mode == "all")
            if is_full_exit:
                try:
                    strat = (getattr(ctx, "controls", {}) or {}).get("strategy", {}) or {}
                    s_mode = str(strat.get("mode") or "").strip().upper()
                    s_params = strat.get("params", {}) or {}
                    profile = str(s_params.get("profile") or "").strip().upper()
                    source = str(s_params.get("source") or "").strip().lower()
                    is_scope_slot = (
                        s_mode in ("SNIPER", "SNIPER(S)")
                        and profile == "SNIPERS"
                        and source == "precision_scope"
                    )
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("engine_router.manual_order L387 except", exc_info=True)
                    is_scope_slot = False

                if is_scope_slot:
                    try:
                        from app.manager.sniper_position_store import sniper_store
                        sniper_store.remove_positions_by_market(market)
                    except (ImportError, AttributeError, TypeError) as exc:
                        logger.warning("[ENGINE_API] sniper_store remove_positions: %s", exc, exc_info=True)
                    try:
                        system.oma_set_market(
                            market=market,
                            state=MarketState.WATCH,
                            reason=["precision_scope_manual_sell_release"],
                        )
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.warning("[ENGINE_API] precision_scope OMA WATCH: %s", exc, exc_info=True)
                    try:
                        ctx.update_controls({"strategy": {"enabled": False, "mode": ""}})
                        if hasattr(ctx, "strategy_mode"):
                            ctx.strategy_mode = ""
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.warning("[ENGINE_API] update_controls after scope sell: %s", exc, exc_info=True)
                    try:
                        system._save_context_state()
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("[ENGINE_API] save_context_state after scope sell: %s", exc, exc_info=True)
                # 2026-03-10: After a manual full sell, set DISABLED immediately + remove context
                elif is_full_exit:
                    try:
                        _strat_name = str(s_mode or "").upper() if 's_mode' in dir() else ""
                        system.oma_set_market(
                            market=market,
                            state=MarketState.DISABLED,
                            reason=[f"manual_sell_full_exit"],
                        )
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.error("[ENGINE_API] manual full_exit OMA DISABLED: %s", exc, exc_info=True)
                    try:
                        system.coordinator.remove_market(market)
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("[ENGINE_API] manual full_exit remove_market: %s", exc, exc_info=True)
        # ★ Clean up local state when a FOCUS position is exited
        if ok and focus_pos and is_full_exit:
            try:
                fm = getattr(system, "focus_manager", None)
                if fm:
                    fm.positions = [p for p in fm.positions if p.market.upper() != market.upper()]
                    fm.position = fm.positions[0] if fm.positions else None  # ★ keep legacy field in sync
                    if not fm.positions:
                        from app.manager.focus_manager import FocusState
                        fm.state = FocusState.DORMANT
                    fm._save_config()
                    logger.info("[ENGINE_API] FOCUS position removed: %s → state=%s", market, fm.state.value)
            except Exception as exc:
                logger.warning("[ENGINE_API] FOCUS state cleanup: %s", exc)

        if reconcile_after:
            system.reconcile(reason="manual_ui_sell")
        return {"ok": bool(ok), "message": msg, "market": market, "side": "sell", "mode": mode, "qty": qty, "order_type": order_type}

    except Exception as e:
        logger.warning("engine_router.manual_order L433: %s", e)
        return {"ok": False, "error": "exception", "message": str(e)}


@router.post(
    "/manual/batch",
    summary="Submit manual orders for multiple markets",
    responses={
        200: {"description": "Batch order results for all markets"},
        400: {"description": "Invalid request parameters"},
    },
)
def manual_batch(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Submit emergency/manual orders for multiple markets at once.

    **Payload (JSON body):**
    - **markets**: List of market codes (e.g., ["BTCUSDT", "ETHUSDT"] or ["BTCUSDT", "ETHUSDT"])
    - **side**: "buy" or "sell"
    - **mode**: Same as manual_order; "usdt_each" for per-market USDT amount
    - **value**: Numeric value applied to each market
    - **force**: Optional bool; override emergency_stop for BUY
    - **reconcile_after**: Optional bool; trigger reconcile after all orders
    """
    system = request.app.state.system

    markets = payload.get("markets") or []
    side = str(payload.get("side") or "").strip().lower()
    mode = str(payload.get("mode") or "").strip().lower()
    value = payload.get("value", None)
    force = bool(payload.get("force") or False)
    reconcile_after = bool(payload.get("reconcile_after") or False)

    if side in ("bid", "buy"):
        side = "buy"
    elif side in ("ask", "sell"):
        side = "sell"
    else:
        return {"ok": False, "error": "bad_request", "message": f"invalid side: {side}"}

    if not isinstance(markets, list) or len(markets) == 0:
        return {"ok": False, "error": "bad_request", "message": "markets list is required"}

    if value is None and mode not in ("all",):
        return {"ok": False, "error": "bad_request", "message": "value is required"}

    if system is None or getattr(system, "order_fsm", None) is None:
        return {"ok": False, "error": "not_ready", "message": "order_fsm not available"}

    if side == "buy" and getattr(system, "emergency_stop", False) and not force:
        return {"ok": False, "error": "emergency_stop", "message": "emergency_stop is active (set force=true to override)"}

    results: List[Dict[str, Any]] = []

    for market in markets:
        mkt = str(market or "").strip()
        if not mkt:
            continue

        try:
            ctx = system.coordinator.get_context(mkt)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("engine_router.manual_batch L494 except", exc_info=True)
            ctx = None
        if ctx is None:
            results.append({"market": mkt, "ok": False, "error": "no_context"})
            continue
        if getattr(ctx, "order_state", None) is not None:
            results.append({"market": mkt, "ok": False, "error": "pending_order"})
            continue

        expected_price = float(system.price_store.get_price(mkt) or 0.0)

        try:
            if side == "buy":
                if mode in ("usdt_each", "usdt"):
                    quote_amount = int(float(value))
                elif mode in ("pct_alloc", "pct"):
                    pct = float(value)
                    alloc = float(getattr(ctx, "allocated_capital", 0.0) or 0.0)
                    usable = float(getattr(ctx, "usable_capital", 0.0) or 0.0)
                    base = usable if usable > 0 else alloc
                    if alloc > 0 and usable > 0:
                        base = min(alloc, usable)
                    quote_amount = int(max(0.0, base * (pct / 100.0)))
                else:
                    results.append({"market": mkt, "ok": False, "error": f"invalid buy mode: {mode}"})
                    continue

                if quote_amount < int(getattr(system, "min_order_usdt", 5) or 5):
                    results.append({"market": mkt, "ok": False, "error": "min_order", "quote_amount": quote_amount})
                    continue

                ok, msg = system.order_fsm.submit_market_buy(
                    ctx=ctx,
                    market=mkt,
                    usdt_amount=quote_amount,
                    expected_price=expected_price,
                    reason="manual_ui_batch_buy",
                )
                if ok:
                    system.ledger.append("MANUAL_ORDER", market=mkt, side="buy", mode=mode, value=value, quote_amount=quote_amount)
                results.append({"market": mkt, "ok": bool(ok), "message": msg, "quote_amount": quote_amount})
                continue

            # SELL
            pos = getattr(ctx, "position", None) or {}
            pos_qty = float(pos.get("qty") or 0.0)
            if mode in ("all",):
                qty = pos_qty
            elif mode in ("pct_pos", "pct"):
                pct = float(value)
                qty = pos_qty * (pct / 100.0)
            elif mode in ("usdt_each", "usdt"):
                quote_amount = float(value)
                if expected_price <= 0:
                    results.append({"market": mkt, "ok": False, "error": "no_price"})
                    continue
                qty = quote_amount / expected_price
            else:
                results.append({"market": mkt, "ok": False, "error": f"invalid sell mode: {mode}"})
                continue

            if qty <= 0:
                results.append({"market": mkt, "ok": False, "error": "no_position"})
                continue

            ok, msg = system.order_fsm.submit_market_sell(
                ctx=ctx,
                market=mkt,
                qty=qty,
                expected_price=expected_price,
                reason="manual_ui_batch_sell",
            )
            if ok:
                system.ledger.append("MANUAL_ORDER", market=mkt, side="sell", mode=mode, value=value, qty=qty)
                try:
                    is_full_exit = (
                        mode == "all"
                        or (mode in ("pct_pos", "pct") and float(value or 0.0) >= 99.0)
                    )
                except (TypeError, ValueError):
                    logger.warning("engine_router.manual_batch L573 except", exc_info=True)
                    is_full_exit = (mode == "all")
                if is_full_exit:
                    try:
                        strat = (getattr(ctx, "controls", {}) or {}).get("strategy", {}) or {}
                        s_mode = str(strat.get("mode") or "").strip().upper()
                        s_params = strat.get("params", {}) or {}
                        profile = str(s_params.get("profile") or "").strip().upper()
                        source = str(s_params.get("source") or "").strip().lower()
                        is_scope_slot = (
                            s_mode in ("SNIPER", "SNIPER(S)")
                            and profile == "SNIPERS"
                            and source == "precision_scope"
                        )
                    except (KeyError, AttributeError, TypeError, ValueError):
                        logger.warning("engine_router.manual_batch L587 except", exc_info=True)
                        is_scope_slot = False
                    if is_scope_slot:
                        try:
                            from app.manager.sniper_position_store import sniper_store
                            sniper_store.remove_positions_by_market(mkt)
                        except (ImportError, AttributeError, TypeError) as exc:
                            logger.warning("[ENGINE_API] batch sniper_store remove: %s", exc, exc_info=True)
                        try:
                            system.oma_set_market(
                                market=mkt,
                                state=MarketState.WATCH,
                                reason=["precision_scope_manual_sell_release"],
                            )
                        except (KeyError, AttributeError, TypeError) as exc:
                            logger.warning("[ENGINE_API] batch precision_scope OMA WATCH: %s", exc, exc_info=True)
                        try:
                            ctx.update_controls({"strategy": {"enabled": False, "mode": ""}})
                            if hasattr(ctx, "strategy_mode"):
                                ctx.strategy_mode = ""
                        except (KeyError, AttributeError, TypeError) as exc:
                            logger.warning("[ENGINE_API] batch update_controls after scope sell: %s", exc, exc_info=True)
                        try:
                            system._save_context_state()
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                            logger.warning("[ENGINE_API] batch save_context_state: %s", exc, exc_info=True)
                    # 2026-03-10: After a batch manual full sell, set DISABLED immediately + remove context
                    elif is_full_exit:
                        try:
                            system.oma_set_market(
                                market=mkt,
                                state=MarketState.DISABLED,
                                reason=[f"manual_batch_sell_full_exit"],
                            )
                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                            logger.error("[ENGINE_API] batch full_exit OMA DISABLED: %s", exc, exc_info=True)
                        try:
                            system.coordinator.remove_market(mkt)
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                            logger.warning("[ENGINE_API] batch full_exit remove_market: %s", exc, exc_info=True)
            results.append({"market": mkt, "ok": bool(ok), "message": msg, "qty": qty})
        except Exception as e:
            logger.warning("engine_router.manual_batch L628: %s", e)
            results.append({"market": mkt, "ok": False, "error": "exception", "message": str(e)})

    if reconcile_after:
        system.reconcile(reason="manual_ui_batch")

    ok_all = all(r.get("ok") for r in results) if results else False
    return {"ok": ok_all, "results": results}

@router.get(
    "/status",
    summary="Get engine and market status",
    responses={
        200: {"description": "Current engine and market execution status"},
    },
)
def engine_status(request: Request) -> Dict[str, Any]:
    """
    Retrieve the current engine and per-market execution status.

    Returns system-wide status including all active markets and their states.
    """
    system = request.app.state.system
    return {
        "ok": True,
        "system": system.status()
    }


# ============================================================
# Engine start
# ============================================================
@router.post(
    "/start",
    summary="Start engine for a market",
    responses={
        200: {"description": "Engine started and market activated"},
    },
)
def start_engine(
    request: Request,
    market: str = Query(..., description="Market code to start (e.g., BTCUSDT)"),
):
    """
    Start the trading engine for the specified market.

    - Initializes engine if not running
    - Sets OMA state to ACTIVE
    - Triggers pricefeed subscription and allocation rebalancing
    """
    system = request.app.state.system

    engine = system.engine
    try:
        engine.start()      # ✅ default form
    except TypeError:
        logger.warning("engine_router.start_engine L683 except", exc_info=True)
        engine.start(market)  # (in case the implementation takes a market arg)

    # Key: raise OMA to ACTIVE to
    # - reflect in the registry
    # - guarantee coordinator/context
    # - re-subscribe pricefeed
    # - rebalance allocation
    # - context_state flush
    system.oma_set_market(
        market=market,
        state=MarketState.ACTIVE,
        reason=["engine_start"],
        budget_usdt=None,
    )


# ------------------------------------------------------------
# Engine stop
# ------------------------------------------------------------
@router.post(
    "/stop",
    summary="Stop the trading engine",
    responses={
        200: {"description": "Engine stopped successfully"},
    },
)
def stop_engine(request: Request):
    """
    Stop the trading engine.

    - Halts all tick processing
    - Does not liquidate positions
    """
    system = request.app.state.system
    engine = system.engine
    engine.stop()
    return {"ok": True, "engine": "nunnaya", "status": "stopped"}


# ------------------------------------------------------------
# Force sync + sell from actual exchange balance
# ------------------------------------------------------------
@router.get(
    "/exchange/balances",
    summary="Get actual exchange account balances",
    responses={200: {"description": "Actual balances from Exchange"}},
)
def get_exchange_balances(request: Request):
    """Query actual exchange balances (not system positions)."""
    system = request.app.state.system
    if not system.trade_client:
        return {"ok": False, "error": "trade_client not available"}
    
    try:
        accounts = system.trade_client.accounts()
        holdings = []
        quote_free = 0.0
        quote_total = 0.0
        
        for acc in accounts:
            currency = acc.get("currency", "")
            balance = float(acc.get("balance") or 0)
            locked = float(acc.get("locked") or 0)
            total = balance + locked
            
            if currency == Q.symbol:
                quote_free = balance
                quote_total = total
                continue
            
            if total <= 0:
                continue
            
            market = Q.market(currency)
            price = float(price_store.get_price(market) or 0)
            
            # Fallback: use avg_buy_price or fetch directly from the exchange API
            if price <= 0:
                price = float(acc.get("avg_buy_price") or 0)
            if price <= 0:
                try:
                    from app.core.constants import (
                        BYBIT_MARKET_TICKERS,
                        bybit_v5_rest_category,
                        parse_bybit_list,
                        normalize_bybit_ticker,
                    )
                    from app.core.rate_limiter import bybit_get
                    resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=2)
                    if resp.status_code == 200:
                        for _t in parse_bybit_list(resp.json()):
                            if isinstance(_t, dict):
                                _tc = normalize_bybit_ticker(_t)
                                if _tc.get("market", "").upper() == market.upper():
                                    price = float(_tc.get("trade_price") or 0)
                                    break
                except Exception as exc:
                    logger.warning("[ENGINE_API] price fallback via exchange API: %s", exc)
            
            value_usdt = total * price if price else 0
            
            holdings.append({
                "currency": currency,
                "market": market,
                "total": total,
                "free": balance,
                "locked": locked,
                "avg_buy_price": float(acc.get("avg_buy_price") or 0),
                "price": price,
                "value_usdt": round(value_usdt, 2),
            })
        
        return {
            "ok": True,
            "quote_free": round(quote_free, 2),
            "quote_total": round(quote_total, 2),
            "holdings": holdings,
        }
    except Exception as e:
        logger.warning("engine_router.get_exchange_balances L797: %s", e)
        return {"ok": False, "error": str(e)}


@router.post(
    "/exchange/force_sell",
    summary="Force sell actual exchange balance (bypass system position)",
    responses={200: {"description": "Sell result"}},
)
def force_sell_exchange(request: Request, payload: Dict[str, Any]):
    """
    Force sell using actual exchange balance (ignores system position).

    Payload:
    - currency: coin symbol (e.g., "BTC", "ETH")
    - pct: sell ratio (default 100%)
    """
    # [2026-04-09 security] Trade rate limiting
    if not _check_rate_limit(request, limit=10):
        return {"ok": False, "error": "rate_limited", "message": "Too many force_sell requests. Max 10/min."}
    system = request.app.state.system
    if not system.trade_client:
        return {"ok": False, "error": "trade_client not available"}
    
    currency = str(payload.get("currency") or "").strip().upper()
    pct = float(payload.get("pct") or 100)
    
    if not currency:
        return {"ok": False, "error": "currency required"}
    
    try:
        free_qty = float(system.trade_client.get_balance(currency, include_locked=False) or 0)
        
        if free_qty <= 0:
            return {"ok": False, "error": f"No free balance for {currency}", "free": 0}
        
        sell_qty = free_qty * (pct / 100.0)
        market = Q.market(currency)

        # Check the minimum order amount
        price = float(price_store.get_price(market) or 0)
        if price <= 0:
            # Query the price directly
            try:
                from app.core.constants import (
                        BYBIT_MARKET_TICKERS,
                        bybit_v5_rest_category,
                        parse_bybit_list,
                        normalize_bybit_ticker,
                    )
                from app.core.rate_limiter import bybit_get
                resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=3)
                if resp.status_code == 200:
                    for _t in parse_bybit_list(resp.json()):
                        if isinstance(_t, dict):
                            _tc = normalize_bybit_ticker(_t)
                            if _tc.get("market", "").upper() == market.upper():
                                price = float(_tc.get("trade_price") or 0)
                                break
            except Exception as exc:
                logger.warning("[ENGINE_API] force_sell price query: %s", exc)

        value_usdt = sell_qty * price if price else 0
        if value_usdt < Q.min_order:
            return {"ok": False, "error": f"Value too small: {value_usdt:.2f} USDT (min min order)", "value_usdt": value_usdt}
        
        # Execute the force sell
        sell_ok, order = system.trade_client.sell_market(market, sell_qty)
        
        if not sell_ok:
            return {"ok": False, "error": f"Sell failed: {order}"}
        
        order_id = order.get("uuid") or order.get("id") if isinstance(order, dict) else str(order)
        
        # Also clear the system position
        ctx = system.coordinator.get_context(market)
        if ctx and ctx.position:
            ctx.position = None
        
        system.ledger.append(
            "FORCE_SELL",
            market=market,
            currency=currency,
            qty=sell_qty,
            price=price,
            value_usdt=value_usdt,
            order_id=order_id,
        )
        
        return {
            "ok": True,
            "market": market,
            "currency": currency,
            "sold_qty": sell_qty,
            "price": price,
            "value_usdt": round(value_usdt, 2),
            "order_id": order_id,
        }
    except Exception as e:
        logger.warning("engine_router.force_sell_exchange L887: %s", e)
        return {"ok": False, "error": str(e)}


@router.post(
    "/clear_dust",
    summary="Clear dust positions from system (positions under threshold USDT)",
    responses={200: {"description": "Dust cleared"}},
)
def clear_dust_positions(request: Request, threshold: float = Query(1000.0, description="USDT threshold (default 1000)")):
    """Clear positions (residue) under the threshold USDT from the system."""
    from app.manager.oma_market_registry import MarketState
    
    system = request.app.state.system
    
    cleared = []
    for market in list(system.coordinator.contexts.keys()):
        ctx = system.coordinator.contexts.get(market)
        if not ctx:
            continue
        
        # If there is no position or qty <= 0, clear immediately
        pos = getattr(ctx, "position", None)
        qty = float(pos.get("qty") or 0) if pos else 0
        entry = float(pos.get("entry") or 0) if pos else 0

        # Compute value at the current price
        price = float(price_store.get_price(market) or entry or 0)
        current_value = qty * price if price else (qty * entry)

        # Clear if at or below threshold (changed to <=)
        if current_value <= threshold:
            # Remove the position
            ctx.position = None

            # Also set DISABLED in the OMA registry
            try:
                system.oma_set_market(
                    market=market,
                    state=MarketState.DISABLED,
                    reason=["dust_cleared"]
                )
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[ENGINE_API] clear_dust OMA DISABLED: %s", exc, exc_info=True)

            # Remove from the coordinator
            try:
                system.coordinator.remove_market(market)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[ENGINE_API] clear_dust remove_market: %s", exc, exc_info=True)
            
            cleared.append({"market": market, "qty": qty, "value_usdt": round(current_value, 4)})
            system.ledger.append("DUST_CLEARED", market=market, qty=qty, value_usdt=current_value)
    
    return {"ok": True, "cleared": cleared, "count": len(cleared), "threshold": threshold}


# ============================================================
# [2026-02-01] True dust vacuum: minimal buy then sell all
# ============================================================
@router.get(
    "/dust/scan",
    summary="Scan for dust positions in exchange account",
    responses={200: {"description": "List of dust positions"}},
)
def scan_dust_positions(
    request: Request,
    threshold_usdt: float = Query(5.0, description="Dust threshold in USDT"),
):
    """Scan the Bybit account for dust positions (at or below threshold_usdt)."""
    system = request.app.state.system
    
    if not system.trade_client:
        return {"ok": False, "error": "NO_TRADE_CLIENT", "dust": []}
    
    try:
        accounts = system.trade_client.accounts()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("engine_router.scan_dust_positions L964: %s", e)
        return {"ok": False, "error": str(e), "dust": []}
    
    dust_list = []
    min_order_usdt = Q.min_order  # min order
    
    # Query all coin prices at once
    all_currencies = [str(acc.get("currency") or "").upper() for acc in accounts if acc.get("currency") != Q.symbol]
    all_markets = [Q.market(c) for c in all_currencies if c]

    # Batch-query current prices via the exchange API
    price_map = {}
    if all_markets:
        try:
            from app.core.rate_limiter import bybit_get
            from app.core.constants import (
                        BYBIT_MARKET_TICKERS,
                        bybit_v5_rest_category,
                        parse_bybit_list,
                        normalize_bybit_ticker,
                    )
            resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5)
            if resp.status_code == 200:
                _market_set = set(m.upper() for m in all_markets)
                for item in parse_bybit_list(resp.json()):
                    if not isinstance(item, dict):
                        continue
                    item = normalize_bybit_ticker(item)
                    mkt = str(item.get("market") or "").upper()
                    px = float(item.get("trade_price") or 0)
                    if mkt in _market_set and px > 0:
                        price_map[mkt] = px
        except Exception as exc:
            logger.warning("[ENGINE_API] scan_dust batch price query: %s", exc)
    
    for acc in accounts:
        try:
            cur = str(acc.get("currency") or "").upper()
            if cur == Q.symbol:  # skip quote currency
                continue
            
            balance = float(acc.get("balance") or 0)
            locked = float(acc.get("locked") or 0)
            qty = balance + locked
            
            if qty <= 0:
                continue
            
            market = Q.market(cur)

            # Price priority: API query > price_store > avg_buy_price
            price = price_map.get(market) or price_store.get_price(market)
            if not price:
                avg_buy = float(acc.get("avg_buy_price") or 0)
                if avg_buy > 0:
                    price = avg_buy
            
            if not price or price <= 0:
                continue
            
            value_usdt = qty * price

            # Dust condition: current value below threshold
            if value_usdt < threshold_usdt:
                # To be sellable, value must be at least the min order
                need_buy_usdt = max(0, min_order_usdt - value_usdt + 100)  # 100 buffer
                
                dust_list.append({
                    "market": market,
                    "currency": cur,
                    "qty": round(qty, 8),
                    "price": round(price, 2),
                    "value_usdt": round(value_usdt, 2),
                    "need_buy_usdt": round(need_buy_usdt, 0),
                    "can_sell_direct": value_usdt >= min_order_usdt,
                })
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[ENGINE_API] scan_dust item processing: %s", exc)
            continue
    
    # Sort by value
    dust_list.sort(key=lambda x: x["value_usdt"])
    
    return {
        "ok": True,
        "dust": dust_list,
        "count": len(dust_list),
        "threshold_usdt": threshold_usdt,
        "min_order_usdt": min_order_usdt,
    }


@router.post(
    "/dust/vacuum",
    summary="Vacuum dust: buy minimum then sell all",
    responses={200: {"description": "Dust vacuumed"}},
)
def vacuum_dust(
    request: Request,
    market: str = Query(..., description="Market to vacuum (e.g., BTCUSDT)"),
    dry_run: bool = Query(True, description="If True, only simulate (no actual orders)"),
):
    """
    True dust vacuum: buy a minimal amount then sell everything.

    1. Check the current holdings
    2. If below the sellable amount (min order), buy a minimal amount more
    3. Wait 3 seconds, then market-sell everything
    4. Result: the coin's balance is zero
    """
    import time
    
    system = request.app.state.system
    market = market.upper()
    if not Q.config.market_prefix and market.startswith(Q.config.market_prefix):
        market = Q.market(market)
    
    if not system.trade_client:
        return {"ok": False, "error": "NO_TRADE_CLIENT"}
    
    min_order_usdt = Q.min_order  # min order
    currency = Q.extract_base(market)
    
    result = {
        "market": market,
        "dry_run": dry_run,
        "steps": [],
    }
    
    try:
        # Step 1: Check the current balance
        balance = system.trade_client.get_balance(currency, include_locked=False)
        price = price_store.get_price(market)

        if not price or price <= 0:
            # Query the price
            try:
                from app.core.rate_limiter import bybit_get
                from app.core.constants import (
                        BYBIT_MARKET_TICKERS,
                        bybit_v5_rest_category,
                        parse_bybit_list,
                        normalize_bybit_ticker,
                    )
                resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=3)
                if resp.status_code == 200:
                    for _t in parse_bybit_list(resp.json()):
                        if isinstance(_t, dict):
                            _tc = normalize_bybit_ticker(_t)
                            if _tc.get("market", "").upper() == market.upper():
                                price = float(_tc.get("trade_price") or 0)
                                break
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[ENGINE_API] vacuum_dust price query: %s", exc)
        
        if not price or price <= 0:
            return {"ok": False, "error": "PRICE_NOT_AVAILABLE", "market": market}
        
        current_value = balance * price
        result["initial_balance"] = round(balance, 8)
        result["initial_value_usdt"] = round(current_value, 2)
        result["price"] = round(price, 2)
        
        result["steps"].append({
            "step": 1,
            "action": "scan",
            "balance": round(balance, 8),
            "value_usdt": round(current_value, 2),
        })
        
        if balance <= 0:
            return {"ok": True, "message": "No balance to vacuum", **result}
        
        # Step 1.5: Skip vacuuming coins that are currently in profit
        # Query the average buy price
        avg_buy_price = 0.0
        try:
            accounts = system.trade_client.client.get_accounts()
            for acc in accounts:
                if str(acc.get("currency", "")).upper() == currency.upper():
                    avg_buy_price = float(acc.get("avg_buy_price") or 0)
                    break
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[ENGINE_API] vacuum_dust avg_buy_price query: %s", exc)

        if avg_buy_price > 0 and price >= avg_buy_price:
            profit_pct = (price - avg_buy_price) / avg_buy_price * 100
            result["avg_buy_price"] = round(avg_buy_price, 2)
            result["profit_pct"] = round(profit_pct, 2)
            result["skipped"] = True
            result["skip_reason"] = "profitable"
            return {"ok": True, "message": f"Skipped: coin is profitable (+{profit_pct:.2f}%)", **result}
        
        # Step 2: Check sellability and buy more if needed
        # [FIX] Account for Bybit minimum order — safely fix at 6 USDT
        dust_buy_amount = 6.0  # fixed 6 USDT

        need_buy = current_value < min_order_usdt
        buy_amount_usdt = 0

        if need_buy:
            buy_amount_usdt = dust_buy_amount  # fixed 6 USDT
            
            result["steps"].append({
                "step": 2,
                "action": "buy_minimum",
                "reason": f"Current value {current_value:.0f} < {min_order_usdt:.0f}",
                "buy_amount_usdt": buy_amount_usdt,
            })
            
            if not dry_run:
                try:
                    buy_order = system.trade_client.market_buy(market, buy_amount_usdt)
                    result["steps"][-1]["order"] = buy_order

                    # Wait for fill (3 seconds)
                    time.sleep(3)

                    # Re-check the balance
                    balance = system.trade_client.get_balance(currency, include_locked=False)
                    result["steps"][-1]["new_balance"] = round(balance, 8)
                except Exception as e:
                    logger.warning("engine_router.vacuum_dust L1179: %s", e)
                    result["steps"][-1]["error"] = str(e)
                    return {"ok": False, "error": f"BUY_FAILED: {e}", **result}
        else:
            result["steps"].append({
                "step": 2,
                "action": "skip_buy",
                "reason": f"Current value {current_value:.0f} >= {min_order_usdt:.0f}",
            })
        
        # Step 3: Sell everything
        if balance <= 0:
            # If there is still no balance after buying (may be locked)
            balance = system.trade_client.get_balance(currency, include_locked=False)
        
        result["steps"].append({
            "step": 3,
            "action": "sell_all",
            "qty": round(balance, 8),
        })
        
        if not dry_run:
            if balance > 0:
                try:
                    sell_order = system.trade_client.market_sell(market, balance)
                    result["steps"][-1]["order"] = sell_order

                    # Wait for fill
                    time.sleep(2)

                    # Check the final balance
                    final_balance = system.trade_client.get_balance(currency, include_locked=False)
                    result["final_balance"] = round(final_balance, 8)
                    result["final_value_usdt"] = round(final_balance * price, 2)

                    # System cleanup
                    try:
                        ctx = system.coordinator.contexts.get(market)
                        if ctx:
                            ctx.position = None
                        system.oma_set_market(market, MarketState.DISABLED, reason=["dust_vacuumed"])
                        system.coordinator.remove_market(market)
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.warning("[ENGINE_API] vacuum_dust system cleanup: %s", exc, exc_info=True)
                    
                    system.ledger.append(
                        "DUST_VACUUMED",
                        market=market,
                        initial_value=current_value,
                        bought_usdt=buy_amount_usdt,
                        sold_qty=balance,
                    )
                    
                except Exception as e:
                    logger.warning("engine_router.vacuum_dust L1232: %s", e)
                    result["steps"][-1]["error"] = str(e)
                    return {"ok": False, "error": f"SELL_FAILED: {e}", **result}
            else:
                result["steps"][-1]["skipped"] = "No balance to sell"
        
        result["ok"] = True
        result["message"] = "Dust vacuumed successfully" if not dry_run else "Dry run completed"
        return result
        
    except Exception as e:
        logger.warning("engine_router.vacuum_dust L1242: %s", e)
        return {"ok": False, "error": str(e), **result}


@router.post(
    "/dust/vacuum_all",
    summary="Vacuum all dust positions",
    responses={200: {"description": "All dust vacuumed"}},
)
def vacuum_all_dust(
    request: Request,
    threshold_usdt: float = Query(5.0, description="Dust threshold in USDT"),
    dry_run: bool = Query(True, description="If True, only simulate"),
    max_coins: int = Query(5, description="Max coins to vacuum in one call"),
):
    """Vacuum all dust positions at once."""
    # Scan first
    scan_result = scan_dust_positions(request, threshold_usdt=threshold_usdt)

    if not scan_result.get("ok"):
        return scan_result

    dust_list = scan_result.get("dust", [])
    if not dust_list:
        return {"ok": True, "message": "No dust found", "vacuumed": []}

    # Limit the max count
    to_vacuum = dust_list[:max_coins]
    
    results = []
    for dust in to_vacuum:
        market = dust["market"]
        vac_result = vacuum_dust(request, market=market, dry_run=dry_run)
        results.append({
            "market": market,
            "result": vac_result,
        })
        
        # Avoid rate limiting
        if not dry_run:
            import time
            time.sleep(1)

    return {
        "ok": True,
        "dry_run": dry_run,
        "total_dust": len(dust_list),
        "vacuumed_count": len(results),
        "results": results,
    }


@router.post(
    "/dust/adopt",
    summary="Adopt dust as LongHold: buy minimum then register as LongHold",
    responses={200: {"description": "Dust adopted as LongHold"}},
)
def adopt_dust_as_longhold(
    request: Request,
    market: str = Query(..., description="Market to adopt (e.g., BTCUSDT)"),
    dry_run: bool = Query(True, description="If True, only simulate"),
    buy_amount_usdt: float = Query(6.0, description="Amount to buy (default 6 USDT)"),
):
    """
    Adopt a dust coin as LongHold: buy a minimal amount then register as LongHold.

    A strategy that accumulates a sharply dropped coin near the bottom and holds long-term.
    Instead of selling, register and manage it as LongHold.
    """
    import time
    
    system = request.app.state.system
    market = market.upper()
    if not Q.config.market_prefix and market.startswith(Q.config.market_prefix):
        market = Q.market(market)
    
    if not system.trade_client:
        return {"ok": False, "error": "NO_TRADE_CLIENT"}
    
    min_order_usdt = Q.min_order  # min order
    currency = Q.extract_base(market)
    
    # Validate the buy amount
    if buy_amount_usdt < min_order_usdt:
        buy_amount_usdt = 6.0
    
    result = {
        "market": market,
        "dry_run": dry_run,
        "buy_amount_usdt": buy_amount_usdt,
        "steps": [],
    }
    
    try:
        # Step 1: Check the current balance and price
        balance = system.trade_client.get_balance(currency, include_locked=False)
        price = price_store.get_price(market)
        
        if not price or price <= 0:
            try:
                from app.core.rate_limiter import bybit_get
                from app.core.constants import (
                        BYBIT_MARKET_TICKERS,
                        bybit_v5_rest_category,
                        parse_bybit_list,
                        normalize_bybit_ticker,
                    )
                resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=3)
                if resp.status_code == 200:
                    for _t in parse_bybit_list(resp.json()):
                        if isinstance(_t, dict):
                            _tc = normalize_bybit_ticker(_t)
                            if _tc.get("market", "").upper() == market.upper():
                                price = float(_tc.get("trade_price") or 0)
                                break
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[ENGINE_API] Step 1 balance/price check: %s", exc, exc_info=True)
        
        if not price or price <= 0:
            return {"ok": False, "error": "PRICE_NOT_AVAILABLE", "market": market}
        
        current_value = balance * price
        result["initial_balance"] = round(balance, 8)
        result["initial_value_usdt"] = round(current_value, 2)
        result["price"] = round(price, 2)
        
        result["steps"].append({
            "step": 1,
            "action": "scan",
            "balance": round(balance, 8),
            "value_usdt": round(current_value, 2),
        })
        
        # Step 2: Buy (buy more if balance is below min_order)
        need_buy = current_value < min_order_usdt
        
        if need_buy:
            result["steps"].append({
                "step": 2,
                "action": "buy_for_longhold",
                "reason": f"Current value {current_value:.0f} < {min_order_usdt:.0f}",
                "buy_amount_usdt": buy_amount_usdt,
            })
            
            if not dry_run:
                try:
                    buy_order = system.trade_client.market_buy(market, buy_amount_usdt)
                    result["steps"][-1]["order"] = buy_order

                    # Wait for fill (3 seconds)
                    time.sleep(3)

                    # Re-check the balance
                    balance = system.trade_client.get_balance(currency, include_locked=False)
                    result["steps"][-1]["new_balance"] = round(balance, 8)
                except Exception as e:
                    logger.warning("engine_router.adopt_dust_as_longhold L1392: %s", e)
                    result["steps"][-1]["error"] = str(e)
                    return {"ok": False, "error": f"BUY_FAILED: {e}", **result}
        else:
            result["steps"].append({
                "step": 2,
                "action": "skip_buy",
                "reason": f"Current value {current_value:.0f} >= {min_order_usdt:.0f}",
            })
        
        # Step 3: Register as LongHold
        # Query the average buy price
        avg_buy_price = price  # default
        try:
            accounts = system.trade_client.client.get_accounts()
            for acc in accounts:
                if str(acc.get("currency", "")).upper() == currency.upper():
                    avg_buy_price = float(acc.get("avg_buy_price") or price)
                    balance = float(acc.get("balance") or balance)
                    break
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[ENGINE_API] average buy price query: %s", exc, exc_info=True)
        
        final_value = balance * price
        
        result["steps"].append({
            "step": 3,
            "action": "register_longhold",
            "balance": round(balance, 8),
            "avg_buy_price": round(avg_buy_price, 2),
            "value_usdt": round(final_value, 2),
        })
        
        if not dry_run and balance > 0:
            try:
                # LongHold config
                longhold_config = {
                    "market": market,
                    "strategy": "GAZUA",  # wait-for-pump strategy
                    "budget_usdt": round(final_value, 0),
                    "entry_price": avg_buy_price,
                    "qty": balance,
                    "target_profit_pct": 100.0,  # 100% profit target (2x)
                    "notify_cooldown_sec": 86400,  # 1-day notify cooldown
                    "source": "dust_adopt",
                }
                
                ladder_mgr = getattr(system, "ladder_manager", None)
                if ladder_mgr:
                    ladder_mgr.save_longhold_config({
                        "market": market,
                        **longhold_config,
                        "enabled": True,
                    })
                result["steps"][-1]["registered"] = True
                result["steps"][-1]["config"] = longhold_config
                
                # [FIX 2026-03-23] Dust Adopt also occupies a slot, so check LongHold slot headroom
                # If LongHold exceeds the strategy quota, skip ACTIVE registration (prevent unbounded slot growth)
                try:
                    system.oma_set_market(market, MarketState.ACTIVE, reason=["dust_adopted", "longhold"])
                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("[ENGINE_API] LongHold ACTIVE registration failed: %s", exc, exc_info=True)
                
                system.ledger.append(
                    "DUST_ADOPTED",
                    market=market,
                    initial_value=current_value,
                    bought_usdt=buy_amount_usdt if need_buy else 0,
                    final_balance=balance,
                    final_value=final_value,
                    avg_buy_price=avg_buy_price,
                )
                
                # Telegram notification
                try:
                    from app.notify.telegram import send_telegram
                    send_telegram(
                        f"🏠 [DUST → LONGHOLD] {market}\n"
                        f"• Qty: {balance:.6g}\n"
                        f"• Avg: {Q.format(avg_buy_price, with_suffix=False)}\n"
                        f"• Value: {Q.format(final_value)}\n"
                        f"• Target: +100% (2x)"
                    )
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[ENGINE_API] telegram notification: %s", exc)
                
            except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
                logger.warning("engine_router.adopt_dust_as_longhold L1479: %s", e)
                result["steps"][-1]["error"] = str(e)
                return {"ok": False, "error": f"LONGHOLD_REGISTER_FAILED: {e}", **result}
        
        result["ok"] = True
        result["final_balance"] = round(balance, 8)
        result["final_value_usdt"] = round(final_value, 2)
        result["message"] = "Dust adopted as LongHold successfully" if not dry_run else "Dry run completed"
        return result
        
    except Exception as e:
        logger.warning("engine_router.adopt_dust_as_longhold L1489: %s", e)
        return {"ok": False, "error": str(e), **result}


@router.post(
    "/dust/adopt_all",
    summary="Adopt all dust positions as LongHold",
    responses={200: {"description": "All dust adopted as LongHold"}},
)
def adopt_all_dust_as_longhold(
    request: Request,
    threshold_usdt: float = Query(5.0, description="Dust threshold in USDT"),
    dry_run: bool = Query(True, description="If True, only simulate"),
    max_coins: int = Query(5, description="Max coins to adopt in one call"),
    buy_amount_usdt: float = Query(6.0, description="Amount to buy per coin (USDT)"),
):
    """Adopt all dust positions as LongHold."""
    # Scan first
    scan_result = scan_dust_positions(request, threshold_usdt=threshold_usdt)

    if not scan_result.get("ok"):
        return scan_result

    dust_list = scan_result.get("dust", [])
    if not dust_list:
        return {"ok": True, "message": "No dust found", "adopted": []}

    # Limit the max count
    to_adopt = dust_list[:max_coins]
    
    results = []
    for dust in to_adopt:
        market = dust["market"]
        adopt_result = adopt_dust_as_longhold(
            request, 
            market=market, 
            dry_run=dry_run, 
            buy_amount_usdt=buy_amount_usdt
        )
        results.append({
            "market": market,
            "result": adopt_result,
        })
        
        # Avoid rate limiting
        if not dry_run:
            import time
            time.sleep(2)  # longer wait since it involves buy + registration
    
    return {
        "ok": True,
        "dry_run": dry_run,
        "total_dust": len(dust_list),
        "adopted_count": len(results),
        "results": results,
    }
