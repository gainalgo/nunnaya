# ============================================================
# GreenPen PA (Price Action) Pattern Detector
# ------------------------------------------------------------
# Implements the 5 PA patterns from the "Green Pen System"
# (XAUUSD 88 trading manual).
#
# Pure functions — no state, no HyperSystem dependency.
# Input: OHLCV candle list.  Output: detected PA signals.
# ============================================================
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


# ── Data Types ──────────────────────────────────────────────

class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class PatternType(str, Enum):
    PIN_BAR = "PIN_BAR"           # Pat1: single candle, long wick
    ENGULFING = "ENGULFING"       # Pat2: 2-candle engulfing
    STAR_V1 = "STAR_V1"          # Pat3-1: Morning/Evening Star
    STAR_V2 = "STAR_V2"          # Pat3-2: same→opposite→breakout
    SQUEEZE_BREAK = "SQUEEZE_BREAK"  # Pat3-3: tight range → breakout
    # ★ [2026-04-24] Break of Structure — newly added per owner's request
    #   Previously only the keyword existed with no detection code = dead signal. Revived.
    BOS_BULLISH = "BOS_BULLISH"  # break above recent N-bar high + close above → LONG
    BOS_BEARISH = "BOS_BEARISH"  # break below recent N-bar low  + close below → SHORT


