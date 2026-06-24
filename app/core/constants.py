# ============================================================
# File: app/core/constants.py
# Autocoin OS v3-H — common constants and utility definitions (Bybit only)
# ============================================================

import logging
import os
from functools import lru_cache
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ============================================================
# Environment Variable Utilities (with caching)
# ============================================================

@lru_cache(maxsize=128)
def env_bool(key: str, default: bool = False) -> bool:
    """Parse an environment variable as bool (cached)."""
    v = str(os.getenv(key, "1" if default else "0")).strip().lower()
    return v in ("1", "true", "yes", "y", "on")

@lru_cache(maxsize=128)
def env_float(key: str, default: float) -> float:
    """Parse an environment variable as float (cached)."""
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        logger.warning("[constants] env_float(%s) parse failed, using default %s", key, default)
        return float(default)

@lru_cache(maxsize=128)
def env_int(key: str, default: int) -> int:
    """Parse an environment variable as int (cached)."""
    try:
        return int(float(os.getenv(key, str(default))))
    except (TypeError, ValueError):
        logger.warning("[constants] env_int(%s) parse failed, using default %s", key, default)
        return int(default)

@lru_cache(maxsize=64)
def env_json_dict(key: str) -> Dict[str, float]:
    """Parse an environment variable as a JSON dict[str, float] (cached)."""
    import json
    raw = os.getenv(key, "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return {str(k): float(v) for k, v in obj.items()}
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[constants] env_json_dict(%s) parse failed", key, exc_info=True)
        logger.warning("[constants] %s: %s", 'constants.env_json_dict fallback', exc, exc_info=True)
    return {}

def clear_env_cache() -> None:
    """Clear the environment-variable cache (use on test/reload)."""
    env_bool.cache_clear()
    env_float.cache_clear()
    env_int.cache_clear()
    env_json_dict.cache_clear()

# ============================================================
# Bybit V5 API Endpoints
# ============================================================
BYBIT_API_BASE = "https://api.bybit.com"
# Default spot URL; live feed should use ``get_bybit_public_ws_url()`` from ``app.core.bybit_trading``.
BYBIT_WS_PUBLIC = "wss://stream.bybit.com/v5/public/spot"
BYBIT_WS_PRIVATE = "wss://stream.bybit.com/v5/private"

# Public REST API (V5)
BYBIT_MARKET_INSTRUMENTS = f"{BYBIT_API_BASE}/v5/market/instruments-info"
BYBIT_MARKET_TICKERS = f"{BYBIT_API_BASE}/v5/market/tickers"
BYBIT_MARKET_ORDERBOOK = f"{BYBIT_API_BASE}/v5/market/orderbook"
BYBIT_MARKET_RECENT_TRADE = f"{BYBIT_API_BASE}/v5/market/recent-trade"
BYBIT_MARKET_KLINE = f"{BYBIT_API_BASE}/v5/market/kline"

# Private REST API (V5)
BYBIT_ORDER_CREATE = f"{BYBIT_API_BASE}/v5/order/create"
BYBIT_ORDER_CANCEL = f"{BYBIT_API_BASE}/v5/order/cancel"
BYBIT_ORDER_REALTIME = f"{BYBIT_API_BASE}/v5/order/realtime"
BYBIT_ACCOUNT_WALLET = f"{BYBIT_API_BASE}/v5/account/wallet-balance"

# Futures / Position REST API (V5)
BYBIT_POSITION_LIST = f"{BYBIT_API_BASE}/v5/position/list"
BYBIT_POSITION_SET_LEVERAGE = f"{BYBIT_API_BASE}/v5/position/set-leverage"
BYBIT_POSITION_SWITCH_MODE = f"{BYBIT_API_BASE}/v5/position/switch-mode"
BYBIT_POSITION_TRADING_STOP = f"{BYBIT_API_BASE}/v5/position/trading-stop"
BYBIT_MARKET_FUNDING_HISTORY = f"{BYBIT_API_BASE}/v5/market/funding/history"

def bybit_v5_rest_category() -> str:
    """REST/WS ticker & kline `category` aligned with ``BYBIT_V5_CATEGORY`` (spot | linear)."""
    from app.core.bybit_trading import get_v5_order_category

    return get_v5_order_category()

# ============================================================
# Bybit V5 Response Helpers
# ============================================================

def parse_bybit_list(data: Any) -> list:
    """Extract list from Bybit V5 nested response or pass through plain list.

    Bybit V5 returns ``{"retCode": 0, "result": {"list": [...]}}``.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            lst = result.get("list")
            if isinstance(lst, list):
                return lst
        # Some Bybit endpoints nest differently
        lst = data.get("list")
        if isinstance(lst, list):
            return lst
    return []

def normalize_bybit_ticker(t: dict) -> dict:
    """Convert a Bybit V5 spot ticker dict to normalized fields.

    Bybit ticker fields (``/v5/market/tickers?category=spot``):
      symbol, lastPrice, highPrice24h, lowPrice24h, prevPrice24h,
      volume24h, turnover24h, bid1Price, ask1Price, ...

    Returns a dict with standardized keys for downstream code.
    """
    if not isinstance(t, dict):
        return t
    # Already converted
    if "market" in t and "trade_price" in t:
        return t

    sym = str(t.get("symbol") or "").upper()
    from app.core.currency import Q
    market = Q.normalize(sym)

    last = _to_float(t.get("lastPrice"), 0.0)
    prev = _to_float(t.get("prevPrice24h"), 0.0)
    high24 = _to_float(t.get("highPrice24h"), 0.0)
    low24 = _to_float(t.get("lowPrice24h"), 0.0)
    vol24 = _to_float(t.get("volume24h"), 0.0)
    turnover24 = _to_float(t.get("turnover24h"), 0.0)

    change = last - prev if prev > 0 else 0.0
    change_rate = change / prev if prev > 0 else 0.0

    return {
        "market": market,
        "trade_price": last,
        "opening_price": prev,
        "high_price": high24,
        "low_price": low24,
        "prev_closing_price": prev,
        "signed_change_price": change,
        "signed_change_rate": change_rate,
        "acc_trade_price_24h": turnover24,
        "acc_trade_volume_24h": vol24,
        "highest_52_week_price": high24,
        "lowest_52_week_price": low24,
        "timestamp": 0,
        # preserve original
        "_bybit_raw": t,
    }

def _to_float(v: Any, default: float = 0.0) -> float:
    """Safe float conversion for Bybit string-number fields."""
    if v is None:
        return default
    try:
        r = float(v)
        import math
        return r if math.isfinite(r) else default
    except (TypeError, ValueError):
        logger.warning("safe_float conversion failed for value %r, using default %s", v, default, exc_info=True)
        return default

# ============================================================
# Telegram API
# ============================================================
TELEGRAM_API_BASE = "https://api.telegram.org"

# ============================================================
# Trading Constants (Quote Currency Abstraction)
# ============================================================
# Use the quote-currency abstraction layer
from app.core.currency import Q as QUOTE_CURRENCY

# Quote currency (USDT)
BASE_QUOTE_CURRENCY = QUOTE_CURRENCY.symbol
MIN_ORDER_AMOUNT = QUOTE_CURRENCY.min_order

# ============================================================
# Timeouts & Intervals
# ============================================================
DEFAULT_REQUEST_TIMEOUT_SEC = 10.0
DEFAULT_TICK_INTERVAL_SEC = 1.0

# ============================================================
# Exchange-agnostic configuration keys
# ============================================================
# Order management
OMA_ORDER_TIMEOUT_SEC = env_float("OMA_ORDER_TIMEOUT_SEC", 30.0)
OMA_ORDER_POLL_SEC = env_float("OMA_ORDER_POLL_SEC", 1.0)
OMA_COOLDOWN_SEC = env_float("OMA_COOLDOWN_SEC", 5.0)

