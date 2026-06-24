# ============================================================
# GreenPen Market Structure Analyzer
# ------------------------------------------------------------
# Implements EP.2 from the Green Pen System:
#   - Swing Point detection (HH, HL, LH, LL)
#   - Trend classification (UPTREND / DOWNTREND / SIDEWAYS)
#   - Break of Structure (BOS) detection
#   - Sideways range identification
#
# Pure functions — no state, no HyperSystem dependency.
# ============================================================
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from .pa_detector import OHLCV


# ── Data Types ──────────────────────────────────────────────

class Trend(str, Enum):
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    SIDEWAYS = "SIDEWAYS"


class SwingType(str, Enum):
    HH = "HH"  # Higher High
    HL = "HL"  # Higher Low
    LH = "LH"  # Lower High
    LL = "LL"  # Lower Low
    EQ = "EQ"  # Equal (same level ± tolerance)


@dataclass
class SwingPoint:
    type: SwingType
    price: float
    candle_idx: int
    is_high: bool  # True = swing high, False = swing low


@dataclass
class BreakOfStructure:
    detected: bool
    direction: str  # "BULLISH" (downtrend broken) or "BEARISH" (uptrend broken)
    break_price: float
    candle_idx: int


@dataclass
class MarketStructure:
    trend: Trend
    swings: List[SwingPoint]
    bos: Optional[BreakOfStructure]
    sw_range: Optional[Tuple[float, float]]  # (support, resistance) if SIDEWAYS
    confidence: float  # 0.0~1.0 how clear the structure is


# ── Core Analysis ───────────────────────────────────────────

def analyze_structure(
    candles: List[OHLCV],
    *,
    lookback: int = 5,
    eq_tolerance_pct: float = 0.1,
    recent_reality_drop_pct: float = 0.0,
    recent_reality_n: int = 5,
) -> MarketStructure:
    """Full market structure analysis.

    Args:
        candles: OHLCV list (oldest first). Minimum 15 candles recommended.
        lookback: N candles on each side to confirm a swing point.
        eq_tolerance_pct: % tolerance for treating two swings as "equal" level.
        recent_reality_drop_pct: ★ [2026-06-14 owner] Fix D. >0 enables it. lookback excludes the
            most recent N candles from swing candidates (range(lookback, len-lookback)), so a
            freshly broken crash/spike is not captured by the structure, leaving a stale trend
            label (e.g. a coin that crashed -9% still tagged UPTREND 100%). This corrects that.
        recent_reality_n: number of recent candles used for the reality check (default 5).

    Returns:
        MarketStructure with trend, swing points, BOS, and sideways range.
    """
    if len(candles) < lookback * 2 + 1:
        return MarketStructure(
            trend=Trend.SIDEWAYS,
            swings=[],
            bos=None,
            sw_range=None,
            confidence=0.0,
        )

    # 1. Detect raw swing highs and lows
    raw_highs = _detect_swing_highs(candles, lookback)
    raw_lows = _detect_swing_lows(candles, lookback)

    # 2. Classify each swing as HH/LH/HL/LL
    swings = _classify_swings(raw_highs, raw_lows, eq_tolerance_pct)

    # 3. Determine trend from classified swings
    trend, confidence = _classify_trend(swings)

    # 4. Detect Break of Structure
    bos = _detect_bos(swings, candles)

    # 4.5 ★ [2026-06-14 owner] Fix D — recent-candle reality check (default OFF: recent_reality_drop_pct=0).
    #   Defensive: UPTREND but recent N candles crashed → demote to SIDEWAYS + cut conf (remove trend-aligned LONG credit).
    #   Asymmetric: DOWNTREND but recent N candles spiked → cut conf only (do NOT flip to UPTREND = prevent dead-cat LONG credit).
    if recent_reality_drop_pct > 0 and len(candles) >= recent_reality_n + 1:
        try:
            c_last = candles[-1].close
            c_base = candles[-1 - recent_reality_n].close
            if c_base > 0:
                recent_chg = (c_last - c_base) / c_base * 100.0
                if trend == Trend.UPTREND and recent_chg <= -recent_reality_drop_pct:
                    trend = Trend.SIDEWAYS
                    confidence = min(confidence, 0.2)
                elif trend == Trend.DOWNTREND and recent_chg >= recent_reality_drop_pct:
                    confidence = min(confidence, 0.2)
        except Exception:
            pass

    # 5. Identify sideways range if applicable
    sw_range = None
    if trend == Trend.SIDEWAYS and swings:
        highs = [s.price for s in swings if s.is_high]
        lows = [s.price for s in swings if not s.is_high]
        if highs and lows:
            sw_range = (min(lows), max(highs))

    return MarketStructure(
        trend=trend,
        swings=swings,
        bos=bos,
        sw_range=sw_range,
        confidence=confidence,
    )


# ── Swing Detection ─────────────────────────────────────────

