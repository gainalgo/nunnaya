# -*- coding: utf-8 -*-
"""
Exchange abstraction base classes and protocols.

These interfaces allow the core system to work with any exchange
without depending on specific implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


class ExchangeType(str, Enum):
    """Supported exchange types."""
    BYBIT = "bybit"
    UPBIT = "upbit"

    def __str__(self) -> str:
        return self.value


@dataclass
class BalanceInfo:
    """Unified balance information."""
    currency: str
    free: float  # Available balance
    locked: float  # In orders
    total: float  # free + locked
    avg_buy_price: float = 0.0  # Average buy price (if available)
    unit_currency: str = "USDT"  # Base currency
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BalanceInfo":
        return cls(
            currency=str(d.get("currency", "")),
            free=float(d.get("balance", 0) or d.get("free", 0) or 0),
            locked=float(d.get("locked", 0) or 0),
            total=float(d.get("total", 0) or 0),
            avg_buy_price=float(d.get("avg_buy_price", 0) or 0),
            unit_currency=str(d.get("unit_currency", "USDT")),
        )


@dataclass
class OrderInfo:
    """Unified order information."""
    uuid: str
    market: str
    side: str  # "bid" (buy) or "ask" (sell)
    ord_type: str  # "market", "limit", "price" (quote-based market)
    state: str  # "wait", "done", "cancel"
    price: float
    volume: float
    remaining_volume: float
    executed_volume: float
    avg_price: float
    paid_fee: float = 0.0
    fee_currency: str = "USDT"
    created_at: str = ""
    trades_count: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_complete(self) -> bool:
        return self.state in ("done", "cancel")
    
    @property
    def is_filled(self) -> bool:
        return self.state == "done"
    
    @property
    def is_cancelled(self) -> bool:
        return self.state == "cancel"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "market": self.market,
            "side": self.side,
            "ord_type": self.ord_type,
            "state": self.state,
            "price": str(self.price),
            "volume": str(self.volume),
            "remaining_volume": str(self.remaining_volume),
            "executed_volume": str(self.executed_volume),
            "avg_price": str(self.avg_price),
            "paid_fee": str(self.paid_fee),
            "fee_currency": self.fee_currency,
            "created_at": self.created_at,
            "trades_count": self.trades_count,
        }


@dataclass
class OrderResult:
    """Result of an order operation."""
    success: bool
    order: Optional[OrderInfo] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    
    @property
    def uuid(self) -> str:
        return self.order.uuid if self.order else ""


@dataclass
class QuickSellResult:
    """Result of quick sell (IOC) operation."""
    success: bool
    action: str  # "filled", "partial", "cancelled", "error"
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_price: float = 0.0
    order: Optional[OrderInfo] = None
    message: str = ""


# =============================================================================
# Protocol Definitions (Interface Contracts)
# =============================================================================

@runtime_checkable
class SymbolMapper(Protocol):
    """Interface for symbol/market name conversion."""
    
    @property
    def exchange_type(self) -> ExchangeType:
        """Return the exchange type."""
        ...
    
    @property
    def quote_currency(self) -> str:
        """Return the quote currency (USDT)."""
        ...
    
    def to_internal(self, exchange_symbol: str) -> str:
        """Convert exchange symbol to internal format.

        Example:
            Bybit: "BTCUSDT" -> "BTC/USDT"
        """
        ...

    def to_exchange(self, internal_symbol: str) -> str:
        """Convert internal symbol to exchange format.

        Example:
            Bybit: "BTC/USDT" -> "BTCUSDT"
        """
        ...
    
    def normalize(self, symbol: str) -> str:
        """Normalize any symbol format to internal format."""
        ...
    
    def get_base_currency(self, symbol: str) -> str:
        """Extract base currency from symbol.

        Example: "BTC/USDT" -> "BTC"
        """
        ...


@runtime_checkable
class TradeClient(Protocol):
    """Interface for exchange trading operations."""
    
    @property
    def exchange_type(self) -> ExchangeType:
        """Return the exchange type."""
        ...
    
    # =========================================================================
    # Balance
    # =========================================================================
    def accounts(
        self,
        *,
        skip_currencies: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Get all account balances."""
        ...
    
    def get_balance(
        self,
        currency: str,
        *,
        include_locked: bool = False,
    ) -> float:
        """Get balance for a specific currency."""
        ...
    
    # =========================================================================
    # Orders - Query
    # =========================================================================
    def list_orders(
        self,
        *,
        state: str = "wait",
        market: Optional[str] = None,
        limit: int = 100,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """List orders by state."""
        ...
    
    def get_order(
        self,
        *,
        uuid: str,
        market: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get order details by ID."""
        ...
    
    # =========================================================================
    # Orders - Execute
    # =========================================================================
    def place_order(
        self,
        *,
        market: str,
        side: str,  # "bid" or "ask"
        ord_type: str,  # "market", "limit", "price"
        volume: Optional[float] = None,
        price: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Place a new order."""
        ...
    
    def market_buy(
        self,
        market: str,
        quote_amount: float,  # USDT amount
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Market buy with quote currency amount."""
        ...
    
    def market_sell(
        self,
        market: str,
        qty: float,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Market sell with base currency quantity."""
        ...
    
    def limit_buy(
        self,
        market: str,
        price: float,
        volume: float,
    ) -> Dict[str, Any]:
        """Limit buy order."""
        ...
    
    def limit_sell(
        self,
        market: str,
        price: float,
        volume: float,
    ) -> Dict[str, Any]:
        """Limit sell order."""
        ...
    
    def quick_sell(
        self,
        market: str,
        qty: float,
        price: float,
    ) -> Dict[str, Any]:
        """Quick sell (IOC) - cancel if not filled immediately."""
        ...
    
    # =========================================================================
    # Orders - Cancel / Wait
    # =========================================================================
    def cancel_order(
        self,
        *,
        uuid: str,
        market: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel an order."""
        ...
    
    def wait_order(
        self,
        *,
        uuid: str,
        market: Optional[str] = None,
        timeout_sec: float = 30.0,
        poll_interval: float = 1.0,
    ) -> Dict[str, Any]:
        """Wait for order completion."""
        ...
    
    # =========================================================================
    # Utilities
    # =========================================================================
    def get_min_order_amount(self, symbol: str) -> Dict[str, float]:
        """Get minimum order amount/cost for a symbol."""
        ...
    
    def get_api_stats(self) -> Dict[str, Any]:
        """Get API usage statistics."""
        ...


@runtime_checkable
class PriceFeed(Protocol):
    """Interface for price data streaming."""
    
    @property
    def exchange_type(self) -> ExchangeType:
        """Return the exchange type."""
        ...
    
    def start(self) -> None:
        """Start the price feed."""
        ...
    
    def stop(self) -> None:
        """Stop the price feed."""
        ...
    
    def subscribe(self, markets: List[str]) -> None:
        """Subscribe to market price updates."""
        ...
    
    def unsubscribe(self, markets: List[str]) -> None:
        """Unsubscribe from market price updates."""
        ...
    
    def get_price(self, market: str) -> Optional[float]:
        """Get current price for a market."""
        ...
    
    def get_volume(self, market: str) -> Optional[float]:
        """Get 24h volume for a market."""
        ...


# =============================================================================
# Exchange Adapter (Composite)
# =============================================================================

class ExchangeAdapter(ABC):
    """
    Composite adapter that bundles all exchange components.
    
    This is the main entry point for exchange operations.
    Each exchange implementation should inherit from this.
    """
    
    @property
    @abstractmethod
    def exchange_type(self) -> ExchangeType:
        """Return the exchange type."""
        pass
    
    @property
    @abstractmethod
    def quote_currency(self) -> str:
        """Return the quote currency (USDT)."""
        pass
    
    @property
    @abstractmethod
    def trade_client(self) -> TradeClient:
        """Return the trade client."""
        pass
    
    @property
    @abstractmethod
    def price_feed(self) -> PriceFeed:
        """Return the price feed."""
        pass
    
    @property
    @abstractmethod
    def symbol_mapper(self) -> SymbolMapper:
        """Return the symbol mapper."""
        pass
    
    @abstractmethod
    def get_runtime_path(self, filename: str) -> str:
        """Get exchange-specific runtime file path.

        Example:
            bybit.get_runtime_path("trade_ledger.jsonl")
            -> "runtime/BYBIT/trade_ledger.jsonl"
        """
        pass
    
    def normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol to internal format."""
        return self.symbol_mapper.normalize(symbol)
    
    def get_base_currency(self, symbol: str) -> str:
        """Extract base currency from symbol."""
        return self.symbol_mapper.get_base_currency(symbol)
