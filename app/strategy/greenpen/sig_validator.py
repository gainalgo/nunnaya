# ============================================================
# GreenPen SIG (Signal) Validator
# ------------------------------------------------------------
# Implements EP.4 from the Green Pen System:
#   - "Complete SIG" = PA pattern + post-SIG wick intact
#   - Wick destruction = SIG vs SIG conflict → Sideways
#   - Validation timing: Pat1 → check 2nd candle,
#                         Pat2 → check 3rd candle,
#                         Pat3 → check 4th candle
#
# Pure functions — no state, no HyperSystem dependency.
# ============================================================
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .pa_detector import OHLCV, PASignal, PatternType, Direction


# ── Data Types ──────────────────────────────────────────────

@dataclass
class SIGResult:
    valid: bool                # True = wick intact → valid SIG
    sig_wick_price: float      # the post-SIG wick price that must hold
    wick_destroyed: bool       # True = wick was broken → SIG invalid
    cycle_started: bool        # True = valid SIG → cycle count begins
    candles_checked: int       # how many post-PA candles were checked
    reason: str = ""


# ── SIG Validation ──────────────────────────────────────────

def validate_sig(
    pa_signal: PASignal,
    candles_after_pa: List[OHLCV],
) -> SIGResult:
    """Validate whether a PA signal becomes a complete SIG.

    Green Pen rule:
      - After PA pattern, the NEXT candle(s) must form a "post-SIG wick"
        (꼬리) in the signal direction.
      - If the wick is NOT destroyed by subsequent price action,
        the SIG is valid and cycle counting starts.
      - If the wick IS destroyed, it's a SIG vs SIG conflict → Sideways.

    Timing:
      Pat1 (Pin Bar): check from 2nd candle after pattern
      Pat2 (Engulfing): check from 3rd candle
      Pat3 (3-candle): check from 4th candle

    Args:
        pa_signal: the detected PA pattern.
        candles_after_pa: candles AFTER the PA pattern completed.
                          e.g., if PA ends at candle[-1], pass candles from
                          the next period onward.
    """
    # Determine how many confirmation candles needed
    wait_candles = _wait_candles_for_pattern(pa_signal.pattern)

    if len(candles_after_pa) < wait_candles:
        return SIGResult(
            valid=False,
            sig_wick_price=0.0,
            wick_destroyed=False,
            cycle_started=False,
            candles_checked=len(candles_after_pa),
            reason=f"Need {wait_candles} candles, have {len(candles_after_pa)}",
        )

    # The confirmation candle is at index (wait_candles - 1)
    confirm_candle = candles_after_pa[wait_candles - 1]

    # Determine the wick price that must hold
    if pa_signal.direction == Direction.LONG:
        # For LONG SIG: the low of confirmation candle is the "SIG wick"
        sig_wick_price = confirm_candle.low

        # Check if any subsequent candle broke below this wick
        destroyed = False
        for c in candles_after_pa[wait_candles:]:
            if c.low < sig_wick_price:
                destroyed = True
                break

        return SIGResult(
            valid=not destroyed,
            sig_wick_price=sig_wick_price,
            wick_destroyed=destroyed,
            cycle_started=not destroyed,
            candles_checked=len(candles_after_pa),
            reason="Wick intact → cycle start" if not destroyed else "Wick destroyed → SW",
        )

    else:  # SHORT
        # For SHORT SIG: the high of confirmation candle is the "SIG wick"
        sig_wick_price = confirm_candle.high

        destroyed = False
        for c in candles_after_pa[wait_candles:]:
            if c.high > sig_wick_price:
                destroyed = True
                break

        return SIGResult(
            valid=not destroyed,
            sig_wick_price=sig_wick_price,
            wick_destroyed=destroyed,
            cycle_started=not destroyed,
            candles_checked=len(candles_after_pa),
            reason="Wick intact → cycle start" if not destroyed else "Wick destroyed → SW",
        )


def _wait_candles_for_pattern(pattern: PatternType) -> int:
    """How many candles after PA before checking the SIG wick.

    Green Pen rule:
      Pat1 → 2nd candle (1 candle wait)
      Pat2 → 3rd candle (2 candles wait)  -- actually check from next candle
      Pat3 → 4th candle (3 candles wait)  -- but we already consumed 3 in pattern
    Since candles_after_pa starts AFTER the pattern, we just need 1 candle.
    """
    return {
        PatternType.PIN_BAR: 1,
        PatternType.ENGULFING: 1,
        PatternType.STAR_V1: 1,
        PatternType.STAR_V2: 1,
        PatternType.SQUEEZE_BREAK: 1,
    }.get(pattern, 1)


# ── Convenience ─────────────────────────────────────────────

def is_sig_still_valid(
    pa_signal: PASignal,
    sig_wick_price: float,
    latest_candle: OHLCV,
) -> bool:
    """Quick check: has the SIG wick been destroyed by the latest candle?

    Useful for ongoing monitoring without re-running full validation.
    """
    if pa_signal.direction == Direction.LONG:
        return latest_candle.low >= sig_wick_price
    else:
        return latest_candle.high <= sig_wick_price
