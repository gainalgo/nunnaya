"""
Exchange Factory — Bybit(선물) / Upbit(현물)
"""

import os
import logging
from typing import Optional

from app.integrations.exchange_adapter import ExchangeAdapter

logger = logging.getLogger(__name__)


def create_exchange_adapter(exchange: str = "BYBIT", **kwargs) -> ExchangeAdapter:
    """
    거래소 어댑터 생성

    Args:
        exchange: 거래소 이름 ("BYBIT" | "UPBIT")
        **kwargs: 거래소별 설정

    Returns:
        ExchangeAdapter: 거래소 어댑터 인스턴스
    """
    exchange = exchange.upper()

    if exchange == "BYBIT":
        return _create_bybit_adapter(**kwargs)
    elif exchange == "UPBIT":
        return _create_upbit_adapter(**kwargs)
    else:
        raise ValueError(f"Unsupported exchange: {exchange}. Supported: BYBIT, UPBIT.")


def _create_bybit_adapter(**kwargs) -> ExchangeAdapter:
    from app.integrations.bybit_adapter import BybitAdapter
    api_key = os.getenv("BYBIT_API_KEY", kwargs.get("api_key", ""))
    api_secret = os.getenv("BYBIT_API_SECRET", kwargs.get("api_secret", ""))
    return BybitAdapter(api_key, api_secret)


def _create_upbit_adapter(**kwargs) -> ExchangeAdapter:
    from app.integrations.upbit_adapter import UpbitAdapter
    access_key = os.getenv("UPBIT_ACCESS_KEY", kwargs.get("access_key", ""))
    secret_key = os.getenv("UPBIT_SECRET_KEY", kwargs.get("secret_key", ""))
    return UpbitAdapter(access_key, secret_key)


def get_available_exchanges():
    return ["BYBIT", "UPBIT"]


def create_bybit(**kwargs) -> ExchangeAdapter:
    return create_exchange_adapter("BYBIT", **kwargs)


def create_upbit(**kwargs) -> ExchangeAdapter:
    return create_exchange_adapter("UPBIT", **kwargs)
