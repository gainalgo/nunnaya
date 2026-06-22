"""
Cross Exchange Signal Provider
거래소 간 차이 데이터를 전략에 제공하는 중앙 시그널 서비스
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
from decimal import Decimal

logger = logging.getLogger(__name__)


@dataclass
class CrossExchangeSignal:
    """거래소 간 시그널"""
    coin: str                          # "BTC"
    
    # 차익거래 정보
    arbitrage_pct: float               # 최대 차익 % (0.5 = 0.5%)
    arbitrage_direction: str           # "BYBIT→BITHUMB", "BITHUMB→BYBIT", "NONE"
    arbitrage_profit_estimate: float   # 예상 수익 (USDT, 100만 USDT 기준)
    
    # 김치 프리미엄
    kimchi_premium_pct: float          # 김치 프리미엄 % (-2.0 = -2%, 3.0 = +3%)
    kimchi_signal: str                 # "OVERHEATED", "NORMAL", "UNDERVALUED"
    
    # 선행지표 (Binance가 Bybit보다 먼저 움직임)
    leading_signal: Optional[str]      # "UP" (Binance 상승 → Bybit 따라올 것), "DOWN", None
    leading_confidence: float          # 신뢰도 (0.0 ~ 1.0)
    leading_change_pct: float          # Binance 변화율 (최근 5분)
    
    # 유동성
    bybit_volume_24h: float            # Bybit 24시간 거래량 (USDT)
    binance_volume_24h: float          # Binance 24시간 거래량 (USDT)
    liquidity_score: float             # 유동성 점수 (0~1, 1=매우 좋음)
    
    # 메타
    timestamp: float                   # 시그널 생성 시각
    data_age_sec: float                # 데이터 나이 (초)


class CrossExchangeSignalProvider:
    """
    거래소 간 시그널 제공자
    
    Cross Exchange Monitor의 데이터를 전략에서 쉽게 사용할 수 있도록
    정제하여 제공하는 서비스
    """
    
    def __init__(self):
        self._signals: Dict[str, CrossExchangeSignal] = {}
        self._last_update: float = 0.0
        self._cache_ttl_sec: float = 10.0  # 10초 캐시
    
    def update_from_monitor(self, monitor_data: Dict[str, Any]):
        """
        Cross Exchange Monitor로부터 데이터 업데이트
        
        Args:
            monitor_data: {
                "arbitrage": [ArbitrageOpportunity, ...],
                "kimchi": [KimchiPremium, ...],
                "leading": [LeadingIndicatorSignal, ...],
                "prices": {...}
            }
        """
        try:
            now = time.time()
            
            # 기존 시그널 초기화 (오래된 것 제거)
            self._clean_stale_signals(now)
            
            # 차익거래 데이터 처리
            arbitrage_map = self._process_arbitrage(monitor_data.get("arbitrage", []))
            
            # 김치 프리미엄 데이터 처리
            kimchi_map = self._process_kimchi(monitor_data.get("kimchi", []))
            
            # 선행지표 데이터 처리
            leading_map = self._process_leading(monitor_data.get("leading", []))
            
            # 가격 데이터 처리
            prices = monitor_data.get("prices", {})
            volume_map = self._process_volumes(prices)
            
            # 통합 시그널 생성
            all_coins = set()
            all_coins.update(arbitrage_map.keys())
            all_coins.update(kimchi_map.keys())
            all_coins.update(leading_map.keys())
            all_coins.update(volume_map.keys())
            
            for coin in all_coins:
                signal = CrossExchangeSignal(
                    coin=coin,
                    
                    # 차익거래
                    arbitrage_pct=arbitrage_map.get(coin, {}).get("pct", 0.0),
                    arbitrage_direction=arbitrage_map.get(coin, {}).get("direction", "NONE"),
                    arbitrage_profit_estimate=arbitrage_map.get(coin, {}).get("profit", 0.0),
                    
                    # 김치 프리미엄
                    kimchi_premium_pct=kimchi_map.get(coin, {}).get("premium_pct", 0.0),
                    kimchi_signal=kimchi_map.get(coin, {}).get("signal", "NORMAL"),
                    
                    # 선행지표
                    leading_signal=leading_map.get(coin, {}).get("signal"),
                    leading_confidence=leading_map.get(coin, {}).get("confidence", 0.0),
                    leading_change_pct=leading_map.get(coin, {}).get("change_pct", 0.0),
                    
                    # 유동성
                    bybit_volume_24h=volume_map.get(coin, {}).get("bybit_volume", 0.0),
                    binance_volume_24h=volume_map.get(coin, {}).get("binance_volume", 0.0),
                    liquidity_score=volume_map.get(coin, {}).get("liquidity_score", 0.5),
                    
                    timestamp=now,
                    data_age_sec=0.0
                )
                
                self._signals[coin] = signal
            
            self._last_update = now
            logger.debug(f"Updated {len(self._signals)} cross-exchange signals")
            
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
            logger.error(f"Failed to update cross-exchange signals: {e}", exc_info=True)
    
    def update_signal(
        self,
        coin: str,
        liquidity_score: float,
        arbitrage_pct: float = 0.0,
        arbitrage_direction: str = "",
        kimchi_premium_pct: float = 0.0,
        leading_signal: str = "",
        leading_confidence: float = 0.0,
        leading_change_pct: float = 0.0
    ):
        """
        개별 코인 시그널 업데이트 (Monitor에서 직접 호출)
        
        Args:
            coin: 코인 심볼 (예: "BTC")
            liquidity_score: 유동성 점수 (0~1)
            arbitrage_pct: 차익거래 % (기본 0.0)
            arbitrage_direction: 차익 방향 (기본 빈 문자열)
            kimchi_premium_pct: 김치 프리미엄 % (기본 0.0)
            leading_signal: 선행지표 ("UP", "DOWN", 빈 문자열)
            leading_confidence: 선행지표 신뢰도 (0~1)
            leading_change_pct: 선행 변화율 % (기본 0.0)
        """
        try:
            clean_coin = coin.upper().replace("USDT", "").replace("_USDT", "")
            now = time.time()
            
            # 김치 시그널 계산
            kimchi_signal = "NORMAL"
            if kimchi_premium_pct > 5.0:
                kimchi_signal = "OVERHEATED"
            elif kimchi_premium_pct < -1.0:
                kimchi_signal = "UNDERVALUED"
            
            signal = CrossExchangeSignal(
                coin=clean_coin,
                arbitrage_pct=arbitrage_pct,
                arbitrage_direction=arbitrage_direction or "NONE",
                arbitrage_profit_estimate=0.0,  # Monitor에서 계산 안함
                kimchi_premium_pct=kimchi_premium_pct,
                kimchi_signal=kimchi_signal,
                leading_signal=leading_signal if leading_signal else None,
                leading_confidence=leading_confidence,
                leading_change_pct=leading_change_pct,
                bybit_volume_24h=0.0,  # 간소화
                binance_volume_24h=0.0,  # 간소화
                liquidity_score=liquidity_score,
                timestamp=now,
                data_age_sec=0.0
            )
            
            self._signals[clean_coin] = signal
            self._last_update = now
            
        except (OSError, AttributeError, TypeError, ValueError, OverflowError) as e:
            logger.error(f"Failed to update signal for {coin}: {e}")
    
    def get_signal(self, coin: str) -> Optional[CrossExchangeSignal]:
        """
        특정 코인의 시그널 조회
        
        Args:
            coin: 코인 심볼 (예: "BTC", "ETH")
        
        Returns:
            CrossExchangeSignal or None
        """
        # 코인 이름 정규화 (USDT-BTC → BTC)
        clean_coin = coin.upper().replace("USDT", "").replace("_USDT", "")
        
        signal = self._signals.get(clean_coin)
        
        if signal:
            # 데이터 나이 업데이트
            signal.data_age_sec = time.time() - signal.timestamp
            
            # 너무 오래되면 None 반환
            if signal.data_age_sec > 60.0:  # 1분 이상
                return None
        
        return signal
    
    def get_all_signals(self) -> Dict[str, CrossExchangeSignal]:
        """모든 시그널 조회 (최신 것만)"""
        now = time.time()
        return {
            coin: signal 
            for coin, signal in self._signals.items() 
            if now - signal.timestamp < 60.0
        }
    
    def _clean_stale_signals(self, now: float):
        """오래된 시그널 제거 (5분 이상)"""
        stale_coins = [
            coin for coin, signal in self._signals.items()
            if now - signal.timestamp > 300.0
        ]
        for coin in stale_coins:
            del self._signals[coin]
    
    def _process_arbitrage(self, opportunities: List[Any]) -> Dict[str, Dict]:
        """차익거래 기회 처리"""
        result = {}
        for opp in opportunities:
            coin = opp.coin
            if coin not in result or abs(opp.diff_pct) > abs(result[coin]["pct"]):
                result[coin] = {
                    "pct": opp.diff_pct,
                    "direction": f"{opp.buy_exchange}→{opp.sell_exchange}",
                    "profit": float(opp.profit_estimate)
                }
        return result
    
    def _process_kimchi(self, kimchi_list: List[Any]) -> Dict[str, Dict]:
        """김치 프리미엄 처리"""
        result = {}
        for k in kimchi_list:
            result[k.coin] = {
                "premium_pct": k.premium_pct,
                "signal": k.signal
            }
        return result
    
    def _process_leading(self, leading_list: List[Any]) -> Dict[str, Dict]:
        """선행지표 처리"""
        result = {}
        for lead in leading_list:
            coin = lead.coin
            # 최신/신뢰도 높은 것만
            if coin not in result or lead.confidence > result[coin]["confidence"]:
                result[coin] = {
                    "signal": lead.direction,  # "UP" or "DOWN"
                    "confidence": lead.confidence,
                    "change_pct": lead.leader_change_pct
                }
        return result
    
    def _process_volumes(self, prices: Dict[str, Dict]) -> Dict[str, Dict]:
        """거래량 처리"""
        result = {}
        
        for coin in prices.get("BYBIT", {}).keys():
            bybit_ticker = prices.get("BYBIT", {}).get(coin)
            binance_ticker = prices.get("BINANCE", {}).get(coin)
            
            bybit_vol = float(bybit_ticker.volume_24h) if bybit_ticker else 0.0
            binance_vol = float(binance_ticker.volume_24h) if binance_ticker else 0.0
            
            # 유동성 점수: 거래량이 클수록 좋음
            # 단순화: 양쪽 거래소 거래량 합산 기준
            total_vol = bybit_vol + binance_vol
            liquidity_score = min(1.0, total_vol / 1_000_000.0)  # 1M USDT 기준
            
            result[coin] = {
                "bybit_volume": bybit_vol,
                "binance_volume": binance_vol,
                "liquidity_score": liquidity_score
            }
        
        return result
    
    def get_stats(self) -> Dict[str, Any]:
        """통계 정보"""
        now = time.time()
        active_signals = [s for s in self._signals.values() if now - s.timestamp < 60.0]
        
        return {
            "total_signals": len(active_signals),
            "last_update": self._last_update,
            "data_age_sec": now - self._last_update if self._last_update > 0 else 999.0,
            "coins": list(self._signals.keys())
        }


# 싱글톤 인스턴스
_signal_provider = CrossExchangeSignalProvider()


def get_cross_exchange_signal_provider() -> CrossExchangeSignalProvider:
    """글로벌 시그널 제공자 반환"""
    return _signal_provider


def get_signal(coin: str) -> Optional[CrossExchangeSignal]:
    """편의 함수: 코인 시그널 조회"""
    return _signal_provider.get_signal(coin)
