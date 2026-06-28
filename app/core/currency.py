# ============================================================
# File: app/core/currency.py
# Autocoin OS v3-H — Quote Currency Abstraction Layer
# ============================================================
# 
# This module abstracts the quote currency used across the entire
# system. Bybit USDT only.
#
# Usage:
#   from app.core.currency import Q
#
#   Q.symbol          # "USDT"
#   Q.format(50000)   # "50,000.00 USDT"
#   Q.market("BTC")   # "BTCUSDT"
#   Q.min_order       # 5
# ============================================================

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass(frozen=True)
class CurrencyConfig:
    """Per-quote-currency settings."""
    symbol: str                    # "USDT"
    name: str                      # "Korean Won" or "Tether"
    min_order: float               # minimum order amount
    order_unit: float              # order unit
    decimals: int                  # decimal places (for amount display)
    market_prefix: str             # market prefix (Bybit: "")
    market_suffix: str             # market suffix ("" or "USDT")
    market_separator: str          # market separator ("-" or "/")
    display_suffix: str            # display suffix (" USDT")
    exchange: str                  # exchange name ("bybit")
    api_base: str                  # API base URL
    order_side_buy: str            # buy-side label ("bid" or "buy")
    order_side_sell: str           # sell-side label ("ask" or "sell")
    # API key env var names
    env_access_key: str            # access key env var name
    env_secret_key: str            # secret key env var name


# Bybit USDT currency settings
_CURRENCY_CONFIGS = {
    "USDT": CurrencyConfig(
        symbol="USDT",
        name="Tether",
        min_order=5.0,
        order_unit=0.01,
        decimals=2,
        market_prefix="",
        market_suffix="USDT",
        market_separator="/",
        display_suffix=" USDT",
        exchange="bybit",
        api_base="https://api.bybit.com",
        order_side_buy="Buy",
        order_side_sell="Sell",
        env_access_key="BYBIT_API_KEY",
        env_secret_key="BYBIT_API_SECRET",
    ),
}


