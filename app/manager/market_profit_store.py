# ============================================================
# File: app/manager/market_profit_store.py
# ------------------------------------------------------------
# MarketProfitStore
# - Module that stores and manages per-market signal statistics.
# - Counts buy/sell/hold occurrences for use in the UI/analysis.
# ============================================================

from __future__ import annotations
from typing import Dict, Any


class MarketProfitStore:
    """
    Store that records per-market signal statistics.
    Example structure:
        stats = {
            "XRPUSDT": { "buy": 10, "sell": 9, "hold": 21 }
        }
    """

    def __init__(self):
        self.stats: Dict[str, Dict[str, int]] = {}

    # --------------------------------------------------------
    # Initialize market
    # --------------------------------------------------------
    def _ensure(self, market: str):
        if market not in self.stats:
            self.stats[market] = {"buy": 0, "sell": 0, "hold": 0}

    # --------------------------------------------------------
    # Update signal
    # --------------------------------------------------------
    def update(self, market: str, signal: str):
        self._ensure(market)

        if signal not in ("buy", "sell", "hold"):
            return

        self.stats[market][signal] += 1

    # --------------------------------------------------------
    # Query
    # --------------------------------------------------------
    def get(self, market: str) -> Dict[str, int]:
        self._ensure(market)
        return dict(self.stats[market])

    # --------------------------------------------------------
    # Query all
    # --------------------------------------------------------
    def all(self) -> Dict[str, Dict[str, int]]:
        return {m: dict(v) for m, v in self.stats.items()}


# ------------------------------------------------------------
# Global instance
# ------------------------------------------------------------
market_profit_store = MarketProfitStore()
