# ============================================================
# File: app/core/symbol_utils.py
# Autocoin OS v3-H — Symbol conversion utilities (Bybit only)
# ============================================================
"""
Per-exchange symbol format conversion utilities.

Bybit USDT market: BTCUSDT, ETHUSDT
"""

from __future__ import annotations

from typing import Optional

from app.core.currency import Q


def normalize_symbol(symbol: str, target: str = "ccxt") -> str:
    """Normalize any format into the target format.

    Args:
        symbol: input symbol (any format)
        target:
            - "ccxt": "BTC/USDT" (ccxt standard)
            - "bybit": "BTCUSDT"
            - "ws": "btcusdt" (for WS)

    Returns:
        normalized symbol
    """
    symbol = str(symbol).upper().strip()

    # Extract base using Q
    base = Q.extract_base(symbol)

    # Convert to target format
    if target == "ccxt":
        return Q.market_ccxt(base)
    elif target == "bybit":
        return Q.market(base)
    elif target == "ws":
        return Q.market_ws(base)

    return Q.market_ccxt(base)


def extract_base_currency(symbol: str) -> str:
    """Extract the base currency from a symbol.

    Examples:
        >>> extract_base_currency("BTCUSDT")
        'BTC'
        >>> extract_base_currency("BTC/USDT")
        'BTC'
    """
    return Q.extract_base(symbol)


def get_quote_currency(symbol: str) -> str:
    """Extract the quote currency from a symbol.

    Examples:
        >>> get_quote_currency("BTCUSDT")
        'USDT'
        >>> get_quote_currency("BTC/USDT")
        'USDT'
    """
    _, quote = Q.parse_market(symbol)
    return quote
