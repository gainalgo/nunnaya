# ============================================================
# File: app/manager/profit_store.py
# ------------------------------------------------------------
# ProfitStore
# - Module that computes and stores realized profit per market.
# - Receives signals (buy/sell) from the engine and updates profit.
# ============================================================

from __future__ import annotations
from typing import Dict, Any


class ProfitStore:
    """
    Manages per-market position state and realized profit.
    Structure:
        trades = {
            "XRPUSDT": {
                "position": None or "long",
                "entry_price": float,
                "realized_profit": float
            }
        }
    """

    def __init__(self):
        self.trades: Dict[str, Dict[str, Any]] = {}

    # --------------------------------------------------------
    # Initialize position structure
    # --------------------------------------------------------
    def _ensure(self, market: str):
        if market not in self.trades:
            self.trades[market] = {
                "position": None,
                "entry_price": 0.0,
                "realized_profit": 0.0
            }

    # --------------------------------------------------------
    # Main update logic
    # --------------------------------------------------------
    def update(self, market: str, signal: str, price: float):
        """
        signal: "buy", "sell", "hold"
        """

        self._ensure(market)
        state = self.trades[market]

        pos = state["position"]
        entry = state["entry_price"]

        # --------------------------
        # BUY
        # --------------------------
        if signal == "buy":
            if pos is None:
                state["position"] = "long"
                state["entry_price"] = price

        # --------------------------
        # SELL
        # --------------------------
        elif signal == "sell":
            if pos == "long":
                # Compute profit
                profit = price - entry
                state["realized_profit"] += profit

                # Close position
                state["position"] = None
                state["entry_price"] = 0.0

        # HOLD → do nothing

    # --------------------------------------------------------
    # Query state for a single market
    # --------------------------------------------------------
    def get(self, market: str) -> Dict[str, Any]:
        self._ensure(market)
        return dict(self.trades[market])

    # --------------------------------------------------------
    # Query state for all markets
    # --------------------------------------------------------
    def all(self) -> Dict[str, Any]:
        return {m: dict(v) for m, v in self.trades.items()}


# ------------------------------------------------------------
# Global instance
# ------------------------------------------------------------
profit_store = ProfitStore()
