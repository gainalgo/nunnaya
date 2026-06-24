# ============================================================
# File: app/manager/time_based_strategy.py
# Autocoin OS v3-H — Time-Based Strategy Selector
# ------------------------------------------------------------
# Purpose:
# - Automatically select the optimal strategy per time slot
# - Active hours → scalping strategies (LIGHTNING, PINGPONG)
# - Quiet hours → long-term strategies (LADDER, GAZUA)
# ============================================================

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class MarketSession(Enum):
    """Market session."""
    ASIA_MORNING = "asia_morning"        # 09:00-12:00 KST
    ASIA_AFTERNOON = "asia_afternoon"    # 12:00-18:00 KST
    EUROPE_OPEN = "europe_open"          # 18:00-22:00 KST (EU morning)
    US_OPEN = "us_open"                  # 22:00-02:00 KST (US morning)
    OVERNIGHT = "overnight"              # 02:00-09:00 KST (quiet hours)


@dataclass
class TimeSlot:
    """Time slot configuration."""
    session: MarketSession
    start_hour: int  # 0-23
    end_hour: int    # 0-23 (if end < start, treated as next day)
    preferred_strategies: List[str]
    avoid_strategies: List[str]
    volatility_expected: str  # "high", "medium", "low"
    description: str


@dataclass
class StrategyRecommendation:
    """Strategy recommendation result."""
    current_session: MarketSession
    recommended_strategies: List[str]
    avoid_strategies: List[str]
    reason: str
    confidence: float
    next_session_in_minutes: int
    volatility_level: str


class TimeBasedStrategySelector:
    """Time-of-day based strategy selector.

    Characteristics per time slot:
    - Asia morning (09-12): medium volatility, PINGPONG/AUTOLOOP
    - Asia afternoon (12-18): low volatility, LADDER/GAZUA
    - Europe open (18-22): high volatility, LIGHTNING/PINGPONG
    - US open (22-02): peak volatility, LIGHTNING
    - Overnight (02-09): low volatility, LADDER/GAZUA
    """

    def __init__(self):
        self.time_slots = self._init_time_slots()

    def _init_time_slots(self) -> List[TimeSlot]:
        """Initialize time slot configuration."""
        return [
            TimeSlot(
                session=MarketSession.ASIA_MORNING,
                start_hour=9,
                end_hour=12,
                preferred_strategies=["PINGPONG", "AUTOLOOP"],
                avoid_strategies=["LIGHTNING"],
                volatility_expected="medium",
                description="Asia morning session, medium volatility",
            ),
            TimeSlot(
                session=MarketSession.ASIA_AFTERNOON,
                start_hour=12,
                end_hour=18,
                preferred_strategies=["PINGPONG", "LADDER", "GAZUA"],
                avoid_strategies=["LIGHTNING"],
                volatility_expected="low",
                description="Asia afternoon session, low volatility",
            ),
            TimeSlot(
                session=MarketSession.EUROPE_OPEN,
                start_hour=18,
                end_hour=22,
                preferred_strategies=["LIGHTNING", "PINGPONG"],
                avoid_strategies=["GAZUA"],
                volatility_expected="high",
                description="Europe open, high volatility",
            ),
            TimeSlot(
                session=MarketSession.US_OPEN,
                start_hour=22,
                end_hour=2,  # 02:00 next day
                preferred_strategies=["LIGHTNING", "PINGPONG"],
                avoid_strategies=["LADDER", "GAZUA"],
                volatility_expected="high",
                description="US open, peak volatility",
            ),
            TimeSlot(
                session=MarketSession.OVERNIGHT,
                start_hour=2,
                end_hour=9,
                preferred_strategies=["LADDER", "GAZUA", "AUTOLOOP"],
                avoid_strategies=["LIGHTNING"],
                volatility_expected="low",
                description="Overnight, dip-accumulation hours",
            ),
        ]

    def get_current_session(self, ts: Optional[float] = None) -> TimeSlot:
        """Return the current time slot."""
        lt = time.localtime(ts or time.time())
        current_hour = lt.tm_hour

        for slot in self.time_slots:
            if self._is_in_slot(current_hour, slot.start_hour, slot.end_hour):
                return slot

        # Fallback (if no slot matched)
        return self.time_slots[0]

    def _is_in_slot(self, current: int, start: int, end: int) -> bool:
        """Check whether the hour falls within the slot."""
        if start <= end:
            return start <= current < end
        else:
            # Spanning midnight (e.g. 22-02)
            return current >= start or current < end

    def get_recommendation(self, ts: Optional[float] = None) -> StrategyRecommendation:
        """Recommend a strategy based on the current time slot."""
        slot = self.get_current_session(ts)

        # Minutes remaining until the next session
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
        """Whether a strategy switch is needed.

        Returns:
            (should_switch, suggested_strategy, reason)
        """
        current_strategy = current_strategy.upper()
        rec = self.get_recommendation(ts)

        # Whether the current strategy is in the avoid list
        if current_strategy in rec.avoid_strategies:
            # Switch to the first recommended strategy
            if rec.recommended_strategies:
                return (
                    True, 
                    rec.recommended_strategies[0],
                    f"time_avoid:{current_strategy}→{rec.recommended_strategies[0]}:{rec.reason}",
                )
        
        # Keep the current strategy if it is in the recommended list
        if current_strategy in rec.recommended_strategies:
            return (False, None, "strategy_optimal")

        # Neither recommended nor avoided → keep current
        return (False, None, "strategy_neutral")

    def filter_candidates_by_time(
        self,
        candidates: List[Dict[str, Any]],
        ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Keep only candidates suited to the current time slot."""
        rec = self.get_recommendation(ts)

        filtered = []
        for c in candidates:
            strategy = str(c.get("strategy", "")).upper()
            if strategy not in rec.avoid_strategies:
                # Boost the score if it is a recommended strategy
                if strategy in rec.recommended_strategies:
                    c = dict(c)
                    c["time_boost"] = 1.2
                    c["time_reason"] = f"preferred_in_{rec.current_session.value}"
                filtered.append(c)
        
        return filtered

    def get_strategy_schedule(self) -> List[Dict[str, Any]]:
        """Return the 24-hour strategy schedule."""
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
    """Return the optimal strategy for a specific hour.

    Args:
        hour: hour 0-23

    Returns:
        (strategy, reason)
    """
    selector = TimeBasedStrategySelector()

    # Build a temporary timestamp
    import calendar
    lt = time.localtime()
    fake_ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))
    
    rec = selector.get_recommendation(fake_ts)
    
    if rec.recommended_strategies:
        return (rec.recommended_strategies[0], rec.reason)
    return ("PINGPONG", "default")
