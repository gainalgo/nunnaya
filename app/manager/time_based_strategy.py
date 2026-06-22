# ============================================================
# File: app/manager/time_based_strategy.py
# Autocoin OS v3-H — Time-Based Strategy Selector
# ------------------------------------------------------------
# 목적:
# - 시간대별 최적 전략 자동 선택
# - 활발한 시간대 → 단타 전략 (LIGHTNING, PINGPONG)
# - 조용한 시간대 → 장기 전략 (LADDER, GAZUA)
# ============================================================

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class MarketSession(Enum):
    """시장 세션."""
    ASIA_MORNING = "asia_morning"        # 09:00-12:00 KST
    ASIA_AFTERNOON = "asia_afternoon"    # 12:00-18:00 KST
    EUROPE_OPEN = "europe_open"          # 18:00-22:00 KST (EU 오전)
    US_OPEN = "us_open"                  # 22:00-02:00 KST (US 오전)
    OVERNIGHT = "overnight"              # 02:00-09:00 KST (조용한 시간)


@dataclass
class TimeSlot:
    """시간대 설정."""
    session: MarketSession
    start_hour: int  # 0-23
    end_hour: int    # 0-23 (end < start면 다음날로 간주)
    preferred_strategies: List[str]
    avoid_strategies: List[str]
    volatility_expected: str  # "high", "medium", "low"
    description: str


@dataclass
class StrategyRecommendation:
    """전략 추천 결과."""
    current_session: MarketSession
    recommended_strategies: List[str]
    avoid_strategies: List[str]
    reason: str
    confidence: float
    next_session_in_minutes: int
    volatility_level: str


