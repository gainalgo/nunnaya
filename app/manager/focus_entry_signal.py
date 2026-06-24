# ============================================================
# FOCUS Entry Signal Engine
# ------------------------------------------------------------
# Implements Green Pen EP.4 + EP.6:
#   Step A: H4 SIG detection (PA 5-pattern + Zone overlap)
#   Step B: M5 precision entry (3 break patterns + 5-step read)
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def check_primary_sig(
    client: Any,
    market: str,
    zones: List[Dict],
    primary_tf: str = "60",
) -> Optional[Dict]:
    """Check for valid primary_tf(H1) SIG at a Zone. [2026-05-15 check_h4_sig→check_primary_sig].

    Returns {"valid": True, "pattern": ..., "direction": ...} or None.
    """
    from app.strategy.greenpen.pa_detector import OHLCV, detect_pa_patterns
    from app.strategy.greenpen.sig_validator import validate_sig

    try:
        raw = client.get_kline(market, interval=primary_tf, limit=20)
    except Exception as exc:
        logger.warning("[FOCUS_ENTRY] PRIMARY kline failed: %s", exc)
        return None

    candles = _raw_to_ohlcv(raw)
    if len(candles) < 6:
        return None

    # Zone prices for PA location validation
    zone_prices = _extract_zone_prices(zones)

    # Detect PA patterns — ★ C1 FIX: detect PA excluding the last 2 candles
    # (the last 2 are passed to validate_sig for post-PA verification)
    if len(candles) < 4:
        return None
    pa_signals = detect_pa_patterns(candles[:-2], zone_prices=zone_prices)
    if not pa_signals:
        return None

    best_pa = pa_signals[0]

    # Check if price is near a zone
    current_price = candles[-1].close
    in_zone = False
    for z in zones:
        zl = z.get("price_low", z.get("low", 0))
        zh = z.get("price_high", z.get("high", 999999))
        if zl <= current_price <= zh:
            in_zone = True
            break

    if not in_zone and zones:
        # Allow near-zone (within 0.5% of any zone)
        for z in zones:
            zl = z.get("price_low", z.get("low", 0))
            zh = z.get("price_high", z.get("high", 0))
            mid = (zl + zh) / 2
            if mid > 0 and abs(current_price - mid) / mid < 0.005:
                in_zone = True
                break

    # Validate SIG (wick integrity)
    # ★ C1 FIX: pass candles[-2:] to verify actual wick destruction
    if len(candles) >= 4:
        sig_result = validate_sig(best_pa, candles[-2:])
        if sig_result.valid:
            return {
                "valid": True,
                "pattern": best_pa.pattern.value,
                "direction": best_pa.direction.value,
                "confidence": best_pa.confidence,
                "in_zone": in_zone,
                "wick_price": sig_result.sig_wick_price,
                "atr": _simple_atr(candles),
            }

    return None


