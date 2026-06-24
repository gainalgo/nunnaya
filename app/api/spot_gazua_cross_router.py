# ============================================================
# Spot GAZUA — Cross-Exchange Control API Router
# ------------------------------------------------------------
# Consolidates the block-decision control (near-miss post-hoc verdict + gate
# stats) of the spot multi-exchange (Upbit/Bithumb/Bybit) GAZUA managers
# running in one box into a single response. The spot counterpart of the
# futures strategy_focus_router /peer-cache — but instead of a neighbor
# "server", it gathers the per-exchange managers on the same server.
#
# ★ 100% local — zero new auth surface like neighbor-server polling, CF Access,
#   or PEER_BRIEF_TOKEN. Exchange Tick is sacrosanct (independent of inter-server
#   Tick). Observation only · not a single byte touches entry.
#   Each manager.get_near_miss_enriched() already has a 25s kline cache → this
#   consolidated cache is a second line of defense.
# ============================================================
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy/spot_gazua_cross", tags=["SPOT_GAZUA_CROSS"])

# (system attribute, key, display label, quote currency) — per-exchange GAZUA manager mapping.
_EXCHANGES = [
    ("upbit_gazua_manager", "upbit", "Upbit", "₩"),
    ("bithumb_gazua_manager", "bithumb", "Bithumb", "₩"),
    ("bybit_spot_gazua_manager", "bybit_spot", "Bybit", "USDT"),
    ("binance_spot_gazua_manager", "binance", "Binance", "USDT"),  # 2026-06-23 wired — BinanceSpotGazuaManager. present:false (dimmed) on unconfigured servers, auto-lights when BINANCE_SPOT_FOCUS_ENABLED+keys are set.
]

# Consolidated response cache — so simultaneous polling from 3 tabs does not redundantly trigger manager near-miss enrich (kline).
_BOX: Dict[str, Any] = {"ts": 0.0, "data": None}
_TTL = 15.0


def _exchange_brief(um, key: str, label: str, quote: str) -> Dict[str, Any]:
    """Single-exchange manager → control brief. Safe partial response even if the manager is missing or some parts fail."""
    ex: Dict[str, Any] = {
        "key": key, "label": label, "quote": quote, "present": um is not None,
        "enabled": False, "paper": True, "contrarian_enabled": False,
        "near_miss": [], "gate_stats": None,
    }
    if um is None:
        return ex
    try:
        ex["enabled"] = bool(getattr(um.config, "enabled", False))
        ex["paper"] = bool(getattr(um.config, "paper", True))
        ex["contrarian_enabled"] = bool(getattr(um.config, "contrarian_enabled", False))
    except Exception:  # noqa: BLE001
        pass
    try:
        ex["near_miss"] = um.get_near_miss_enriched()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SPOT_CROSS] %s near-miss skip: %s", key, exc)
    try:
        gl = getattr(um, "_gate_ledger", None)
        if gl is not None and getattr(um.config, "gate_ledger_enabled", False):
            ex["gate_stats"] = gl.snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SPOT_CROSS] %s gate-stats skip: %s", key, exc)
    return ex


@router.get("/control")
def spot_cross_control(request: Request):
    """Consolidated spot multi-exchange block-decision control — near-miss time-series
    post-hoc verdict + per-gate block quality + per-exchange conservativeness comparison
    material. Observation only · entry-independent · 100% local."""
    now = time.time()
    if _BOX.get("data") is not None and (now - float(_BOX.get("ts") or 0.0)) < _TTL:
        return _BOX["data"]

    system = request.app.state.system
    exchanges: List[Dict[str, Any]] = []
    for attr, key, label, quote in _EXCHANGES:
        # getattr only — None if absent (avoids the side effect of the control panel spinning up a new manager).
        um = getattr(system, attr, None)
        exchanges.append(_exchange_brief(um, key, label, quote))

    data = {"ok": True, "ts": now, "exchanges": exchanges}
    _BOX["ts"] = now
    _BOX["data"] = data
    return data
