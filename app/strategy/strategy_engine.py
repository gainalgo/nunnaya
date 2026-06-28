# ============================================================
# File: app/strategy/strategy_engine.py
# ------------------------------------------------------------
# StrategyEngine
# - Manages the strategy policy (refine_policy) and performs automatic fine-tuning
# - Maintains price history per market
# ============================================================

from __future__ import annotations
from typing import Dict, Any


class StrategyEngine:
    """
    Responsible for storing the strategy policy and automatic fine-tuning.
    Handles policy at a higher level than Brain/Judge/Risk/Optimizer.
    """

    def __init__(self):
        # Per-market price history (minimal version)
        self.price_history: Dict[str, list[float]] = {}

    # --------------------------------------------------------
    # Price recording
    # --------------------------------------------------------
    def _record(self, market: str, price: float) -> list[float]:
        arr = self.price_history.setdefault(market, [])
        arr.append(price)

        # Limit history length (keep last 20)
        if len(arr) > 20:
            arr.pop(0)

        return arr

    # --------------------------------------------------------
    # Automatic policy fine-tuning
    # --------------------------------------------------------
    def refine_policy(self, policy: Dict[str, Any], market: str, price: float) -> Dict[str, Any]:
        """
        Simple logic that automatically adjusts the policy based on market
        volatility, recent price changes, and similar factors.

        The refined result is passed straight through to Brain/Judge/Risk/Optimizer.
        """

        refined = dict(policy)  # copy the existing policy

        prices = self._record(market, price)
        if len(prices) < 5:
            return refined  # not enough data, leave unchanged

        avg = sum(prices) / len(prices)
        # Guard: avoid division-by-zero / invalid prices
        if avg <= 0 or prices[0] == 0:
            return refined
        variance = sum((p - avg) ** 2 for p in prices) / len(prices)
        vol = (variance ** 0.5) / avg * 100  # volatility (%)

        trend = (prices[-1] - prices[0]) / prices[0] * 100  # up/down %

        # High volatility -> automatically loosen TP/SL
        if vol > 3:
            refined["tp"] = refined.get("tp", 1.2) * 1.02
            refined["sl"] = refined.get("sl", -3.0) * 1.02

        # Uptrend -> tighten TP
        if trend > 1:
            refined["tp"] = refined.get("tp", 1.2) * 1.01

        # Downtrend -> make SL more conservative
        if trend < -1:
            refined["sl"] = refined.get("sl", -3.0) * 0.99

        return refined
