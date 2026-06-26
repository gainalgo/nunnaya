"""
Time-based Volatility Adjuster
Leverages time-of-day volatility patterns
"""

import logging
from datetime import datetime
from typing import Dict, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class TimeVolatilityAdjuster:
    """
    Time-of-day volatility adjuster

    Patterns (KST):
    - 2-6 AM KST: peak volatility after US market close
    - 9 AM KST: Korean market open, BTC moves
    - 10 PM KST: US market open, volatility increases
    - Weekend: lower volume, higher volatility
    """
    
    def __init__(self):
        self.timezone = ZoneInfo("Asia/Seoul")
    
    def get_current_hour(self) -> int:
        """Current hour (KST)"""
        return datetime.now(self.timezone).hour

    def get_current_weekday(self) -> int:
        """Current weekday (0=Monday, 6=Sunday)"""
        return datetime.now(self.timezone).weekday()
    
    def is_high_volatility_time(self) -> bool:
        """
        Whether the current time is a high-volatility window
        """
        hour = self.get_current_hour()
        weekday = self.get_current_weekday()

        # Weekend
        if weekday >= 5:  # Sat, Sun
            return True

        # 2-6 AM KST (after US market close)
        if 2 <= hour <= 6:
            return True

        # 10 PM-midnight KST (US market open)
        if 22 <= hour <= 23:
            return True

        return False

    def is_low_volatility_time(self) -> bool:
        """
        Whether the current time is a low-volatility window
        """
        hour = self.get_current_hour()
        weekday = self.get_current_weekday()

        # Weekday daytime KST (11 AM-5 PM)
        if weekday < 5 and 11 <= hour <= 17:
            return True

        return False
    
    def get_volatility_multiplier(self) -> float:
        """
        Time-of-day volatility multiplier

        Returns:
            Volatility multiplier (baseline 1.0)
        """
        hour = self.get_current_hour()
        weekday = self.get_current_weekday()

        # Weekend: 1.3x
        if weekday >= 5:
            return 1.3

        # 2-6 AM: 1.5x (peak volatility)
        if 2 <= hour <= 6:
            return 1.5

        # 10 PM-midnight: 1.4x (US market open)
        if 22 <= hour <= 23:
            return 1.4

        # 9-10 AM: 1.2x (Korean market open)
        if 9 <= hour <= 10:
            return 1.2

        # Daytime 11 AM-5 PM: 0.8x (low volatility)
        if 11 <= hour <= 17:
            return 0.8

        # Other: 1.0x
        return 1.0
    
    def adjust_trailing_stop(
        self,
        base_trailing_pct: float,
    ) -> float:
        """
        Adjust Trailing Stop by time of day

        Args:
            base_trailing_pct: base Trailing Stop (%)

        Returns:
            adjusted Trailing Stop (%)
        """
        multiplier = self.get_volatility_multiplier()

        # High volatility -> wider Trailing Stop
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
        Adjust score by time of day

        Args:
            base_score: base score
            strategy: strategy name

        Returns:
            adjusted score
        """
        multiplier = self.get_volatility_multiplier()

        # Per-strategy time sensitivity
        time_sensitive_strategies = {
            "PINGPONG": 1.2,   # fast rotation -> exploits volatility
            "AUTOLOOP": 1.1,   # medium rotation -> exploits volatility
            "LIGHTNING": 1.5,  # volatility strategy -> time is key
            "SNIPER": 1.0,     # sniping -> time-agnostic (signal-based)
            "LADDER": 0.9,     # DCA -> time-agnostic
            "GAZUA": 0.8,      # long-term -> time-agnostic
            "CONTRARIAN": 1.0, # contrarian -> time-agnostic
        }

        sensitivity = time_sensitive_strategies.get(strategy, 1.0)

        # Apply sensitivity
        bonus = 1.0 + ((multiplier - 1.0) * sensitivity)
        adjusted = base_score * bonus
        
        logger.debug(
            f"TimeVolatility: {strategy} Score {base_score:.2f} "
            f"→ {adjusted:.2f} (bonus={bonus:.2f})"
        )
        
        return adjusted
    
    def get_time_context(self) -> Dict[str, any]:
        """
        Return the current time context
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


# Singleton instance
_ADJUSTER_INSTANCE: TimeVolatilityAdjuster = None


def get_time_volatility_adjuster() -> TimeVolatilityAdjuster:
    """
    Return the TimeVolatility Adjuster singleton instance
    """
    global _ADJUSTER_INSTANCE
    if _ADJUSTER_INSTANCE is None:
        _ADJUSTER_INSTANCE = TimeVolatilityAdjuster()
        logger.info("TimeVolatility Adjuster initialized")
    return _ADJUSTER_INSTANCE


def get_time_volatility_multiplier() -> float:
    """Time-of-day volatility multiplier (used e.g. for sizing entries)."""
    return get_time_volatility_adjuster().get_volatility_multiplier()
