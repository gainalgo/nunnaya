# -*- coding: utf-8 -*-
"""Bybit Instrument Cache — per-symbol trading rules cache (spot / linear separated)."""
from __future__ import annotations

import logging
import time
import threading
from typing import Any, Dict, Optional

from app.core.bybit_trading import get_v5_order_category
from app.core.rate_limiter import bybit_get
from app.core.constants import BYBIT_MARKET_INSTRUMENTS

logger = logging.getLogger(__name__)
CACHE_TTL = 3600


def _instrument_row(inst: dict) -> Optional[Dict[str, Any]]:
    symbol = inst.get("symbol", "")
    if not symbol:
        return None
    lot = inst.get("lotSizeFilter") or {}
    pf = inst.get("priceFilter") or {}
    tick_raw = pf.get("tickSize") or pf.get("tick_size") or "0.01"
    step_raw = lot.get("qtyStep") or lot.get("basePrecision") or "0.000001"
    min_amt_raw = lot.get("minOrderAmt") or lot.get("minNotionalValue") or "1"
    # Bybit's own risk tier: priceLimitRatioY (how far price may deviate). High-risk/Innovation-Zone
    # coins get a wider ratio (e.g. 0.3) vs blue chips (BTC ~0.02). Used as the futures analog of an
    # exchange "warning listing" flag. [2026-06-27]
    rp = inst.get("riskParameters") or {}
    try:
        price_limit_ratio = float(rp.get("priceLimitRatioY") or rp.get("priceLimitRatioX") or 0.0)
    except (TypeError, ValueError):
        price_limit_ratio = 0.0
    return {
        "tick_size": float(tick_raw or "0.01"),
        "qty_step": float(step_raw or "0.000001"),
        "min_qty": float(lot.get("minOrderQty", "0") or "0"),
        "max_qty": float(lot.get("maxOrderQty", "0") or "0"),
        "min_notional": float(min_amt_raw or "1"),
        "status": inst.get("status", ""),
        "price_limit_ratio": price_limit_ratio,
    }


class BybitInstrumentCache:
    _caches: Dict[str, Dict[str, Any]] = {}
    _loaded_at: Dict[str, float] = {}
    _lock = threading.Lock()

    @classmethod
    def load(cls, *, category: Optional[str] = None, force: bool = False) -> int:
        cat = (category or get_v5_order_category()).lower()
        now = time.time()
        prev = cls._caches.get(cat) or {}
        ts = cls._loaded_at.get(cat, 0.0)
        if not force and prev and (now - ts) < CACHE_TTL:
            return len(prev)
        try:
            resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": cat}, timeout=10)
            resp.raise_for_status()
            instruments = resp.json().get("result", {}).get("list", [])
            new_cache: Dict[str, Dict[str, Any]] = {}
            for inst in instruments:
                row = _instrument_row(inst)
                if row:
                    new_cache[inst.get("symbol", "")] = row
            with cls._lock:
                cls._caches[cat] = new_cache
                cls._loaded_at[cat] = now
            logger.info("[BybitInstrumentCache] Loaded %d instruments (category=%s)", len(new_cache), cat)
            return len(new_cache)
        except Exception as e:
            logger.error("[BybitInstrumentCache] Failed category=%s: %s", cat, e, exc_info=True)
            return len(cls._caches.get(cat) or {})

    @classmethod
    def get(cls, symbol: str, *, category: Optional[str] = None) -> Optional[Dict[str, Any]]:
        cat = (category or get_v5_order_category()).lower()
        cls._ensure_loaded_category(cat)
        if symbol is None:
            return None
        key = str(symbol).strip().upper()
        return (cls._caches.get(cat) or {}).get(key)

    @classmethod
    def get_tick_size(cls, symbol: str) -> float:
        info = cls.get(symbol)
        return info["tick_size"] if info else 0.01

    @classmethod
    def get_qty_step(cls, symbol: str, *, category: Optional[str] = None) -> float:
        info = cls.get(symbol, category=category)
        return info["qty_step"] if info else 0.000001

    @classmethod
    def get_price_limit_ratio(cls, symbol: str, *, category: Optional[str] = None) -> float:
        """Bybit's risk tier (priceLimitRatioY). High-risk/Innovation-Zone coins ~0.3, blue chips ~0.02.
        Returns 0.0 if unknown (treated as 'no warning flag')."""
        info = cls.get(symbol, category=category)
        return float(info.get("price_limit_ratio", 0.0)) if info else 0.0

    @classmethod
    def get_min_qty(cls, symbol: str, *, category: Optional[str] = None) -> float:
        info = cls.get(symbol, category=category)
        return info["min_qty"] if info else 0.0

    @classmethod
    def get_min_notional(cls, symbol: str, *, category: Optional[str] = None) -> float:
        info = cls.get(symbol, category=category)
        return info["min_notional"] if info else 1.0

    @classmethod
    def adjust_price(cls, symbol: str, price: float, side: str = "") -> float:
        tick = cls.get_tick_size(symbol)
        if tick <= 0 or price <= 0:
            return price
        from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP
        p, t = Decimal(str(price)), Decimal(str(tick))
        s = str(side).lower()
        r = ROUND_FLOOR if s in ("buy", "bid") else (ROUND_CEILING if s in ("sell", "ask") else ROUND_HALF_UP)
        return float((p / t).to_integral_value(rounding=r) * t)

    @classmethod
    def adjust_qty(cls, symbol: str, qty: float, *, category: Optional[str] = None) -> float:
        step = cls.get_qty_step(symbol, category=category)
        if step <= 0 or qty <= 0:
            return qty
        from decimal import Decimal, ROUND_DOWN
        return float((Decimal(str(qty)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))

    @classmethod
    def is_tradable(cls, symbol: str) -> bool:
        info = cls.get(symbol)
        return info.get("status", "") == "Trading" if info else False

    @classmethod
    def usdt_symbols(cls) -> list[str]:
        cat = get_v5_order_category().lower()
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

    @classmethod
    def _ensure_loaded(cls) -> None:
        cls._ensure_loaded_category(get_v5_order_category().lower())
