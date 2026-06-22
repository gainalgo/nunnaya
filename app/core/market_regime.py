"""
Market Regime Detection Module
- 시장 국면(BULL/BEAR/SIDEWAYS/VOLATILE) 인식
- TP/SL 조절을 위한 변동성 스케일 제공
- 예산 배분을 위한 국면별 배수 제공

[MIGRATED 2026-03-31] Bybit USDT
"""

from __future__ import annotations
from enum import Enum
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
import time
import math
import logging

from app.core.constants import env_bool, env_float, env_int
from app.core.hyper_price_store import price_store

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    SIDEWAYS = "SIDEWAYS"
    VOLATILE = "VOLATILE"


@dataclass
class RegimeResult:
    regime: MarketRegime
    confidence: float  # 0.0 ~ 1.0
    atr_pct: float
    vol_pct: float
    ret_pct: float
    momentum: float
    rsi: float
    timestamp: float


class RegimeDetector:
    """시장 국면 감지기"""
    
    def __init__(self):
        self.enabled = env_bool("OMA_REGIME_ENABLED", default=True)
        self.cache_sec = env_float("OMA_REGIME_CACHE_SEC", default=30.0)
        self.min_hold_sec = env_float("OMA_REGIME_MIN_HOLD_SEC", default=300.0)
        self.lookback_ticks = env_int("OMA_REGIME_LOOKBACK_TICKS", default=60)
        
        # 국면 판정 임계치
        self.atr_th = env_float("OMA_REGIME_ATR_TH", default=3.0)
        self.vol_th = env_float("OMA_REGIME_VOL_TH", default=5.0)
        self.bull_ret_th = env_float("OMA_REGIME_BULL_RET_TH", default=3.0)
        self.bear_ret_th = env_float("OMA_REGIME_BEAR_RET_TH", default=3.0)
        
        # 예산 배수
        self.bull_max_mult_x = env_float("OMA_REGIME_BULL_MAX_MULT_X", default=1.25)
        self.bear_max_mult_x = env_float("OMA_REGIME_BEAR_MAX_MULT_X", default=0.70)
        self.volatile_corr_x = env_float("OMA_REGIME_VOLATILE_CORR_X", default=1.50)
        
        # 캐시
        self._cache: Dict[str, RegimeResult] = {}
        self._last_regime: Optional[MarketRegime] = None
        self._regime_since: float = 0.0
        self._data_warning_logged: bool = False
    
    def detect(self, market: str = "BTCUSDT") -> RegimeResult:
        """시장 국면 감지."""
        if not self.enabled:
            return RegimeResult(
                regime=MarketRegime.SIDEWAYS,
                confidence=0.0,
                atr_pct=0.0,
                vol_pct=0.0,
                ret_pct=0.0,
                momentum=0.0,
                rsi=50.0,
                timestamp=time.time()
            )
        
        now = time.time()
        
        # 캐시 확인
        cached = self._cache.get(market)
        if cached and (now - cached.timestamp) < self.cache_sec:
            return cached
        
        # 가격 데이터 가져오기
        prices = self._get_prices(market, count=self.lookback_ticks)
        if not prices or len(prices) < 10:
            result = self._default_result()
            self._cache[market] = result
            if not self._data_warning_logged:
                logger.warning("[MarketRegime] Warmup: price data < 10 samples for %s, using defaults", market)
                self._data_warning_logged = True
            return result
        
        # 지표 계산
        atr_pct = self._calc_atr_pct(prices)
        vol_pct = self._calc_volatility_pct(prices)
        ret_pct = self._calc_return_pct(prices)
        momentum = self._calc_momentum(prices)
        rsi = self._calc_rsi(prices)
        
        # 국면 판정
        regime = self._determine_regime(atr_pct, vol_pct, ret_pct, momentum, rsi)
        
        # 최소 홀드 시간 체크
        if self._last_regime and regime != self._last_regime:
            if (now - self._regime_since) < self.min_hold_sec:
                regime = self._last_regime
            else:
                self._last_regime = regime
                self._regime_since = now
        else:
            self._last_regime = regime
            if self._regime_since == 0:
                self._regime_since = now
        
        result = RegimeResult(
            regime=regime,
            confidence=0.8,
            atr_pct=atr_pct,
            vol_pct=vol_pct,
            ret_pct=ret_pct,
            momentum=momentum,
            rsi=rsi,
            timestamp=now
        )
        
        self._cache[market] = result
        return result
    
    def _get_prices(self, market: str, count: int) -> List[float]:
        """price_store에서 가격 히스토리 가져오기 (fallback 포함)"""
        if hasattr(price_store, 'get_prices'):
            prices = price_store.get_prices(market, count=count)
            if prices:
                return prices
        
        # fallback: 현재 가격만 반환
        current = price_store.get_price(market)
        if current:
            return [current]
        return []
    
    def _determine_regime(self, atr_pct: float, vol_pct: float, ret_pct: float, momentum: float, rsi: float) -> MarketRegime:
        # VOLATILE: 변동성 급증
        if atr_pct >= self.atr_th or vol_pct >= self.vol_th:
            return MarketRegime.VOLATILE
        
        # BULL: 상승 추세
        if ret_pct >= self.bull_ret_th and (momentum > 0 or rsi >= 55):
            return MarketRegime.BULL
        
        # BEAR: 하락 추세
        if ret_pct <= -self.bear_ret_th and (momentum < 0 or rsi <= 45):
            return MarketRegime.BEAR
        
        # SIDEWAYS: 횡보
        return MarketRegime.SIDEWAYS
    
    def _calc_atr_pct(self, prices: List[float]) -> float:
        if len(prices) < 2:
            return 0.0
        trs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
        atr = sum(trs) / len(trs) if trs else 0.0
        avg_price = sum(prices) / len(prices)
        return (atr / avg_price * 100) if avg_price > 0 else 0.0
    
    def _calc_volatility_pct(self, prices: List[float]) -> float:
        if len(prices) < 2:
            return 0.0
        avg = sum(prices) / len(prices)
        variance = sum((p - avg) ** 2 for p in prices) / len(prices)
        std = math.sqrt(variance)
        return (std / avg * 100) if avg > 0 else 0.0
    
    def _calc_return_pct(self, prices: List[float]) -> float:
        if len(prices) < 2:
            return 0.0
        first, last = prices[0], prices[-1]
        return ((last - first) / first * 100) if first > 0 else 0.0
    
    def _calc_momentum(self, prices: List[float]) -> float:
        if len(prices) < 10:
            return 0.0
        mid = len(prices) // 2
        first_half = sum(prices[:mid]) / mid
        second_half = sum(prices[mid:]) / (len(prices) - mid)
        return second_half - first_half
    
    def _calc_rsi(self, prices: List[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, min(period + 1, len(prices))):
            diff = prices[i] - prices[i-1]
            if diff > 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(diff))
        avg_gain = sum(gains) / len(gains) if gains else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    def _default_result(self) -> RegimeResult:
        return RegimeResult(
            regime=MarketRegime.SIDEWAYS,
            confidence=0.0,
            atr_pct=0.0,
            vol_pct=0.0,
            ret_pct=0.0,
            momentum=0.0,
            rsi=50.0,
            timestamp=time.time()
        )
    
    def get_budget_multiplier(self, regime: MarketRegime) -> float:
        """국면별 예산 배수 반환"""
        if regime == MarketRegime.BULL:
            return self.bull_max_mult_x
        elif regime == MarketRegime.BEAR:
            return self.bear_max_mult_x
        return 1.0
    
    def get_tp_sl_scale(self, regime: MarketRegime) -> Dict[str, float]:
        """국면별 TP/SL 스케일 반환"""
        scales = {
            MarketRegime.BULL: {"sl": 1.20, "tp": 1.25, "trail": 1.0},
            MarketRegime.BEAR: {"sl": 0.75, "tp": 0.70, "trail": 1.0},
            MarketRegime.SIDEWAYS: {"sl": 0.90, "tp": 0.85, "trail": 1.0},
            MarketRegime.VOLATILE: {"sl": 1.30, "tp": 1.10, "trail": 1.3},
        }
        return scales.get(regime, {"sl": 1.0, "tp": 1.0, "trail": 1.0})


# 싱글톤
_regime_detector: Optional[RegimeDetector] = None

def get_regime_detector() -> RegimeDetector:
    global _regime_detector
    if _regime_detector is None:
        _regime_detector = RegimeDetector()
    return _regime_detector
