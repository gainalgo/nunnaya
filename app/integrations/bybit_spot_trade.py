# -*- coding: utf-8 -*-
"""Bybit V5 SPOT trade client — adapts SpotGazuaManager's client interface to Bybit spot.

[2026-06-17 owner] Build Bybit spot first to establish the "USDT-spot FOCUS" reference base
(Binance spot later inherits from this). SpotGazuaManager is exchange-agnostic (depends only on
the client method interface), so implementing the Upbit-style methods on Bybit v5 spot lets the
brain (scan/entry/exit/scoring) be reused as-is.

Core buy/sell/balance/order/candle are already provided by BybitTradeClient(category="spot") → inherited.
Only the missing public market queries (market list/ticker/price/orderbook) and helpers
(open_orders/MIN_ORDER) are filled in here.

Differences from KRW spot:
  - quote = USDT (not KRW). symbol = "BTCUSDT" (not KRW-BTC).
    Note: Upbit base_currency("BTCUSDT")=="BTC" already works (to_upbit_market handles the USDT
    suffix) → manager-compatible.
  - No Korean-style investment-warning/caution (market_warning) → get_market_warnings() = {} (no blocking).
  - Minimum order = USDT notional (per-symbol min_notional is handled by the base BybitInstrumentCache).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from app.core.constants import (
    BYBIT_MARKET_INSTRUMENTS,
    BYBIT_MARKET_ORDERBOOK,
    BYBIT_MARKET_TICKERS,
)
from app.core.rate_limiter import bybit_get
from app.integrations.bybit_trade import BybitTradeClient

logger = logging.getLogger(__name__)

# Coarse USDT minimum-order floor that stands in for Upbit MIN_ORDER_KRW(5000) (per-symbol precise min validated by base).
MIN_ORDER_USDT = 5.0


def bybit_base_currency(symbol: str) -> str:
    """Bybit symbol ("BTCUSDT") → base ("BTC"). Strips the quote suffix. (Helper to match Upbit base_currency results)"""
    s = str(symbol).upper().replace("-", "").replace("/", "")
    for q in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


class BybitSpotTradeClient(BybitTradeClient):
    """Bybit spot. Compatible with the UpbitTradeClient interface — inherits BybitTradeClient(category="spot")."""

    # Read by the manager via getattr(client, "MIN_ORDER_KRW", 5000.0) (name kept, unit=USDT).
    MIN_ORDER_KRW = MIN_ORDER_USDT

    def __init__(self, api_key: str = "", api_secret: str = "", *, timeout: float = 10.0):
        super().__init__(api_key or None, api_secret or None, timeout=timeout, category="spot")
        self._mkt_cache: tuple = (0.0, [])

    # ── base helper ───────────────────────────────────────────────────
    @staticmethod
    def base_currency(symbol: str) -> str:
        return bybit_base_currency(symbol)

    # ── Market list (all USDT spot) — mirrors Upbit shape ───────────────
    def get_all_markets(self) -> List[Dict[str, Any]]:
        """All USDT spot markets. Mirrors Upbit shape({market, market_event}) (no spot warning = empty event)."""
        ts0, cached = self._mkt_cache
        if cached and (time.time() - ts0) < 300.0:
            return cached
        try:
            resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": "spot"}, timeout=self.timeout)
            resp.raise_for_status()
            lst = resp.json().get("result", {}).get("list", []) or []
            out: List[Dict[str, Any]] = []
            for it in lst:
                sym = str(it.get("symbol", ""))
                if not sym.endswith("USDT"):
                    continue
                st = str(it.get("status", "")).lower()
                if st and st != "trading":
                    continue
                out.append({"market": sym, "korean_name": sym, "english_name": sym, "market_event": {}})
            if out:
                self._mkt_cache = (time.time(), out)
            return out
        except Exception as e:
            logger.warning("[BybitSpot] get_all_markets failed: %s", e)
            return list(cached) if cached else []

    def get_market_warnings(self, *, ttl: float = 300.0) -> Dict[str, Dict[str, Any]]:
        """Bybit spot has no Korean-style investment warning/caution → empty dict (no blocking, fail-open)."""
        return {}

    # ── Ticker / price — mirrors Upbit shape ────────────────────────────
    def get_tickers(self, markets: List[str]) -> List[Dict[str, Any]]:
        """Current price + 24h turnover. Mirrors Upbit shape({market, trade_price, acc_trade_price_24h}).
        Empty markets = all (for selector turnover ranking)."""
        out: List[Dict[str, Any]] = []
        try:
            resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": "spot"}, timeout=self.timeout)
            resp.raise_for_status()
            lst = resp.json().get("result", {}).get("list", []) or []
            want = {self._normalize_symbol(m) for m in markets} if markets else None
            for t in lst:
                sym = str(t.get("symbol", ""))
                if not sym.endswith("USDT"):
                    continue
                if want is not None and sym not in want:
                    continue
                out.append({
                    "market": sym,
                    "trade_price": float(t.get("lastPrice", 0) or 0),
                    "acc_trade_price_24h": float(t.get("turnover24h", 0) or 0),
                    "acc_trade_volume_24h": float(t.get("volume24h", 0) or 0),
                    "signed_change_rate": float(t.get("price24hPcnt", 0) or 0),
                })
        except Exception as e:
            logger.warning("[BybitSpot] get_tickers failed: %s", e)
        return out

    def get_price(self, market: str) -> float:
        try:
            t = self.get_tickers([market])
            return float(t[0].get("trade_price", 0) or 0) if t else 0.0
        except Exception as e:
            logger.warning("[BybitSpot] get_price %s failed: %s", market, e)
            return 0.0

    # ── Orderbook — mirrors Upbit shape ─────────────────────────────────
    def get_orderbook(self, market: str, *, depth: int = 15) -> Dict[str, Any]:
        """Mirrors Upbit shape({market, bids:[{price,size}], asks:[{price,size}], ts}).
        Bybit v5 orderbook: b=[[price,size],...] (bids descending), a=[...] (asks ascending)."""
        sym = self._normalize_symbol(market)
        bids: List[Dict[str, float]] = []
        asks: List[Dict[str, float]] = []
        try:
            resp = bybit_get(
                BYBIT_MARKET_ORDERBOOK,
                params={"category": "spot", "symbol": sym, "limit": min(max(int(depth), 1), 50)},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            r = resp.json().get("result", {}) or {}
            for row in (r.get("b") or [])[: max(int(depth), 1)]:
                bids.append({"price": float(row[0]), "size": float(row[1])})
            for row in (r.get("a") or [])[: max(int(depth), 1)]:
                asks.append({"price": float(row[0]), "size": float(row[1])})
        except Exception as e:
            logger.warning("[BybitSpot] get_orderbook %s failed: %s", market, e)
        return {"market": sym, "bids": bids, "asks": asks, "ts": 0}

    # ── Open orders (Upbit open_orders interface) ───────────────────────
    def open_orders(self, market: str, *, side: Optional[str] = None) -> List[Dict[str, Any]]:
        orders = self.list_wait_orders(market=market)
        if side:
            s = "bid" if str(side).lower() in ("bid", "buy", "long") else "ask"
            orders = [o for o in orders if str(o.get("side", "")).lower() == s]
        return orders
