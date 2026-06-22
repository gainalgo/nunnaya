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
        recent_reality_drop_pct: ★ [2026-06-14 부모] Fix D. >0 활성. lookback이 최근 N봉을 swing
            후보에서 제외(range(lookback, len-lookback))해 갓 터진 폭락/폭등이 구조에 안 잡혀
            옛 추세 라벨이 잔존하는 것(예: -9% 폭락 코인이 UPTREND 100%)을 교정한다.
        recent_reality_n: reality check 에 쓸 최근 봉 수 (기본 5).

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

    # 4.5 ★ [2026-06-14 부모] Fix D — 최근봉 reality check (default OFF: recent_reality_drop_pct=0).
    #   방어적: UPTREND 인데 최근 N봉 급락 → SIDEWAYS 강등 + conf 컷 (추세정렬 LONG credit 제거).
    #   비대칭: DOWNTREND 인데 최근 N봉 급등 → conf 만 컷 (UPTREND 로 뒤집지 않음 = dead-cat LONG credit 방지).
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
        # ★ [2026-04-17] Recency weighting: 최근 swing high가 LH면 신뢰도 -0.3
        # UPTREND인데 마지막 고점이 낮아졌다 = 꼭대기 신호
        recent_highs = [s for s in recent if s.is_high and s.type != SwingType.EQ]
        if recent_highs and recent_highs[-1].type == SwingType.LH:
            conf = max(0.0, conf - 0.3)
        return Trend.UPTREND, conf
    elif down_ratio >= 0.7:
        conf = min(1.0, down_ratio)
        # ★ [2026-04-17] Recency weighting: 최근 swing low가 HL이면 신뢰도 -0.3
        # DOWNTREND인데 마지막 저점이 올라갔다 = 바닥 신호
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
# [2026-06-02 부모님 레짐 컴퍼스 Phase 3] swing(HH/LH/HL/LL/EQ) + BOS 토대 조립.
#   M(쌍봉)=고점2개 EQ/LH(못 넘음)→천장 / W(쌍바닥)=저점2개 EQ/HL(안 깸)→바닥
#   H&S=고점3개 어깨-머리(최고)-어깨→천장 / 역H&S=저점3개 어깨-머리(최저)-어깨→바닥
#   confirmed = BOS 방향 일치(넥라인 돌파 확정). 우선순위: H&S(3봉,강함) → M/W(2봉).

@dataclass
class ReversalPattern:
    pattern: str       # "M" / "W" / "HS_TOP" / "HS_BOTTOM" / "NONE"
    direction: str     # "BEARISH"(M/HS_TOP) / "BULLISH"(W/HS_BOTTOM) / "NONE"
    confirmed: bool    # BOS(넥라인 돌파)로 확정됐는지
    detail: str


def detect_reversal(structure: MarketStructure, shoulder_tol_pct: float = 1.0) -> ReversalPattern:
    """M/W/H&S 반전 패턴 감지 — 이미 분류된 swing + BOS 를 조립만 (Phase 3 paper)."""
    swings = structure.swings or []
    if len(swings) < 2:
        return ReversalPattern("NONE", "NONE", False, "no_swings")
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]
    bos = structure.bos
    bos_dir = bos.direction if (bos and bos.detected) else None

    def _eq(a: float, b: float) -> bool:
        return abs(a - b) / max(abs(a), 1e-12) * 100 <= shoulder_tol_pct

    # H&S 천장: 고점 3개 = 왼어깨-머리(최고)-오른어깨, 양 어깨 < 머리 + 어깨 유사
    if len(highs) >= 3:
        l, h, r = highs[-3].price, highs[-2].price, highs[-1].price
        if h > l and h > r and _eq(l, r):
            return ReversalPattern("HS_TOP", "BEARISH", bos_dir == "BEARISH",
                                   f"H&S천장 L{l:.4g}/H{h:.4g}/R{r:.4g}")
    # 역H&S 바닥: 저점 3개 = 왼어깨-머리(최저)-오른어깨
    if len(lows) >= 3:
        l, h, r = lows[-3].price, lows[-2].price, lows[-1].price
        if h < l and h < r and _eq(l, r):
            return ReversalPattern("HS_BOTTOM", "BULLISH", bos_dir == "BULLISH",
                                   f"역H&S바닥 L{l:.4g}/H{h:.4g}/R{r:.4g}")
    # M 쌍봉: 최근 고점 2개, 2번째가 EQ/LH (못 넘음)
    if len(highs) >= 2 and highs[-1].type in (SwingType.EQ, SwingType.LH):
        return ReversalPattern("M", "BEARISH", bos_dir == "BEARISH",
                               f"쌍봉 {highs[-2].price:.4g}/{highs[-1].price:.4g}({highs[-1].type.value})")
    # W 쌍바닥: 최근 저점 2개, 2번째가 EQ/HL (안 깸)
    if len(lows) >= 2 and lows[-1].type in (SwingType.EQ, SwingType.HL):
        return ReversalPattern("W", "BULLISH", bos_dir == "BULLISH",
                               f"쌍바닥 {lows[-2].price:.4g}/{lows[-1].price:.4g}({lows[-1].type.value})")

    return ReversalPattern("NONE", "NONE", False, "no_pattern")
