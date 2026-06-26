# -*- coding: utf-8 -*-
"""Binance Instrument Cache — per-symbol trading rules cache (spot / linear(USDT-M futures) split).

Mirror of Bybit's BybitInstrumentCache. Parses the filters in exchangeInfo to
provide tickSize / stepSize / minQty / minNotional. No API key required (public).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from decimal import Decimal, ROUND_DOWN, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)
CACHE_TTL = 3600


def _is_testnet() -> bool:
    return str(os.getenv("BINANCE_TESTNET", "0")).strip().lower() in ("1", "true", "yes")


def _exchange_info_url(category: str) -> str:
    """category: 'spot' | 'linear'(USDT-M futures)."""
    if category == "linear":
        base = "https://testnet.binancefuture.com" if _is_testnet() else "https://fapi.binance.com"
        return f"{base}/fapi/v1/exchangeInfo"
    base = "https://testnet.binance.vision" if _is_testnet() else "https://api.binance.com"
    return f"{base}/api/v3/exchangeInfo"


def _instrument_row(sym: dict) -> Optional[Dict[str, Any]]:
    symbol = sym.get("symbol", "")
    if not symbol:
        return None
    tick = "0.01"
    step = "0.000001"
    min_qty = "0"
    max_qty = "0"
    min_notional = "1"
    for f in sym.get("filters", []):
        ft = f.get("filterType", "")
        if ft == "PRICE_FILTER":
            tick = f.get("tickSize") or tick
        elif ft == "LOT_SIZE":
            step = f.get("stepSize") or step
            min_qty = f.get("minQty") or min_qty
            max_qty = f.get("maxQty") or max_qty
        elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
            min_notional = f.get("minNotional") or f.get("notional") or min_notional
    return {
        "tick_size": float(tick or "0.01"),
        "qty_step": float(step or "0.000001"),
        "min_qty": float(min_qty or "0"),
        "max_qty": float(max_qty or "0"),
        "min_notional": float(min_notional or "1"),
        "status": sym.get("status", ""),
    }


class BinanceInstrumentCache:
    _caches: Dict[str, Dict[str, Any]] = {}
    _loaded_at: Dict[str, float] = {}
    _lock = threading.Lock()

    @classmethod
    def load(cls, *, category: str = "spot", force: bool = False) -> int:
        cat = (category or "spot").lower()
        now = time.time()
        prev = cls._caches.get(cat) or {}
        ts = cls._loaded_at.get(cat, 0.0)
        if not force and prev and (now - ts) < CACHE_TTL:
            return len(prev)
        try:
            resp = requests.get(_exchange_info_url(cat), timeout=10)
            resp.raise_for_status()
            symbols = resp.json().get("symbols", [])
            new_cache: Dict[str, Dict[str, Any]] = {}
            for sym in symbols:
                row = _instrument_row(sym)
                if row:
                    new_cache[sym.get("symbol", "")] = row
            with cls._lock:
                cls._caches[cat] = new_cache
                cls._loaded_at[cat] = now
            logger.info("[BinanceInstrumentCache] Loaded %d instruments (category=%s)", len(new_cache), cat)
            return len(new_cache)
        except Exception as e:
            logger.error("[BinanceInstrumentCache] Failed category=%s: %s", cat, e, exc_info=True)
            return len(cls._caches.get(cat) or {})

    @classmethod
    def get(cls, symbol: str, *, category: str = "spot") -> Optional[Dict[str, Any]]:
        cat = (category or "spot").lower()
        cls._ensure_loaded_category(cat)
        if symbol is None:
            return None
        key = str(symbol).strip().upper()
        return (cls._caches.get(cat) or {}).get(key)

    @classmethod
    def get_tick_size(cls, symbol: str, *, category: str = "spot") -> float:
        info = cls.get(symbol, category=category)
        return info["tick_size"] if info else 0.01

    @classmethod
    def get_qty_step(cls, symbol: str, *, category: str = "spot") -> float:
        info = cls.get(symbol, category=category)
        return info["qty_step"] if info else 0.000001

    @classmethod
    def get_min_qty(cls, symbol: str, *, category: str = "spot") -> float:
        info = cls.get(symbol, category=category)
        return info["min_qty"] if info else 0.0

    @classmethod
    def get_min_notional(cls, symbol: str, *, category: str = "spot") -> float:
        info = cls.get(symbol, category=category)
        return info["min_notional"] if info else 1.0

    @classmethod
    def adjust_price(cls, symbol: str, price: float, side: str = "", *, category: str = "spot") -> float:
        tick = cls.get_tick_size(symbol, category=category)
        if tick <= 0 or price <= 0:
            return price
        p, t = Decimal(str(price)), Decimal(str(tick))
        s = str(side).lower()
        r = ROUND_FLOOR if s in ("buy", "bid") else (ROUND_CEILING if s in ("sell", "ask") else ROUND_HALF_UP)
        return float((p / t).to_integral_value(rounding=r) * t)

    @classmethod
    def adjust_qty(cls, symbol: str, qty: float, *, category: str = "spot") -> float:
        step = cls.get_qty_step(symbol, category=category)
        if step <= 0 or qty <= 0:
            return qty
        return float((Decimal(str(qty)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))

    @classmethod
    def is_tradable(cls, symbol: str, *, category: str = "spot") -> bool:
        info = cls.get(symbol, category=category)
        return info.get("status", "") == "TRADING" if info else False

    @classmethod
    def usdt_symbols(cls, *, category: str = "spot") -> "list[str]":
        cat = (category or "spot").lower()
        cls._ensure_loaded_category(cat)
        c = cls._caches.get(cat) or {}
        return [s for s in c if s.endswith("USDT")]

    @classmethod
    def _ensure_loaded_category(cls, cat: str) -> None:
        now = time.time()
        c = cls._caches.get(cat)
        ts = cls._loaded_at.get(cat, 0.0)
        if c and (now - ts) < CACHE_TTL:
            return
        cls.load(category=cat, force=True)
