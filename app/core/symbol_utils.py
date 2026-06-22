# ============================================================
# File: app/core/symbol_utils.py
# Autocoin OS v3-H — 심볼 변환 유틸리티 (Bybit 전용)
# ============================================================
"""
거래소별 심볼 형식 변환 유틸리티.

Bybit USDT 마켓: BTCUSDT, ETHUSDT
"""

from __future__ import annotations

from typing import Optional

from app.core.currency import Q


def normalize_symbol(symbol: str, target: str = "ccxt") -> str:
    """어떤 형식이든 목표 형식으로 정규화.

    Args:
        symbol: 입력 심볼 (어떤 형식이든)
        target:
            - "ccxt": "BTC/USDT" (ccxt 표준)
            - "bybit": "BTCUSDT"
            - "ws": "btcusdt" (WS용)

    Returns:
        정규화된 심볼
    """
    symbol = str(symbol).upper().strip()

    # Q를 사용하여 base 추출
    base = Q.extract_base(symbol)

    # 목표 형식으로 변환
    if target == "ccxt":
        return Q.market_ccxt(base)
    elif target == "bybit":
        return Q.market(base)
    elif target == "ws":
        return Q.market_ws(base)

    return Q.market_ccxt(base)


def extract_base_currency(symbol: str) -> str:
    """심볼에서 기본 통화(베이스) 추출.

    Examples:
        >>> extract_base_currency("BTCUSDT")
        'BTC'
        >>> extract_base_currency("BTC/USDT")
        'BTC'
    """
    return Q.extract_base(symbol)


def get_quote_currency(symbol: str) -> str:
    """심볼에서 기축 통화(쿼트) 추출.

    Examples:
        >>> get_quote_currency("BTCUSDT")
        'USDT'
        >>> get_quote_currency("BTC/USDT")
        'USDT'
    """
    _, quote = Q.parse_market(symbol)
    return quote