def check_m5_entry(
    client: Any,
    market: str,
    direction: str,
    entry_tf: str = "5",
    *,
    btc_price: float = 0.0,
    zones: Optional[List[Dict]] = None,
) -> Optional[Dict]:
    """M5 precision entry — enter after confirming 4-stage multi-timeframe alignment.

    Multi-timeframe hierarchy:
      H4: direction + zone (already confirmed by WATCHING entry condition)
      H1: mid-term trend alignment — 1 API call
      30M/15M: short-term momentum — derived from M5 candles (0 extra API)
      M5: precision entry timing

    H4+H1 match → enter immediately on first reversal candle (high conviction)
    H4 only, H1 sideways → PA pattern required (cautious)
    H4↔H1 conflict → no entry (risky)

    Returns {"enter": True, "direction": ..., "price": ..., "atr": ...} or None.
    """
    from app.strategy.greenpen.pa_detector import OHLCV, detect_pa_patterns
    from app.strategy.greenpen.market_structure import analyze_structure, Trend

    try:
        raw = client.get_kline(market, interval=entry_tf, limit=30)
    except Exception as exc:
        logger.warning("[FOCUS_ENTRY] M5 kline failed: %s", exc)
        return None

    candles = _raw_to_ohlcv(raw)
    if len(candles) < 10:
        return None

    current_price = candles[-1].close
    atr = _simple_atr(candles)
    if atr <= 0:
        return None

    # ── H1 trend alignment check ──
    h1_aligned = False    # is H1 in the same direction as entry
    h1_neutral = False    # is H1 sideways
    h1_opposed = False    # is H1 in the opposite direction
    try:
        h1_raw = client.get_kline(market, interval="60", limit=20)
        h1_candles = _raw_to_ohlcv(h1_raw)
        if len(h1_candles) >= 10:
            h1_struct = analyze_structure(h1_candles, lookback=3)
            h1_trend = h1_struct.trend.value if hasattr(h1_struct.trend, 'value') else str(h1_struct.trend)
            if direction == "LONG":
                h1_aligned = (h1_trend == "UPTREND")
                h1_opposed = (h1_trend == "DOWNTREND")
            else:
                h1_aligned = (h1_trend == "DOWNTREND")
                h1_opposed = (h1_trend == "UPTREND")
            h1_neutral = not h1_aligned and not h1_opposed
            logger.debug("[FOCUS_ENTRY] H1 trend=%s, dir=%s → aligned=%s", h1_trend, direction, h1_aligned)
    except Exception as exc:
        logger.debug("[FOCUS_ENTRY] H1 check failed: %s — skip entry (no H1 data)", exc)
        return None  # block entry if no H1 data (no defaulting to neutral)

    # ── if H1 is opposite direction, no entry ──
    if h1_opposed:
        logger.debug("[FOCUS_ENTRY] H1 opposed to %s — skip", direction)
        return None

    # ── 30M/15M momentum check (derived from M5 candles, 0 extra API) ──
    # 30M = 6 M5 candles, 15M = direction of 3 M5 candles
    m15_ok = True   # is 15M momentum in entry direction
    m30_ok = True   # is 30M momentum in entry direction
    if len(candles) >= 6:
        # 15M: close direction of recent 3 M5 candles
        c3 = candles[-3:]
        m15_move = c3[-1].close - c3[0].open
        if direction == "LONG" and m15_move < -atr * 0.1:
            m15_ok = False  # 15M falling — LONG risky
        elif direction == "SHORT" and m15_move > atr * 0.1:
            m15_ok = False  # 15M rising — SHORT risky

        # 30M: close direction of recent 6 M5 candles
        c6 = candles[-6:]
        m30_move = c6[-1].close - c6[0].open
        if direction == "LONG" and m30_move < -atr * 0.15:
            m30_ok = False
        elif direction == "SHORT" and m30_move > atr * 0.15:
            m30_ok = False

    # block entry if 30M is opposite direction (15M allowed since it may be early reversal)
    if not m30_ok:
        logger.debug("[FOCUS_ENTRY] 30M momentum opposed to %s — skip", direction)
        return None

    # BTC stability check (BTC 5min change < 0.5%)
    if btc_price > 0:
        try:
            btc_raw = client.get_kline("BTCUSDT", interval=entry_tf, limit=5)
            btc_candles = _raw_to_ohlcv(btc_raw)
            if len(btc_candles) >= 2:
                btc_change = abs(btc_candles[-1].close - btc_candles[-2].close) / btc_candles[-2].close * 100
                if btc_change > 0.5:
                    return None
        except Exception:
            pass

    # ── recent spike filter: prevent chasing tops/bottoms ──
    # if the recent 2 M5 candles surged/dropped ATR×2+ in one direction,
    # block entry in that direction (already too late — pullback risk)
    if len(candles) >= 3 and atr > 0:
        recent_move = candles[-1].close - candles[-3].open  # change over recent 10 minutes
        spike_threshold = atr * 2.0
        if direction == "LONG" and recent_move > spike_threshold:
            logger.info("[FOCUS_ENTRY] SPIKE GUARD: %s LONG blocked — recent 2-candle surge %.4f > ATR×2 (%.4f). Chasing top.",
                        market, recent_move, spike_threshold)
            return None
        if direction == "SHORT" and recent_move < -spike_threshold:
            logger.info("[FOCUS_ENTRY] SPIKE GUARD: %s SHORT blocked — recent 2-candle drop %.4f > ATR×2 (%.4f). Chasing bottom.",
                        market, abs(recent_move), spike_threshold)
            return None

    last = candles[-1]
    prev = candles[-2]
    avg_body = sum(c.body_len for c in candles[-6:]) / 6 if len(candles) >= 6 else last.body_len

    # ── Path 1: H4+H1+15M aligned → enter immediately on first reversal candle ──
    if h1_aligned and m15_ok:
        reversal = False
        if direction == "LONG":
            if prev.is_bearish and last.is_bullish and last.body_len >= avg_body * 0.8:
                reversal = True
            if last.is_bullish and last.lower_wick >= last.body_len * 0.5 and last.body_len >= avg_body:
                reversal = True
        else:
            if prev.is_bullish and last.is_bearish and last.body_len >= avg_body * 0.8:
                reversal = True
            if last.is_bearish and last.upper_wick >= last.body_len * 0.5 and last.body_len >= avg_body:
                reversal = True

        if reversal and last.total_range > 0 and last.body_len / last.total_range >= 0.4:
            return {
                "enter": True,
                "direction": direction,
                "price": current_price,
                "atr": atr,
                "reason": f"primary+h1+15m+reversal",
                "candle_score": _candle_read_5step(candles[-10:], direction),
            }

    # ── M5 structure analysis (for Path 2 filter) ──
    m5_trend_opposed = False
    try:
        m5_struct = analyze_structure(candles, lookback=3)
        m5_trend = m5_struct.trend.value if hasattr(m5_struct.trend, 'value') else str(m5_struct.trend)
        if direction == "LONG" and m5_trend == "DOWNTREND":
            m5_trend_opposed = True
        elif direction == "SHORT" and m5_trend == "UPTREND":
            m5_trend_opposed = True
        logger.debug("[FOCUS_ENTRY] M5 trend=%s, dir=%s → opposed=%s", m5_trend, direction, m5_trend_opposed)
    except Exception:
        pass  # disable filter on M5 analysis failure (conservative)

    # ── Path 2: H1 sideways → PA pattern required (cautious entry) ──
    # ★ if M5 trend is opposed, block counter-trend entry on PA pattern alone
    if h1_neutral or h1_aligned:
        if m5_trend_opposed and not h1_aligned:
            logger.debug("[FOCUS_ENTRY] Path2 blocked: M5 trend opposed + H1 neutral — skip %s", direction)
        else:
            pa_signals = detect_pa_patterns(candles[-6:])
            if pa_signals:
                for pa in pa_signals:
                    pa_dir = pa.direction.value if hasattr(pa.direction, 'value') else str(pa.direction)
                    if pa_dir == direction and pa.confidence >= 0.5:
                        return {
                            "enter": True,
                            "direction": direction,
                            "price": current_price,
                            "atr": atr,
                            "reason": f"m5_pa:{pa.pattern.value}+h1:{'aligned' if h1_aligned else 'neutral'}",
                            "candle_score": _candle_read_5step(candles[-10:], direction),
                        }

    # ── Path 3: H1 aligned + SW Break ──
    if h1_aligned:
        break_detected = _detect_m5_break(candles, direction)
        if break_detected:
            cs = _candle_read_5step(candles[-10:], direction)
            if cs >= 2:
                return {
                    "enter": True,
                    "direction": direction,
                    "price": current_price,
                    "atr": atr,
                    "reason": f"m5_break:{break_detected}+h1_aligned+cs:{cs}",
                    "candle_score": cs,
                }

    return None


