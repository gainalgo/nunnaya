# -*- coding: utf-8 -*-
"""Bybit V5 trading surface (spot vs USDT linear perpetual).

Engine/strategy layers remain spot-oriented by default. Setting BYBIT_V5_CATEGORY=linear
routes REST order category + instrument cache to linear so futures-style long/short
(Buy/Sell in one-way mode) can be wired in incrementally.

Dashboard / ``ui_settings.json`` can override via ``set_v5_order_category_runtime``;
when cleared, ``BYBIT_V5_CATEGORY`` env is used again.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

_VALID_CATEGORIES = frozenset({"spot", "linear"})
_lock = threading.Lock()
_runtime_override: Optional[str] = None


def v5_order_category_from_env_only() -> str:
    """Category from ``BYBIT_V5_CATEGORY`` only (ignores dashboard override)."""
    raw = (os.getenv("BYBIT_V5_CATEGORY") or "spot").strip().lower()
    return raw if raw in _VALID_CATEGORIES else "spot"


def get_v5_order_category() -> str:
    """Effective V5 ``category``: UI/runtime override if set, else env."""
    with _lock:
        if _runtime_override is not None and _runtime_override in _VALID_CATEGORIES:
            return _runtime_override
    return v5_order_category_from_env_only()


def set_v5_order_category_runtime(cat: Optional[str]) -> str:
    """Set dashboard override (``spot`` / ``linear``) or ``None``/empty to use env only."""
    global _runtime_override
    with _lock:
        if cat is None or str(cat).strip() == "":
            _runtime_override = None
        else:
            c = str(cat).strip().lower()
            _runtime_override = c if c in _VALID_CATEGORIES else None
    return get_v5_order_category()


def is_v5_order_category_runtime_overridden() -> bool:
    """True if dashboard set an explicit spot/linear (not following ``BYBIT_V5_CATEGORY`` env)."""
    with _lock:
        return _runtime_override is not None


def get_bybit_public_ws_url() -> str:
    """Bybit V5 public WebSocket URL for tickers/orderbook (spot vs linear)."""
    cat = get_v5_order_category()
    return f"wss://stream.bybit.com/v5/public/{cat}"
