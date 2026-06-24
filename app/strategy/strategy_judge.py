# ============================================================
# File: app/strategy/strategy_judge.py
# ------------------------------------------------------------
# StrategyJudge
# - Makes buy/sell/hold decisions based on Brain analysis results + policy.
# ============================================================

from __future__ import annotations
from typing import Dict, Any

from .strategy_types import (
    StrategySignal,
    StrategyPolicy,
    StrategyBrainOutput,
)


class StrategyJudge:
    """
    Module that determines buy/sell/hold signals from a combination of BrainOutput + Policy.
    """

    # --------------------------------------------------------
    # Main decision function
    # --------------------------------------------------------
    def decide(
        self,
        market: str,
        price: float,
        policy: StrategyPolicy,
        brain: StrategyBrainOutput
    ) -> StrategySignal:
        """
        Core logic that makes the buy/sell/hold decision.
        """

        rsi_val = brain.rsi
        macd_hist = brain.macd_histogram
        trend_val = brain.trend

        # --------------------------
        # Simple RSI-based condition
        # --------------------------
        if rsi_val is not None:
            if rsi_val < policy.get("rsi_low", 30):
                return StrategySignal("buy")
            if rsi_val > policy.get("rsi_high", 70):
                return StrategySignal("sell")

        # --------------------------
        # MACD Histogram-based momentum decision
        # --------------------------
        if macd_hist is not None:
            if macd_hist > 0:
                if trend_val is not None and trend_val > 0:
                    return StrategySignal("buy")
            elif macd_hist < 0:
                if trend_val is not None and trend_val < 0:
                    return StrategySignal("sell")

        # --------------------------
        # Default: Hold
        # --------------------------
        return StrategySignal("hold")
