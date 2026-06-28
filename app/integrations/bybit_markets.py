# -*- coding: utf-8 -*-
"""Bybit Market Fetcher (REST)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.constants import (
    BYBIT_MARKET_INSTRUMENTS,
    BYBIT_MARKET_TICKERS,
    DEFAULT_REQUEST_TIMEOUT_SEC,
    bybit_v5_rest_category,
    parse_bybit_list,
    normalize_bybit_ticker,
)
from app.core.currency import Q
from app.core.rate_limiter import bybit_rate_limit

logger = logging.getLogger(__name__)

_bybit_session: Optional[requests.Session] = None

def _get_bybit_session() -> requests.Session:
    """Session reusing TCP/SSL connections with automatic ConnectionError retries."""
    global _bybit_session
    if _bybit_session is None:
        _bybit_session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=5)
        _bybit_session.mount("https://", adapter)
        _bybit_session.mount("http://", adapter)
    return _bybit_session

@bybit_rate_limit
def fetch_bybit_markets(is_details=False, timeout=10.0):
    params = {"category": bybit_v5_rest_category()}
    try:
        resp = _get_bybit_session().get(BYBIT_MARKET_INSTRUMENTS, params=params, timeout=float(timeout))
        resp.raise_for_status()
        instruments = parse_bybit_list(resp.json())
    except requests.RequestException as e:
        logger.error("[fetch_bybit_markets] API error: %s", e)
        raise
    result = []
    for item in instruments:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol", "")
        base_coin = item.get("baseCoin", "")
        quote_coin = item.get("quoteCoin", "")
        entry = {"market": symbol, "base": base_coin, "quote": quote_coin}
        if is_details:
            entry["status"] = item.get("status", "")
            entry["lot_size_filter"] = item.get("lotSizeFilter", {})
            entry["price_filter"] = item.get("priceFilter", {})
        result.append(entry)
    return result


def filter_usdt_markets(markets):
    return filter_quote_markets(markets, quote="USDT")

def filter_quote_markets(markets, quote="USDT"):
    quote = str(quote).upper()
    return [m.get("market", "") for m in markets if m.get("quote") == quote and m.get("market")]

def _default_cache_path():
    return Path(__file__).resolve().parents[1] / "data" / "bybit_markets.json"

def _atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def ensure_bybit_markets_cache(markets=None, *, path=None, min_interval_sec=3600.0, quote="USDT", is_details=True, timeout=5.0):
    p = Path(path) if path else _default_cache_path()
    now = time.time()
    if p.exists():
        age = max(0.0, now - p.stat().st_mtime)
        if age < float(min_interval_sec):
            try:
                cached = json.loads(p.read_text(encoding="utf-8"))
                if cached.get("items") and len(cached["items"]) >= 10:
                    return {"ok": True, "updated": False, "path": str(p), "age_sec": age}
            except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[bybit_markets] %s: %s", 'bybit_markets.ensure_bybit_markets_cache fallback', exc, exc_info=True)
    if markets is None:
        markets = fetch_bybit_markets(is_details=is_details, timeout=timeout)
    items = [m for m in markets if m.get("quote") == quote]
    _atomic_write_json(p, {"ts": int(now), "items": items})
    return {"ok": True, "updated": True, "path": str(p), "count": len(items), "ts": now}

def load_bybit_markets_cache(path=None):
    p = Path(path) if path else _default_cache_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "items" in data:
            return data["items"]
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[bybit_markets] %s: %s", 'bybit_markets.load_bybit_markets_cache fallback', exc, exc_info=True)
    return []

@bybit_rate_limit
def fetch_bybit_kline(market: str, interval: str = "D", limit: int = 7, timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Fetch kline (candle) data from Bybit V5 API.

    Args:
        market: Symbol e.g. "BTCUSDT"
        interval: Bybit interval string — "1","3","5","15","30","60","120","240","360","720","D","W","M"
        limit: Number of candles (max 200)
        timeout: Request timeout

    Returns:
        List of candle dicts (oldest first), each with keys:
        opening_price, high_price, low_price, trade_price,
        candle_acc_trade_volume, timestamp
    """
    from app.core.constants import BYBIT_MARKET_KLINE
    symbol = Q.normalize(market)
    resp = _get_bybit_session().get(
        BYBIT_MARKET_KLINE,
        params={
            "category": bybit_v5_rest_category(),
            "symbol": symbol,
            "interval": str(interval),
            "limit": str(min(limit, 200)),
        },
        timeout=float(timeout),
    )
    resp.raise_for_status()
    raw = parse_bybit_list(resp.json())
    result = []
    for k in reversed(raw):
        if isinstance(k, (list, tuple)) and len(k) >= 6:
            result.append({
                "opening_price": float(k[1]),
                "high_price": float(k[2]),
                "low_price": float(k[3]),
                "trade_price": float(k[4]),
                "candle_acc_trade_volume": float(k[5]),
                "timestamp": int(k[0]),
            })
    return result


@bybit_rate_limit
def fetch_bybit_tickers(markets, timeout=10.0, retry_count=0, max_retries=3):
    if not markets:
        return []
    try:
        resp = requests.get(
            BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=float(timeout)
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        logger.warning("[fetch_bybit_tickers] HTTPError (retry %d/%d): %s", retry_count, max_retries, e, exc_info=True)
        if e.response and e.response.status_code == 429 and retry_count < max_retries:
            time.sleep(2 ** retry_count)
            return fetch_bybit_tickers(markets, timeout, retry_count + 1, max_retries)
        raise
    except requests.RequestException as e:
        logger.error("[fetch_bybit_tickers] API error: %s", e)
        raise
    all_tickers = parse_bybit_list(resp.json())
    market_set = set(m.upper() for m in markets)
    return [normalize_bybit_ticker(t) for t in all_tickers if isinstance(t, dict) and t.get("symbol", "") in market_set]

def get_market_info(symbol, markets=None):
    if markets is None:
        markets = load_bybit_markets_cache()
    normalized = Q.normalize(symbol)
    base = Q.extract_base(symbol)
    for m in markets:
        if m.get("market") == normalized or m.get("base") == base:
            return m
    return None

def fetch_delisting_markets(timeout=5.0):
    return []