class QuoteCurrency:
    """Quote currency abstraction class.

    Singleton interface for consistent currency handling across the system.
    """
    
    def __init__(self, symbol: str = "USDT"):
        self._symbol = symbol.upper().strip()
        if self._symbol not in _CURRENCY_CONFIGS:
            raise ValueError(f"Unsupported currency: {self._symbol}. Use 'USDT'.")
        self._config = _CURRENCY_CONFIGS[self._symbol]
    
    @property
    def config(self) -> CurrencyConfig:
        """Return the current currency settings."""
        return self._config

    @property
    def symbol(self) -> str:
        """Currency symbol (e.g. "USDT")."""
        return self._config.symbol

    @property
    def name(self) -> str:
        """Currency name."""
        return self._config.name

    @property
    def min_order(self) -> float:
        """Minimum order amount."""
        return self._config.min_order

    @property
    def order_unit(self) -> float:
        """Order unit."""
        return self._config.order_unit

    @property
    def decimals(self) -> int:
        """Decimal places."""
        return self._config.decimals

    @property
    def exchange(self) -> str:
        """Exchange name."""
        return self._config.exchange

    @property
    def api_base(self) -> str:
        """API base URL."""
        return self._config.api_base

    @property
    def is_usdt(self) -> bool:
        """Check whether running in USDT mode."""
        return self._symbol == "USDT"

    # =========================================================================
    # API keys
    # =========================================================================

    @property
    def env_access_key(self) -> str:
        """Access key env var name."""
        return self._config.env_access_key

    @property
    def env_secret_key(self) -> str:
        """Secret key env var name."""
        return self._config.env_secret_key

    def get_access_key(self) -> str:
        """Look up the access key from environment variables."""
        return os.getenv(self._config.env_access_key, "").strip()

    def get_secret_key(self) -> str:
        """Look up the secret key from environment variables."""
        return os.getenv(self._config.env_secret_key, "").strip()

    def has_api_keys(self) -> bool:
        """Check whether API keys are configured."""
        return bool(self.get_access_key() and self.get_secret_key())

    # =========================================================================
    # Amount formatting
    # =========================================================================

    def format(self, amount: float, *, with_suffix: bool = True) -> str:
        """Format an amount for the currency.

        Examples:
            50.00 → "50.00 USDT"
            1234.56 → "1,234.56 USDT"
        """
        try:
            val = float(amount)
            if self._config.decimals == 0:
                formatted = f"{val:,.0f}"
            else:
                formatted = f"{val:,.{self._config.decimals}f}"
            
            if with_suffix:
                return f"{formatted}{self._config.display_suffix}"
            return formatted
        except (TypeError, ValueError):
            logger.warning("[Currency] format() failed for amount=%r", amount)
            return str(amount)
    
    def format_compact(self, amount: float) -> str:
        """Format an amount in compact form (K, M, B suffixes).
        
        Examples:
            1500000 → "1.5M"
            50000 → "50K"
        """
        try:
            val = abs(float(amount))
            sign = "-" if float(amount) < 0 else ""
            
            if val >= 1_000_000_000:
                return f"{sign}{val/1_000_000_000:.1f}B"
            elif val >= 1_000_000:
                return f"{sign}{val/1_000_000:.1f}M"
            elif val >= 1_000:
                return f"{sign}{val/1_000:.1f}K"
            else:
                return self.format(amount, with_suffix=False)
        except (TypeError, ValueError):
            logger.warning("[Currency] format_compact() failed for amount=%r", amount)
            return str(amount)
    
    # =========================================================================
    # Market symbol conversion
    # =========================================================================

    def market(self, base: str) -> str:
        """Build a market symbol from the base currency.

        Args:
            base: base currency (e.g. "BTC", "ETH")

        Returns:
            Example: "BTCUSDT"
            USDT: "BTCUSDT"
        """
        b = str(base).upper().strip()
        # Already a complete symbol — return as-is
        if self._is_complete_market(b):
            return b
        # Strip any prefix/suffix
        b = self._extract_base(b)
        
        if self._config.market_prefix:
            return f"{self._config.market_prefix}{b}"
        elif self._config.market_suffix:
            return f"{b}{self._config.market_suffix}"
        else:
            return f"{b}{self._config.market_separator}{self._config.symbol}"
    
    def market_ccxt(self, base: str) -> str:
        """Build a CCXT-style market symbol (e.g. "BTC/USDT").

        Args:
            base: base currency (e.g. "BTC")
        
        Returns:
            "BTC/USDT"
        """
        b = self._extract_base(base)
        return f"{b}/{self._config.symbol}"
    
    def market_ws(self, base: str) -> str:
        """Build a WebSocket-style market symbol (lowercase).

        Args:
            base: base currency (e.g. "BTC")
        
        Returns:
            Example: "btcusdt"
            USDT: "btcusdt"
        """
        return self.market(base).lower()
    
    def parse_market(self, market: str) -> Tuple[str, str]:
        """Extract base and quote from a market symbol.

        Args:
            market: market symbol (e.g. "BTCUSDT", "BTC/USDT")

        Returns:
            (base, quote) tuple (e.g. ("BTC", "USDT"))
        """
        m = str(market).upper().strip()
        
        # Dash format (BTC-XXX) — Upbit/Poloniex legacy, not used on Bybit
        if m.startswith("BTC-"):
            return (m[4:], "BTC")
        
        # BTC/USDT format
        if "/" in m:
            parts = m.split("/", 1)
            return (parts[0], parts[1])

        # BTCUSDT format
        for quote in ("USDT", "BUSD", "USDC"):
            # Bare quote symbols ("USDT") are not complete markets.
            # Require at least 1-char base to avoid parsing base="".
            if len(m) > len(quote) and m.endswith(quote):
                return (m[:-len(quote)], quote)
        
        # Unknown format — return base only
        return (m, self._config.symbol)

    def extract_base(self, market: str) -> str:
        """Extract the base currency from a market symbol.

        Args:
            market: market symbol (e.g. "BTCUSDT")

        Returns:
            base currency (e.g. "BTC")
        """
        base, _ = self.parse_market(market)
        return base
    
    def normalize(self, market: str) -> str:
        """Normalize a market symbol to the current currency format.

        Converts various input formats to the currently configured currency format.

        Examples:
            Bybit: "BTC" → "BTCUSDT"
            
        """
        base, _ = self.parse_market(market)
        return self.market(base)
    
    def _is_complete_market(self, s: str) -> bool:
        """Check whether this is a complete market symbol."""
        return s.endswith("USDT") or s.endswith("BUSD") or "/" in s

    def _extract_base(self, s: str) -> str:
        """Extract just the base currency from a string."""
        base, _ = self.parse_market(s)
        return base
    
    # =========================================================================
    # Order side conversion
    # =========================================================================

    @property
    def side_buy(self) -> str:
        """Buy-side string."""
        return self._config.order_side_buy

    @property
    def side_sell(self) -> str:
        """Sell-side string."""
        return self._config.order_side_sell

    def normalize_side(self, side: str) -> str:
        """Normalize an order side to the current exchange's format.

        Args:
            side: one of "buy", "sell", "bid", "ask"

        Returns:
            side string matching the current exchange
        """
        s = str(side).lower().strip()
        if s in ("buy", "bid", "long"):
            return self._config.order_side_buy
        elif s in ("sell", "ask", "short"):
            return self._config.order_side_sell
        return s
    
    def is_buy_side(self, side: str) -> bool:
        """Check whether this is a buy side."""
        return str(side).lower().strip() in ("buy", "bid", "long")

    def is_sell_side(self, side: str) -> bool:
        """Check whether this is a sell side."""
        return str(side).lower().strip() in ("sell", "ask", "short")
    
    # =========================================================================
    # Amount validation and conversion
    # =========================================================================

    def floor_to_unit(self, amount: float) -> float:
        """Floor an amount to the order unit.

        Examples:
            USDT: 10.567 → 10.56
        """
        if self._config.order_unit <= 0:
            return float(amount)
        return float(int(float(amount) / self._config.order_unit) * self._config.order_unit)
    
    def is_valid_order_amount(self, amount: float) -> bool:
        """Check whether an order amount meets the minimum."""
        return float(amount) >= self._config.min_order

    def validate_order_amount(self, amount: float) -> Tuple[bool, str]:
        """Validate an order amount and return a message.

        Returns:
            (valid, message) tuple
        """
        val = float(amount)
        if val < self._config.min_order:
            return (False, f"Below minimum order amount: {self.format(val)} < {self.format(self._config.min_order)}")
        return (True, "")
    
    # =========================================================================
    # Utilities
    # =========================================================================
    
    def __repr__(self) -> str:
        return f"QuoteCurrency({self._symbol})"
    
    def __str__(self) -> str:
        return self._symbol
    
    def to_dict(self) -> dict:
        """Return the settings as a dictionary."""
        return {
            "symbol": self._config.symbol,
            "name": self._config.name,
            "min_order": self._config.min_order,
            "order_unit": self._config.order_unit,
            "decimals": self._config.decimals,
            "exchange": self._config.exchange,
            "api_base": self._config.api_base,
            "market_prefix": self._config.market_prefix,
            "market_suffix": self._config.market_suffix,
            "is_usdt": self.is_usdt,
            "has_api_keys": self.has_api_keys(),
            "env_access_key": self._config.env_access_key,
            "env_secret_key": self._config.env_secret_key,
        }


