# ============================================================
# File: app/api/triage_router.py
# Autocoin OS v3-H — Triage Mode API Router
# ============================================================

from fastapi import APIRouter, HTTPException, Request
from typing import Dict, Any, Optional
from pydantic import BaseModel

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/triage", tags=["triage"])

# ============================================================
# Request Models
# ============================================================

class EnterTriageRequest(BaseModel):
    reason: str = "manual"
    trigger_pnl_pct: Optional[float] = None       # temporary override (None = use ENV value)
    profit_target_pct: Optional[float] = None
    max_dca_ratio: Optional[float] = None

class SettingsPatchRequest(BaseModel):
    enabled: Optional[bool] = None
    trigger_pnl_pct: Optional[float] = None
    trigger_loss_count: Optional[int] = None
    max_dca_ratio: Optional[float] = None
    profit_target_pct: Optional[float] = None
    max_loss_exclude_pct: Optional[float] = None
    dca_interval_sec: Optional[float] = None
    max_duration_hours: Optional[float] = None
    coin_timeout_hours: Optional[float] = None
    exit_pnl_pct: Optional[float] = None
    notify: Optional[bool] = None
    buy_mode: Optional[str] = None          # "block_all" | "allow_non_loss"
    opportunistic_dca: Optional[bool] = None  # conditional immediate DCA on losing coins
    market_recovery_exit_enabled: Optional[bool] = None   # auto-release on market recovery
    market_recovery_min_hours: Optional[float] = None     # minimum elapsed time before auto-release
    loss_grace_min: Optional[float] = None                # exclude from loss count within N minutes after buy
    max_concurrent_targets: Optional[int] = None          # number of concurrent recovery targets
    recovery_target: Optional[str] = None                 # ALL / 0.6 / 3, etc.
    emergency_exit_enabled: Optional[bool] = None         # emergency exit mode
    emergency_moderate_avg_loss_pct: Optional[float] = None  # warning threshold (default -10%)
    emergency_severe_avg_loss_pct: Optional[float] = None    # emergency threshold (default -30%)
    # [2026-06-01] connect fields present in GET settings but missing from PATCH (applied if patch_settings key is in tm.settings)
    global_dca_cap_pct: Optional[float] = None               # total DCA aggregate portfolio % cap
    focus_dca_allow: Optional[bool] = None                   # allow focus market PRM bypass
    sell_timeout_sec: Optional[float] = None                 # TRIAGE_SELL timeout
    min_position_usdt: Optional[float] = None                # dust exclusion threshold

class SkipRequest(BaseModel):
    reason: str = "manual skip"
    market: Optional[str] = None   # skip a specific target (None = first one)

# ============================================================
# Endpoints
# ============================================================

@router.get("/status")
async def get_triage_status(request: Request) -> Dict[str, Any]:
    """
    Get the current triage mode status

    Returns:
        - state: current state (NORMAL / TRIAGE_INIT / TRIAGE_SCAN / TRIAGE_DCA / TRIAGE_WAIT / TRIAGE_SELL / TRIAGE_EXIT)
        - active: whether triage is active
        - current_target: market currently being recovered
        - recovered: list of recovered markets
        - skipped: list of skipped markets
        - trigger_reason: reason for activation
        - elapsed_sec: elapsed time since activation
        - settings: current settings
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)
        if tm is None:
            return {"state": "NORMAL", "active": False, "message": "TriageManager not initialized"}
        return tm.get_status_dict()
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("triage_router.unknown L83: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/enter")
async def enter_triage(request: Request, req: EnterTriageRequest) -> Dict[str, Any]:
    """
    Manually enter triage mode

    Manual entry is allowed even when OMA_TRIAGE_ENABLED=0.
    The enabled setting means 'auto-trigger disabled', not 'manual entry forbidden'.
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)
        if tm is None:
            raise HTTPException(status_code=503, detail="TriageManager not initialized")

        from app.manager.triage_manager import TriageManager
        if tm.state != TriageManager.STATE_NORMAL:
            return {
                "success": False,
                "message": f"Already in triage mode: {tm.state}",
                "state": tm.state
            }

        # apply temporary parameter overrides
        overrides = {}
        if req.trigger_pnl_pct is not None:
            overrides["trigger_pnl_pct"] = req.trigger_pnl_pct
        if req.profit_target_pct is not None:
            overrides["profit_target_pct"] = req.profit_target_pct
        if req.max_dca_ratio is not None:
            overrides["max_dca_ratio"] = req.max_dca_ratio

        if overrides:
            tm.settings.update(overrides)

        tm.enter_triage(system, reason=req.reason)

        return {
            "success": True,
            "message": f"Triage mode activated: {req.reason}",
            "state": tm.state,
            "current_target": tm.current_target,
            "active_targets": len(tm.active_targets),
        }
    except HTTPException:
        logger.warning("triage_router.unknown L130 except", exc_info=True)
        raise
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("triage_router.unknown L132: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/exit")
async def exit_triage(request: Request) -> Dict[str, Any]:
    """
    Manually exit triage mode

    Force termination even if the recovery target is not met.
    Releases BUY block, restores budget, resets state.
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)
        if tm is None:
            raise HTTPException(status_code=503, detail="TriageManager not initialized")

        from app.manager.triage_manager import TriageManager
        if tm.state == TriageManager.STATE_NORMAL:
            return {"success": False, "message": "Triage mode is not active"}

        tm.exit_triage(system, reason="manual exit by operator")

        return {
            "success": True,
            "message": "Triage mode deactivated",
            "state": tm.state,
            "recovered": tm.recovered,
            "skipped": tm.skipped,
        }
    except HTTPException:
        logger.warning("triage_router.unknown L163 except", exc_info=True)
        raise
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("triage_router.unknown L165: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/skip")
async def skip_current_target(request: Request, req: SkipRequest) -> Dict[str, Any]:
    """
    Skip the market currently being recovered

    Move the current focus market to the skipped list and proceed to the next market.
    Transition to TRIAGE_SCAN state to select the next target.
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)
        if tm is None:
            raise HTTPException(status_code=503, detail="TriageManager not initialized")

        from app.manager.triage_manager import TriageManager
        if tm.state == TriageManager.STATE_NORMAL:
            return {"success": False, "message": "Triage mode is not active"}

        if not tm.active_targets:
            return {"success": False, "message": "No active target to skip"}

        # skip the specified target if a market is given, otherwise the first one
        if req.market:
            target = tm._find_target(req.market)
            if not target:
                return {"success": False, "message": f"Market {req.market} not in active targets"}
            tm.skip_target(target, system, reason=req.reason)
            skipped_market = req.market
        else:
            skipped_market = tm.active_targets[0].get("market", "?")
            tm.skip_target(tm.active_targets[0], system, reason=req.reason)

        return {
            "success": True,
            "message": f"Skipped {skipped_market}",
            "skipped_market": skipped_market,
            "state": tm.state,
            "active_targets": len(tm.active_targets),
            "skipped": tm.skipped,
        }
    except HTTPException:
        logger.warning("triage_router.unknown L209 except", exc_info=True)
        raise
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
        logger.warning("triage_router.unknown L211: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/portfolio-loss")
