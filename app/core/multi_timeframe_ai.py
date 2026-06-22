# ============================================================
# File: app/core/multi_timeframe_ai.py
# Multi-Timeframe AI Selector
# ------------------------------------------------------------
# 여러 타임프레임(5분, 15분, 1시간)에서 AI 점수를 계산하고
# 가장 높은 점수의 타임프레임을 자동 선택합니다.
#
# Created: 2026-01-31
# ============================================================

from __future__ import annotations

import time
import logging
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from threading import Lock
import requests

from app.core.rate_limiter import bybit_get
from app.core.constants import BYBIT_MARKET_KLINE, DEFAULT_REQUEST_TIMEOUT_SEC, bybit_v5_rest_category
from app.strategy import indicators

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

# 지원하는 타임프레임 (분 단위)
TIMEFRAMES = [5, 15, 60]  # 5분, 15분, 1시간
TIMEFRAME_LABELS = {5: "5m", 15: "15m", 60: "1h"}

# 캐시 설정
CACHE_TTL_SEC = 60.0  # 1분 캐시
MAX_CANDLE_COUNT = 100  # 최대 캔들 수

# RSI 임계값
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0


# ============================================================
# Data Classes
# ============================================================

@dataclass
class TimeframeScore:
    """단일 타임프레임의 AI 점수."""
    timeframe_min: int
    label: str
    ai_score: float
    rsi: float
    macd_histogram: float
    trend: float
    momentum: float
    volatility: float
    volume_change_pct: float
    signal: str  # "buy", "sell", "hold"
    confidence: float
    candle_count: int
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timeframe_min": self.timeframe_min,
            "label": self.label,
            "ai_score": round(self.ai_score, 4),
            "rsi": round(self.rsi, 2),
            "macd_histogram": round(self.macd_histogram, 6),
            "trend": round(self.trend, 4),
            "momentum": round(self.momentum, 4),
            "volatility": round(self.volatility, 4),
            "volume_change_pct": round(self.volume_change_pct, 2),
            "signal": self.signal,
            "confidence": round(self.confidence, 4),
            "candle_count": self.candle_count,
            "updated_at": self.updated_at,
        }


@dataclass
class MultiTimeframeResult:
    """다중 타임프레임 분석 결과."""
    market: str
    best_timeframe: TimeframeScore
    all_timeframes: List[TimeframeScore]
    selection_reason: str
    computed_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market": self.market,
            "best_timeframe": self.best_timeframe.to_dict(),
            "all_timeframes": [tf.to_dict() for tf in self.all_timeframes],
            "selection_reason": self.selection_reason,
            "computed_at": self.computed_at,
        }


# ============================================================
# Candle Fetcher
# ============================================================

def _normalize_market(market: str) -> str:
    """Normalize market format to XXXUSDT."""
    from app.core.currency import Q
    return Q.normalize(str(market).upper().strip())


