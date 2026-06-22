"""
Whale Activity Detector
고래 거래량 감지 — 비정상적 거래량 + 가격 변동 패턴으로 대형 플레이어 활동 추정

사용처:
  1. hyper_system.py Smart Allocation — whale_mult로 예산 가중치 조절
  2. reserved_selector.py — 전략별 코인 선발 스코어 가감점
"""

import logging
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class WhaleSignal(Enum):
    """고래 신호 유형"""
    NONE = "none"              # 정상 거래량
    WHALE_BUY = "whale_buy"    # 거래량 폭증 + 가격 상승 → 고래 매수 추정
    WHALE_SELL = "whale_sell"  # 거래량 폭증 + 가격 하락 → 고래 매도 추정
    WHALE_CHURN = "whale_churn"  # 거래량 폭증 + 가격 횡보 → 고래 교체/세탁


@dataclass
class WhaleInfo:
    """고래 감지 결과"""
    signal: WhaleSignal = WhaleSignal.NONE
    spike_ratio: float = 0.0       # 거래량 / 평균 거래량
    price_change_pct: float = 0.0  # 가격 변화율 (%)
    confidence: float = 0.0        # 0.0 ~ 1.0


# 전략별 고래 신호 스코어 가감점
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
    고래 활동 감지기

    판단 기준:
    - 거래량이 평균 대비 3배 이상 → 고래 활동 의심
    - 거래량 3배 이상 + 가격 +3% 이상 → whale_buy
    - 거래량 3배 이상 + 가격 -3% 이하 → whale_sell
    - 거래량 3배 이상 + 가격 변동 ±3% 이내 → whale_churn
    - 거래량 2~3배 → 낮은 confidence로 같은 판단
    """

    STRONG_SPIKE = 3.0    # 강한 거래량 급등 배율
    MEDIUM_SPIKE = 2.0    # 중간 거래량 급등 배율
    PRICE_THRESHOLD = 3.0  # 가격 변동 판단 기준 (%)

    def __init__(self):
        # 마켓별 최근 감지 결과 캐시
        self._cache: Dict[str, tuple] = {}  # market -> (timestamp, WhaleInfo)
        self._cache_ttl = 300  # 5분 캐시
        logger.info("WhaleDetector initialized")

    def detect(
        self,
        vol_24h: float,
        avg_vol: float,
        price_change_pct: float,
        market: str = "",
    ) -> WhaleInfo:
        """
        고래 활동 감지

        Args:
            vol_24h: 24시간 거래량 (USDT)
            avg_vol: 평균 거래량 (USDT) — 7일 평균 또는 시장 평균
            price_change_pct: 24시간 가격 변화율 (%)
            market: 마켓 코드 (캐시용, 선택)

        Returns:
            WhaleInfo
        """
        # 캐시 확인
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
            # 거래량 정상 → 고래 아님
            if market:
                self._cache[market] = (time.time(), info)
            return info

        # confidence: 2배=0.3, 3배=0.6, 5배+=1.0
        if spike_ratio >= self.STRONG_SPIKE:
            info.confidence = min(1.0, 0.6 + (spike_ratio - self.STRONG_SPIKE) * 0.1)
        else:
            info.confidence = 0.3 + (spike_ratio - self.MEDIUM_SPIKE) * 0.3

        # 신호 판단
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
        예산 배분 가중치 반환 (hyper_system Smart Allocation용)

        - whale_buy: 예산 소폭 확대 (세력 매집 = 추세 추종)
        - whale_sell: 예산 소폭 축소 (위험)
        - whale_churn: 중립
        - none: 1.0 (변화 없음)
        """
        if info.signal == WhaleSignal.NONE:
            return 1.0
        if info.signal == WhaleSignal.WHALE_BUY:
            return 1.0 + (0.2 * info.confidence)  # 최대 1.2
        if info.signal == WhaleSignal.WHALE_SELL:
            return 1.0 - (0.15 * info.confidence)  # 최소 0.85
        # CHURN
        return 1.0

    def get_strategy_score_bonus(
        self,
        info: WhaleInfo,
        strategy: str,
    ) -> float:
        """
        전략별 코인 선발 스코어 가감점 (reserved_selector용)

        Args:
            info: WhaleInfo 감지 결과
            strategy: 전략명 (LIGHTNING, SNIPER, etc.)

        Returns:
            스코어 가감점 (양수=가점, 음수=감점)
        """
        if info.signal == WhaleSignal.NONE:
            return 0.0

        scores = STRATEGY_WHALE_SCORES.get(strategy.upper(), {})
        base_bonus = scores.get(info.signal.value, 0.0)

        # confidence 반영
        return round(base_bonus * info.confidence, 2)


# 싱글톤
_INSTANCE: Optional[WhaleDetector] = None


def get_whale_detector() -> WhaleDetector:
    """WhaleDetector 싱글톤 인스턴스"""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = WhaleDetector()
    return _INSTANCE
