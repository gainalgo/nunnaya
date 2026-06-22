# -*- coding: utf-8 -*-
"""
거래소 통합 모듈 (Bybit Spot).
"""

from __future__ import annotations

import os

from app.integrations.bybit_trade import BybitTradeClient, BybitAPIError


def get_trade_client(
    api_key: str | None = None,
    api_secret: str | None = None,
    **kwargs,
) -> BybitTradeClient:
    """BybitTradeClient 인스턴스 반환."""
    return BybitTradeClient(
        api_key=api_key or os.getenv("BYBIT_API_KEY"),
        api_secret=api_secret or os.getenv("BYBIT_API_SECRET"),
        **kwargs,
    )


# 편의를 위한 alias
TradeClient = BybitTradeClient
APIError = BybitAPIError
