"""
Volume Spike Detector
Detects volume spikes - a leading signal for sharp price moves
"""

import logging
import time
from typing import Dict, List, Optional, Any
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class VolumeSpikeSignal:
    """Volume spike signal"""
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
    Volume spike detector

    Strategy:
    - 24h volume > 7d average × 3 → strong signal
    - 24h volume > 7d average × 2 → medium signal
    - Price rising alongside → bullish signal
    - Price falling alongside → bearish signal
    """
    
    def __init__(
        self,
        trade_client: Any,
        spike_threshold: float = 3.0,  # 3x or more
        medium_threshold: float = 2.0,  # 2x or more
        history_size: int = 100,
    ):
        self.trade_client = trade_client
        self.spike_threshold = spike_threshold
        self.medium_threshold = medium_threshold
        
        # Per-market volume history
        self.volume_history: Dict[str, deque] = {}
        self.history_size = history_size

        # Recent signal cache (dedup)
        self.recent_signals: Dict[str, VolumeSpikeSignal] = {}
        self.signal_cooldown_sec = 3600  # 1 hour
        
        logger.info(
            f"VolumeSpike: spike={spike_threshold}x, "
            f"medium={medium_threshold}x"
        )
    
    def update_volume_data(self, markets: List[str]) -> None:
        """
        Update per-market volume data
        """
        try:
            for market in markets:
                # Daily candles (7 days)
                candles = self.trade_client.get_candles_daily(market, count=7)
                if not candles or len(candles) < 7:
                    continue
                
                if market not in self.volume_history:
                    self.volume_history[market] = deque(maxlen=self.history_size)
                
                # 7-day average volume
                volumes = [float(c.get("candle_acc_trade_volume", 0)) for c in candles]
                avg_volume_7d = sum(volumes) / len(volumes)

                # 24h volume (latest candle)
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
        Detect volume spikes
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
                
                # Below threshold
                if spike_ratio < self.medium_threshold:
                    continue

                # Dedup against recent signal
                if market in self.recent_signals:
                    prev_signal = self.recent_signals[market]
                    if (now - prev_signal.timestamp) < self.signal_cooldown_sec:
                        continue
                
                # Compute price change
                price_change_24h = 0.0
                if len(history) >= 2:
                    prev = history[-2]
                    price_change_24h = (
                        (latest["price"] / prev["price"] - 1.0) * 100
                    ) if prev["price"] > 0 else 0.0
                
                # Determine direction
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
        Get the latest signal for a specific market
        """
        if market in self.recent_signals:
            signal = self.recent_signals[market]
            now = time.time()
            # Only signals within the last hour are valid
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
        Adjust the score based on the volume spike signal

        Args:
            market: market symbol
            base_score: base score
            strategy: strategy name

        Returns:
            adjusted score
        """
        signal = self.get_signal_for_market(market)
        if not signal:
            return base_score
        
        # Per-strategy weighting
        strategy_multipliers = {
            "PINGPONG": 1.2,  # fast rotation -> leverage volume spikes
            "AUTOLOOP": 1.3,  # medium rotation -> leverage volume spikes
            "LIGHTNING": 1.5, # volatility strategy -> volume spikes are key
            "SNIPER": 1.4,    # sniper strategy -> prefers volume spikes
            "LADDER": 1.0,    # DCA -> volume-agnostic
            "GAZUA": 1.0,     # long-term -> volume-agnostic
            "CONTRARIAN": 0.8, # contrarian -> avoids volume spikes
        }

        multiplier = strategy_multipliers.get(strategy, 1.0)

        # Confidence-based bonus
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


# Singleton instance
_DETECTOR_INSTANCE: Optional[VolumeSpikeDetector] = None


def get_volume_spike_detector() -> Optional[VolumeSpikeDetector]:
    """
    Return the VolumeSpike Detector singleton instance
    """
    global _DETECTOR_INSTANCE
    return _DETECTOR_INSTANCE


def initialize_volume_spike_detector(trade_client: Any) -> VolumeSpikeDetector:
    """
    Initialize the VolumeSpike Detector
    """
    global _DETECTOR_INSTANCE
    if _DETECTOR_INSTANCE is None:
        _DETECTOR_INSTANCE = VolumeSpikeDetector(trade_client)
        logger.info("VolumeSpike Detector initialized")
    return _DETECTOR_INSTANCE