async def get_portfolio_loss(request: Request) -> Dict[str, Any]:
    """
    Current portfolio loss status

    Returns:
        - total_loss_pct: total unrealized loss %
        - markets: per-market loss detail (loss_pct, loss_usdt, val_usdt, qty, avg_buy_price, current_price)
        - loss_coin_count: number of coins in loss
        - triage_trigger_threshold: triage trigger threshold
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)

        # get total loss rate from PRM
        prm = getattr(system, "portfolio_risk_manager", None)
        total_loss_pct = 0.0
        if prm and prm.daily_status:
            total_loss_pct = prm.daily_status.loss_pct

        # compute per-market loss detail
        markets_detail = {}
        try:
            from app.core.hyper_price_store import price_store
            active_markets = system.oma_registry.list_active()
            for m in active_markets:
                ctx = system.coordinator.get_context(m)
                if not ctx or not getattr(ctx, "position", None):
                    continue
                qty = float(ctx.position.get("qty", 0.0) or 0.0)
                avg_buy = float(ctx.position.get("entry", 0.0) or 0.0)
                if avg_buy <= 0 or qty <= 0:
                    continue
                current = price_store.get_price(m) or avg_buy
                val_usdt = qty * current
                upnl = (current - avg_buy) * qty
                upnl_pct = (current - avg_buy) / avg_buy * 100
                if upnl_pct < 0:
                    markets_detail[m] = {
                        "loss_pct": round(upnl_pct, 2),
                        "loss_usdt": round(upnl, 0),
                        "val_usdt": round(val_usdt, 0),
                        "qty": qty,
                        "avg_buy_price": avg_buy,
                        "current_price": current,
                    }
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[triage_router] %s: %s", 'compute per-market loss detail', exc, exc_info=True)

        triage_trigger = tm.settings.get("trigger_pnl_pct", -5.0) if tm else -5.0

        return {
            "total_loss_pct": round(total_loss_pct, 2),
            "triage_trigger_threshold": triage_trigger,
            "loss_coin_count": len(markets_detail),
            "markets": dict(sorted(markets_detail.items(), key=lambda x: x[1]["loss_pct"])),
        }
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
        logger.warning("triage_router.unknown L273: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/settings")
async def patch_settings(request: Request, req: SettingsPatchRequest) -> Dict[str, Any]:
    """
    Modify triage settings at runtime

    Applied immediately even while triage is active.
    Send only the changed values (None items keep their current value).
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)
        if tm is None:
            raise HTTPException(status_code=503, detail="TriageManager not initialized")

        changes = {}
        patch_data = req.model_dump(exclude_none=True)
        for key, val in patch_data.items():
            if key in tm.settings:
                tm.settings[key] = val
                changes[key] = val

        if not changes:
            return {"success": False, "message": "No valid settings to update"}

        # update state file (includes settings snapshot; ENV takes priority on restart)
        tm.save_state()

        return {
            "success": True,
            "message": f"Updated {len(changes)} setting(s)",
            "changes": changes,
            "current_settings": {k: v for k, v in tm.settings.items() if k != "fee_pct"},
        }
    except HTTPException:
        logger.warning("triage_router.unknown L310 except", exc_info=True)
        raise
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("triage_router.unknown L312: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
