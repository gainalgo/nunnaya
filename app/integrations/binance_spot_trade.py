# -*- coding: utf-8 -*-
"""Binance SPOT trade client — adapts the SpotGazuaManager client interface to Binance spot.

Mirrors BybitSpotTradeClient. Buy/sell/balance/orders/candles are provided by
BinanceTradeClient(category="spot") → inherited. Only the missing public market
queries (market list / ticker / current price / orderbook) and helpers are filled in.

quote = USDT, symbol = "BTCUSDT". No Korean-style investment warning (market_warning) → fail-open.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

from app.integrations.binance_trade import BinanceTradeClient

logger = logging.getLogger(__name__)

MIN_ORDER_USDT = 5.0


def _spot_base() -> str:
    testnet = str(os.getenv("BINANCE_TESTNET", "0")).strip().lower() in ("1", "true", "yes")
    return "https://testnet.binance.vision" if testnet else "https://api.binance.com"


def binance_base_currency(symbol: str) -> str:
    s = str(symbol).upper().replace("-", "").replace("/", "")
    for q in ("USDT", "USDC", "FDUSD", "BUSD", "USD", "BTC", "ETH"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


class BinanceSpotTradeClient(BinanceTradeClient):
    """Binance spot. SpotGazuaManager compatible — inherits BinanceTradeClient(category="spot")."""

    MIN_ORDER_KRW = MIN_ORDER_USDT  # manager reads it by this name (unit = USDT)

    def __init__(self, api_key: str = "", api_secret: str = "", *, timeout: float = 10.0):
        super().__init__(api_key or None, api_secret or None, timeout=timeout, category="spot")
        self._mkt_cache: tuple = (0.0, [])

    @staticmethod
    def base_currency(symbol: str) -> str:
        return binance_base_currency(symbol)

    def _pub_get(self, path: str, params: dict) -> Any:
        resp = self._session.get(f"{_spot_base()}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ── Market list (all USDT spot) — mirrors Upbit shape ────────────────
    def get_all_markets(self) -> List[Dict[str, Any]]:
        ts0, cached = self._mkt_cache
        if cached and (time.time() - ts0) < 300.0:
            return cached
        try:
            data = self._pub_get("/api/v3/exchangeInfo", {})
            out: List[Dict[str, Any]] = []
            for it in data.get("symbols", []):
                sym = str(it.get("symbol", ""))
                if it.get("quoteAsset") != "USDT":
                    continue
                if str(it.get("status", "")).upper() != "TRADING":
                    continue
                out.append({"market": sym, "korean_name": sym, "english_name": sym, "market_event": {}})
            if out:
                self._mkt_cache = (time.time(), out)
            return out
        except Exception as e:
            logger.warning("[BinanceSpot] get_all_markets failed: %s", e)
            return list(cached) if cached else []

    def get_market_warnings(self, *, ttl: float = 300.0) -> Dict[str, Dict[str, Any]]:
        return {}  # fail-open

    # ── Ticker / current price — mirrors Upbit shape ────────────────────
    def get_tickers(self, markets: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            data = self._pub_get("/api/v3/ticker/24hr", {})
            want = {self._normalize_symbol(m) for m in markets} if markets else None
            for t in data:
                sym = str(t.get("symbol", ""))
                if not sym.endswith("USDT"):
                    continue
                if want is not None and sym not in want:
                    continue
                out.append({
                    "market": sym,
                    "trade_price": float(t.get("lastPrice", 0) or 0),
                    "acc_trade_price_24h": float(t.get("quoteVolume", 0) or 0),
                    "acc_trade_volume_24h": float(t.get("volume", 0) or 0),
                    "signed_change_rate": float(t.get("priceChangePercent", 0) or 0) / 100.0,
                })
        except Exception as e:
            logger.warning("[BinanceSpot] get_tickers failed: %s", e)
        return out

    def get_price(self, market: str) -> float:
        try:
            data = self._pub_get("/api/v3/ticker/price", {"symbol": self._normalize_symbol(market)})
            return float(data.get("price", 0) or 0)
        except Exception as e:
            logger.warning("[BinanceSpot] get_price %s failed: %s", market, e)
            return 0.0

    # ── Orderbook — mirrors Upbit shape ─────────────────────────────────
    def get_orderbook(self, market: str, *, depth: int = 15) -> Dict[str, Any]:
        sym = self._normalize_symbol(market)
        bids: List[Dict[str, float]] = []
        asks: List[Dict[str, float]] = []
        try:
            lim = min(max(int(depth), 1), 100)
            r = self._pub_get("/api/v3/depth", {"symbol": sym, "limit": lim})
            for row in (r.get("bids") or [])[: max(int(depth), 1)]:
                bids.append({"price": float(row[0]), "size": float(row[1])})
            for row in (r.get("asks") or [])[: max(int(depth), 1)]:
                asks.append({"price": float(row[0]), "size": float(row[1])})
        except Exception as e:
            logger.warning("[BinanceSpot] get_orderbook %s failed: %s", market, e)
        return {"market": sym, "bids": bids, "asks": asks, "ts": 0}

    # ── Open orders (Upbit open_orders interface) ───────────────────────
    def open_orders(self, market: str, *, side: Optional[str] = None) -> List[Dict[str, Any]]:
        orders = self.list_wait_orders(market=market)
        if side:
            s = "bid" if str(side).lower() in ("bid", "buy", "long") else "ask"
            orders = [o for o in orders if str(o.get("side", "")).lower() == s]
        return orders