def _detect_swing_highs(candles: List[OHLCV], lookback: int) -> List[Tuple[int, float]]:
    """Find swing highs: candle.high > all neighbors within lookback."""
    results = []
    for i in range(lookback, len(candles) - lookback):
        high = candles[i].high
        is_swing = True
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if candles[j].high >= high:
                is_swing = False
                break
        if is_swing:
            results.append((i, high))
    return results


def _detect_swing_lows(candles: List[OHLCV], lookback: int) -> List[Tuple[int, float]]:
    """Find swing lows: candle.low < all neighbors within lookback."""
    results = []
    for i in range(lookback, len(candles) - lookback):
        low = candles[i].low
        is_swing = True
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if candles[j].low <= low:
                is_swing = False
                break
        if is_swing:
            results.append((i, low))
    return results


def _classify_swings(
    highs: List[Tuple[int, float]],
    lows: List[Tuple[int, float]],
    eq_tol_pct: float,
) -> List[SwingPoint]:
    """Classify swing points as HH/LH/HL/LL by comparing consecutive swings."""
    # Merge and sort by index
    all_points: List[Tuple[int, float, bool]] = []
    for idx, price in highs:
        all_points.append((idx, price, True))
    for idx, price in lows:
        all_points.append((idx, price, False))
    all_points.sort(key=lambda x: x[0])

    if not all_points:
        return []

    result: List[SwingPoint] = []
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None

    for idx, price, is_high in all_points:
        if is_high:
            if prev_high is None:
                st = SwingType.HH  # first swing, default
            else:
                diff_pct = (price - prev_high) / max(abs(prev_high), 1e-12) * 100
                if diff_pct > eq_tol_pct:
                    st = SwingType.HH
                elif diff_pct < -eq_tol_pct:
                    st = SwingType.LH
                else:
                    st = SwingType.EQ
            prev_high = price
        else:
            if prev_low is None:
                st = SwingType.HL  # first swing, default
            else:
                diff_pct = (price - prev_low) / max(abs(prev_low), 1e-12) * 100
                if diff_pct > eq_tol_pct:
                    st = SwingType.HL
                elif diff_pct < -eq_tol_pct:
                    st = SwingType.LL
                else:
                    st = SwingType.EQ
            prev_low = price

        result.append(SwingPoint(type=st, price=price, candle_idx=idx, is_high=is_high))

    return result


# ── Trend Classification ────────────────────────────────────

def _classify_trend(swings: List[SwingPoint]) -> Tuple[Trend, float]:
    """Classify trend from recent swing sequence.

    Returns: (Trend, confidence 0.0~1.0)
    """
    if len(swings) < 4:
        return Trend.SIDEWAYS, 0.2

    # Look at last 6 swings (or all if fewer)
    recent = swings[-min(6, len(swings)):]

    up_signals = 0  # HH + HL count
    down_signals = 0  # LH + LL count
    total = 0

    for s in recent:
        if s.type == SwingType.EQ:
            continue
        total += 1
        if s.type in (SwingType.HH, SwingType.HL):
            up_signals += 1
        elif s.type in (SwingType.LH, SwingType.LL):
            down_signals += 1

    if total == 0:
        return Trend.SIDEWAYS, 0.1

    up_ratio = up_signals / total
    down_ratio = down_signals / total

    if up_ratio >= 0.7:
        conf = min(1.0, up_ratio)
        # ★ [2026-04-17] Recency weighting: if the latest swing high is LH, confidence -0.3
        # UPTREND but the last high dropped = topping signal
        recent_highs = [s for s in recent if s.is_high and s.type != SwingType.EQ]
        if recent_highs and recent_highs[-1].type == SwingType.LH:
            conf = max(0.0, conf - 0.3)
        return Trend.UPTREND, conf
    elif down_ratio >= 0.7:
        conf = min(1.0, down_ratio)
        # ★ [2026-04-17] Recency weighting: if the latest swing low is HL, confidence -0.3
        # DOWNTREND but the last low rose = bottoming signal
        recent_lows = [s for s in recent if not s.is_high and s.type != SwingType.EQ]
        if recent_lows and recent_lows[-1].type == SwingType.HL:
            conf = max(0.0, conf - 0.3)
        return Trend.DOWNTREND, conf
    else:
        return Trend.SIDEWAYS, 1.0 - abs(up_ratio - down_ratio)


# ── Break of Structure ──────────────────────────────────────

def _detect_bos(
    swings: List[SwingPoint],
    candles: List[OHLCV],
) -> Optional[BreakOfStructure]:
    """Detect Break of Structure — trend reversal signal.

    Uptrend BOS: after HH+HL sequence, a LL forms (breaks below last HL).
    Downtrend BOS: after LH+LL sequence, a HH forms (breaks above last LH).
    """
    if len(swings) < 4:
        return None

    # Check recent 4 swings for BOS
    recent = swings[-4:]

    # Uptrend breakdown: was trending up (HH/HL), now LL formed
    had_uptrend = any(s.type in (SwingType.HH, SwingType.HL) for s in recent[:2])
    last_is_ll = recent[-1].type == SwingType.LL and not recent[-1].is_high

    if had_uptrend and last_is_ll:
        return BreakOfStructure(
            detected=True,
            direction="BEARISH",
            break_price=recent[-1].price,
            candle_idx=recent[-1].candle_idx,
        )

    # Downtrend breakdown: was trending down (LH/LL), now HH formed
    had_downtrend = any(s.type in (SwingType.LH, SwingType.LL) for s in recent[:2])
    last_is_hh = recent[-1].type == SwingType.HH and recent[-1].is_high

    if had_downtrend and last_is_hh:
        return BreakOfStructure(
            detected=True,
            direction="BULLISH",
            break_price=recent[-1].price,
            candle_idx=recent[-1].candle_idx,
        )

    return None