@dataclass
class OHLCV:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ts: float = 0.0

    @property
    def body_top(self) -> float:
        return max(self.open, self.close)

    @property
    def body_bottom(self) -> float:
        return min(self.open, self.close)

    @property
    def body_len(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - self.body_top

    @property
    def lower_wick(self) -> float:
        return self.body_bottom - self.low

    @property
    def total_range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


@dataclass
class PASignal:
    pattern: PatternType
    direction: Direction
    confidence: float          # 0.0 ~ 1.0
    candle_idx: int            # index in input list (-1 = last)
    trigger_price: float = 0.0  # recommended entry price
    invalidation_price: float = 0.0  # price that invalidates the signal
    meta: dict = field(default_factory=dict)


# ── Detection Functions ─────────────────────────────────────

def detect_pa_patterns(
    candles: List[OHLCV],
    zone_prices: Optional[Tuple[float, float]] = None,
    *,
    min_body_ratio: float = 0.3,
    pin_wick_mult: float = 2.0,
    engulf_pct: float = 0.8,
) -> List[PASignal]:
    """Detect all GreenPen PA 5-patterns in candle array.

    Args:
        candles: OHLCV list (oldest first, newest last). Minimum 4 candles.
        zone_prices: (support, resistance) for Pin Bar location validation.
                     If None, location validation is skipped.
        min_body_ratio: minimum body/range ratio to consider a candle "real"
        pin_wick_mult: wick must be >= body × this multiplier for Pin Bar
        engulf_pct: how much of prior body must be engulfed (0.8 = 80%)

    Returns:
        List of PASignal, newest patterns first, sorted by confidence.
    """
    if len(candles) < 4:
        return []

    signals: List[PASignal] = []

    # Pat1 — Pin Bar (single candle)
    sig = _detect_pin_bar(candles, zone_prices, pin_wick_mult)
    if sig:
        signals.append(sig)

    # Pat2 — Engulfing (2-candle)
    sig = _detect_engulfing(candles, engulf_pct)
    if sig:
        signals.append(sig)

    # Pat3-1 — Morning/Evening Star (3-candle)
    sig = _detect_star_v1(candles, min_body_ratio)
    if sig:
        signals.append(sig)

    # Pat3-2 — Same→Opposite→Breakout (3-candle)
    sig = _detect_star_v2(candles)
    if sig:
        signals.append(sig)

    # Pat3-3 — Squeeze→Breakout (3-candle)
    sig = _detect_squeeze_break(candles)
    if sig:
        signals.append(sig)

    # ★ [2026-04-24] BOS — Break of Structure (lookback breakout)
    #   Owner's request: fix the gap where only the BOS_BULLISH/BEARISH keywords
    #   existed with no detection. "A signal HARPOON should catch" —
    #   support/resistance breakouts are HARPOON's real prey.
    sig = _detect_bos(candles, lookback=10)
    if sig:
        signals.append(sig)

    # Sort by confidence descending
    signals.sort(key=lambda s: -s.confidence)
    return signals


# ── Pat1: Pin Bar ───────────────────────────────────────────

def _detect_pin_bar(
    candles: List[OHLCV],
    zone_prices: Optional[Tuple[float, float]],
    wick_mult: float,
) -> Optional[PASignal]:
    """Pin Bar: long wick > body × wick_mult, at support/resistance zone."""
    c = candles[-1]
    if c.total_range <= 0:
        return None

    body = c.body_len
    if body <= 0:
        body = c.total_range * 0.01  # doji: treat as tiny body

    # Bullish Pin Bar (long lower wick at support)
    if c.lower_wick >= body * wick_mult and c.lower_wick > c.upper_wick * 1.5:
        direction = Direction.LONG
        trigger = c.high
        invalidation = c.low

        # Location validation: bullish pin at resistance = FAKE
        if zone_prices:
            _, resistance = zone_prices
            if resistance > 0 and c.close > resistance:
                return None  # fake signal — pin at wrong location

        conf = min(1.0, c.lower_wick / (body * wick_mult) * 0.7)
        return PASignal(
            pattern=PatternType.PIN_BAR,
            direction=direction,
            confidence=conf,
            candle_idx=len(candles) - 1,
            trigger_price=trigger,
            invalidation_price=invalidation,
            meta={"wick_body_ratio": c.lower_wick / max(body, 1e-12)},
        )

    # Bearish Pin Bar (long upper wick at resistance)
    if c.upper_wick >= body * wick_mult and c.upper_wick > c.lower_wick * 1.5:
        direction = Direction.SHORT
        trigger = c.low
        invalidation = c.high

        # Location validation: bearish pin at support = FAKE
        if zone_prices:
            support, _ = zone_prices
            if support > 0 and c.close < support:
                return None

        conf = min(1.0, c.upper_wick / (body * wick_mult) * 0.7)
        return PASignal(
            pattern=PatternType.PIN_BAR,
            direction=direction,
            confidence=conf,
            candle_idx=len(candles) - 1,
            trigger_price=trigger,
            invalidation_price=invalidation,
            meta={"wick_body_ratio": c.upper_wick / max(body, 1e-12)},
        )

    return None


# ── Pat2: Engulfing ─────────────────────────────────────────

def _detect_engulfing(
    candles: List[OHLCV],
    engulf_pct: float,
) -> Optional[PASignal]:
    """Engulfing: current candle's body covers >= engulf_pct of prior body."""
    if len(candles) < 2:
        return None

    prev, curr = candles[-2], candles[-1]
    if prev.body_len <= 0 or curr.body_len <= 0:
        return None

    # Bullish Engulfing: prev bearish + curr bullish, curr body covers prev body
    if prev.is_bearish and curr.is_bullish:
        coverage = (curr.body_top - curr.body_bottom) / max(prev.body_len, 1e-12)
        if coverage >= engulf_pct and curr.body_bottom <= prev.body_bottom:
            conf = min(1.0, coverage * 0.6)
            return PASignal(
                pattern=PatternType.ENGULFING,
                direction=Direction.LONG,
                confidence=conf,
                candle_idx=len(candles) - 1,
                trigger_price=curr.high,
                invalidation_price=min(prev.low, curr.low),
                meta={"coverage": coverage},
            )

    # Bearish Engulfing: prev bullish + curr bearish
    if prev.is_bullish and curr.is_bearish:
        coverage = (curr.body_top - curr.body_bottom) / max(prev.body_len, 1e-12)
        if coverage >= engulf_pct and curr.body_top >= prev.body_top:
            conf = min(1.0, coverage * 0.6)
            return PASignal(
                pattern=PatternType.ENGULFING,
                direction=Direction.SHORT,
                confidence=conf,
                candle_idx=len(candles) - 1,
                trigger_price=curr.low,
                invalidation_price=max(prev.high, curr.high),
                meta={"coverage": coverage},
            )

    return None


# ── Pat3-1: Morning/Evening Star ────────────────────────────

def _detect_star_v1(
    candles: List[OHLCV],
    min_body_ratio: float,
) -> Optional[PASignal]:
    """3-candle star: big body → small body (doji) → reversal body."""
    if len(candles) < 3:
        return None

    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    # All must have non-zero range
    if c1.total_range <= 0 or c3.total_range <= 0:
        return None

    c1_body_ratio = c1.body_len / max(c1.total_range, 1e-12)
    c2_body_ratio = c2.body_len / max(c2.total_range, 1e-12) if c2.total_range > 0 else 0
    c3_body_ratio = c3.body_len / max(c3.total_range, 1e-12)

    # c1: strong body, c2: small body (doji-ish), c3: strong reversal
    if c1_body_ratio < min_body_ratio or c3_body_ratio < min_body_ratio:
        return None
    if c2_body_ratio > 0.4:  # c2 must be small
        return None

    # Morning Star: c1 bearish, c3 bullish, c3 closes above c1 midpoint
    c1_mid = (c1.open + c1.close) / 2
    if c1.is_bearish and c3.is_bullish and c3.close > c1_mid:
        conf = min(1.0, (c3.body_len / max(c1.body_len, 1e-12)) * 0.65)
        return PASignal(
            pattern=PatternType.STAR_V1,
            direction=Direction.LONG,
            confidence=conf,
            candle_idx=len(candles) - 1,
            trigger_price=c3.high,
            invalidation_price=min(c1.low, c2.low, c3.low),
            meta={"type": "morning_star"},
        )

    # Evening Star: c1 bullish, c3 bearish, c3 closes below c1 midpoint
    if c1.is_bullish and c3.is_bearish and c3.close < c1_mid:
        conf = min(1.0, (c3.body_len / max(c1.body_len, 1e-12)) * 0.65)
        return PASignal(
            pattern=PatternType.STAR_V1,
            direction=Direction.SHORT,
            confidence=conf,
            candle_idx=len(candles) - 1,
            trigger_price=c3.low,
            invalidation_price=max(c1.high, c2.high, c3.high),
            meta={"type": "evening_star"},
        )

    return None


# ── Pat3-2: Same→Opposite→Breakout ──────────────────────────

def _detect_star_v2(candles: List[OHLCV]) -> Optional[PASignal]:
    """3-candle: same color → opposite color → breakout beyond c1."""
    if len(candles) < 3:
        return None

    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    # Bullish: c1 bearish, c2 bearish (same), c3 bullish (opposite) + breaks above c1 high
    if c1.is_bearish and c2.is_bearish and c3.is_bullish and c3.close > c1.high:
        conf = min(1.0, c3.body_len / max(c1.body_len + c2.body_len, 1e-12) * 0.7)
        return PASignal(
            pattern=PatternType.STAR_V2,
            direction=Direction.LONG,
            confidence=conf,
            candle_idx=len(candles) - 1,
            trigger_price=c3.high,
            invalidation_price=min(c1.low, c2.low, c3.low),
            meta={"type": "bullish_v2"},
        )

    # Bearish: c1 bullish, c2 bullish (same), c3 bearish (opposite) + breaks below c1 low
    if c1.is_bullish and c2.is_bullish and c3.is_bearish and c3.close < c1.low:
        conf = min(1.0, c3.body_len / max(c1.body_len + c2.body_len, 1e-12) * 0.7)
        return PASignal(
            pattern=PatternType.STAR_V2,
            direction=Direction.SHORT,
            confidence=conf,
            candle_idx=len(candles) - 1,
            trigger_price=c3.low,
            invalidation_price=max(c1.high, c2.high, c3.high),
            meta={"type": "bearish_v2"},
        )

    return None


# ── Pat3-3: Squeeze → Breakout ──────────────────────────────

def _detect_squeeze_break(candles: List[OHLCV]) -> Optional[PASignal]:
    """3-candle: tight range → tight range → explosive breakout."""
    if len(candles) < 4:
        return None

    # Use candles[-4] as reference for "normal" range
    ref = candles[-4]
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    ref_range = ref.total_range
    if ref_range <= 0:
        return None

    # c1, c2 must be tight (< 50% of reference range)
    if c1.total_range > ref_range * 0.5 or c2.total_range > ref_range * 0.5:
        return None

    # c3 must be explosive (> 120% of reference range)
    if c3.total_range < ref_range * 1.2:
        return None

    # Bullish breakout
    squeeze_high = max(c1.high, c2.high)
    squeeze_low = min(c1.low, c2.low)

    if c3.is_bullish and c3.close > squeeze_high:
        conf = min(1.0, c3.total_range / max(ref_range, 1e-12) * 0.5)
        return PASignal(
            pattern=PatternType.SQUEEZE_BREAK,
            direction=Direction.LONG,
            confidence=conf,
            candle_idx=len(candles) - 1,
            trigger_price=c3.high,
            invalidation_price=squeeze_low,
            meta={"squeeze_range": squeeze_high - squeeze_low, "break_range": c3.total_range},
        )

    # Bearish breakout
    if c3.is_bearish and c3.close < squeeze_low:
        conf = min(1.0, c3.total_range / max(ref_range, 1e-12) * 0.5)
        return PASignal(
            pattern=PatternType.SQUEEZE_BREAK,
            direction=Direction.SHORT,
            confidence=conf,
            candle_idx=len(candles) - 1,
            trigger_price=c3.low,
            invalidation_price=squeeze_high,
            meta={"squeeze_range": squeeze_high - squeeze_low, "break_range": c3.total_range},
        )

    return None


# ── Utility ─────────────────────────────────────────────────

def candles_from_prices(prices: list, *, window: int = 4) -> List[OHLCV]:
    """Convert a flat price list into pseudo-OHLCV candles.

    Groups `window` consecutive prices into one candle.
    Useful when only tick prices are available (no real OHLCV).
    """
    result: List[OHLCV] = []
    for i in range(0, len(prices) - window + 1, window):
        chunk = [float(p) for p in prices[i:i + window] if p]
        if len(chunk) < 2:
            continue
        result.append(OHLCV(
            open=chunk[0],
            high=max(chunk),
            low=min(chunk),
            close=chunk[-1],
        ))
    return result


# ── BOS: Break of Structure (lookback breakout) ─────────────
# ★ [2026-04-24] Owner's request: "a signal HARPOON should catch" — support/resistance breakout.
# Previously only the PatternType BOS_BULLISH/BEARISH keywords were registered
# with no actual detection code, so it was a dead signal. Now activated.
def _detect_bos(
    candles: List[OHLCV],
    lookback: int = 10,
    *,
    close_margin_pct: float = 0.0005,  # close must be at least 0.05% outside the break level
) -> Optional[PASignal]:
    """Break of Structure: break of the recent lookback high/low + close above/below it.

    BOS_BULLISH (LONG):
      - last candle high  > prior lookback bars' max(high)
      - last candle close > prior lookback bars' max(high) × (1 + margin)
      - close is bullish
    BOS_BEARISH (SHORT):
      - last candle low   < prior lookback bars' min(low)
      - last candle close < prior lookback bars' min(low)  × (1 - margin)
      - close is bearish

    confidence: breakout size / lookback range. Smaller breakouts → lower confidence.
    """
    if len(candles) < lookback + 1:
        return None

    last = candles[-1]
    prior = candles[-lookback - 1:-1]  # prior lookback bars (excluding the last candle)
    if not prior:
        return None

    prior_high = max(c.high for c in prior)
    prior_low = min(c.low for c in prior)
    prior_range = max(prior_high - prior_low, 1e-12)

    # Bullish BOS
    if (last.high > prior_high
        and last.close > prior_high * (1 + close_margin_pct)
        and last.is_bullish):
        breakout_pct = (last.close - prior_high) / prior_range
        conf = min(1.0, max(0.45, breakout_pct * 2.0))  # guarantee 0.45 even for small breakouts
        return PASignal(
            pattern=PatternType.BOS_BULLISH,
            direction=Direction.LONG,
            confidence=conf,
            candle_idx=len(candles) - 1,
            trigger_price=last.high,
            invalidation_price=prior_high,  # falling back below the break level = invalidation
            meta={
                "lookback": lookback,
                "prior_high": prior_high,
                "breakout_pct_of_range": breakout_pct,
            },
        )

    # Bearish BOS
    if (last.low < prior_low
        and last.close < prior_low * (1 - close_margin_pct)
        and last.is_bearish):
        breakout_pct = (prior_low - last.close) / prior_range
        conf = min(1.0, max(0.45, breakout_pct * 2.0))
        return PASignal(
            pattern=PatternType.BOS_BEARISH,
            direction=Direction.SHORT,
            confidence=conf,
            candle_idx=len(candles) - 1,
            trigger_price=last.low,
            invalidation_price=prior_low,
            meta={
                "lookback": lookback,
                "prior_low": prior_low,
                "breakout_pct_of_range": breakout_pct,
            },
        )

    return None
