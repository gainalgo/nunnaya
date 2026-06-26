# ============================================================
# GreenPen — Price Action Trading Engine
# ------------------------------------------------------------
# Shared library implementing the "Green Pen System" concepts
# from XAUUSD 88 trading manual, adapted for cryptocurrency.
#
# Modules:
#   pa_detector      — PA 5-pattern detection (Pin Bar, Engulfing, Star, Squeeze)
#   market_structure — Swing points, trend (HH/HL/LH/LL), BOS detection
#   zone_engine      — Support/Resistance zones via Zok·Sai·Koo method
#   cycle_tp         — ATR-based cycle TP1/TP2/SL + position sizing
#   sig_validator    — Post-PA SIG validation (wick integrity check)
#
# Usage:
#   from app.strategy.greenpen import full_analysis
#   result = full_analysis(candles_h4, price=current_price)
# ============================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .pa_detector import (
    OHLCV,
    PASignal,
    PatternType,
    Direction,
    detect_pa_patterns,
    candles_from_prices,
)
from .market_structure import (
    MarketStructure,
    Trend,
    SwingPoint,
    SwingType,
    BreakOfStructure,
    analyze_structure,
    is_above_support,
    is_below_resistance,
)
from .zone_engine import (
    Zone,
    ZoneType,
    DailyRange,
    compute_zones,
    compute_daily_range,
    is_price_in_zone,
    nearest_zone,
    is_price_in_daily_range,
)
from .cycle_tp import (
    CycleTargets,
    PartialExit,
    PositionSizing,
    compute_cycle_targets,
    should_partial_exit,
    should_full_exit,
    compute_position_size,
)
from .sig_validator import (
    SIGResult,
    validate_sig,
    is_sig_still_valid,
)


# ── Unified Analysis ────────────────────────────────────────

@dataclass
class GreenPenAnalysis:
    structure: MarketStructure
    zones: List[Zone]
    pa_signals: List[PASignal]
    daily_range: Optional[DailyRange]
    atr: float
    price: float


def full_analysis(
    candles: List[OHLCV],
    *,
    price: Optional[float] = None,
    atr: Optional[float] = None,
    candles_d1: Optional[List[OHLCV]] = None,
    max_zones: int = 4,
) -> GreenPenAnalysis:
    """Run complete GreenPen analysis on a set of candles.

    This is the main convenience function for strategies that want
    a one-shot analysis without importing individual modules.

    Args:
        candles: H4 (or primary TF) OHLCV list, oldest first.
        price: current price (defaults to last candle close).
        atr: pre-computed ATR. If None, computed from candles.
        candles_d1: daily candles for daily range calculation.
        max_zones: max support/resistance zones to return.

    Returns:
        GreenPenAnalysis with structure, zones, PA signals, daily range.
    """
    if not candles:
        return GreenPenAnalysis(
            structure=MarketStructure(Trend.SIDEWAYS, [], None, None, 0.0),
            zones=[],
            pa_signals=[],
            daily_range=None,
            atr=0.0,
            price=0.0,
        )

    current_price = price if price is not None else candles[-1].close

    # Compute ATR if not provided
    if atr is None or atr <= 0:
        atr = _simple_atr(candles, period=14)

    # Market Structure
    structure = analyze_structure(candles)

    # Zones
    zones = compute_zones(candles, atr, max_zones=max_zones)

    # Zone-aware PA detection
    zone_prices = None
    supports = [z for z in zones if z.type == ZoneType.SUPPORT]
    resistances = [z for z in zones if z.type == ZoneType.RESISTANCE]
    if supports and resistances:
        zone_prices = (
            max(z.price_high for z in supports),
            min(z.price_low for z in resistances),
        )
    pa_signals = detect_pa_patterns(candles, zone_prices=zone_prices)

    # Daily Range
    daily_range = None
    if candles_d1:
        d1_atr = _simple_atr(candles_d1, period=14)
        daily_range = compute_daily_range(candles_d1, d1_atr)

    return GreenPenAnalysis(
        structure=structure,
        zones=zones,
        pa_signals=pa_signals,
        daily_range=daily_range,
        atr=atr,
        price=current_price,
    )


