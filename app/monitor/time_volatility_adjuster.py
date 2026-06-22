"""
Time-based Volatility Adjuster
시간대별 변동성 패턴 활용
"""

import logging
from datetime import datetime
from typing import Dict, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class TimeVolatilityAdjuster:
    """
    시간대별 변동성 조정기
    
    패턴:
    - 한국 새벽 2-6시: 미국 장 마감 후 변동성 최대
    - 한국 오전 9시: 한국 장 개장, BTC 움직임
    - 한국 오후 10시: 미국 장 개장, 변동성 증가
    - 주말: 거래량 감소, 변동성 증가
    """
    
    def __init__(self):
        self.timezone = ZoneInfo("Asia/Seoul")
    
    def get_current_hour(self) -> int:
        """현재 시각 (한국 시간)"""
        return datetime.now(self.timezone).hour
    
    def get_current_weekday(self) -> int:
        """현재 요일 (0=월요일, 6=일요일)"""
        return datetime.now(self.timezone).weekday()
    
    def is_high_volatility_time(self) -> bool:
        """
        고변동성 시간대 여부
        """
        hour = self.get_current_hour()
        weekday = self.get_current_weekday()
        
        # 주말
        if weekday >= 5:  # 토, 일
            return True
        
        # 한국 새벽 2-6시 (미국 장 마감 후)
        if 2 <= hour <= 6:
            return True
        
        # 한국 오후 10시-자정 (미국 장 개장)
        if 22 <= hour <= 23:
            return True
        
        return False
    
    def is_low_volatility_time(self) -> bool:
        """
        저변동성 시간대 여부
        """
        hour = self.get_current_hour()
        weekday = self.get_current_weekday()
        
        # 평일 한국 낮 시간 (11-17시)
        if weekday < 5 and 11 <= hour <= 17:
            return True
        
        return False
    
    def get_volatility_multiplier(self) -> float:
        """
        시간대별 변동성 배율
        
        Returns:
            변동성 배율 (1.0 기준)
        """
        hour = self.get_current_hour()
        weekday = self.get_current_weekday()
        
        # 주말: 1.3배
        if weekday >= 5:
            return 1.3
        
        # 새벽 2-6시: 1.5배 (최고 변동성)
        if 2 <= hour <= 6:
            return 1.5
        
        # 오후 10시-자정: 1.4배 (미국 장 개장)
        if 22 <= hour <= 23:
            return 1.4
        
        # 오전 9-10시: 1.2배 (한국 장 개장)
        if 9 <= hour <= 10:
            return 1.2
        
        # 낮 시간 11-17시: 0.8배 (저변동성)
        if 11 <= hour <= 17:
            return 0.8
        
        # 기타: 1.0배
        return 1.0
    
    def adjust_trailing_stop(
        self,
        base_trailing_pct: float,
    ) -> float:
        """
        시간대별 Trailing Stop 조정
        
        Args:
            base_trailing_pct: 기본 Trailing Stop (%)
        
        Returns:
            조정된 Trailing Stop (%)
        """
        multiplier = self.get_volatility_multiplier()
        
        # 고변동성 → Trailing Stop 넓게
        adjusted = base_trailing_pct * multiplier
        
        logger.debug(
            f"TimeVolatility: Trailing {base_trailing_pct:.2f}% "
            f"→ {adjusted:.2f}% (×{multiplier:.2f})"
        )
        
        return adjusted
    
    def adjust_score_for_time(
        self,
        base_score: float,
        strategy: str,
    ) -> float:
        """
        시간대별 스코어 조정
        
        Args:
            base_score: 기본 스코어
            strategy: 전략명
        
        Returns:
            조정된 스코어
        """
        multiplier = self.get_volatility_multiplier()
        
        # 전략별 시간 민감도
        time_sensitive_strategies = {
            "PINGPONG": 1.2,   # 빠른 회전 → 변동성 활용
            "AUTOLOOP": 1.1,   # 중속 회전 → 변동성 활용
            "LIGHTNING": 1.5,  # 변동성 전략 → 시간 핵심
            "SNIPER": 1.0,     # 저격 → 시간 무관 (신호 기반)
            "LADDER": 0.9,     # DCA → 시간 무관
            "GAZUA": 0.8,      # 장기 → 시간 무관
            "CONTRARIAN": 1.0, # 역발상 → 시간 무관
        }
        
        sensitivity = time_sensitive_strategies.get(strategy, 1.0)
        
        # 민감도 적용
        bonus = 1.0 + ((multiplier - 1.0) * sensitivity)
        adjusted = base_score * bonus
        
        logger.debug(
            f"TimeVolatility: {strategy} Score {base_score:.2f} "
            f"→ {adjusted:.2f} (bonus={bonus:.2f})"
        )
        
        return adjusted
    
    def get_time_context(self) -> Dict[str, any]:
        """
        현재 시간 컨텍스트 반환
        """
        hour = self.get_current_hour()
        weekday = self.get_current_weekday()
        multiplier = self.get_volatility_multiplier()
        
        return {
            "hour": hour,
            "weekday": weekday,
            "is_weekend": weekday >= 5,
            "is_high_volatility": self.is_high_volatility_time(),
            "is_low_volatility": self.is_low_volatility_time(),
            "volatility_multiplier": multiplier,
        }


# 싱글톤 인스턴스
_ADJUSTER_INSTANCE: TimeVolatilityAdjuster = None


def get_time_volatility_adjuster() -> TimeVolatilityAdjuster:
    """
    TimeVolatility Adjuster 싱글톤 인스턴스 반환
    """
    global _ADJUSTER_INSTANCE
    if _ADJUSTER_INSTANCE is None:
        _ADJUSTER_INSTANCE = TimeVolatilityAdjuster()
        logger.info("TimeVolatility Adjuster initialized")
    return _ADJUSTER_INSTANCE


def get_time_volatility_multiplier() -> float:
    """시간대별 변동성 배율 (진입 규모 조절 등에서 사용)."""
    return get_time_volatility_adjuster().get_volatility_multiplier()
