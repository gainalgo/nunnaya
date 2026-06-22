"""BTC Regime — BTC 4H 추세 판정 + 역방향 진입 페널티.

관찰 (2026-04-18):
- 개별 코인 H4 만 보면 전환점을 못 읽음 → BTC 전체 레짐이 근본적 분기
- ETH SHORT 계속 털리는 동안 BTC 는 이미 반등 전환 (역방향 단타)
- 레짐은 4단계:
    BULL    — EMA20 상승 + 가격 > EMA50 + swing high 상승
    BEAR    — EMA20 하락 + 가격 < EMA50 + swing low 하락
    TRANS   — EMA 기울기 반전 or 가격 ±1% 내에서 EMA50 를 왕복 (= 가장 비싼 장)
    NEUTRAL — 위 조건 미해당 (횡보)

효과 (delta):
    BULL  × LONG  = +1    BEAR × LONG  = -2
    BULL  × SHORT = -2    BEAR × SHORT = +1
    TRANS × any   = -1   (전환점 = 학비 비싸니까 보수적)
    NEUT  × any   =  0

입력: BTC H4 캔들 리스트 — 두 형식 지원
    (a) Bybit raw: [[start_ts, o, h, l, c, v, ...], ...]
    (b) OHLCV objects: list of obj with .high/.low/.close
    (c) 단순 closes: list of float (EMA 만 계산, swing 분석 스킵 → 약한 판정)

캐시: regime 판정은 매 tick 할 필요 없음 → 10분 TTL.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _extract_ohlc(candles: List[Any]) -> Tuple[List[float], List[float], List[float]]:
    """다양한 포맷에서 (highs, lows, closes) 추출. 실패 시 빈 리스트."""
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
        """핵심 레짐 판정. 반환: "BULL"|"BEAR"|"TRANS"|"NEUTRAL" """
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

        # EMA20 slope (최근 5봉 기울기)
        slope_len = min(5, len(ema20_s) - 1)
        if slope_len <= 0:
            return "NEUTRAL"
        ema20_past = ema20_s[-1 - slope_len]
        slope_pct = (ema20_now - ema20_past) / ema20_past if ema20_past else 0.0
        # slope_pct 기준: ±flat_thr_pct 이내 = flat (config A/B 테스트 가능)
        flat_thr = float(getattr(cfg, "btc_regime_slope_flat_thr_pct", 0.3)) / 100.0

        # TRANS 판정: 가격이 EMA50 근처 (±trans_band_pct) + slope flat
        near_ema50 = abs(price - ema50_now) / ema50_now < trans_band_pct
        if near_ema50 and abs(slope_pct) < flat_thr:
            return "TRANS"

        # 최근 slope 반전 감지 (5봉 전 slope vs 현재 slope 부호 반전)
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
        """캐시 고려 레짐. 반환: (regime, price)"""
        cfg = self.config
        ttl = float(getattr(cfg, "btc_regime_cache_ttl_sec", 600.0))
        if self._cache and (now_ts - self._cache[0]) < ttl:
            return self._cache[1], self._cache[2]

        # [2026-04-19 형 검수 CE#5] fetch 실패(빈 candles) 시
        # 이전 캐시가 있으면 stale 그대로 사용, 없으면 NEUTRAL 임시 캐시 (10분간 재시도 방지)
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
        """direction 에 대한 conviction delta.

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

        # [2026-05-17 100점 ×10] delta table (config-overridable). 옛 ±1/±2 → ±10/±20
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