# ── M5 Break Patterns ──────────────────────────────────────

def _detect_m5_break(candles: List, direction: str) -> Optional[str]:
    """Detect M5 break patterns (Green Pen EP.6).

    Pattern 1: SW break — flat bottom/top → breakout
    Pattern 2: Trend — HH+HL (long) or LH+LL (short)
    Pattern 3: OVL — failed first, succeed second
    """
    if len(candles) < 10:
        return None

    recent = candles[-10:]

    # minimum move threshold: total 5-bar range >= 2x recent avg candle size
    avg_body = sum(c.body_len for c in recent) / len(recent) if recent else 0
    if avg_body <= 0:
        return None  # no candle movement — cannot analyze
    min_move = avg_body * 2.0  # substantive move, not noise

    if direction == "LONG":
        # Pattern 1: flat bottom + breakout above
        lows = [c.low for c in recent[:6]]
        low_range = max(lows) - min(lows)
        avg_range = sum(c.total_range for c in recent[:6]) / 6
        if avg_range > 0 and low_range / avg_range < 0.3:  # flat bottom
            breakout = recent[-1].close - max(c.high for c in recent[:6])
            if breakout > 0 and breakout >= avg_body * 0.5:
                return "sw_break"

        # Pattern 2: HH + HL sequence — verify substantive upward move
        highs = [c.high for c in recent[-5:]]
        lows_r = [c.low for c in recent[-5:]]
        if all(highs[i] <= highs[i+1] for i in range(len(highs)-1)):
            if all(lows_r[i] <= lows_r[i+1] for i in range(len(lows_r)-1)):
                total_move = highs[-1] - lows_r[0]
                if total_move >= min_move:
                    # also: the last candle must be bullish (confirmation candle)
                    if recent[-1].is_bullish and recent[-1].body_len >= avg_body * 0.8:
                        return "trend_hh_hl"

    else:  # SHORT
        # Pattern 1: flat top + breakdown
        highs = [c.high for c in recent[:6]]
        high_range = max(highs) - min(highs)
        avg_range = sum(c.total_range for c in recent[:6]) / 6
        if avg_range > 0 and high_range / avg_range < 0.3:
            breakdown = min(c.low for c in recent[:6]) - recent[-1].close
            if breakdown > 0 and breakdown >= avg_body * 0.5:
                return "sw_break"

        # Pattern 2: LH + LL — verify substantive downward move
        highs_r = [c.high for c in recent[-5:]]
        lows = [c.low for c in recent[-5:]]
        if all(highs_r[i] >= highs_r[i+1] for i in range(len(highs_r)-1)):
            if all(lows[i] >= lows[i+1] for i in range(len(lows)-1)):
                total_move = highs_r[0] - lows[-1]
                if total_move >= min_move:
                    if recent[-1].is_bearish and recent[-1].body_len >= avg_body * 0.8:
                        return "trend_lh_ll"

    return None


