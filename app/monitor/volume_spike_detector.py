"""
Volume Spike Detector
거래량 급등 감지 - 급등락 선행 신호
"""

import logging
import time
from typing import Dict, List, Optional, Any
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class VolumeSpikeSignal:
    """거래량 급등 신호"""
    market: str
    volume_24h: float
    avg_volume_7d: float
    spike_ratio: float  # volume_24h / avg_volume_7d
    price_change_24h: float
    confidence: float  # 0.0 ~ 1.0
    direction: str  # 'bullish' | 'bearish' | 'neutral'
    timestamp: float


class VolumeSpikeDetector:
    """
    거래량 급등 감지기
    
    전략:
    - 24시간 거래량 > 7일 평균 × 3배 → 강한 신호
    - 24시간 거래량 > 7일 평균 × 2배 → 중간 신호
    - 가격 동반 상승 → 상승 신호
    - 가격 동반 하락 → 하락 신호
    """
    
    def __init__(
        self,
        trade_client: Any,
        spike_threshold: float = 3.0,  # 3배 이상
        medium_threshold: float = 2.0,  # 2배 이상
        history_size: int = 100,
    ):
        self.trade_client = trade_client
        self.spike_threshold = spike_threshold
        self.medium_threshold = medium_threshold
        
        # 마켓별 거래량 히스토리
        self.volume_history: Dict[str, deque] = {}
        self.history_size = history_size
        
        # 최근 신호 캐시 (중복 방지)
        self.recent_signals: Dict[str, VolumeSpikeSignal] = {}
        self.signal_cooldown_sec = 3600  # 1시간
        
        logger.info(
            f"VolumeSpike: spike={spike_threshold}x, "
            f"medium={medium_threshold}x"
        )
    
    def update_volume_data(self, markets: List[str]) -> None:
        """
        마켓별 거래량 데이터 업데이트
        """
        try:
            for market in markets:
                # 일봉 데이터 (7일)
                candles = self.trade_client.get_candles_daily(market, count=7)
                if not candles or len(candles) < 7:
                    continue
                
                if market not in self.volume_history:
                    self.volume_history[market] = deque(maxlen=self.history_size)
                
                # 7일 평균 거래량
                volumes = [float(c.get("candle_acc_trade_volume", 0)) for c in candles]
                avg_volume_7d = sum(volumes) / len(volumes)
                
                # 24시간 거래량 (최신 캔들)
                volume_24h = float(candles[0].get("candle_acc_trade_volume", 0))
                
                self.volume_history[market].append({
                    "timestamp": time.time(),
                    "volume_24h": volume_24h,
                    "avg_volume_7d": avg_volume_7d,
                    "price": float(candles[0].get("trade_price", 0)),
                })
                
        except (OSError, KeyError, IndexError, AttributeError, TypeError, ValueError, OverflowError) as e:
            logger.error(f"VolumeSpike: update error - {e}")
    
    def detect_spikes(self) -> List[VolumeSpikeSignal]:
        """
        거래량 급등 감지
        """
        signals = []
        now = time.time()
        
        try:
            for market, history in self.volume_history.items():
                if not history or len(history) < 2:
                    continue
                
                latest = history[-1]
                volume_24h = latest["volume_24h"]
                avg_volume_7d = latest["avg_volume_7d"]
                
                if avg_volume_7d <= 0:
                    continue
                
                spike_ratio = volume_24h / avg_volume_7d
                
                # 임계값 미달
                if spike_ratio < self.medium_threshold:
                    continue
                
                # 최근 신호 중복 체크
                if market in self.recent_signals:
                    prev_signal = self.recent_signals[market]
                    if (now - prev_signal.timestamp) < self.signal_cooldown_sec:
                        continue
                
                # 가격 변화 계산
                price_change_24h = 0.0
                if len(history) >= 2:
                    prev = history[-2]
                    price_change_24h = (
                        (latest["price"] / prev["price"] - 1.0) * 100
                    ) if prev["price"] > 0 else 0.0
                
                # 방향성 판단
                direction = "neutral"
                confidence = 0.5
                
                if spike_ratio >= self.spike_threshold:
                    confidence = 0.9
                    if price_change_24h > 5.0:
                        direction = "bullish"
                    elif price_change_24h < -5.0:
                        direction = "bearish"
                elif spike_ratio >= self.medium_threshold:
                    confidence = 0.7
                    if price_change_24h > 3.0:
                        direction = "bullish"
                    elif price_change_24h < -3.0:
                        direction = "bearish"
                
                signal = VolumeSpikeSignal(
                    market=market,
                    volume_24h=volume_24h,
                    avg_volume_7d=avg_volume_7d,
                    spike_ratio=spike_ratio,
                    price_change_24h=price_change_24h,
                    confidence=confidence,
                    direction=direction,
                    timestamp=now,
                )
                
                signals.append(signal)
                self.recent_signals[market] = signal
                
                logger.info(
                    f"VolumeSpike: {market} - "
                    f"Ratio={spike_ratio:.2f}x, "
                    f"PriceΔ={price_change_24h:+.2f}%, "
                    f"Direction={direction}"
                )
        
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
            logger.error(f"VolumeSpike: detect error - {e}")
        
        return signals
    
    def get_signal_for_market(self, market: str) -> Optional[VolumeSpikeSignal]:
        """
        특정 마켓의 최신 신호 조회
        """
        if market in self.recent_signals:
            signal = self.recent_signals[market]
            now = time.time()
            # 1시간 이내 신호만 유효
            if (now - signal.timestamp) < self.signal_cooldown_sec:
                return signal
        return None
    
    def adjust_score_for_volume_spike(
        self,
        market: str,
        base_score: float,
        strategy: str,
    ) -> float:
        """
        거래량 급등 신호를 기반으로 스코어 조정
        
        Args:
            market: 마켓 심볼
            base_score: 기본 스코어
            strategy: 전략명
        
        Returns:
            조정된 스코어
        """
        signal = self.get_signal_for_market(market)
        if not signal:
            return base_score
        
        # 전략별 가중치
        strategy_multipliers = {
            "PINGPONG": 1.2,  # 빠른 회전 → 거래량 급등 활용
            "AUTOLOOP": 1.3,  # 중속 회전 → 거래량 급등 활용
            "LIGHTNING": 1.5, # 변동성 전략 → 거래량 급등 핵심
            "SNIPER": 1.4,    # 저격 전략 → 거래량 급등 선호
            "LADDER": 1.0,    # DCA → 거래량 무관
            "GAZUA": 1.0,     # 장기 → 거래량 무관
            "CONTRARIAN": 0.8, # 역발상 → 거래량 급등 회피
        }
        
        multiplier = strategy_multipliers.get(strategy, 1.0)
        
        # Confidence 기반 보너스
        bonus = 1.0
        if signal.direction == "bullish":
            bonus = 1.0 + (signal.confidence * 0.3 * multiplier)
        elif signal.direction == "bearish":
            bonus = 1.0 - (signal.confidence * 0.2 * multiplier)
        else:
            bonus = 1.0 + (signal.confidence * 0.1 * multiplier)
        
        adjusted = base_score * bonus
        
        logger.debug(
            f"VolumeSpike: {market} {strategy} - "
            f"Score {base_score:.2f} → {adjusted:.2f} "
            f"(bonus={bonus:.2f})"
        )
        
        return adjusted


# 싱글톤 인스턴스
_DETECTOR_INSTANCE: Optional[VolumeSpikeDetector] = None


def get_volume_spike_detector() -> Optional[VolumeSpikeDetector]:
    """
    VolumeSpike Detector 싱글톤 인스턴스 반환
    """
    global _DETECTOR_INSTANCE
    return _DETECTOR_INSTANCE


def initialize_volume_spike_detector(trade_client: Any) -> VolumeSpikeDetector:
    """
    VolumeSpike Detector 초기화
    """
    global _DETECTOR_INSTANCE
    if _DETECTOR_INSTANCE is None:
        _DETECTOR_INSTANCE = VolumeSpikeDetector(trade_client)
        logger.info("VolumeSpike Detector initialized")
    return _DETECTOR_INSTANCE
