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
    price: float            # 주문 가격 (MARKET이면 0)
    volume: float          # 주문 수량
    leverage: int = 1       # 레버리지 (현물은 1 고정)
    uuid: str = ""        # 거래소 주문 ID
    status: OrderStatus = OrderStatus.PENDING


@dataclass
class StandardPosition:
    exchange: str
    market: str
    side: Side              # LONG | SHORT
    entry_price: float      # 진입 가격
    qty: float              # 보유 수량
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
