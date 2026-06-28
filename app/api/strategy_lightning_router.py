# ============================================================
# File: app/api/strategy_lightning_router.py
# Extracted from strategy_router.py — Phase 1-C (file diet)
#
# LIGHTNING strategy setup/query/stop endpoints
# ============================================================

from fastapi import APIRouter, Request, Query
from typing import Dict, Any, List, Optional
import logging
import json
import os
from pydantic import BaseModel
from app.manager.oma_market_registry import MarketState
from app.core.hyper_price_store import price_store
from app.api.strategy_utils import (
    _check_manual_overflow, _generate_coin_warnings,
    _sync_policy_tp_sl, StrategyStopRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ============================================================
# LIGHTNING Guards — persistence (runtime/guards/lightning_guards.json) + actual ctx.controls application
# Actual enforcement paths (verified):
#   - guards bucket  → hs_mixin_intent _g_bool/_g_float (per-market enforcement)
#   - strategy.params → plugin_lightning + nunnaya_engine/intent (per-market)
#   ※ drawdown_guard / entry_global_gap_sec are global attrs, not per-market → excluded
#   ※ deep_sl is not enforced anywhere (no-op) → excluded
# ============================================================
_LTG_GUARDS_PATH = os.path.join("runtime", "guards", "lightning_guards.json")
_LTG_GUARD_KEYS = ("entry_ob_guard_enabled", "entry_ceiling_guard", "exit_profit_guard",
                   "exit_min_net_profit_pct", "exit_slippage_guard_bps")   # → ctx.controls["guards"]
_LTG_PARAM_KEYS = ("min_order_usdt", "user_sell_only", "hold_sell")        # → ctx.controls["strategy"]["params"]


def _ltg_load_guards() -> Dict[str, Any]:
    try:
        if os.path.exists(_LTG_GUARDS_PATH):
            with open(_LTG_GUARDS_PATH, encoding="utf-8") as f:
                return json.load(f) or {}
    except (OSError, ValueError) as exc:
        logger.warning("[lightning/guards] load failed: %s", exc)
    return {}


def _ltg_persist_guards(values: Dict[str, Any]) -> None:
    cur = _ltg_load_guards()
    cur.update(values)
    os.makedirs(os.path.dirname(_LTG_GUARDS_PATH), exist_ok=True)
    with open(_LTG_GUARDS_PATH, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=2, ensure_ascii=False)


def _ltg_controls_patch(values: Dict[str, Any]) -> Dict[str, Any]:
    """Saved guard values → ctx.update_controls patch (guards bucket + strategy.params)."""
    g = {k: values[k] for k in _LTG_GUARD_KEYS if values.get(k) is not None}
    p = {k: values[k] for k in _LTG_PARAM_KEYS if values.get(k) is not None}
    patch: Dict[str, Any] = {}
    if g:
        patch["guards"] = g
    if p:
        patch["strategy"] = {"params": p}
    return patch

# ============================================================
# Pydantic Models
# ============================================================
class LightningSetupRequest(BaseModel):
    market: str
    budget_usdt: Optional[float] = None
    tp_pct: float = 5.0
    sl_pct: float = -3.0

    @property
    def budget(self) -> float:
        return self.budget_usdt or 0.0

# ============================================================
# LIGHTNING Setup
# ============================================================
@router.post(
    "/lightning/setup",
    summary="Setup a market with LIGHTNING strategy",
    responses={
        200: {"description": "Market configured with LIGHTNING strategy"},
    },
)
def setup_lightning_market(req: LightningSetupRequest, request: Request):
    """
    Register a market with LIGHTNING strategy.

    - Sets OMA state to ACTIVE with specified budget
    - Configures TP/SL percentages for fast scalping
    """
    system = request.app.state.system
    market = req.market.strip().upper()

    # [2026-03-07] Manual order slot overflow check (+2 limit)
    overflow_check = _check_manual_overflow(system, "LIGHTNING", market)
    coin_warnings = _generate_coin_warnings(system, market, "LIGHTNING")
    if not overflow_check["allowed"]:
        return {"ok": False, "error": "slot_overflow", "detail": overflow_check["message"],
                "overflow": overflow_check, "warnings": coin_warnings}

    # 1. Set OMA State to ACTIVE with Budget
    system.oma_set_market(
        market=market,
        state=MarketState.ACTIVE,
        reason=["lightning_factory_setup"],
        budget_usdt=req.budget
    )

    # 2. Configure Strategy Controls (LIGHTNING)
    # Use defaults from market_controls.py
    from app.manager.market_controls import apply_engine_controls, build_strategy_controls_payload
    # Apply defaults first
    apply_engine_controls(system, market, "LIGHTNING")

    # Override with user inputs
    ctx = system.coordinator.ensure_market(market)
    # [2026-06-01] Manual deploy marker → A/M column in list (autopilot auto-placement has no marker = A)
    patch = {"strategy": {"params": {"tp": req.tp_pct, "sl": req.sl_pct, "entry_source": "manual"}}}
    ctx.update_controls(patch)
    _sync_policy_tp_sl(ctx, tp=req.tp_pct, sl=req.sl_pct)

    # Apply saved LIGHTNING guards to the newly deployed market too (consistent per deploy — actual effect)
    saved_patch = _ltg_controls_patch(_ltg_load_guards())
    if saved_patch:
        ctx.update_controls(saved_patch)
        try:
            system._save_context_state()
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            logger.warning("[lightning/setup] guards save_context_state: %s", exc)

    return {"ok": True, "market": market, "setup": req.dict(),
            "overflow": overflow_check, "warnings": coin_warnings}

@router.get(
    "/lightning/list",
    summary="List all LIGHTNING strategy markets",
    responses={
        200: {"description": "List of markets running LIGHTNING strategy"},
    },
)
def list_lightning_markets(request: Request):
    """
    List all markets currently running LIGHTNING strategy.

    - Includes position, PnL, TP/SL params, and readiness status
    """
    system = request.app.state.system
    items = []

    for market, ctx in system.coordinator.contexts.items():
        try:
            ctrls = getattr(ctx, "controls", {}) or {}
            strat = ctrls.get("strategy", {}) or {}
            if not strat.get("enabled"):
                continue
            mode = str(strat.get("mode") or "").upper()
            if mode != "LIGHTNING":
                continue

            # Collect info
            pos = getattr(ctx, "position", {}) or {}
            # Extract params
            params = strat.get("params") or {}

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

            # Stats
            trade_count = getattr(ctx, "win_count", 0) + getattr(ctx, "loss_count", 0)
            total_profit = getattr(ctx, "total_profit", 0.0)

            items.append({
                "market": market,
                "state": getattr(ctx, "market_state", "UNKNOWN"),
                "am": "M" if (params.get("entry_source") == "manual") else "A",   # manual (M) / autopilot auto (A)
                "budget": getattr(ctx, "allocated_capital", 0.0),
                "params": {
                    "tp": params.get("tp", 5.0),
                    "sl": params.get("sl", -3.0),
                    "probe_ratio": params.get("probe_ratio", 0.30),
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
                "v2": {
                    "lt_state": ctx.get_var("lt_state", "IDLE") if hasattr(ctx, "get_var") else "IDLE",
                    "regime": ctx.get_var("lt_regime", "TREND") if hasattr(ctx, "get_var") else "TREND",
                },
            })
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[strategy_lightning_router] %s: %s", 'Stats except-> continue', exc, exc_info=True)
            continue

    return {"ok": True, "items": items}

@router.post(
    "/lightning/stop",
    summary="Stop LIGHTNING strategy for a market",
    responses={
        200: {"description": "LIGHTNING strategy stopped"},
    },
)
def stop_lightning_market(req: StrategyStopRequest, request: Request):
    """
    Disable LIGHTNING strategy for a market.

    - **liquidate**: If true, triggers recovery liquidation
    - **delete**: If true, sets state to DISABLED
    """
    system = request.app.state.system
    market = req.market.strip().upper()

    if req.liquidate:
        target_state = MarketState.RECOVERY
        reason = ["lightning_stop_liquidate"]
    elif req.delete:
        target_state = MarketState.DISABLED
        reason = ["lightning_delete_btn", "user_disabled"]
    else:
        target_state = MarketState.WATCH
        reason = ["lightning_stop_btn"]

    system.oma_set_market(
        market=market,
        state=target_state,
        reason=reason
    )

    if req.liquidate:
        system.request_recovery_liquidate(market=market, reason="lightning_stop_liquidate")

    ctx = system.coordinator.ensure_market(market)
    patch = {
        "strategy": { "enabled": False }
    }
    ctx.update_controls(patch)
    system._save_context_state()

    return {"ok": True, "market": market, "status": "stopped", "liquidating": req.liquidate}


# ============================================================
# LIGHTNING Guards — save (live ctx.controls application to all LIGHTNING markets) / query
# ============================================================
class LightningGuardsRequest(BaseModel):
    # → ctx.controls["guards"] (per-market enforcement: hs_mixin_intent _g_bool/_g_float)
    entry_ob_guard_enabled: Optional[bool] = None
    entry_ceiling_guard: Optional[bool] = None
    exit_profit_guard: Optional[bool] = None
    exit_min_net_profit_pct: Optional[float] = None   # evaluated when exit_profit_guard is ON
    exit_slippage_guard_bps: Optional[float] = None   # evaluated when exit_profit_guard is ON
    # → ctx.controls["strategy"]["params"] (per-market)
    min_order_usdt: Optional[float] = None
    user_sell_only: Optional[bool] = None             # never sell
    hold_sell: Optional[bool] = None                  # HOLD (block only TP auto-sell, allow SL)


@router.post(
    "/lightning/guards",
    summary="Apply LIGHTNING guards to all LIGHTNING markets (real per-market effect)",
)
def save_lightning_guards(req: LightningGuardsRequest, request: Request):
    """Save LIGHTNING Guards + apply live to ctx.controls of all current LIGHTNING markets.

    - guards bucket / strategy.params are read per-market and enforced by hs_mixin_intent / plugin.
    - Persisted to runtime/guards/lightning_guards.json → auto-reapplied on later setup (deploy).
    """
    system = request.app.state.system
    values = {k: v for k, v in req.dict().items() if v is not None}
    if not values:
        return {"ok": True, "applied_markets": [], "saved": 0, "message": "no changes"}

    _ltg_persist_guards(values)
    patch = _ltg_controls_patch(values)
    applied: List[str] = []
    for market, ctx in system.coordinator.contexts.items():
        try:
            strat = (getattr(ctx, "controls", {}) or {}).get("strategy", {}) or {}
            if str(strat.get("mode") or "").upper() != "LIGHTNING":
                continue
            if patch:
                ctx.update_controls(patch)
            applied.append(market)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[lightning/guards] %s apply failed: %s", market, exc, exc_info=True)

    try:
        system._save_context_state()
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        logger.warning("[lightning/guards] save_context_state: %s", exc)

    return {"ok": True, "applied_markets": applied, "saved": len(values)}


@router.get(
    "/lightning/guards",
    summary="Get saved LIGHTNING guards",
)
def get_lightning_guards():
    return {"ok": True, "guards": _ltg_load_guards()}
