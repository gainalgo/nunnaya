# ============================================================
# File: app/backtest/strategy_simulator.py
# Autocoin OS v3-H — Strategy Simulator for Backtesting
# ------------------------------------------------------------
# 각 전략별 매매 시그널 생성 (간소화된 로직)
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class StrategySimulator:
    """전략 시뮬레이터 (백테스팅용) - 간소화된 독립 로직"""
    
    def __init__(self, strategy_name: str):
        self.strategy_name = strategy_name.upper()
        self.strategy = self.strategy_name  # alias for backwards compat

        supported = ["PINGPONG", "AUTOLOOP", "LADDER", "LIGHTNING", "GAZUA", "CONTRARIAN", "SNIPER"]
        if self.strategy_name not in supported:
            raise ValueError(f"Unsupported strategy: {strategy_name}. Supported: {supported}")
    
    def generate_entry_signal(
        self,
        candles: List[Dict[str, Any]],
        current_idx: int
    ) -> Optional[Dict[str, Any]]:
        """진입 시그널 생성
        
        Args:
            candles: 캔들 데이터 (과거부터 정렬)
            current_idx: 현재 캔들 인덱스
        
        Returns:
            진입 시그널 정보 또는 None
            {
                "entry_price": float,
                "tp_pct": float,
                "sl_pct": float,
                "reason": str
            }
        """
        if current_idx < 20:  # 최소 20개 캔들 필요 (지표 계산용)
            return None
        
        # 현재까지의 캔들로 컨텍스트 구성
        recent_candles = candles[max(0, current_idx - 100):current_idx + 1]
        current_candle = candles[current_idx]
        
        try:
            # 가격 및 지표 계산
            close_prices = [c["trade_price"] for c in recent_candles]
            volumes = [c["candle_acc_trade_volume"] for c in recent_candles]
            
            current_price = current_candle["trade_price"]
            
            # 전략별 진입 조건 체크 (간단 버전)
            if self.strategy_name == "PINGPONG":
                return self._check_pingpong_entry(close_prices, current_price)
            elif self.strategy_name == "AUTOLOOP":
                return self._check_autoloop_entry(close_prices, current_price)
            elif self.strategy_name == "LADDER":
                return self._check_ladder_entry(close_prices, current_price)
            elif self.strategy_name == "LIGHTNING":
                return self._check_lightning_entry(close_prices, volumes, current_price)
            elif self.strategy_name == "GAZUA":
                return self._check_gazua_entry(close_prices, current_price)
            elif self.strategy_name == "CONTRARIAN":
                return self._check_contrarian_entry(close_prices, current_price)
            elif self.strategy_name == "SNIPER":
                return self._check_sniper_entry(close_prices, current_price)
            
        except (KeyError, AttributeError, TypeError) as e:
            logger.error(f"Error generating entry signal: {e}")
        
        return None
    
    def _check_pingpong_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """PINGPONG 진입 조건 (단순 버전)"""
        if len(prices) < 20:
            return None
        
        # 20일 평균
        ma20 = sum(prices[-20:]) / 20
        
        # 가격이 평균 아래 2% → 진입
        if current_price < ma20 * 0.98:
            return {
                "entry_price": current_price,
                "tp_pct": 3.0,
                "sl_pct": -5.0,
                "reason": "below_ma20"
            }
        
        return None
    
    def _check_autoloop_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """AUTOLOOP 진입 조건"""
        if len(prices) < 30:
            return None
        
        # 변동성 체크
        recent_high = max(prices[-30:])
        recent_low = min(prices[-30:])
        volatility = (recent_high - recent_low) / recent_low * 100
        
        # 변동성 10% 이상 + 중간 가격대
        mid_price = (recent_high + recent_low) / 2
        
        if volatility >= 10 and abs(current_price - mid_price) / mid_price < 0.05:
            return {
                "entry_price": current_price,
                "tp_pct": 5.0,
                "sl_pct": -7.0,
                "reason": "high_volatility"
            }
        
        return None
    
    def _check_ladder_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """LADDER 진입 조건 (하락 추세)"""
        if len(prices) < 20:
            return None
        
        # 최근 10일 하락 추세
        ma10_old = sum(prices[-20:-10]) / 10
        ma10_new = sum(prices[-10:]) / 10
        
        if ma10_new < ma10_old * 0.95:  # 5% 하락
            return {
                "entry_price": current_price,
                "tp_pct": 10.0,
                "sl_pct": -15.0,
                "reason": "downtrend"
            }
        
        return None
    
    def _check_lightning_entry(self, prices: List[float], volumes: List[float], current_price: float) -> Optional[Dict]:
        """LIGHTNING 진입 조건 (급등)"""
        if len(prices) < 10:
            return None
        
        # 최근 5분봉 급등 체크 (2% 이상)
        price_change = (prices[-1] - prices[-5]) / prices[-5] * 100
        
        # 거래량 급증
        avg_volume = sum(volumes[-20:-5]) / 15 if len(volumes) >= 20 else sum(volumes) / len(volumes)
        current_volume = volumes[-1]
        
        if price_change >= 2.0 and current_volume > avg_volume * 2:
            return {
                "entry_price": current_price,
                "tp_pct": 5.0,
                "sl_pct": -10.0,
                "reason": "surge"
            }
        
        return None
    
    def _check_gazua_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """GAZUA 진입 조건 (강한 상승)"""
        if len(prices) < 30:
            return None
        
        # 최근 20일 상승 추세
        ma20 = sum(prices[-20:]) / 20
        
        if current_price > ma20 * 1.1:  # 평균 대비 10% 상승
            return {
                "entry_price": current_price,
                "tp_pct": 25.0,
                "sl_pct": -25.0,
                "reason": "strong_uptrend"
            }
        
        return None
    
    def _check_contrarian_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """CONTRARIAN 진입 조건 (과매도)"""
        if len(prices) < 30:
            return None
        
        # RSI 계산 (간단 버전)
        changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [max(c, 0) for c in changes[-14:]]
        losses = [abs(min(c, 0)) for c in changes[-14:]]
        
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        
        if avg_loss == 0:
            return None
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        # RSI < 30 (과매도)
        if rsi < 30:
            return {
                "entry_price": current_price,
                "tp_pct": 15.0,
                "sl_pct": -70.0,  # 극한 SL
                "reason": "oversold"
            }
        
        return None
    
    def _check_sniper_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """SNIPER 진입 조건 (급등 초기)"""
        if len(prices) < 20:
            return None
        
        # 최근 3분봉 급등 (1.5% 이상)
        price_change = (prices[-1] - prices[-3]) / prices[-3] * 100
        
        # EMA 위에 있음
        ema20 = self._calc_ema(prices, 20)
        
        if price_change >= 1.5 and current_price > ema20:
            return {
                "entry_price": current_price,
                "tp_pct": 3.0,
                "sl_pct": -5.0,
                "reason": "early_surge"
            }
        
        return None
    
    def _calc_ema(self, prices: List[float], period: int) -> float:
        """EMA 계산"""
        if len(prices) < period:
            return sum(prices) / len(prices)
        
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