# ============================================================
# Global singleton instance
# ============================================================

# Load the quote currency setting from environment variables (Bybit = USDT)
_QUOTE_CURRENCY_SYMBOL = _env_str("QUOTE_CURRENCY", "USDT").upper()
if _QUOTE_CURRENCY_SYMBOL not in _CURRENCY_CONFIGS:
    _QUOTE_CURRENCY_SYMBOL = "USDT"

# Global instance (usable in short form as Q)
Q = QuoteCurrency(_QUOTE_CURRENCY_SYMBOL)


# ============================================================
# Convenience functions (directly importable)
# ============================================================

def get_quote_currency() -> QuoteCurrency:
    """Return the currently configured quote currency instance."""
    return Q


def set_quote_currency(symbol: str) -> QuoteCurrency:
    """Change the quote currency (for runtime switching).

    Note: this function mutates global state.
    Configuring it via an environment variable at server startup is generally recommended.
    """
    global Q
    Q = QuoteCurrency(symbol)
    return Q


def format_amount(amount: float, *, with_suffix: bool = True) -> str:
    """Format an amount for the current currency."""
    return Q.format(amount, with_suffix=with_suffix)


def market(base: str) -> str:
    """Build a market symbol from the base currency."""
    return Q.market(base)


def normalize_market(market_str: str) -> str:
    """Normalize a market symbol to the current currency format."""
    return Q.normalize(market_str)


def extract_base(market_str: str) -> str:
    """Extract the base currency from a market symbol."""
    return Q.extract_base(market_str)


# ============================================================
# Examples for testing
# ============================================================

if __name__ == "__main__":
    print(f"Current Quote Currency: {Q}")
    print(f"Config: {Q.to_dict()}")
    print()
    print("Format examples:")
    print(f"  50000 → {Q.format(50000)}")
    print(f"  1234567.89 → {Q.format(1234567.89)}")
    print(f"  Compact 1500000 → {Q.format_compact(1500000)}")
    print()
    print("Market examples:")
    print(f"  BTC → {Q.market('BTC')}")
    print(f"  ETH → {Q.market('ETH')}")
    print(f"  CCXT BTC → {Q.market_ccxt('BTC')}")
    print(f"  WS BTC → {Q.market_ws('BTC')}")
    print()
    print("Parse examples:")
    print(f"  BTCUSDT → {Q.parse_market('BTCUSDT')}")
    print(f"  BTCUSDT → {Q.parse_market('BTCUSDT')}")
    print(f"  BTC/USDT → {Q.parse_market('BTC/USDT')}")
    print()
    print("Normalize examples:")
    print(f"  BTCUSDT → {Q.normalize('BTCUSDT')}")
    print(f"  BTC/USDT → {Q.normalize('BTC/USDT')}")
    print(f"  ETHUSDT → {Q.normalize('ETHUSDT')}")
