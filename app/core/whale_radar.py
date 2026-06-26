"""WhaleRadar — whale detection engine 🐋

Combined detector for whale precursor patterns + PA back-wick signals (GreenPen theory).
Called every 60s from the FOCUS scan loop, reusing existing 5M klines with no extra API calls.

Detection layers:
  Layer 1  Precursor   (volume, squeeze, drift, test pump)
  Layer 2  PA Pattern  (Pin, Engulf, 3-candle)
  Layer 3  Back Wick   (SIG survival check)
  Layer 4  Zone        (S/R zone validation)
  Layer 5  Classify    (A Flash / B Grind / C Hunt / D Rotation)

Score:
  >= 7  🐋🐋🐋 WHALE POD   conviction +3
  >= 5  🐋🐋   HIGH ALERT  conviction +2
  >= 3  🐋     WATCH       conviction +1 + telegram
  <  3         Normal      no action

Usage:
  from app.core.whale_radar import whale_radar
  sig = whale_radar.scan("ZECUSDT", klines_5m, zones=[...])
  if sig:
      conviction += sig.conviction_boost
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Data Structures
# ─────────────────────────────────────────────

@dataclass
class WhaleSignal:
    """A single whale detection signal."""
    market: str
    timestamp: float
    score: int                  # 0-10
    direction: str              # "LONG" | "SHORT"
    pattern_type: str           # A_FLASH / B_GRIND / C_HUNT / D_ROTATION / UNKNOWN
    # ── details ──
    precursors: Dict[str, Any] = field(default_factory=dict)
    pa_pattern: str = ""        # PIN_BUY / PIN_SELL / ENGULF_BUY / ENGULF_SELL / PAT3_BUY / PAT3_SELL
    back_wick_valid: bool = False
    back_wick_level: float = 0.0
    zone_valid: bool = False
    expected_move_pct: float = 0.0
    conviction_boost: int = 0   # 0, 1, 2, 3

    def to_dict(self) -> Dict:
        return asdict(self)


# ─────────────────────────────────────────────
#  Kline helpers
# ─────────────────────────────────────────────

def _parse_candle(raw: list) -> Dict:
    """Bybit kline → dict. raw=[ts_ms, o, h, l, c, vol, turnover]"""
    o, h, l, c, vol = float(raw[1]), float(raw[2]), float(raw[3]), float(raw[4]), float(raw[5])
    body = abs(c - o)
    rng = h - l if h > l else 1e-12
    return {
        "ts": int(raw[0]) / 1000,
        "open": o, "high": h, "low": l, "close": c,
        "vol": vol,
        "body": body,
        "range": rng,
        "body_pct": body / o * 100 if o > 0 else 0,
        "range_pct": rng / o * 100 if o > 0 else 0,
        "bullish": c >= o,
        "upper_wick": h - max(o, c),
        "lower_wick": min(o, c) - l,
    }


def _candles(raw_klines: list) -> List[Dict]:
    """raw klines → parsed candle list (oldest first). Avoids double-parsing."""
    if not raw_klines:
        return []
    # already a dict → return as-is
    if isinstance(raw_klines[0], dict) and "open" in raw_klines[0]:
        return raw_klines
    return [_parse_candle(r) for r in raw_klines]


# ─────────────────────────────────────────────
#  Layer 1 — Precursor Detection
# ─────────────────────────────────────────────

_WINDOW = 12          # 60 min = 5M × 12
_BASELINE = 100       # 100-candle average baseline
_VOL_BUILDUP_THR = 0.50   # second-half volume +50% increase
_SQUEEZE_THR = 0.25        # range squeeze of 25%+
_DRIFT_THR = 0.8           # directional drift of 0.8%+
_TEST_PUMP_MIN = 0.3       # test pump minimum 0.3%
_ACCUM_BIAS_MIN = 7        # 7+ of 12 candles in the same direction


def _detect_precursors(candles: List[Dict]) -> Dict[str, Any]:
    """Analyze precursors over the last 60 min (12 candles). Baseline is the 100-candle average."""
    result = {
        "accumulation": False, "accumulation_count": 0,
        "test_pump": False, "test_pump_count": 0, "test_pump_details": [],
        "vol_buildup": False, "vol_buildup_pct": 0.0,
        "squeeze": False, "squeeze_pct": 0.0,
        "drift": False, "drift_pct": 0.0,
        "direction_hint": "NEUTRAL",
        "score": 0,
    }
    if len(candles) < _WINDOW + 10:
        return result

    window = candles[-_WINDOW:]           # last 12 candles
    baseline = candles[-_BASELINE - _WINDOW:-_WINDOW] if len(candles) >= _BASELINE + _WINDOW else candles[:-_WINDOW]

    if not baseline:
        return result

    # ── 1. Accumulation Bias ──
    bull_count = sum(1 for c in window if c["bullish"])
    bear_count = _WINDOW - bull_count
    if bull_count >= _ACCUM_BIAS_MIN:
        result["accumulation"] = True
        result["direction_hint"] = "LONG"
    elif bear_count >= _ACCUM_BIAS_MIN:
        result["accumulation"] = True
        result["direction_hint"] = "SHORT"
    result["accumulation_count"] = max(bull_count, bear_count)

    # ── 2. Micro Test Pump ──
    test_pumps = []
    for i in range(len(window) - 1):
        c = window[i]
        n = window[i + 1]
        # bullish test: large body and the next candle retraces 50%+
        if c["body_pct"] >= _TEST_PUMP_MIN:
            if c["bullish"] and not n["bullish"] and n["body"] > c["body"] * 0.3:
                test_pumps.append({"idx": i, "dir": "UP", "size_pct": c["body_pct"]})
            elif not c["bullish"] and n["bullish"] and n["body"] > c["body"] * 0.3:
                test_pumps.append({"idx": i, "dir": "DOWN", "size_pct": c["body_pct"]})
    result["test_pump"] = len(test_pumps) > 0
    result["test_pump_count"] = len(test_pumps)
    result["test_pump_details"] = test_pumps

    # ── 3. Volume Buildup ──
    avg_base_vol = sum(c["vol"] for c in baseline) / len(baseline) if baseline else 1
    first_half_vol = sum(c["vol"] for c in window[:6]) / 6
    second_half_vol = sum(c["vol"] for c in window[6:]) / 6
    if first_half_vol > 0:
        vol_change = (second_half_vol - first_half_vol) / first_half_vol
        result["vol_buildup_pct"] = round(vol_change * 100, 1)
        if vol_change >= _VOL_BUILDUP_THR:
            result["vol_buildup"] = True
    # or overall volume is 2x+ the baseline
    window_avg = sum(c["vol"] for c in window) / _WINDOW
    if avg_base_vol > 0 and window_avg / avg_base_vol >= 2.0:
        result["vol_buildup"] = True

    # ── 4. Range Squeeze ──
    recent_ranges = [c["range_pct"] for c in window[-6:]]
    baseline_ranges = [c["range_pct"] for c in baseline[-20:]] if len(baseline) >= 20 else [c["range_pct"] for c in baseline]
    if baseline_ranges:
        avg_recent = sum(recent_ranges) / len(recent_ranges)
        avg_baseline = sum(baseline_ranges) / len(baseline_ranges)
        if avg_baseline > 0:
            squeeze = 1 - (avg_recent / avg_baseline)
            result["squeeze_pct"] = round(squeeze * 100, 1)
            if squeeze >= _SQUEEZE_THR:
                result["squeeze"] = True

    # ── 5. Net Drift ──
    if window[0]["close"] > 0:
        drift = (window[-1]["close"] - window[0]["close"]) / window[0]["close"] * 100
        result["drift_pct"] = round(drift, 3)
        if abs(drift) >= _DRIFT_THR:
            result["drift"] = True
            if drift > 0 and result["direction_hint"] == "NEUTRAL":
                result["direction_hint"] = "LONG"
            elif drift < 0 and result["direction_hint"] == "NEUTRAL":
                result["direction_hint"] = "SHORT"

    # ── Precursor Score (max 5) ──
    score = 0
    if result["accumulation"]:
        score += 2
    if result["test_pump"]:
        score += 2
    if result["vol_buildup"]:
        score += 1
    if result["squeeze"]:
        score += 1
    if result["drift"]:
        score += 1
    result["score"] = min(score, 5)

    return result


# ─────────────────────────────────────────────
#  Layer 2 — PA Pattern Detection
# ─────────────────────────────────────────────

_PIN_WICK_RATIO = 0.60      # pin bar: wick is 60%+ of the full range
_PIN_BODY_RATIO = 0.35      # pin bar: body is 35% or less of the full range
_ENGULF_BODY_RATIO = 1.2    # engulfing: current body is 1.2x+ the previous one


def _detect_pa_pattern(candles: List[Dict]) -> Dict[str, Any]:
    """Detect PA patterns over the last 5 candles.

    Returns: {pattern, direction, candle_idx, confirmation_at, back_wick_candle_idx}
    """
    result = {
        "pattern": "",           # PIN / ENGULF / PAT3
        "pa_name": "",           # PIN_BUY, ENGULF_SELL, etc.
        "direction": "",         # LONG / SHORT
        "signal_candle_idx": -1, # signal candle position (reverse-indexed from the end)
        "confirmation_at": 0,    # confirmation candle number (Pat1→2, Pat2→3, Pat3→4)
        "detected": False,
    }
    if len(candles) < 6:
        return result

    recent = candles[-5:]  # last 5 candles

    # ── Pat 1: Pin Bar ──
    # Search the 2nd–4th most recent candles for a pin bar (the very last may still be incomplete)
    for offset in range(1, 4):  # candles[-2], candles[-3], candles[-4]
        if offset >= len(recent):
            break
        c = recent[-offset]
        rng = c["range"]
        if rng <= 0:
            continue

        lower_ratio = c["lower_wick"] / rng
        upper_ratio = c["upper_wick"] / rng
        body_ratio = c["body"] / rng

        # BUY Pin: long lower wick (bounce off support)
        if lower_ratio >= _PIN_WICK_RATIO and body_ratio <= _PIN_BODY_RATIO and lower_ratio > upper_ratio * 2:
            result.update({
                "pattern": "PIN", "pa_name": "PIN_BUY", "direction": "LONG",
                "signal_candle_idx": offset, "confirmation_at": 2, "detected": True,
            })
            return result

        # SELL Pin: long upper wick (rejection at resistance)
        if upper_ratio >= _PIN_WICK_RATIO and body_ratio <= _PIN_BODY_RATIO and upper_ratio > lower_ratio * 2:
            result.update({
                "pattern": "PIN", "pa_name": "PIN_SELL", "direction": "SHORT",
                "signal_candle_idx": offset, "confirmation_at": 2, "detected": True,
            })
            return result

    # ── Pat 2: Engulfing ──
    for offset in range(1, 4):
        if offset + 1 >= len(recent):
            break
        prev = recent[-(offset + 1)]
        curr = recent[-offset]

        if prev["body"] <= 0:
            continue

        # Bullish Engulfing
        if (not prev["bullish"] and curr["bullish"] and
                curr["body"] >= prev["body"] * _ENGULF_BODY_RATIO and
                curr["close"] > prev["open"] and curr["open"] <= prev["close"]):
            result.update({
                "pattern": "ENGULF", "pa_name": "ENGULF_BUY", "direction": "LONG",
                "signal_candle_idx": offset, "confirmation_at": 3, "detected": True,
            })
            return result

        # Bearish Engulfing
        if (prev["bullish"] and not curr["bullish"] and
                curr["body"] >= prev["body"] * _ENGULF_BODY_RATIO and
                curr["close"] < prev["open"] and curr["open"] >= prev["close"]):
            result.update({
                "pattern": "ENGULF", "pa_name": "ENGULF_SELL", "direction": "SHORT",
                "signal_candle_idx": offset, "confirmation_at": 3, "detected": True,
            })
            return result

    # ── Pat 3: Three-candle reversal (morning star / evening star) ──
    for offset in range(1, 3):
        if offset + 2 >= len(recent):
            break
        c1 = recent[-(offset + 2)]  # first
        c2 = recent[-(offset + 1)]  # middle (small body)
        c3 = recent[-offset]         # third

        if c1["body"] <= 0 or c3["body"] <= 0:
            continue

        # the middle candle must be small (indecision)
        if c2["body"] > min(c1["body"], c3["body"]) * 0.5:
            continue

        # Morning Star (BUY): down → small → up, third recovers 50%+ of the first
        if (not c1["bullish"] and c3["bullish"] and
                c3["close"] > (c1["open"] + c1["close"]) / 2):
            result.update({
                "pattern": "PAT3", "pa_name": "PAT3_BUY", "direction": "LONG",
                "signal_candle_idx": offset, "confirmation_at": 4, "detected": True,
            })
            return result

        # Evening Star (SELL): up → small → down, third drops 50%+ of the first
        if (c1["bullish"] and not c3["bullish"] and
                c3["close"] < (c1["open"] + c1["close"]) / 2):
            result.update({
                "pattern": "PAT3", "pa_name": "PAT3_SELL", "direction": "SHORT",
                "signal_candle_idx": offset, "confirmation_at": 4, "detected": True,
            })
            return result

    return result


# ─────────────────────────────────────────────
#  Layer 3 — Back Wick SIG Validation
# ─────────────────────────────────────────────

def _check_back_wick(candles: List[Dict], pa: Dict) -> Dict[str, Any]:
    """Confirm back-wick survival after a PA pattern.

    Back wick = the confirmation candle's wick. SIG is valid if subsequent candles do not break this level.
    """
    result = {"valid": False, "level": 0.0, "destroyed": False, "reason": ""}

    if not pa.get("detected"):
        result["reason"] = "no_pa"
        return result

    sig_idx = pa["signal_candle_idx"]
    conf_offset = pa["confirmation_at"]  # Pat1→2, Pat2→3, Pat3→4

    # confirmation candle = the (conf_offset-1)th candle after the signal candle
    # recent[-sig_idx] is the signal → confirmation candle is recent[-sig_idx + (conf_offset - 1)]
    conf_candle_pos = sig_idx - (conf_offset - 1)

    if conf_candle_pos < 1:
        # confirmation candle has not formed yet or is the last candle → cannot validate
        result["reason"] = "awaiting_confirmation"
        # while still awaiting confirmation, treat as valid for now (so we don't miss the opportunity)
        result["valid"] = True
        return result

    recent = candles[-5:]
    if conf_candle_pos >= len(recent):
        result["reason"] = "idx_out_of_range"
        return result

    conf = recent[-conf_candle_pos]
    direction = pa["direction"]

    # determine the back-wick level
    if direction == "LONG":
        # BUY signal → tip of the confirmation candle's lower wick = back-wick level
        result["level"] = conf["low"]
        # destroyed if a subsequent candle drops below this level
        subsequent = recent[-conf_candle_pos + 1:] if conf_candle_pos > 1 else []
        for s in subsequent:
            if s["low"] < result["level"]:
                result["destroyed"] = True
                result["reason"] = "wick_broken_below"
                return result
    else:
        # SELL signal → tip of the confirmation candle's upper wick = back-wick level
        result["level"] = conf["high"]
        subsequent = recent[-conf_candle_pos + 1:] if conf_candle_pos > 1 else []
        for s in subsequent:
            if s["high"] > result["level"]:
                result["destroyed"] = True
                result["reason"] = "wick_broken_above"
                return result

    result["valid"] = True
    result["reason"] = "wick_survived"
    return result


# ─────────────────────────────────────────────
#  Layer 4 — Zone Validation (lightweight)
# ─────────────────────────────────────────────

def _check_zone(candles: List[Dict], pa: Dict, zones: list | None) -> bool:
    """Check whether the PA signal occurred at an S/R zone.

    If no zones are provided, compute a lightweight S/R from the candle data.
    """
    if not pa.get("detected"):
        return False

    direction = pa["direction"]
    if not candles:
        return False

    current_price = candles[-1]["close"]

    # use FOCUS zones if available
    if zones:
        for z in zones:
            ztype = z.get("type", "")
            low = float(z.get("low", 0))
            high = float(z.get("high", 0))
            if low <= current_price <= high:
                if direction == "LONG" and "support" in ztype.lower():
                    return True
                if direction == "SHORT" and "resistance" in ztype.lower():
                    return True
            # nearby zone (within 0.5% of price)
            dist_pct = min(abs(current_price - low), abs(current_price - high)) / current_price * 100
            if dist_pct < 0.5:
                if direction == "LONG" and "support" in ztype.lower():
                    return True
                if direction == "SHORT" and "resistance" in ztype.lower():
                    return True

    # lightweight S/R: major highs/lows over the last 100 candles
    if len(candles) >= 50:
        highs = sorted([c["high"] for c in candles[-100:]], reverse=True)
        lows = sorted([c["low"] for c in candles[-100:]])

        # top 5% region = resistance, bottom 5% region = support
        top5 = len(highs) // 20 or 1
        bot5 = len(lows) // 20 or 1
        resistance_zone = sum(highs[:top5]) / top5
        support_zone = sum(lows[:bot5]) / bot5

        price_range = resistance_zone - support_zone
        if price_range > 0:
            proximity = 0.03  # within 3% of the price range
            if direction == "LONG" and (current_price - support_zone) / price_range < proximity * 3:
                return True
            if direction == "SHORT" and (resistance_zone - current_price) / price_range < proximity * 3:
                return True

    return False


# ─────────────────────────────────────────────
#  Layer 5 — Pattern Classification
# ─────────────────────────────────────────────

def _classify_pattern(precursors: Dict, pa: Dict) -> str:
    """Classify the pattern type.

    A_FLASH:  volume spike + pin bar → 1-candle explosion
    B_GRIND:  range squeeze + accumulation → sustained pump
    C_HUNT:   reversal wicking → liquidation hunt
    D_ROTATION: multi-coin (decided in scan_multiple)
    """
    squeeze = precursors.get("squeeze", False)
    accum = precursors.get("accumulation", False)
    vol = precursors.get("vol_buildup", False)
    test = precursors.get("test_pump", False)
    pa_type = pa.get("pattern", "")

    # B_GRIND: squeeze + accumulation (bigger and longer-lasting)
    if squeeze and accum:
        return "B_GRIND"

    # A_FLASH: volume + pin bar (fast and powerful)
    if vol and pa_type == "PIN":
        return "A_FLASH"

    # A_FLASH: volume + test pump (likely a whale)
    if vol and test:
        return "A_FLASH"

    # C_HUNT: big wicking followed by an immediate reversal (not implemented yet — needs OI data later)
    # for now, roughly estimated from a large wick + small body
    if pa_type == "PIN" and precursors.get("squeeze_pct", 0) < 10:
        return "C_HUNT"

    # default
    if precursors["score"] >= 3:
        return "B_GRIND" if squeeze else "A_FLASH"

    return "UNKNOWN"


# ─────────────────────────────────────────────
#  Score Calculation & Expected Move
# ─────────────────────────────────────────────

def _calculate_score(precursors: Dict, pa: Dict, back_wick: Dict, zone_valid: bool) -> int:
    """Compute the composite score (0-10)."""
    score = precursors.get("score", 0)  # max 5

    # PA Layer (max 3)
    if pa.get("detected"):
        score += 1
    if back_wick.get("valid"):
        score += 1
    if zone_valid:
        score += 1

    # Classification bonus handled by caller (D_ROTATION)
    return min(score, 10)


def _estimate_move(candles: List[Dict], pattern_type: str) -> float:
    """ATR-based expected move (%). Approximates the running cycle per TF."""
    if len(candles) < 20:
        return 0.0

    # 20-candle ATR
    atr_sum = sum(c["range"] for c in candles[-20:])
    atr = atr_sum / 20
    price = candles[-1]["close"]
    if price <= 0:
        return 0.0

    atr_pct = atr / price * 100

    # per-pattern multipliers (PDF-based)
    multipliers = {
        "A_FLASH": 3.0,     # ATR × 3 (fast explosion, short)
        "B_GRIND": 6.0,     # ATR × 6 (slow but goes far)
        "C_HUNT": 2.0,      # ATR × 2 (retracement only)
        "D_ROTATION": 4.0,  # ATR × 4 (medium)
        "UNKNOWN": 2.0,
    }
    mult = multipliers.get(pattern_type, 2.0)
    return round(atr_pct * mult, 2)


def _score_to_boost(score: int) -> int:
    """score → conviction boost."""
    if score >= 7:
        return 3  # 🐋🐋🐋 WHALE POD
    if score >= 5:
        return 2  # 🐋🐋 HIGH ALERT
    if score >= 3:
        return 1  # 🐋 WATCH
    return 0


def _score_label(score: int) -> str:
    if score >= 7:
        return "🐋🐋🐋 WHALE POD"
    if score >= 5:
        return "🐋🐋 HIGH ALERT"
    if score >= 3:
        return "🐋 WATCH"
    return ""


# ─────────────────────────────────────────────
#  Main WhaleRadar Class
# ─────────────────────────────────────────────

_ALERT_COOLDOWN = 300  # 5-min interval between alerts for the same coin
_HISTORY_MAX = 200     # max history retained


class WhaleRadar:
    """Main whale detection engine."""

    def __init__(self):
        self._history: List[WhaleSignal] = []
        self._last_alert_ts: Dict[str, float] = {}  # market → last telegram ts
        self._active_alerts: Dict[str, WhaleSignal] = {}  # market → latest signal
        self._notify_fn = None  # telegram notify function (injected externally)

    def set_notify(self, fn):
        """Inject the telegram notify function. fn(message: str) → None"""
        self._notify_fn = fn

    def scan(self, market: str, klines_5m: list,
             zones: list | None = None) -> WhaleSignal | None:
        """Scan a single market. Requires 5M kline data (100+ candles recommended).

        Returns: WhaleSignal if score >= 3, else None.
        """
        candles = _candles(klines_5m)
        if len(candles) < 24:  # at least 2 hours of data
            return None

        # ── Layer 1: Precursor ──
        precursors = _detect_precursors(candles)

        # ── Layer 2: PA Pattern ──
        pa = _detect_pa_pattern(candles)

        # ── Layer 3: Back Wick ──
        back_wick = _check_back_wick(candles, pa)

        # ── Layer 4: Zone ──
        zone_valid = _check_zone(candles, pa, zones)

        # ── Score ──
        score = _calculate_score(precursors, pa, back_wick, zone_valid)

        if score < 3:
            # low score → also remove from active alerts
            self._active_alerts.pop(market.upper(), None)
            return None

        # ── Determine Direction ──
        direction = pa.get("direction", "") or precursors.get("direction_hint", "NEUTRAL")

        # ── Classification ──
        pattern_type = _classify_pattern(precursors, pa)

        # ── Expected Move ──
        expected_move = _estimate_move(candles, pattern_type)

        # ── Build Signal ──
        signal = WhaleSignal(
            market=market.upper(),
            timestamp=time.time(),
            score=score,
            direction=direction,
            pattern_type=pattern_type,
            precursors=precursors,
            pa_pattern=pa.get("pa_name", ""),
            back_wick_valid=back_wick.get("valid", False),
            back_wick_level=back_wick.get("level", 0.0),
            zone_valid=zone_valid,
            expected_move_pct=expected_move,
            conviction_boost=_score_to_boost(score),
        )

        # ── Record ──
        self._active_alerts[market.upper()] = signal
        self._history.append(signal)
        if len(self._history) > _HISTORY_MAX:
            self._history = self._history[-_HISTORY_MAX:]

        # ── Log ──
        label = _score_label(score)
        logger.info(
            "[WHALE] %s %s score=%d dir=%s type=%s pa=%s wick=%s zone=%s move=%.1f%% boost=+%d",
            label, market, score, direction, pattern_type,
            pa.get("pa_name", "-"), back_wick.get("valid"), zone_valid,
            expected_move, signal.conviction_boost,
        )

        # ── Telegram Alert ──
        self._send_alert(signal)

        return signal

    def scan_multiple(self, markets_klines: Dict[str, list],
                      zones_map: Dict[str, list] | None = None) -> Dict[str, WhaleSignal]:
        """Scan multiple markets at once. Includes Type D (Rotation) detection.

        Args:
            markets_klines: {market: klines_5m_list}
            zones_map: {market: zones_list} (optional)

        Returns: {market: WhaleSignal} for score >= 3 only.
        """
        results: Dict[str, WhaleSignal] = {}
        zones_map = zones_map or {}

        for market, klines in markets_klines.items():
            sig = self.scan(market, klines, zones_map.get(market))
            if sig:
                results[market] = sig

        # ── Type D: Rotation detection (3+ coins at score >= 3 simultaneously) ──
        if len(results) >= 3:
            for sig in results.values():
                if sig.score < 7:  # D_ROTATION bonus
                    sig.pattern_type = "D_ROTATION"
                    sig.score = min(sig.score + 1, 10)
                    sig.conviction_boost = _score_to_boost(sig.score)
            logger.warning("[WHALE] 🐋🐋🐋 ROTATION detected! %d coins: %s",
                          len(results), list(results.keys()))
            if self._notify_fn:
                coins = ", ".join(f"{m}({s.score})" for m, s in results.items())
                self._notify_fn(f"🐋🐋🐋 ROTATION DETECTED!\n{len(results)} coins active: {coins}")

        return results

    def get_active_alerts(self) -> Dict[str, Dict]:
        """Currently active alerts (for the dashboard). Signals within 5 minutes only."""
        now = time.time()
        active = {}
        for mk, sig in list(self._active_alerts.items()):
            if now - sig.timestamp < 300:
                active[mk] = sig.to_dict()
            else:
                del self._active_alerts[mk]
        return active

    def get_history(self, market: str | None = None, hours: float = 24) -> List[Dict]:
        """Query history."""
        cutoff = time.time() - hours * 3600
        result = []
        for sig in self._history:
            if sig.timestamp < cutoff:
                continue
            if market and sig.market != market.upper():
                continue
            result.append(sig.to_dict())
        return result

    def _send_alert(self, signal: WhaleSignal):
        """Telegram alert (cooldown applied)."""
        if not self._notify_fn:
            return
        now = time.time()
        last = self._last_alert_ts.get(signal.market, 0)
        if now - last < _ALERT_COOLDOWN:
            return

        self._last_alert_ts[signal.market] = now
        label = _score_label(signal.score)
        msg_lines = [
            f"{label}",
            f"📍 {signal.market} | {signal.direction}",
            f"Score: {signal.score}/10 | Type: {signal.pattern_type}",
        ]
        if signal.pa_pattern:
            msg_lines.append(f"PA: {signal.pa_pattern} | Wick: {'✅' if signal.back_wick_valid else '❌'}")
        p = signal.precursors
        details = []
        if p.get("accumulation"):
            details.append(f"Accumulation {p['accumulation_count']}/12")
        if p.get("test_pump"):
            details.append(f"Test pump {p['test_pump_count']}x")
        if p.get("vol_buildup"):
            details.append(f"Volume↑{p['vol_buildup_pct']:.0f}%")
        if p.get("squeeze"):
            details.append(f"Squeeze {p['squeeze_pct']:.0f}%")
        if details:
            msg_lines.append(" | ".join(details))
        if signal.expected_move_pct > 0:
            msg_lines.append(f"Expected move: ±{signal.expected_move_pct:.1f}%")
        msg_lines.append(f"Conviction boost: +{signal.conviction_boost}")

        self._notify_fn("\n".join(msg_lines))


# ─────────────────────────────────────────────
#  Singleton
# ─────────────────────────────────────────────

whale_radar = WhaleRadar()