# ── Candle Read 5-Step ──────────────────────────────────────

def _candle_read_5step(candles: List, direction: str) -> int:
    """Green Pen candle reading 5 steps. Returns score 0-5."""
    if len(candles) < 5:
        return 0

    score = 0

    # STEP 1: Big strong candle present (momentum)
    avg_body = sum(c.body_len for c in candles) / len(candles)
    if any(c.body_len > avg_body * 1.5 for c in candles[-3:]):
        score += 1

    # STEP 2: Weakening (candles getting smaller → exhaustion)
    if len(candles) >= 4:
        recent_avg = sum(c.body_len for c in candles[-2:]) / 2
        earlier_avg = sum(c.body_len for c in candles[-4:-2]) / 2
        if earlier_avg > 0 and recent_avg < earlier_avg * 0.7:
            score += 1

    # STEP 3: Reject at zone (long lower wick for LONG, upper for SHORT)
    last = candles[-1]
    if direction == "LONG" and last.lower_wick > last.body_len:
        score += 1
    elif direction == "SHORT" and last.upper_wick > last.body_len:
        score += 1

    # STEP 4: Color change (reversal candle)
    if len(candles) >= 2:
        if direction == "LONG" and candles[-2].is_bearish and candles[-1].is_bullish:
            score += 1
        elif direction == "SHORT" and candles[-2].is_bullish and candles[-1].is_bearish:
            score += 1

    # STEP 5: Retest (price returned to previous level and held)
    if len(candles) >= 5:
        mid_price = (candles[-5].high + candles[-5].low) / 2
        current = candles[-1].close
        # ★ M20 FIX: distinguish LONG "coming up from below" vs SHORT "coming down from above"
        if direction == "LONG" and current > mid_price and abs(current - mid_price) / mid_price < 0.003:
            score += 1
        elif direction == "SHORT" and current < mid_price and abs(current - mid_price) / mid_price < 0.003:
            score += 1

    return score


# ── Helpers ─────────────────────────────────────────────────

def _raw_to_ohlcv(raw: list) -> list:
    """Convert Bybit kline raw data to OHLCV objects."""
    from app.strategy.greenpen.pa_detector import OHLCV
    result = []
    for r in (raw or []):
        try:
            result.append(OHLCV(
                open=float(r[1]), high=float(r[2]),
                low=float(r[3]), close=float(r[4]),
                volume=float(r[5]) if len(r) > 5 else 0,
                ts=float(r[0]) / 1000 if r[0] else 0,
            ))
        except (IndexError, TypeError, ValueError):
            continue
    return result


def _extract_zone_prices(zones: List[Dict]):
    """Extract (support, resistance) from serialized zones."""
    supports = [z for z in zones if z.get("type") == "SUPPORT"]
    resistances = [z for z in zones if z.get("type") == "RESISTANCE"]
    s = max((z.get("price_high", z.get("high", 0)) for z in supports), default=0)
    r = min((z.get("price_low", z.get("low", 999999)) for z in resistances), default=999999)
    if s > 0 and r < 999999:
        return (s, r)
    return None


def _simple_atr(candles: list, period: int = 14) -> float:
    """Simple ATR from OHLCV candles."""
    if len(candles) < 2:
        return 0
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        pc = candles[i-1].close
        tr = max(c.high - c.low, abs(c.high - pc), abs(c.low - pc))
        trs.append(tr)
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent) if recent else 0