# ── Convenience ─────────────────────────────────────────────

def is_above_support(price: float, structure: MarketStructure, margin_pct: float = 0.5) -> bool:
    """Check if price is above the nearest support level."""
    if structure.sw_range:
        support = structure.sw_range[0]
        return price >= support * (1 - margin_pct / 100)
    # Use lowest recent swing low
    lows = [s.price for s in structure.swings if not s.is_high]
    if not lows:
        return True
    return price >= min(lows[-3:]) * (1 - margin_pct / 100)


def is_below_resistance(price: float, structure: MarketStructure, margin_pct: float = 0.5) -> bool:
    """Check if price is below the nearest resistance level."""
    if structure.sw_range:
        resistance = structure.sw_range[1]
        return price <= resistance * (1 + margin_pct / 100)
    highs = [s.price for s in structure.swings if s.is_high]
    if not highs:
        return True
    return price <= max(highs[-3:]) * (1 + margin_pct / 100)


# ── Reversal Patterns: M / W / Head&Shoulders ───────────────
# [2026-06-02 owner Regime Compass Phase 3] assembled on top of swing(HH/LH/HL/LL/EQ) + BOS.
#   M (double top) = 2 highs, 2nd is EQ/LH (failed to exceed) → top / W (double bottom) = 2 lows, 2nd is EQ/HL (held) → bottom
#   H&S = 3 highs, shoulder-head(highest)-shoulder → top / inverse H&S = 3 lows, shoulder-head(lowest)-shoulder → bottom
#   confirmed = BOS direction matches (neckline break confirmed). Priority: H&S (3 swings, strong) → M/W (2 swings).

@dataclass
class ReversalPattern:
    pattern: str       # "M" / "W" / "HS_TOP" / "HS_BOTTOM" / "NONE"
    direction: str     # "BEARISH"(M/HS_TOP) / "BULLISH"(W/HS_BOTTOM) / "NONE"
    confirmed: bool    # whether confirmed by BOS (neckline break)
    detail: str


def detect_reversal(structure: MarketStructure, shoulder_tol_pct: float = 1.0) -> ReversalPattern:
    """Detect M/W/H&S reversal patterns — only assembles already-classified swings + BOS (Phase 3 paper)."""
    swings = structure.swings or []
    if len(swings) < 2:
        return ReversalPattern("NONE", "NONE", False, "no_swings")
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]
    bos = structure.bos
    bos_dir = bos.direction if (bos and bos.detected) else None

    def _eq(a: float, b: float) -> bool:
        return abs(a - b) / max(abs(a), 1e-12) * 100 <= shoulder_tol_pct

    # H&S top: 3 highs = left shoulder-head(highest)-right shoulder, both shoulders < head + shoulders similar
    if len(highs) >= 3:
        l, h, r = highs[-3].price, highs[-2].price, highs[-1].price
        if h > l and h > r and _eq(l, r):
            return ReversalPattern("HS_TOP", "BEARISH", bos_dir == "BEARISH",
                                   f"H&S top L{l:.4g}/H{h:.4g}/R{r:.4g}")
    # inverse H&S bottom: 3 lows = left shoulder-head(lowest)-right shoulder
    if len(lows) >= 3:
        l, h, r = lows[-3].price, lows[-2].price, lows[-1].price
        if h < l and h < r and _eq(l, r):
            return ReversalPattern("HS_BOTTOM", "BULLISH", bos_dir == "BULLISH",
                                   f"inverse H&S bottom L{l:.4g}/H{h:.4g}/R{r:.4g}")
    # M double top: latest 2 highs, 2nd is EQ/LH (failed to exceed)
    if len(highs) >= 2 and highs[-1].type in (SwingType.EQ, SwingType.LH):
        return ReversalPattern("M", "BEARISH", bos_dir == "BEARISH",
                               f"double top {highs[-2].price:.4g}/{highs[-1].price:.4g}({highs[-1].type.value})")
    # W double bottom: latest 2 lows, 2nd is EQ/HL (held)
    if len(lows) >= 2 and lows[-1].type in (SwingType.EQ, SwingType.HL):
        return ReversalPattern("W", "BULLISH", bos_dir == "BULLISH",
                               f"double bottom {lows[-2].price:.4g}/{lows[-1].price:.4g}({lows[-1].type.value})")

    return ReversalPattern("NONE", "NONE", False, "no_pattern")
