"""BTC Regime — BTC 4H trend detection + counter-trend entry penalty.

Observation (2026-04-18):
- Looking only at an individual coin's H4 misses turning points → the overall BTC regime is the fundamental divergence
- While ETH SHORT keeps getting stopped out, BTC has already reversed into a bounce (counter-trend scalp)
- Four regime stages:
    BULL    — EMA20 rising + price > EMA50 + rising swing high
    BEAR    — EMA20 falling + price < EMA50 + falling swing low
    TRANS   — EMA slope flip or price oscillating around EMA50 within ±1% (= the most expensive market)
    NEUTRAL — none of the above (ranging)

Effect (delta):
    BULL  × LONG  = +1    BEAR × LONG  = -2
    BULL  × SHORT = -2    BEAR × SHORT = +1
    TRANS × any   = -1   (turning point = tuition is expensive, so be conservative)
    NEUT  × any   =  0

Input: BTC H4 candle list — two formats supported
    (a) Bybit raw: [[start_ts, o, h, l, c, v, ...], ...]
    (b) OHLCV objects: list of obj with .high/.low/.close
    (c) plain closes: list of float (EMA only, swing analysis skipped → weak detection)

Cache: regime detection doesn't need to run every tick → 10min TTL.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _extract_ohlc(candles: List[Any]) -> Tuple[List[float], List[float], List[float]]:
    """Extract (highs, lows, closes) from various formats. Empty lists on failure."""
    highs: List[float] = []
    lows: List[float] = []
    closes: List[float] = []
    if not candles:
        return highs, lows, closes
    try:
        for c in candles:
            if hasattr(c, "high") and hasattr(c, "low") and hasattr(c, "close"):
                highs.append(float(c.high))
                lows.append(float(c.low))
                closes.append(float(c.close))
            elif isinstance(c, (list, tuple)) and len(c) >= 5:
                # Bybit raw: [ts, o, h, l, c, v]
                highs.append(float(c[2]))
                lows.append(float(c[3]))
                closes.append(float(c[4]))
            elif isinstance(c, dict):
                highs.append(float(c.get("high", c.get("h", 0))))
                lows.append(float(c.get("low", c.get("l", 0))))
                closes.append(float(c.get("close", c.get("c", 0))))
            elif isinstance(c, (int, float)):
                # closes only
                closes.append(float(c))
    except Exception as exc:
        logger.debug("[btc_regime] ohlc extract error: %s", exc)
    return highs, lows, closes


def _ema(data: List[float], length: int) -> Optional[float]:
    if len(data) < length:
        return None
    k = 2.0 / (length + 1)
    ema = sum(data[:length]) / length
    for x in data[length:]:
        ema = x * k + ema * (1 - k)
    return ema


def _ema_series(data: List[float], length: int) -> List[float]:
    if len(data) < length:
        return []
    out = []
    k = 2.0 / (length + 1)
    ema = sum(data[:length]) / length
    out.append(ema)
    for x in data[length:]:
        ema = x * k + ema * (1 - k)
        out.append(ema)
    return out


class BtcRegimeModule:
    def __init__(self, config: Any):
        self.config = config
        # Cache: (ts, regime_str, last_price)
        self._cache: Optional[Tuple[float, str, float]] = None

    def _detect_regime(
        self, highs: List[float], lows: List[float], closes: List[float]
    ) -> str:
        """Core regime detection. Returns: "BULL"|"BEAR"|"TRANS"|"NEUTRAL" """
        if len(closes) < 50:
            return "NEUTRAL"

        cfg = self.config
        trans_band_pct = float(getattr(cfg, "btc_regime_trans_band_pct", 1.0)) / 100.0
        ema20_len = int(getattr(cfg, "btc_regime_ema_short", 20))
        ema50_len = int(getattr(cfg, "btc_regime_ema_long", 50))

        ema20_s = _ema_series(closes, ema20_len)
        ema50_s = _ema_series(closes, ema50_len)
        if not ema20_s or not ema50_s:
            return "NEUTRAL"

        ema20_now = ema20_s[-1]
        ema50_now = ema50_s[-1]
        price = closes[-1]

        # EMA20 slope (slope over the last 5 bars)
        slope_len = min(5, len(ema20_s) - 1)
        if slope_len <= 0:
            return "NEUTRAL"
        ema20_past = ema20_s[-1 - slope_len]
        slope_pct = (ema20_now - ema20_past) / ema20_past if ema20_past else 0.0
        # slope_pct rule: within ±flat_thr_pct = flat (config A/B testable)
        flat_thr = float(getattr(cfg, "btc_regime_slope_flat_thr_pct", 0.3)) / 100.0

        # TRANS detection: price near EMA50 (±trans_band_pct) + slope flat
        near_ema50 = abs(price - ema50_now) / ema50_now < trans_band_pct
        if near_ema50 and abs(slope_pct) < flat_thr:
            return "TRANS"

        # Detect recent slope flip (slope 5 bars ago vs current slope sign reversal)
        if slope_len >= 3 and len(ema20_s) > 2 * slope_len:
            past_slope = (ema20_s[-1 - slope_len] - ema20_s[-1 - 2 * slope_len]) / max(
                ema20_s[-1 - 2 * slope_len], 1e-9
            )
            if past_slope * slope_pct < 0 and abs(past_slope) > flat_thr:
                return "TRANS"

        # BULL / BEAR
        if slope_pct > flat_thr and price > ema50_now:
            return "BULL"
        if slope_pct < -flat_thr and price < ema50_now:
            return "BEAR"

        return "NEUTRAL"

    def _cached_regime(
        self, btc_candles: Optional[List[Any]], now_ts: float
    ) -> Tuple[str, float]:
        """Cache-aware regime. Returns: (regime, price)"""
        cfg = self.config
        ttl = float(getattr(cfg, "btc_regime_cache_ttl_sec", 600.0))
        if self._cache and (now_ts - self._cache[0]) < ttl:
            return self._cache[1], self._cache[2]

        # [2026-04-19 this agent review CE#5] on fetch failure (empty candles):
        # if a previous cache exists, reuse it stale; otherwise cache NEUTRAL as a placeholder (avoid retries for 10min)
        if not btc_candles:
            if self._cache:
                logger.debug("[btc_regime] fetch empty → reuse stale cache (%s)", self._cache[1])
                return self._cache[1], self._cache[2]
            self._cache = (now_ts, "NEUTRAL", 0.0)
            logger.debug("[btc_regime] fetch empty → NEUTRAL placeholder cache")
            return ("NEUTRAL", 0.0)

        highs, lows, closes = _extract_ohlc(btc_candles)
        regime = self._detect_regime(highs, lows, closes)
        price = closes[-1] if closes else 0.0
        self._cache = (now_ts, regime, price)
        logger.info("[btc_regime] detected: %s (price=%.2f, %d candles)",
                    regime, price, len(closes))
        return regime, price

    def evaluate(
        self, direction: str, btc_candles: Optional[List[Any]], now_ts: float
    ) -> Dict[str, Any]:
        """Conviction delta for the given direction.

        Returns:
            {"delta": int, "regime": str, "price": float}
        """
        out: Dict[str, Any] = {"delta": 0, "regime": "NEUTRAL", "price": 0.0}
        cfg = self.config
        if not getattr(cfg, "btc_regime_enabled", False):
            return out

        regime, price = self._cached_regime(btc_candles, now_ts)
        out["regime"] = regime
        out["price"] = price
        dir_u = direction.upper()

        # [2026-05-17 100-scale ×10] delta table (config-overridable). old ±1/±2 → ±10/±20
        bull_long = float(getattr(cfg, "btc_regime_bull_long_delta", 10.0))
        bull_short = float(getattr(cfg, "btc_regime_bull_short_delta", -20.0))
        bear_long = float(getattr(cfg, "btc_regime_bear_long_delta", -20.0))
        bear_short = float(getattr(cfg, "btc_regime_bear_short_delta", 10.0))
        trans_delta = float(getattr(cfg, "btc_regime_trans_delta", -10.0))

        if regime == "BULL":
            out["delta"] = bull_long if dir_u == "LONG" else bull_short
        elif regime == "BEAR":
            out["delta"] = bear_long if dir_u == "LONG" else bear_short
        elif regime == "TRANS":
            out["delta"] = trans_delta
        # NEUTRAL: 0

        return out
