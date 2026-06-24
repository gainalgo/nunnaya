"""Cross-Strategy Guard — check for FOCUS ↔ Nunnaya cross positions.

A stateless, pure-query module.
Reads only existing data (focus_manager.positions, coordinator.contexts).
Same pattern as Harpoon: read-only cross-reference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class StrategyOwnership:
    market: str
    owner: str          # "FOCUS" | "NUNNAYA"
    qty: float
    direction: str      # "LONG" | "SHORT" | ""
    deployed_usdt: float


def _get_focus_positions(system) -> Dict[str, StrategyOwnership]:
    """Return a map of active positions from the FOCUS manager."""
    result: Dict[str, StrategyOwnership] = {}
    try:
        fm = getattr(system, "focus_manager", None)
        if not fm or not getattr(fm, "enabled", False):
            return result
        for p in list(getattr(fm, "positions", None) or []):
            mkt = (getattr(p, "market", "") or "").upper()
            if not mkt:
                continue
            qty = float(getattr(p, "qty", 0) or 0)
            if qty <= 0:
                continue
            direction = getattr(p, "direction", "") or ""
            entry_price = float(getattr(p, "entry_price", 0) or 0)
            deployed = qty * entry_price if entry_price > 0 else 0.0
            result[mkt] = StrategyOwnership(
                market=mkt, owner="FOCUS", qty=qty,
                direction=direction, deployed_usdt=deployed,
            )
    except Exception as exc:
        logger.debug("[CrossGuard] _get_focus_positions: %s", exc)
    return result


def _get_nunnaya_positions(system) -> Dict[str, StrategyOwnership]:
    """Return a map of active positions from the Nunnaya engine (coordinator.contexts)."""
    result: Dict[str, StrategyOwnership] = {}
    try:
        coordinator = getattr(system, "coordinator", None)
        if not coordinator:
            return result
        contexts = getattr(coordinator, "contexts", None) or {}
        for mkt, ctx in list(contexts.items()):
            pos = getattr(ctx, "position", None) or {}
            qty = float(pos.get("qty", 0) or 0)
            if qty <= 0:
                continue
            mkt_upper = mkt.upper()
            entry_price = float(pos.get("entry", 0) or 0)
            deployed = qty * entry_price if entry_price > 0 else 0.0
            result[mkt_upper] = StrategyOwnership(
                market=mkt_upper, owner="NUNNAYA", qty=qty,
                direction="",  # direction is tracked separately in the Nunnaya ctx
                deployed_usdt=deployed,
            )
    except Exception as exc:
        logger.debug("[CrossGuard] _get_nunnaya_positions: %s", exc)
    return result


def is_market_owned_by_other(
    system, market: str, caller: str,
) -> Optional[StrategyOwnership]:
    """Return OwnershipInfo if another strategy holds this market, else None.

    caller="FOCUS"  → check the Nunnaya side
    caller="NUNNAYA" → check the FOCUS side
    """
    mkt = market.upper()
    try:
        if caller == "FOCUS":
            others = _get_nunnaya_positions(system)
        else:
            others = _get_focus_positions(system)
        return others.get(mkt)
    except Exception as exc:
        logger.debug("[CrossGuard] is_market_owned_by_other: %s", exc)
        return None


def get_total_deployed_usdt(system) -> Tuple[float, float]:
    """Return (focus_deployed, nunnaya_deployed) in USDT."""
    focus_total = 0.0
    nunnaya_total = 0.0
    try:
        for info in _get_focus_positions(system).values():
            focus_total += info.deployed_usdt
    except Exception:
        pass
    try:
        for info in _get_nunnaya_positions(system).values():
            nunnaya_total += info.deployed_usdt
    except Exception:
        pass
    return focus_total, nunnaya_total
