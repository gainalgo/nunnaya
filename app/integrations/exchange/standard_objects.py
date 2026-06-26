from __future__ import annotations

from enum import Enum
from dataclasses import dataclass
from typing import Optional


class Side(Enum):
    LONG = "long"
    SHORT = "short"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELED = "canceled"
    PARTIAL = "partial"


@dataclass
class StandardOrder:
    exchange: str           # "bybit"
    market: str             # "BTCUSDT"
    side: Side              # LONG | SHORT
    order_type: OrderType   # MARKET | LIMIT
    price: float            # order price (0 for MARKET)
    volume: float          # order quantity
    leverage: int = 1       # leverage (fixed at 1 for spot)
    uuid: str = ""        # exchange order ID
    status: OrderStatus = OrderStatus.PENDING


@dataclass
class StandardPosition:
    exchange: str
    market: str
    side: Side              # LONG | SHORT
    entry_price: float      # entry price
    qty: float              # held quantity
    leverage: int = 1
    margin_type: str = "isolated"  # "isolated" | "cross"
    unrealized_pnl: float = 0.0

    def calc_pnl_pct(self, current_price: float) -> float:
        """Calculate PnL percentage for long/short positions.

        Returns percentage (e.g. 1.23 means +1.23%).
        """
        if self.entry_price <= 0:
            return 0.0
        if self.side == Side.LONG:
            return (current_price - self.entry_price) / self.entry_price * 100
        # SHORT
        return (self.entry_price - current_price) / self.entry_price * 100


__all__ = ["Side", "OrderType", "OrderStatus", "StandardOrder", "StandardPosition"]