def fetch_candles(
    market: str,
    unit: int,
    count: int = 60,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    """Fetch minute candles from Bybit API.
    
    Args:
        market: Market code (e.g., "BTCUSDT")
        unit: Candle unit in minutes (1, 3, 5, 15, 30, 60, 240)
        count: Number of candles to fetch (max 200)
        timeout: Request timeout in seconds
        session: Optional requests session for connection reuse
    
    Returns:
        List of candle dictionaries (newest first from API, reversed to oldest first)
    """
    m = _normalize_market(market)
    if not m:
        return []

    try:
        from app.core.constants import parse_bybit_list
        r = bybit_get(
            BYBIT_MARKET_KLINE,
            params={
                "category": bybit_v5_rest_category(),
                "symbol": m,
                "interval": str(unit),
                "limit": str(min(count, 200)),
            },
            timeout=float(timeout),
        )
        r.raise_for_status()
        raw = parse_bybit_list(r.json())
        if not raw:
            return []
        # Bybit returns newest first → reverse to oldest first
        # Each row: [startTime, open, high, low, close, volume, turnover]
        result = []
        for k in reversed(raw):
            if isinstance(k, (list, tuple)) and len(k) >= 6:
                result.append({
                    "opening_price": float(k[1]),
                    "high_price": float(k[2]),
                    "low_price": float(k[3]),
                    "trade_price": float(k[4]),
                    "candle_acc_trade_volume": float(k[5]),
                    "timestamp": int(k[0]),
                })
        return result
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.warning("fetch_candles(%s, %sm) network error: %s", m, unit, e)
        return []
    except requests.HTTPError as e:
        logger.warning("fetch_candles(%s, %sm) HTTP %s", m, unit, e.response.status_code if e.response else "?")
        return []
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("fetch_candles(%s, %sm): %s", m, unit, e)
        return []


# ============================================================
# Indicator Calculation
# ============================================================

def _extract_prices_from_candles(candles: List[Dict[str, Any]]) -> List[float]:
    """Extract closing prices from candles (oldest first)."""
    prices = []
    for c in candles:
        p = c.get("trade_price") or c.get("close")
        if p:
            try:
                prices.append(float(p))
            except (TypeError, ValueError):
                logger.warning("[MultiTF] Invalid price value in candle: %r", p)
                continue
    return prices


def _extract_volumes_from_candles(candles: List[Dict[str, Any]]) -> List[float]:
    """Extract volumes from candles (oldest first)."""
    volumes = []
    for c in candles:
        v = c.get("candle_acc_trade_volume") or c.get("volume")
        if v:
            try:
                volumes.append(float(v))
            except (TypeError, ValueError):
                logger.warning("[MultiTF] Invalid volume value in candle: %r", v)
                continue
    return volumes


def calculate_timeframe_score(
    market: str,
    candles: List[Dict[str, Any]],
    timeframe_min: int,
) -> Optional[TimeframeScore]:
    """Calculate AI score for a single timeframe.
    
    Args:
        market: Market code
        candles: List of candle data (oldest first)
        timeframe_min: Timeframe in minutes
    
    Returns:
        TimeframeScore or None if insufficient data
    """
    if not candles or len(candles) < 14:
        return None
    
    prices = _extract_prices_from_candles(candles)
    volumes = _extract_volumes_from_candles(candles)
    
    if len(prices) < 14:
        return None
    
    # Current price
    current_price = prices[-1] if prices else 0.0
    if current_price <= 0:
        return None
    
    # --- Indicators ---
    
    # RSI (14-period)
    rsi = indicators.rsi(prices, 14) or 50.0
    
    # MACD (12, 26, 9)
    macd_line, signal_line, macd_hist = indicators.macd(prices, 12, 26, 9)
    macd_histogram = 0.0
    if macd_hist is not None and current_price > 0:
        macd_histogram = (float(macd_hist) / current_price) * 100.0
    
    # Trend (price change over period)
    trend_len = min(len(prices), 20)
    trend = indicators.trend(prices, trend_len) or 0.0
    
    # Momentum (short-term)
    momentum = indicators.trend(prices, 3) or 0.0
    
    # Volatility
    vol_len = min(max(0, len(prices) - 1), 14)
    volatility = indicators.volatility(prices, vol_len) or 0.0
    
    # Volume change
    volume_change_pct = 0.0
    if len(volumes) >= 10:
        recent_vol = sum(volumes[-3:]) / 3.0 if len(volumes) >= 3 else 0
        prev_vol = sum(volumes[-10:-3]) / 7.0 if len(volumes) >= 10 else 0
        if prev_vol > 0:
            volume_change_pct = (recent_vol - prev_vol) / prev_vol * 100.0
    
    # --- AI Score Calculation ---
    # 0.0 = strong sell, 0.5 = neutral, 1.0 = strong buy
    
    score_components = []
    
    # RSI component (0.0 ~ 1.0)
    if rsi <= RSI_OVERSOLD:
        rsi_score = 0.8 + (RSI_OVERSOLD - rsi) / RSI_OVERSOLD * 0.2  # 0.8 ~ 1.0
    elif rsi >= RSI_OVERBOUGHT:
        rsi_score = 0.2 - (rsi - RSI_OVERBOUGHT) / (100 - RSI_OVERBOUGHT) * 0.2  # 0.0 ~ 0.2
    else:
        # Linear interpolation between 30-70
        rsi_score = 0.2 + (RSI_OVERBOUGHT - rsi) / (RSI_OVERBOUGHT - RSI_OVERSOLD) * 0.6
    score_components.append(("rsi", rsi_score, 0.30))
    
    # MACD component
    macd_score = 0.5
    if macd_histogram > 0.01:
        macd_score = min(0.5 + macd_histogram * 10, 1.0)
    elif macd_histogram < -0.01:
        macd_score = max(0.5 + macd_histogram * 10, 0.0)
    score_components.append(("macd", macd_score, 0.25))
    
    # Trend component
    trend_score = 0.5 + min(max(trend, -5), 5) / 10.0  # -5% ~ +5% -> 0.0 ~ 1.0
    score_components.append(("trend", trend_score, 0.20))
    
    # Momentum component
    momentum_score = 0.5 + min(max(momentum, -3), 3) / 6.0  # -3% ~ +3% -> 0.0 ~ 1.0
    score_components.append(("momentum", momentum_score, 0.15))
    
    # Volume surge bonus
    volume_score = 0.5
    if volume_change_pct > 50:
        volume_score = 0.7
    elif volume_change_pct > 100:
        volume_score = 0.8
    elif volume_change_pct < -30:
        volume_score = 0.3
    score_components.append(("volume", volume_score, 0.10))
    
    # Weighted average
    ai_score = sum(s * w for _, s, w in score_components)
    ai_score = max(0.0, min(1.0, ai_score))
    
    # Confidence (distance from neutral)
    confidence = abs(ai_score - 0.5) * 2.0
    
    # Signal
    if ai_score >= 0.65:
        signal = "buy"
    elif ai_score <= 0.35:
        signal = "sell"
    else:
        signal = "hold"
    
    return TimeframeScore(
        timeframe_min=timeframe_min,
        label=TIMEFRAME_LABELS.get(timeframe_min, f"{timeframe_min}m"),
        ai_score=ai_score,
        rsi=rsi,
        macd_histogram=macd_histogram,
        trend=trend,
        momentum=momentum,
        volatility=volatility,
        volume_change_pct=volume_change_pct,
        signal=signal,
        confidence=confidence,
        candle_count=len(candles),
    )


# ============================================================
# Multi-Timeframe Selector
# ============================================================

class MultiTimeframeAI:
    """다중 타임프레임 AI 점수 계산 및 최적 타임프레임 선택."""
    
    def __init__(self, timeframes: Optional[List[int]] = None):
        self._timeframes = timeframes or TIMEFRAMES
        self._cache: Dict[str, MultiTimeframeResult] = {}
        self._cache_lock = Lock()
        self._session = None  # bybit_get() handles connection pooling
    
    def analyze(
        self,
        market: str,
        *,
        force_refresh: bool = False,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
    ) -> Optional[MultiTimeframeResult]:
        """Analyze market across multiple timeframes and select the best one.
        
        Args:
            market: Market code (e.g., "BTCUSDT")
            force_refresh: Skip cache and fetch fresh data
            timeout: Request timeout per API call
        
        Returns:
            MultiTimeframeResult or None if analysis fails
        """
        m = _normalize_market(market)
        cache_key = m
        
        # Check cache
        if not force_refresh:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached and (time.time() - cached.computed_at) < CACHE_TTL_SEC:
                    return cached
        
        # Fetch and analyze all timeframes
        scores: List[TimeframeScore] = []
        
        for tf in self._timeframes:
            candles = fetch_candles(
                m,
                unit=tf,
                count=MAX_CANDLE_COUNT,
                timeout=timeout,
                session=self._session,
            )
            
            if not candles:
                logger.debug(f"MultiTimeframeAI: No candles for {m} @ {tf}m")
                continue
            
            score = calculate_timeframe_score(m, candles, tf)
            if score:
                scores.append(score)
        
        if not scores:
            return None
        
        # Select best timeframe
        best, reason = self._select_best_timeframe(scores)
        
        result = MultiTimeframeResult(
            market=m,
            best_timeframe=best,
            all_timeframes=scores,
            selection_reason=reason,
        )
        
        # Update cache
        with self._cache_lock:
            self._cache[cache_key] = result
        
        return result
    
    def _select_best_timeframe(
        self,
        scores: List[TimeframeScore],
    ) -> Tuple[TimeframeScore, str]:
        """Select the best timeframe based on AI score and confidence.
        
        Selection criteria:
        1. Highest AI score (away from neutral 0.5)
        2. Tie-breaker: Higher confidence
        3. Tie-breaker: Shorter timeframe (more responsive)
        
        Returns:
            (best_score, selection_reason)
        """
        if len(scores) == 1:
            return scores[0], "only_available"
        
        # Sort by:
        # 1. Distance from neutral (higher is better for both buy/sell signals)
        # 2. Confidence
        # 3. Shorter timeframe
        def sort_key(s: TimeframeScore) -> Tuple[float, float, int]:
            distance_from_neutral = abs(s.ai_score - 0.5)
            return (-distance_from_neutral, -s.confidence, s.timeframe_min)
        
        sorted_scores = sorted(scores, key=sort_key)
        best = sorted_scores[0]
        
        # Determine reason
        if best.ai_score >= 0.7:
            reason = f"strong_buy_signal_{best.label}"
        elif best.ai_score <= 0.3:
            reason = f"strong_sell_signal_{best.label}"
        elif best.confidence > 0.3:
            reason = f"high_confidence_{best.label}"
        else:
            reason = f"best_available_{best.label}"
        
        return best, reason
    
    def get_cached(self, market: str) -> Optional[MultiTimeframeResult]:
        """Get cached result without fetching."""
        m = _normalize_market(market)
        with self._cache_lock:
            return self._cache.get(m)
    
    def clear_cache(self, market: Optional[str] = None) -> None:
        """Clear cache for a specific market or all markets."""
        with self._cache_lock:
            if market:
                m = _normalize_market(market)
                self._cache.pop(m, None)
            else:
                self._cache.clear()


# ============================================================
# Global Instance
# ============================================================

_mtf_ai: Optional[MultiTimeframeAI] = None
_mtf_lock = Lock()


def get_multi_timeframe_ai() -> MultiTimeframeAI:
    """Get or create the global MultiTimeframeAI instance."""
    global _mtf_ai
    if _mtf_ai is None:
        with _mtf_lock:
            if _mtf_ai is None:
                _mtf_ai = MultiTimeframeAI()
    return _mtf_ai


def analyze_multi_timeframe(
    market: str,
    *,
    force_refresh: bool = False,
) -> Optional[MultiTimeframeResult]:
    """Convenience function to analyze a market with multi-timeframe AI.
    
    Args:
        market: Market code (e.g., "BTCUSDT")
        force_refresh: Skip cache
    
    Returns:
        MultiTimeframeResult or None
    """
    return get_multi_timeframe_ai().analyze(market, force_refresh=force_refresh)
