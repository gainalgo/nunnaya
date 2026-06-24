"""
Harpoon Strategy API Router

Endpoints:
    GET  /api/strategy/harpoon/status     — get status
    POST /api/strategy/harpoon/config     — change config
    POST /api/strategy/harpoon/enable     — enable
    POST /api/strategy/harpoon/disable    — disable
    GET  /api/strategy/harpoon/history    — recent scalp history
    POST /api/strategy/harpoon/reset      — reset counters
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy/harpoon", tags=["HARPOON"])


def _get_hm(request: Request):
    """Get HarpoonManager from system."""
    system = request.app.state.system
    hm = getattr(system, "harpoon_manager", None)
    if hm is None:
        from app.manager.harpoon_manager import HarpoonManager
        fm = getattr(system, "focus_manager", None)
        # ★ A4 FIX: warn when fm=None (safe via _is_focus_compatible() False in tick)
        if fm is None:
            logger.warning("[HARPOON] FocusManager not initialized — Harpoon will be inactive until FOCUS starts")
        hm = HarpoonManager(focus_manager=fm, system=system)
        system.harpoon_manager = hm
    return hm


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------

@router.get("/status")
def harpoon_status(request: Request):
    """Current Harpoon state, position, PnL, guards."""
    hm = _get_hm(request)
    return {"ok": True, **hm.get_status()}


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

@router.post("/config")
def harpoon_config(
    request: Request,
    enabled: Optional[bool] = Query(None),
    leverage: Optional[int] = Query(None, ge=1, le=100),
    budget_pct: Optional[float] = Query(None, ge=1, le=50),
    budget_usdt: Optional[float] = Query(None, ge=0),
    tp_atr_mult: Optional[float] = Query(None, ge=0.01, le=1.0),
    sl_atr_mult: Optional[float] = Query(None, ge=0.01, le=1.0),
    risk_pct: Optional[float] = Query(None, ge=0.1, le=5.0),
    zone_proximity_atr: Optional[float] = Query(None, ge=0.1, le=2.0),
    max_scalps_per_hour: Optional[int] = Query(None, ge=1, le=20),
    max_daily_scalps: Optional[int] = Query(None, ge=1, le=50),
    max_consecutive_loss: Optional[int] = Query(None, ge=1, le=10),
    max_daily_loss_pct: Optional[float] = Query(None, ge=0.5, le=10.0),
    cooldown_sec: Optional[float] = Query(None, ge=5, le=300),
    entry_tf: Optional[str] = Query(None),
    server_side_tpsl: Optional[bool] = Query(None),
    # ── ADX Filter ──
    min_adx: Optional[int] = Query(None, ge=0, le=50, description="Harpoon ADX threshold (0=inherit from FOCUS)"),
    # ── Dynamic Trailing SL ──
    dynamic_trailing: Optional[bool] = Query(None, description="scalp dynamic trailing ON/OFF"),
    breakeven_trigger_pct: Optional[float] = Query(None, ge=0.01, le=2.0, description="breakeven trigger (%)"),
    trailing_preserve_pct: Optional[float] = Query(None, ge=10, le=90, description="profit preservation rate (%)"),
    # ── Stage 0 (2026-04-22 owner decision B, plan v3 integration) ──
    paper_mode: Optional[bool] = Query(None, description="Stage 0: paper mode (True=block entry + JSONL logging only)"),
    respect_b11_regime_lock: Optional[bool] = Query(None, description="Stage 0-1: integrate FOCUS B11 regime_lock"),
    min_adx_v2: Optional[int] = Query(None, ge=0, le=50, description="Stage 0-2: used as min_adx 0 fallback (default 20)"),
    respect_focus_adx_slope: Optional[bool] = Query(None, description="Stage 0-3: share FOCUS J v2 ADX decline"),
    respect_morning_guard: Optional[bool] = Query(None, description="Stage 0-4: Morning Guard auto standby"),
    respect_coin_loss_cap: Optional[bool] = Query(None, description="Stage 0-5: share FOCUS coin_loss_cap"),
    fast_reject_v2_enabled: Optional[bool] = Query(None, description="Stage 0-6: Fast-Reject v2 (instant cut at 60s peak 0%)"),
    fast_reject_v2_max_sec: Optional[float] = Query(None, ge=10, le=300),
    fast_reject_v2_peak_threshold_pct: Optional[float] = Query(None, ge=0, le=1),
    fast_reject_v2_pnl_pct: Optional[float] = Query(None, ge=-2, le=0),
    post_sl_cooldown_min: Optional[float] = Query(None, ge=0, le=240, description="Stage 0-7: minutes to block same coin+direction after SL"),
    morning_extended_end_hour_kst: Optional[float] = Query(None, ge=7, le=12, description="Stage 0-9: HARPOON Morning Guard extended end hour (KST)"),
    pa_double_confirm_enabled: Optional[bool] = Query(None, description="Stage 0-10: PA 2-signal consensus (default OFF)"),
    pa_double_confirm_window_sec: Optional[float] = Query(None, ge=30, le=300),
    # ── ★ Phase M (2026-04-24) — Multi-Market + Budget split ──
    # [M.A] Scan Universe
    scan_universe: Optional[str] = Query(None, description="Phase M: scan target (all/top20/top50/custom)"),
    scan_blacklist: Optional[str] = Query(None, description="Phase M: excluded coins (comma-separated)"),
    scan_whitelist: Optional[str] = Query(None, description="Phase M: exclusive coins (comma-separated, empty=all)"),
    scan_min_volume_usdt_24h: Optional[float] = Query(None, ge=0, description="Phase M: minimum 24h turnover USDT"),
    # [M.B] Multi-position
    max_concurrent_scalps: Optional[int] = Query(None, ge=1, le=10, description="Phase M: max concurrent scalps"),
    max_same_direction_scalps: Optional[int] = Query(None, ge=1, le=10, description="Phase M: max same direction"),
    cooldown_per_coin_sec: Optional[float] = Query(None, ge=0, le=3600, description="Phase M: per-coin cooldown (sec)"),
    # [M.C] FOCUS coordination
    respect_focus_coin_lock: Optional[bool] = Query(None, description="Phase M: skip coins held by FOCUS"),
    respect_focus_direction_lock: Optional[bool] = Query(None, description="Phase M: forbid opposite direction to FOCUS"),
    coin_exclusive_priority: Optional[str] = Query(None, description="Phase M: first_come/focus/harpoon"),
    focus_entry_freeze_sec: Optional[float] = Query(None, ge=0, le=300, description="Phase M: HARPOON freeze seconds after FOCUS entry"),
    # [M.D] HARPOON own threshold
    min_adx_self: Optional[int] = Query(None, ge=0, le=50, description="Phase M: HARPOON own ADX threshold"),
    min_conviction_self: Optional[float] = Query(None, ge=0, le=100, description="[2026-05-17 100pt ×10] HARPOON own conviction (default 50)"),
    # [M.E] Zone
    zone_source: Optional[str] = Query(None, description="Phase M: zone source (self/focus)"),
    zone_lookback_bars: Optional[int] = Query(None, ge=10, le=200, description="Phase M: zone lookback bars"),
    harpoon_standalone_mode: Optional[bool] = Query(None, description="★ Phase M.F: HARPOON standalone mode (independent of FOCUS)"),
):
    """Update Harpoon config."""
    hm = _get_hm(request)
    patch = {}
    for k, v in {
        "enabled": enabled,
        "leverage": leverage,
        "budget_pct": budget_pct,
        "budget_usdt": budget_usdt,
        "tp_atr_mult": tp_atr_mult,
        "sl_atr_mult": sl_atr_mult,
        "risk_pct": risk_pct,
        "zone_proximity_atr": zone_proximity_atr,
        "max_scalps_per_hour": max_scalps_per_hour,
        "max_daily_scalps": max_daily_scalps,
        "max_consecutive_loss": max_consecutive_loss,
        "max_daily_loss_pct": max_daily_loss_pct,
        "cooldown_sec": cooldown_sec,
        "entry_tf": entry_tf,
        "server_side_tpsl": server_side_tpsl,
        "min_adx": min_adx,
        "dynamic_trailing": dynamic_trailing,
        "breakeven_trigger_pct": breakeven_trigger_pct,
        "trailing_preserve_pct": trailing_preserve_pct,
        # ★ Stage 0 (2026-04-22 owner decision B, plan v3 integration)
        "paper_mode": paper_mode,
        "respect_b11_regime_lock": respect_b11_regime_lock,
        "min_adx_v2": min_adx_v2,
        "respect_focus_adx_slope": respect_focus_adx_slope,
        "respect_morning_guard": respect_morning_guard,
        "respect_coin_loss_cap": respect_coin_loss_cap,
        "fast_reject_v2_enabled": fast_reject_v2_enabled,
        "fast_reject_v2_max_sec": fast_reject_v2_max_sec,
        "fast_reject_v2_peak_threshold_pct": fast_reject_v2_peak_threshold_pct,
        "fast_reject_v2_pnl_pct": fast_reject_v2_pnl_pct,
        "post_sl_cooldown_min": post_sl_cooldown_min,
        "morning_extended_end_hour_kst": morning_extended_end_hour_kst,
        "pa_double_confirm_enabled": pa_double_confirm_enabled,
        "pa_double_confirm_window_sec": pa_double_confirm_window_sec,
        # ★ Phase M (2026-04-24) — Multi-Market + Budget split
        "scan_universe": scan_universe,
        "scan_min_volume_usdt_24h": scan_min_volume_usdt_24h,
        "max_concurrent_scalps": max_concurrent_scalps,
        "max_same_direction_scalps": max_same_direction_scalps,
        "cooldown_per_coin_sec": cooldown_per_coin_sec,
        "respect_focus_coin_lock": respect_focus_coin_lock,
        "respect_focus_direction_lock": respect_focus_direction_lock,
        "coin_exclusive_priority": coin_exclusive_priority,
        "focus_entry_freeze_sec": focus_entry_freeze_sec,
        "min_adx_self": min_adx_self,
        "min_conviction_self": min_conviction_self,
        "zone_source": zone_source,
        "zone_lookback_bars": zone_lookback_bars,
        "harpoon_standalone_mode": harpoon_standalone_mode,
    }.items():
        if v is not None:
            patch[k] = v

    # handle comma-separated list (blacklist / whitelist)
    if scan_blacklist is not None:
        patch["scan_blacklist"] = [x.strip().upper() for x in scan_blacklist.split(",") if x.strip()]
    if scan_whitelist is not None:
        patch["scan_whitelist"] = [x.strip().upper() for x in scan_whitelist.split(",") if x.strip()]

    if not patch:
        return {"ok": True, "config": hm.get_status()["config"], "message": "no changes"}

    result = hm.update_config(patch)
    return {"ok": True, "config": result}


# ------------------------------------------------------------------
# Enable / Disable
# ------------------------------------------------------------------

@router.post("/enable")
def harpoon_enable(request: Request):
    """Enable Harpoon."""
    hm = _get_hm(request)
    hm.update_config({"enabled": True})
    logger.info("[HARPOON-API] Enabled")
    return {"ok": True, "enabled": True}


@router.post("/disable")
def harpoon_disable(request: Request):
    """Disable Harpoon. Closes open scalp if any."""
    hm = _get_hm(request)
    # ★ H10 FIX: prevent bg_loop race via lock
    with hm._lock:
        if hm.current_scalp:
            price = hm._get_current_price(hm.current_scalp.market)
            if not price or price <= 0:
                price = hm.current_scalp.entry_price
            hm._execute_scalp_exit("MANUAL_DISABLE", price)
        hm.config.enabled = False
        hm._save_config()
    logger.info("[HARPOON-API] Disabled")
    return {"ok": True, "enabled": False}


# ------------------------------------------------------------------
# History
# ------------------------------------------------------------------

@router.get("/history")
def harpoon_history(
    request: Request,
    limit: int = Query(20, ge=1, le=50),
):
    """Recent scalp history."""
    hm = _get_hm(request)
    with hm._lock:
        scalps = list(hm.recent_scalps[-limit:])
    # Summary stats
    wins = sum(1 for s in scalps if s.get("pnl_usdt", 0) > 0)
    losses = sum(1 for s in scalps if s.get("pnl_usdt", 0) < 0)
    total_pnl = sum(s.get("pnl_usdt", 0) for s in scalps)
    avg_duration = sum(s.get("duration_sec", 0) for s in scalps) / max(len(scalps), 1)

    return {
        "ok": True,
        "count": len(scalps),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / max(wins + losses, 1) * 100, 1),
        "total_pnl": round(total_pnl, 4),
        "avg_duration_sec": round(avg_duration, 1),
        "scalps": list(reversed(scalps)),  # newest first
    }


# ------------------------------------------------------------------
# Reset
# ------------------------------------------------------------------

@router.post("/reset")
def harpoon_reset(request: Request):
    """Reset daily counters and clear pause."""
    hm = _get_hm(request)
    with hm._lock:
        hm.scalps_today = 0
        hm.scalps_this_hour = 0
        hm.consecutive_losses = 0
        hm.daily_pnl = 0.0
        hm.loss_pause_until = 0.0
        # keep state (no change)
        hm._save_config()
    logger.info("[HARPOON-API] Counters reset")
    return {"ok": True, "message": "counters_reset"}


@router.post("/reload")
def harpoon_reload(request: Request):
    """Reload config + state from disk (harpoon_config.json)."""
    hm = _get_hm(request)
    with hm._lock:
        hm._load_config()
    logger.info("[HARPOON-API] Config reloaded from disk")
    return {"ok": True, "message": "reloaded",
            "daily_pnl": hm.daily_pnl, "total_pnl": hm.total_pnl,
            "recent_scalps": len(hm.recent_scalps)}
