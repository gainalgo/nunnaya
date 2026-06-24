# -*- coding: utf-8 -*-
"""
Exchange integration module (Bybit Spot).
"""

from __future__ import annotations

import os

from app.integrations.bybit_trade import BybitTradeClient, BybitAPIError


def get_trade_client(
    api_key: str | None = None,
    api_secret: str | None = None,
    **kwargs,
) -> BybitTradeClient:
    """Return a BybitTradeClient instance."""
    return BybitTradeClient(
        api_key=api_key or os.getenv("BYBIT_API_KEY"),
        api_secret=api_secret or os.getenv("BYBIT_API_SECRET"),
        **kwargs,
    )


# Convenience aliases
TradeClient = BybitTradeClient
APIError = BybitAPIError
