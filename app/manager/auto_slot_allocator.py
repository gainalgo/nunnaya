# ============================================================
# File: app/manager/auto_slot_allocator.py
# Capital-based Auto Slot Allocation
# ------------------------------------------------------------
# Pure function: no state, no HyperSystem dependency.
# Given total equity (USDT), compute optimal slot counts
# per strategy.
# ============================================================
from __future__ import annotations

from typing import Dict, List, Tuple

# Strategy weights (higher = more slots proportionally)
STRATEGY_WEIGHTS: Dict[str, float] = {
    "pingpong": 1.0,
    "autoloop": 1.1,
    "ladder": 1.4,
    "lightning": 0.7,
    "gazua": 1.2,
    "contrarian": 0.8,
    "sniper": 0.9,
    "whale": 0.8,
}

# Per-strategy slot caps
SLOT_CAPS: Dict[str, int] = {
    "sniper": 10,
    "contrarian": 10,
    "whale": 10,
}
DEFAULT_CAP = 20

# Capital tiers: (threshold_usdt, total_slots)
CAPITAL_TIERS: List[Tuple[float, int]] = [
    (10_000, 20),
    (2_000, 12),
    (500, 8),
    (100, 4),
    (0, 2),
]


def _total_slots_for_equity(equity: float) -> int:
    for threshold, slots in CAPITAL_TIERS:
        if equity >= threshold:
            return slots
    return 2


def compute_auto_slots(equity_usdt: float) -> Dict[str, int]:
    """Compute per-strategy slot counts based on total equity.

    Returns dict with keys like ``pingpong_n``, ``autoloop_n``, etc.
    """
    if equity_usdt < 1.0:
        return {f"{s}_n": 0 for s in STRATEGY_WEIGHTS}

    total = _total_slots_for_equity(equity_usdt)
    strategies = list(STRATEGY_WEIGHTS.keys())
    weights = [STRATEGY_WEIGHTS[s] for s in strategies]
    sum_w = sum(weights)

    # Weighted allocation with largest-remainder rounding
    raw = [(s, total * w / sum_w) for s, w in zip(strategies, weights)]
    floored = {s: int(v) for s, v in raw}
    assigned = sum(floored.values())
    remainder = total - assigned

    # Distribute remainder by largest fractional part
    fracs = sorted(
        [(s, v - int(v)) for s, v in raw],
        key=lambda x: -x[1],
    )
    for s, _ in fracs:
        if remainder <= 0:
            break
        floored[s] += 1
        remainder -= 1

    # Minimum guarantees: PINGPONG and AUTOLOOP each get >= 1
    protected = {"pingpong", "autoloop"}
    if total >= 2:
        for key in ("pingpong", "autoloop"):
            if floored.get(key, 0) < 1:
                # Steal from the strategy with the most slots (excluding protected)
                donor = max(
                    (s for s in strategies if s not in protected and floored[s] > 0),
                    key=lambda s: floored[s],
                    default=None,
                )
                if donor:
                    floored[donor] -= 1
                    floored[key] = 1

    # Apply caps
    for s in strategies:
        cap = SLOT_CAPS.get(s, DEFAULT_CAP)
        if floored[s] > cap:
            floored[s] = cap

    return {f"{s}_n": floored[s] for s in strategies}
