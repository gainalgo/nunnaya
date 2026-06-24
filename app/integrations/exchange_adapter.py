"""
Exchange integration adapter interface

Defines the common interface that all exchange adapters must implement.
This allows the Bybit exchange to be used in a unified way.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from enum import Enum


class OrderSide(Enum):
    """Order side"""
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """Order type"""
    MARKET = "market"  # market order
    LIMIT = "limit"    # limit order


class OrderStatus(Enum):
    """Order status"""
    PENDING = "pending"      # pending
    FILLED = "filled"        # filled
    CANCELLED = "cancelled"  # cancelled
    FAILED = "failed"        # failed


@dataclass
class MarketInfo:
    """Standard format for market info"""
    exchange: str           # exchange name (BYBIT)
    symbol: str             # market symbol (BTCUSDT)
    base_currency: str      # base currency (BTC, ETH)
    quote_currency: str     # quote currency (USDT)
    min_order_size: float   # minimum order size
    tradable: bool          # whether trading is available


@dataclass
class TickerInfo:
    """Standard format for ticker info"""
    exchange: str               # exchange name
    market_code: str            # market code
    current_price: Any          # current price (Decimal)
    bid_price: Any              # bid price (Decimal)
    ask_price: Any              # ask price (Decimal)
    volume_24h: Any             # 24h volume (Decimal)
    change_24h_pct: Any         # 24h change % (Decimal)
    high_24h: Any               # 24h high (Decimal)
    low_24h: Any                # 24h low (Decimal)
    timestamp: int              # timestamp (ms)


@dataclass
class OrderbookUnit:
    """Orderbook unit"""
    price: float    # price
    size: float     # size


@dataclass
class OrderbookInfo:
    """Standard format for orderbook info"""
    exchange: str               # exchange name
    market_code: str            # market code
    bids: List[tuple]          # bids [(price, size), ...]
    asks: List[tuple]          # asks [(price, size), ...]
    timestamp: int              # timestamp (ms)


@dataclass
class BalanceInfo:
    """Standard format for balance info"""
    exchange: str           # exchange name
    currency: str           # currency code
    available: Any          # available amount (Decimal)
    locked: Any             # amount locked in orders (Decimal)
    total: Any              # total amount (Decimal)


@dataclass
class OrderResult:
    exchange: str               # exchange name
    order_id: str               # unique order ID
    market_code: str            # market code
    side: OrderSide             # order side
    order_type: str             # order type
    price: Optional[Any]        # order price (Decimal, None for market orders)
    amount: Any                 # order amount (Decimal)
    filled_amount: Any          # filled amount (Decimal)
    status: OrderStatus         # order status
    timestamp: str              # order time
    raw_data: Dict              # raw data (wait, done, cancel)
    created_at: str         # order time


class ExchangeAdapter(ABC):
    """
    Abstract base class for exchange integration adapters

    All exchange adapters must inherit from this class and implement it.
    """

    # Exchange branch selector for the SLArbiter/liquidation module (DESIGN_A §4.2, INV-3).
    #   True=futures (has liquidation -> fast_cut) / False=spot (no liquidation -> longhold allowed).
    #   Defaults to True (assumes futures). Spot adapter (UpbitAdapter) overrides to False.
    has_liquidation = True

    @abstractmethod
    def get_name(self) -> str:
        """
        Return the exchange name

        Returns:
            str: exchange name (e.g. "BYBIT")
        """
        pass

    @abstractmethod
    def get_quote_currency(self) -> str:
        """
        Return the exchange's default quote currency

        Returns:
            str: quote currency (e.g. "USDT")
        """
        pass

    # ========== Market Data API ==========

    @abstractmethod
    def get_markets(self) -> List[MarketInfo]:
        """
        Get the full list of tradable markets

        Returns:
            List[MarketInfo]: list of market info
        """
        pass

    @abstractmethod
    def get_ticker(self, market: str) -> TickerInfo:
        """
        Get current price info for a specific market

        Args:
            market: market code (e.g. "BTCUSDT")

        Returns:
            TickerInfo: ticker info
        """
        pass

    @abstractmethod
    def get_tickers(self, markets: Optional[List[str]] = None) -> List[TickerInfo]:
        """
        Get current price info for multiple markets at once

        Args:
            markets: list of market codes (None means all)

        Returns:
            List[TickerInfo]: list of ticker info
        """
        pass

    @abstractmethod
    def get_orderbook(self, market: str) -> OrderbookInfo:
        """
        Get orderbook info for a specific market

        Args:
            market: market code

        Returns:
            OrderbookInfo: orderbook info
        """
        pass

    # ========== Account API ==========

    @abstractmethod
    def get_balances(self) -> List[BalanceInfo]:
        """
        Get all held assets

        Returns:
            List[BalanceInfo]: list of balance info
        """
        pass

    @abstractmethod
    def get_balance(self, currency: str) -> Optional[BalanceInfo]:
        """
        Get the balance for a specific currency

        Args:
            currency: currency code (e.g. "USDT", "BTC")

        Returns:
            Optional[BalanceInfo]: balance info (None if not found)
        """
        pass

    # ========== Order API ==========

    @abstractmethod
    def buy_market_order(
        self,
        market: str,
        volume: Optional[float] = None,
        price: Optional[float] = None
    ) -> OrderResult:
        """
        Market buy order

        Args:
            market: market code
            volume: quantity to buy (Binance style)
            price: buy amount (USDT)

        Returns:
            OrderResult: order result

        Note:
            - Bybit: quote amount (USDT)
            - Binance: uses volume (buy quantity)
        """
        pass

    @abstractmethod
    def sell_market_order(
        self,
        market: str,
        volume: float
    ) -> OrderResult:
        """
        Market sell order

        Args:
            market: market code
            volume: quantity to sell

        Returns:
            OrderResult: order result
        """
        pass

    @abstractmethod
    def buy_limit_order(
        self,
        market: str,
        price: float,
        volume: float
    ) -> OrderResult:
        """
        Limit buy order

        Args:
            market: market code
            price: limit price
            volume: buy quantity

        Returns:
            OrderResult: order result
        """
        pass

    @abstractmethod
    def sell_limit_order(
        self,
        market: str,
        price: float,
        volume: float
    ) -> OrderResult:
        """
        Limit sell order

        Args:
            market: market code
            price: limit price
            volume: sell quantity

        Returns:
            OrderResult: order result
        """
        pass

    @abstractmethod
    def cancel_order(self, uuid: str) -> Dict[str, Any]:
        """
        Cancel an order

        Args:
            uuid: unique order ID

        Returns:
            Dict: cancellation result
        """
        pass

    @abstractmethod
    def get_order(self, uuid: str) -> Dict[str, Any]:
        """
        Get a single order

        Args:
            uuid: unique order ID

        Returns:
            Dict: order details
        """
        pass

    @abstractmethod
    def get_orders(
        self,
        market: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get a list of orders

        Args:
            market: market code (None means all)
            state: order status (wait, done, cancel, etc.)
            limit: number to fetch

        Returns:
            List[Dict]: list of orders
        """
        pass

    # ========== Utility Methods ==========

    @abstractmethod
    def normalize_market_code(self, market: str) -> str:
        """
        Normalize a market code to the exchange's format

        Args:
            market: input market code

        Returns:
            str: normalized market code

        Example:
            Bybit: "BTC" -> "BTCUSDT"
            Binance: "BTC" -> "BTCUSDT"
        """
        pass

    @abstractmethod
    def parse_market_code(self, market: str) -> tuple[str, str]:
        """
        Split a market code into base/quote currencies

        Args:
            market: market code

        Returns:
            tuple[str, str]: (base currency, quote currency)

        Example:
            "BTCUSDT" -> ("BTC", "USDT")
            "BTCUSDT" -> ("BTC", "USDT")
        """
        pass

    @abstractmethod
    def get_fee_rate(self, order_type: str = "market") -> float:
        """
        Return the trading fee rate

        Args:
            order_type: order type (market, limit)

        Returns:
            float: fee rate (0.0005 = 0.05%)
        """
        pass

    @abstractmethod
    def get_min_order_amount(self, market: str) -> float:
        """
        Return the minimum order amount/quantity

        Args:
            market: market code

        Returns:
            float: minimum order size
        """
        pass
    
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(exchange={self.get_name()}, quote={self.get_quote_currency()})>"
