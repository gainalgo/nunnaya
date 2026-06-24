"""
Whale Activity Detector
Detects whale volume — estimates large-player activity from abnormal volume + price-move patterns

Used by:
  1. hyper_system.py Smart Allocation — adjusts budget weight via whale_mult
  2. reserved_selector.py — per-strategy coin selection score bonus/penalty
"""

import logging
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class WhaleSignal(Enum):
    """Whale signal types"""
    NONE = "none"              # normal volume
    WHALE_BUY = "whale_buy"    # volume surge + price up → estimated whale buy
    WHALE_SELL = "whale_sell"  # volume surge + price down → estimated whale sell
    WHALE_CHURN = "whale_churn"  # volume surge + price flat → whale rotation/wash


@dataclass
class WhaleInfo:
    """Whale detection result"""
    signal: WhaleSignal = WhaleSignal.NONE
    spike_ratio: float = 0.0       # volume / average volume
    price_change_pct: float = 0.0  # price change rate (%)
    confidence: float = 0.0        # 0.0 ~ 1.0


# Per-strategy whale signal score bonus/penalty
# (whale_buy_bonus, whale_sell_bonus, whale_churn_bonus)
STRATEGY_WHALE_SCORES: Dict[str, Dict[str, float]] = {
    "LIGHTNING": {"whale_buy": +5.0, "whale_sell": +5.0, "whale_churn": +2.0},
    "SNIPER":    {"whale_buy": -2.0, "whale_sell": +5.0, "whale_churn": +1.0},
    "CONTRARIAN":{"whale_buy": -1.0, "whale_sell": +3.0, "whale_churn": +0.5},
    "PINGPONG":  {"whale_buy": -1.0, "whale_sell": +2.0, "whale_churn": +0.0},
    "AUTOLOOP":  {"whale_buy": -1.0, "whale_sell": +2.0, "whale_churn": +0.0},
    "GAZUA":     {"whale_buy": +3.0, "whale_sell": -3.0, "whale_churn": +0.0},
    "LADDER":    {"whale_buy": -1.0, "whale_sell": +2.0, "whale_churn": +0.0},
}


class WhaleDetector:
    """
    Whale activity detector

    Decision criteria:
    - volume >= 3x average → suspected whale activity
    - volume >= 3x + price >= +3% → whale_buy
    - volume >= 3x + price <= -3% → whale_sell
    - volume >= 3x + price move within ±3% → whale_churn
    - volume 2~3x → same decision with lower confidence
    """

    STRONG_SPIKE = 3.0    # strong volume-spike multiple
    MEDIUM_SPIKE = 2.0    # medium volume-spike multiple
    PRICE_THRESHOLD = 3.0  # price-move decision threshold (%)

    def __init__(self):
        # cache of recent detection results per market
        self._cache: Dict[str, tuple] = {}  # market -> (timestamp, WhaleInfo)
        self._cache_ttl = 300  # 5-minute cache
        logger.info("WhaleDetector initialized")

    def detect(
        self,
        vol_24h: float,
        avg_vol: float,
        price_change_pct: float,
        market: str = "",
    ) -> WhaleInfo:
        """
        Detect whale activity

        Args:
            vol_24h: 24-hour volume (USDT)
            avg_vol: average volume (USDT) — 7-day average or market average
            price_change_pct: 24-hour price change rate (%)
            market: market code (for cache, optional)

        Returns:
            WhaleInfo
        """
        # check cache
        if market:
            cached = self._cache.get(market)
            if cached and (time.time() - cached[0]) < self._cache_ttl:
                return cached[1]

        info = WhaleInfo(price_change_pct=price_change_pct)

        if avg_vol <= 0 or vol_24h <= 0:
            return info

        spike_ratio = vol_24h / avg_vol
        info.spike_ratio = round(spike_ratio, 2)

        if spike_ratio < self.MEDIUM_SPIKE:
            # normal volume → not a whale
            if market:
                self._cache[market] = (time.time(), info)
            return info

        # confidence: 2x=0.3, 3x=0.6, 5x+=1.0
        if spike_ratio >= self.STRONG_SPIKE:
            info.confidence = min(1.0, 0.6 + (spike_ratio - self.STRONG_SPIKE) * 0.1)
        else:
            info.confidence = 0.3 + (spike_ratio - self.MEDIUM_SPIKE) * 0.3

        # determine signal
        if price_change_pct >= self.PRICE_THRESHOLD:
            info.signal = WhaleSignal.WHALE_BUY
        elif price_change_pct <= -self.PRICE_THRESHOLD:
            info.signal = WhaleSignal.WHALE_SELL
        else:
            info.signal = WhaleSignal.WHALE_CHURN

        if market:
            self._cache[market] = (time.time(), info)
            logger.debug(
                f"WhaleDetector: {market} signal={info.signal.value} "
                f"spike={spike_ratio:.1f}x price={price_change_pct:+.1f}% "
                f"conf={info.confidence:.2f}"
            )

        return info

    def get_budget_weight(self, info: WhaleInfo) -> float:
        """
        Return budget allocation weight (for hyper_system Smart Allocation)

        - whale_buy: slightly increase budget (smart-money accumulation = trend following)
        - whale_sell: slightly decrease budget (risk)
        - whale_churn: neutral
        - none: 1.0 (no change)
        """
        if info.signal == WhaleSignal.NONE:
            return 1.0
        if info.signal == WhaleSignal.WHALE_BUY:
            return 1.0 + (0.2 * info.confidence)  # max 1.2
        if info.signal == WhaleSignal.WHALE_SELL:
            return 1.0 - (0.15 * info.confidence)  # min 0.85
        # CHURN
        return 1.0

    def get_strategy_score_bonus(
        self,
        info: WhaleInfo,
        strategy: str,
    ) -> float:
        """
        Per-strategy coin selection score bonus/penalty (for reserved_selector)

        Args:
            info: WhaleInfo detection result
            strategy: strategy name (LIGHTNING, SNIPER, etc.)

        Returns:
            score adjustment (positive=bonus, negative=penalty)
        """
        if info.signal == WhaleSignal.NONE:
            return 0.0

        scores = STRATEGY_WHALE_SCORES.get(strategy.upper(), {})
        base_bonus = scores.get(info.signal.value, 0.0)

        # apply confidence
        return round(base_bonus * info.confidence, 2)


# singleton
_INSTANCE: Optional[WhaleDetector] = None


def get_whale_detector() -> WhaleDetector:
    """WhaleDetector singleton instance"""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = WhaleDetector()
    return _INSTANCE