class TimeBasedStrategySelector:
    """시간대 기반 전략 선택기.
    
    시간대별 특성:
    - 아시아 오전 (09-12): 중간 변동성, PINGPONG/AUTOLOOP
    - 아시아 오후 (12-18): 낮은 변동성, LADDER/GAZUA
    - 유럽 오픈 (18-22): 높은 변동성, LIGHTNING/PINGPONG
    - 미국 오픈 (22-02): 최고 변동성, LIGHTNING
    - 새벽 (02-09): 낮은 변동성, LADDER/GAZUA
    """

    def __init__(self):
        self.time_slots = self._init_time_slots()

    def _init_time_slots(self) -> List[TimeSlot]:
        """시간대 설정 초기화."""
        return [
            TimeSlot(
                session=MarketSession.ASIA_MORNING,
                start_hour=9,
                end_hour=12,
                preferred_strategies=["PINGPONG", "AUTOLOOP"],
                avoid_strategies=["LIGHTNING"],
                volatility_expected="medium",
                description="아시아 오전장, 중간 변동성",
            ),
            TimeSlot(
                session=MarketSession.ASIA_AFTERNOON,
                start_hour=12,
                end_hour=18,
                preferred_strategies=["PINGPONG", "LADDER", "GAZUA"],
                avoid_strategies=["LIGHTNING"],
                volatility_expected="low",
                description="아시아 오후장, 낮은 변동성",
            ),
            TimeSlot(
                session=MarketSession.EUROPE_OPEN,
                start_hour=18,
                end_hour=22,
                preferred_strategies=["LIGHTNING", "PINGPONG"],
                avoid_strategies=["GAZUA"],
                volatility_expected="high",
                description="유럽 오픈, 높은 변동성",
            ),
            TimeSlot(
                session=MarketSession.US_OPEN,
                start_hour=22,
                end_hour=2,  # 다음날 02시
                preferred_strategies=["LIGHTNING", "PINGPONG"],
                avoid_strategies=["LADDER", "GAZUA"],
                volatility_expected="high",
                description="미국 오픈, 최고 변동성",
            ),
            TimeSlot(
                session=MarketSession.OVERNIGHT,
                start_hour=2,
                end_hour=9,
                preferred_strategies=["LADDER", "GAZUA", "AUTOLOOP"],
                avoid_strategies=["LIGHTNING"],
                volatility_expected="low",
                description="새벽, 저점 매집 시간",
            ),
        ]

    def get_current_session(self, ts: Optional[float] = None) -> TimeSlot:
        """현재 시간대 반환."""
        lt = time.localtime(ts or time.time())
        current_hour = lt.tm_hour
        
        for slot in self.time_slots:
            if self._is_in_slot(current_hour, slot.start_hour, slot.end_hour):
                return slot
        
        # 기본값 (찾지 못한 경우)
        return self.time_slots[0]

    def _is_in_slot(self, current: int, start: int, end: int) -> bool:
        """시간이 슬롯에 포함되는지 확인."""
        if start <= end:
            return start <= current < end
        else:
            # 자정을 넘는 경우 (예: 22-02)
            return current >= start or current < end

    def get_recommendation(self, ts: Optional[float] = None) -> StrategyRecommendation:
        """현재 시간대 기반 전략 추천."""
        slot = self.get_current_session(ts)
        
        # 다음 세션까지 남은 시간
        lt = time.localtime(ts or time.time())
        current_minutes = lt.tm_hour * 60 + lt.tm_min
        
        end_minutes = slot.end_hour * 60
        if slot.end_hour < slot.start_hour:
            end_minutes += 24 * 60
        if current_minutes > end_minutes:
            current_minutes -= 24 * 60
        
        next_session_min = max(0, end_minutes - current_minutes)
        
        return StrategyRecommendation(
            current_session=slot.session,
            recommended_strategies=slot.preferred_strategies,
            avoid_strategies=slot.avoid_strategies,
            reason=slot.description,
            confidence=0.8,
            next_session_in_minutes=next_session_min,
            volatility_level=slot.volatility_expected,
        )

    def should_switch_strategy(
        self,
        current_strategy: str,
        ts: Optional[float] = None,
    ) -> Tuple[bool, Optional[str], str]:
        """전략 전환 필요 여부.
        
        Returns:
            (should_switch, suggested_strategy, reason)
        """
        current_strategy = current_strategy.upper()
        rec = self.get_recommendation(ts)
        
        # 현재 전략이 피해야 할 목록에 있는지
        if current_strategy in rec.avoid_strategies:
            # 추천 전략 중 첫 번째로 전환
            if rec.recommended_strategies:
                return (
                    True, 
                    rec.recommended_strategies[0],
                    f"time_avoid:{current_strategy}→{rec.recommended_strategies[0]}:{rec.reason}",
                )
        
        # 현재 전략이 추천 목록에 있으면 유지
        if current_strategy in rec.recommended_strategies:
            return (False, None, "strategy_optimal")
        
        # 추천 전략도 피해야 할 전략도 아닌 경우 → 유지
        return (False, None, "strategy_neutral")

    def filter_candidates_by_time(
        self,
        candidates: List[Dict[str, Any]],
        ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """시간대에 맞는 후보만 필터링."""
        rec = self.get_recommendation(ts)
        
        filtered = []
        for c in candidates:
            strategy = str(c.get("strategy", "")).upper()
            if strategy not in rec.avoid_strategies:
                # 추천 전략이면 점수 부스트
                if strategy in rec.recommended_strategies:
                    c = dict(c)
                    c["time_boost"] = 1.2
                    c["time_reason"] = f"preferred_in_{rec.current_session.value}"
                filtered.append(c)
        
        return filtered

    def get_strategy_schedule(self) -> List[Dict[str, Any]]:
        """24시간 전략 스케줄 반환."""
        schedule = []
        for slot in self.time_slots:
            schedule.append({
                "session": slot.session.value,
                "start_hour": slot.start_hour,
                "end_hour": slot.end_hour,
                "preferred": slot.preferred_strategies,
                "avoid": slot.avoid_strategies,
                "volatility": slot.volatility_expected,
                "description": slot.description,
            })
        return schedule


def get_optimal_strategy_for_time(hour: int) -> Tuple[str, str]:
    """특정 시간대의 최적 전략 반환.
    
    Args:
        hour: 0-23 시간
        
    Returns:
        (strategy, reason)
    """
    selector = TimeBasedStrategySelector()
    
    # 임시 timestamp 생성
    import calendar
    lt = time.localtime()
    fake_ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))
    
    rec = selector.get_recommendation(fake_ts)
    
    if rec.recommended_strategies:
        return (rec.recommended_strategies[0], rec.reason)
    return ("PINGPONG", "default")
