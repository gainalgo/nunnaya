# ============================================================
# File: app/strategy/strategy_risk_auto_regulator.py
# ------------------------------------------------------------
# StrategyRiskAutoRegulator
# - Adjusts signals from the Judge according to market risk conditions.
# ============================================================

from __future__ import annotations
from typing import Dict, Any

from .strategy_types import (
    StrategyPolicy,
    StrategySignal,
    StrategyBrainOutput,
)


class StrategyRiskAutoRegulator:
    """
    Layer that regulates the Judge's signal based on Brain/Policy data.
    """

    # --------------------------------------------------------
    # Main risk adjustment function
    # --------------------------------------------------------
    def adjust(
        self,
        market: str,
        price: float,
        policy: StrategyPolicy,
        brain: StrategyBrainOutput,
        signal: StrategySignal
    ) -> StrategySignal:
        """
        Decide whether to keep the Judge's signal as-is, or switch to hold when risky.
        """

        volatility = brain.volatility
        trend_val = brain.trend

        # --------------------------
        # Halt trading when market volatility is too high
        # --------------------------
        if volatility is not None and volatility > policy.get("max_volatility", 5.0):
            return StrategySignal("hold")

        # --------------------------
        # Ignore a buy signal when the downtrend is very strong
        # --------------------------
        if signal.signal == "buy":
            if trend_val is not None and trend_val < policy.get("min_uptrend", -2.0):
                return StrategySignal("hold")

        # --------------------------
        # Hold off a sell signal when the uptrend is strong
        # --------------------------
        if signal.signal == "sell":
            if trend_val is not None and trend_val > policy.get("max_downtrend", 2.0):
                return StrategySignal("hold")

        # --------------------------
        # Default: keep the signal
        # --------------------------
        return signal
