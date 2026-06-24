# ============================================================
# File: app/strategy/strategy_types.py
# ------------------------------------------------------------
# Shared data structures used across the entire strategy layer
# StrategySignal / StrategyPolicy / StrategyBrainOutput
# ============================================================

from __future__ import annotations
from typing import Dict, Any


# ------------------------------------------------------------
# Trade signal structure
# ------------------------------------------------------------
class StrategySignal:
    """
    Final trade signal object.
    signal: "buy", "sell", "hold"
    """

    def __init__(self, signal: str):
        self.signal = signal

    def to_dict(self) -> Dict[str, Any]:
        return {"signal": self.signal}


# ------------------------------------------------------------
# Policy structure (simple dict wrapper)
# ------------------------------------------------------------
class StrategyPolicy(dict):
    """
    Structure representing a strategy policy.
    Effectively operates as a dict, but is wrapped in a class
    for type safety and finer-grained structural extension.
    """

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


# ------------------------------------------------------------
# Brain output structure (technical analysis result)
# ------------------------------------------------------------
class StrategyBrainOutput:
    """
    Stores all technical indicator values computed in the Brain stage.
    Referenced by Judge / Risk / Optimizer alike.
    """

    def __init__(
        self,
        rsi: float | None,
        macd: float | None,
        macd_signal: float | None,
        macd_histogram: float | None,
        sma: float | None,
        ema: float | None,
        volatility: float | None,
        trend: float | None,
        momentum: float | None,
    ):
        self.rsi = rsi
        self.macd = macd
        self.macd_signal = macd_signal
        self.macd_histogram = macd_histogram
        self.sma = sma
        self.ema = ema
        self.volatility = volatility
        self.trend = trend
        self.momentum = momentum

    # --------------------------------------------------------
    # dict conversion
    # --------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "rsi": self.rsi,
            "macd": self.macd,
            "macd_signal": self.macd_signal,
            "macd_histogram": self.macd_histogram,
            "sma": self.sma,
            "ema": self.ema,
            "volatility": self.volatility,
            "trend": self.trend,
            "momentum": self.momentum,
        }