def _simple_atr(candles: List[OHLCV], period: int = 14) -> float:
    """Simple ATR calculation from OHLCV candles."""
    if len(candles) < 2:
        return 0.0

    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
        trs.append(tr)

    if not trs:
        return 0.0

    # Use last `period` true ranges
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent)


# ── Strategy Entry Guard (shared by all plugins) ────────────

import logging as _logging

_gp_logger = _logging.getLogger("greenpen")


def check_entry_guard(
    strategy_name: str,
    price_history: list,
    price: float = 0.0,
    *,
    candle_window: int = 5,
    min_candles: int = 4,
) -> dict:
    """Shared GreenPen entry guard for all strategy plugins.

    Returns:
        {
            "allow": True/False,
            "reason": "pa_found" / "no_pa" / "candle_insufficient" / "error",
            "pa_pattern": "PIN_BAR" or None,
            "pa_direction": "LONG" or None,
            "pa_confidence": 0.85 or 0,
            "trend": "UPTREND" / "DOWNTREND" / "SIDEWAYS",
            "trend_confidence": 0.8,
            "zones_count": 3,
        }
    """
    tag = f"[GP:{strategy_name}]"
    result = {
        "allow": True, "reason": "unchecked",
        "pa_pattern": None, "pa_direction": None, "pa_confidence": 0,
        "trend": "UNKNOWN", "trend_confidence": 0, "zones_count": 0,
    }

    try:
        # Convert price history to candles
        raw = [float(x) for x in price_history[-60:] if x]
        if len(raw) < candle_window * min_candles:
            result["allow"] = False
            result["reason"] = f"candle_insufficient({len(raw)}/{candle_window * min_candles})"
            _gp_logger.debug("%s SKIP: %s", tag, result["reason"])
            return result

        candles = candles_from_prices(raw, window=candle_window)
        if len(candles) < min_candles:
            result["allow"] = False
            result["reason"] = f"candle_too_few({len(candles)}/{min_candles})"
            _gp_logger.debug("%s SKIP: %s", tag, result["reason"])
            return result

        # Quick analysis
        structure = analyze_structure(candles)
        result["trend"] = structure.trend.value
        result["trend_confidence"] = structure.confidence

        atr = _simple_atr(candles)
        zones = compute_zones(candles, atr, max_zones=4) if atr > 0 else []
        result["zones_count"] = len(zones)

        # Zone-aware PA detection
        zone_prices = None
        supports = [z for z in zones if z.type == ZoneType.SUPPORT]
        resistances = [z for z in zones if z.type == ZoneType.RESISTANCE]
        if supports and resistances:
            zone_prices = (
                max(z.price_high for z in supports),
                min(z.price_low for z in resistances),
            )

        pa_signals = detect_pa_patterns(candles, zone_prices=zone_prices)

        if pa_signals:
            best = pa_signals[0]
            result["allow"] = True
            result["reason"] = "pa_found"
            result["pa_pattern"] = best.pattern.value
            result["pa_direction"] = best.direction.value
            result["pa_confidence"] = best.confidence
            _gp_logger.info(
                "%s PASS: %s %s (conf=%.0f%%) trend=%s zones=%d",
                tag, best.pattern.value, best.direction.value,
                best.confidence * 100, structure.trend.value, len(zones),
            )
        else:
            result["allow"] = False
            result["reason"] = "no_pa_pattern"
            _gp_logger.info(
                "%s BLOCK: no PA pattern | trend=%s(%.0f%%) zones=%d price=%.2f",
                tag, structure.trend.value, structure.confidence * 100,
                len(zones), price,
            )

    except Exception as exc:
        result["allow"] = True  # fail-open: GreenPen error doesn't block trading
        result["reason"] = f"error:{exc}"
        _gp_logger.warning("%s ERROR (fail-open): %s", tag, exc)

    return result
