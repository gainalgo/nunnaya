"""
Harpoon (작살) — Scalp TP/SL Calculator

FOCUS의 H4 ATR을 기반으로 초단타 TP/SL을 계산한다.
일반 cycle_tp.py 대비 훨씬 좁은 밴드 (ATR × 0.1~0.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ScalpTargets:
    """Scalp TP/SL targets for Harpoon."""
    tp: float = 0.0          # Take Profit price
    sl: float = 0.0          # Stop Loss price
    tp_dist: float = 0.0     # TP distance from entry ($)
    sl_dist: float = 0.0     # SL distance from entry ($)
    rr_ratio: float = 0.0    # Risk/Reward ratio
    atr_used: float = 0.0    # ATR value used


@dataclass
class ScalpSizing:
    """Position sizing result for a single scalp."""
    qty: float = 0.0
    risk_usdt: float = 0.0
    notional: float = 0.0
    leverage_qty: float = 0.0


def compute_scalp_targets(
    entry_price: float,
    direction: str,
    atr: float,
    *,
    tp_atr_mult: float = 0.15,
    sl_atr_mult: float = 0.10,
    min_tp_pct: float = 0.015,   # TP 최소 0.015% (수수료 커버)
    min_sl_pct: float = 0.010,   # SL 최소 0.01%
) -> ScalpTargets:
    """
    Compute scalp TP/SL from ATR.

    Args:
        entry_price: entry price
        direction: "LONG" or "SHORT"
        atr: ATR value (from FOCUS H4)
        tp_atr_mult: TP = ATR * this (default 0.15)
        sl_atr_mult: SL = ATR * this (default 0.10)
        min_tp_pct: minimum TP as % of price
        min_sl_pct: minimum SL as % of price

    Returns:
        ScalpTargets with TP/SL prices and distances
    """
    if entry_price <= 0 or atr <= 0:
        return ScalpTargets()

    # Raw distances
    tp_dist = atr * tp_atr_mult
    sl_dist = atr * sl_atr_mult

    # Enforce minimums (수수료 + 슬리피지 보호)
    min_tp_abs = entry_price * (min_tp_pct / 100.0)
    min_sl_abs = entry_price * (min_sl_pct / 100.0)
    tp_dist = max(tp_dist, min_tp_abs)
    sl_dist = max(sl_dist, min_sl_abs)

    # Fee buffer: Bybit taker 0.055% × 2 (round trip) = 0.11%
    fee_buffer = entry_price * 0.0011
    tp_dist += fee_buffer  # TP must cover fees

    # Compute prices
    if direction.upper() == "LONG":
        tp = entry_price + tp_dist
        sl = entry_price - sl_dist
    else:  # SHORT
        tp = entry_price - tp_dist
        sl = entry_price + sl_dist

    rr = tp_dist / sl_dist if sl_dist > 0 else 0.0

    return ScalpTargets(
        tp=round(tp, 4),
        sl=round(sl, 4),
        tp_dist=round(tp_dist, 4),
        sl_dist=round(sl_dist, 4),
        rr_ratio=round(rr, 2),
        atr_used=atr,
    )


def compute_scalp_size(
    budget_usdt: float,
    risk_pct: float,
    sl_distance: float,
    current_price: float,
    *,
    leverage: float = 20.0,
) -> ScalpSizing:
    """
    Position sizing for a single scalp.

    Args:
        budget_usdt: allocated budget (e.g. $62.5)
        risk_pct: max risk per scalp as % of budget (e.g. 0.5)
        sl_distance: SL distance in price units
        current_price: current market price
        leverage: leverage multiplier

    Returns:
        ScalpSizing with qty and risk metrics
    """
    if budget_usdt <= 0 or current_price <= 0 or sl_distance <= 0:
        return ScalpSizing()

    # Max risk in USDT
    risk_usdt = budget_usdt * (risk_pct / 100.0)

    # Position size: risk / sl_distance
    qty = risk_usdt / sl_distance

    # Notional check
    notional = qty * current_price
    max_notional = budget_usdt * leverage
    if notional > max_notional:
        qty = max_notional / current_price
        notional = max_notional

    leverage_qty = qty  # For linear perpetual, qty = contracts

    return ScalpSizing(
        qty=round(qty, 6),
        risk_usdt=round(risk_usdt, 4),
        notional=round(notional, 4),
        leverage_qty=round(leverage_qty, 6),
    )


def should_scalp_exit(
    current_price: float,
    entry_price: float,
    direction: str,
    targets: ScalpTargets,
) -> Optional[str]:
    """
    Check if scalp should exit (TP or SL hit).
    Server-side TP/SL is primary safety net; this is backup check.

    Returns:
        "TP" / "SL" / None
    """
    if not targets or entry_price <= 0 or current_price <= 0:
        return None

    if direction.upper() == "LONG":
        if current_price >= targets.tp:
            return "TP"
        if current_price <= targets.sl:
            return "SL"
    else:  # SHORT
        if current_price <= targets.tp:
            return "TP"
        if current_price >= targets.sl:
            return "SL"

    return None
