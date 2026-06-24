# ============================================================
# File: app/manager/reserved_selector_utils.py
# Autocoin OS — Utility functions, constants, and data classes
# extracted from reserved_selector.py
# ============================================================

from __future__ import annotations

import math
import os
import time
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from app.core.constants import (
    BYBIT_API_BASE,
    DEFAULT_REQUEST_TIMEOUT_SEC,
)
from app.core.currency import Q

_logger = logging.getLogger(__name__)

# Coin-selection exclusion marker (sorts to the bottom -> not selected)
_SCORE_EXCLUDED: float = -9999.0

BYBIT_BASE = BYBIT_API_BASE
DEFAULT_TIMEOUT = DEFAULT_REQUEST_TIMEOUT_SEC


# ============================================================
# [2026-03-03] Execution-quality penalty for low-price / low-volume coins
# ============================================================
def _execution_quality_penalty(price: float, vol24_usdt: float, spread_bps: float = 0.0) -> float:
    """Tick-size slippage + insufficient-volume penalty (return value <= 0).

    Low-price coins have a single tick that spans a large percentage,
    so on entry you already lose 20~40% of the TP — a structural problem.
    This function applies a common penalty across all scoring functions.

    Returns:
        float: penalty value between 0 (normal) and -15 (extreme low-price + extreme low-volume)
    """
    penalty = 0.0

    # ── 1. Tick-size slippage penalty ──
    # Slippage penalty based on Bybit tick size
    if price <= 0:
        return -15.0
    from app.integrations.bybit_trade import get_tick_size
    tick = get_tick_size(price)
    tick_pct = (tick / price) * 100.0 if price > 0 else 99.0

    if tick_pct >= 1.0:         # 1 tick = 1% or more (extreme low-price)
        penalty -= 10.0
    elif tick_pct >= 0.5:       # 1 tick = 0.5% or more
        penalty -= 5.0
    elif tick_pct >= 0.2:       # 1 tick = 0.2% or more
        penalty -= 2.0
    elif tick_pct >= 0.1:       # 1 tick = 0.1%
        penalty -= 0.5

    # ── 2. Insufficient-volume penalty ──
    # Based on 24h turnover (USDT)
    if vol24_usdt < 500_000:           # under 500K USDT
        penalty -= 8.0
    elif vol24_usdt < 1_000_000:       # under 1M USDT
        penalty -= 4.0
    elif vol24_usdt < 3_000_000:       # under 3M USDT
        penalty -= 1.5

    # ── 3. Extra penalty for excessive spread ──
    if spread_bps > 50:
        penalty -= min(5.0, (spread_bps - 50) * 0.1)

    return penalty


# ============================================================
# Bybit Market Conversion Utilities
# ============================================================

def _normalize_market(market: str) -> str:
    """Normalize market to Bybit format (e.g., 'BTC' or 'BTCUSDT' -> 'BTCUSDT')."""
    m = str(market or "").strip().upper()
    if not m:
        return ""
    normalized = Q.normalize(m)
    # [2026-02-05] Filter out malformed market formats
    # Guard against the bug where Q.normalize('') returns '''
    if normalized in ("BTC-", "USDT-") or len(normalized) < 5:
        return ""
    return normalized


def _chunks(seq: Sequence[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        if not math.isfinite(x):
            return default
        return x
    except (TypeError, ValueError):
        _logger.warning("_sf float conversion failed for value %r", v, exc_info=True)
        return default


def _si(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        _logger.warning("_si int conversion failed for value %r", v, exc_info=True)
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        return float(min(max(float(x), float(lo)), float(hi)))
    except (TypeError, ValueError):
        _logger.warning("_clamp conversion failed for x=%r lo=%r hi=%r", x, lo, hi, exc_info=True)
        return float(lo)


def _finalize_usdt_notional(amount: float, min_order_usdt: float) -> Optional[float]:
    """USDT notional amount: normalized to 2 decimal places. Returns None if below `min_order_usdt` (candidate/budget not viable)."""
    try:
        mo = float(min_order_usdt)
    except (TypeError, ValueError):
        return None
    if mo <= 0.0:
        return None
    try:
        a = round(float(amount), 2)
    except (TypeError, ValueError):
        return None
    if a < mo:
        return None
    return float(a)


def _norm(market: str) -> str:
    return str(market or "").strip().upper()


def _currency(market: str) -> str:
    """Extract base currency from market (e.g., 'BTCUSDT' -> 'BTC')."""
    m = _norm(market)
    if "-" in m:
        return m.split("-", 1)[1]
    return m.replace("USDT", "")


_DEFAULT_GLOBAL_EXCLUDE_BASES: Tuple[str, ...] = (
    "USDT",
    "USDC",
    "DAI",
    "TUSD",
    "FDUSD",
    "USDP",
    "PYUSD",
    "USDE",
    "GUSD",
    "FRAX",
)


def _csv_upper(raw: str) -> List[str]:
    out: List[str] = []
    for part in str(raw or "").split(","):
        token = str(part or "").strip().upper()
        if token:
            out.append(token)
    return out


def _global_exclude_bases() -> List[str]:
    raw = os.getenv("OMA_SELECTOR_GLOBAL_EXCLUDE_BASES", ",".join(_DEFAULT_GLOBAL_EXCLUDE_BASES))
    vals = _csv_upper(raw)
    return vals or list(_DEFAULT_GLOBAL_EXCLUDE_BASES)


def _global_exclude_markets() -> set[str]:
    raw = os.getenv("OMA_SELECTOR_GLOBAL_EXCLUDE_MARKETS", "")
    vals = _csv_upper(raw)
    return {_norm(v) for v in vals if _norm(v)}


def _calc_spread_bps(best_bid: float, best_ask: float) -> float:
    if best_bid <= 0 or best_ask <= 0:
        return 999999.0
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return 999999.0
    return ((best_ask - best_bid) / mid) * 10000.0


def _calc_depth_notional(
    units: Sequence[Dict[str, Any]],
    *,
    best_bid: float,
    best_ask: float,
    depth_bps: float,
) -> Tuple[float, float]:
    """Notional depth within +/- depth_bps around the top of book."""
    if best_bid <= 0 or best_ask <= 0 or depth_bps <= 0:
        return 0.0, 0.0

    ask_lim = best_ask * (1.0 + float(depth_bps) / 10000.0)
    bid_lim = best_bid * (1.0 - float(depth_bps) / 10000.0)

    ask_notional = 0.0
    bid_notional = 0.0
    for u in units:
        ap = _sf(u.get("ask_price"), 0.0)
        asz = _sf(u.get("ask_size"), 0.0)
        bp = _sf(u.get("bid_price"), 0.0)
        bsz = _sf(u.get("bid_size"), 0.0)
        if ap > 0 and asz > 0 and ap <= ask_lim:
            ask_notional += ap * asz
        if bp > 0 and bsz > 0 and bp >= bid_lim:
            bid_notional += bp * bsz
    return float(ask_notional), float(bid_notional)


@dataclass
class MarketSnapshot:
    market: str
    price: float
    vol24_usdt: float
    range_ratio_24h: float
    best_bid: float
    best_ask: float
    spread_bps: float
    depth_ask_usdt: float
    depth_bid_usdt: float
    recent_trades: Optional[int] = None
    caution: bool = False
    delisting: bool = False  # trading support to be discontinued
    delisting_date: Optional[str] = None  # scheduled delisting date
    names: Optional[Dict[str, str]] = None
    # ICAG v3: ATR / Bollinger enrichment (optional)
    atr_pct: float = 0.0
    bb_width_pct: float = 0.0
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0


def _snapshot_from_ticker_and_ob(
    market: str,
    ticker: Optional[Dict[str, Any]],
    orderbook: Optional[Dict[str, Any]],
    *,
    depth_bps: float,
    caution: bool,
    delisting: bool = False,
    delisting_date: Optional[str] = None,
    names: Optional[Dict[str, str]],
) -> Optional[MarketSnapshot]:
    m = _norm(market)
    if not m or not ticker:
        return None

    price = _sf(ticker.get("trade_price"), 0.0)
    if price <= 0:
        return None

    vol24 = _sf(ticker.get("acc_trade_price_24h"), 0.0)
    hi = _sf(ticker.get("high_price"), price)
    lo = _sf(ticker.get("low_price"), price)
    rr = ((hi - lo) / price) if price > 0 else 0.0

    best_bid = 0.0
    best_ask = 0.0
    spread_bps = 999999.0
    depth_ask = 0.0
    depth_bid = 0.0

    if orderbook and isinstance(orderbook, dict):
        units = orderbook.get("orderbook_units") or []
        if isinstance(units, list) and units:
            try:
                best_ask = _sf(units[0].get("ask_price"), 0.0)
                best_bid = _sf(units[0].get("bid_price"), 0.0)
            except (KeyError, IndexError, AttributeError, TypeError) as e:
                _logger.warning("[snapshot_parse] orderbook_units parse failed %s: %s", market, e, exc_info=True)
                best_ask, best_bid = 0.0, 0.0

            spread_bps = _calc_spread_bps(best_bid, best_ask)
            if depth_bps > 0:
                depth_ask, depth_bid = _calc_depth_notional(units[:15], best_bid=best_bid, best_ask=best_ask, depth_bps=depth_bps)

    return MarketSnapshot(
        market=m,
        price=float(price),
        vol24_usdt=float(vol24),
        range_ratio_24h=float(rr),
        best_bid=float(best_bid),
        best_ask=float(best_ask),
        spread_bps=float(spread_bps),
        depth_ask_usdt=float(depth_ask),
        depth_bid_usdt=float(depth_bid),
        recent_trades=None,
        caution=bool(caution),
        delisting=bool(delisting),
        delisting_date=delisting_date,
        names=names,
    )


# ============================================================
# [2026-03-03] SharedMarketData — shared cache for round-robin scanning
# ============================================================
class SharedMarketData:
    """Cache that shares Ticker/Orderbook/Snapshot across strategies.

    In round-robin scanning, instead of re-fetching ticker/orderbook every round,
    the once-collected data is shared for a TTL (default 120 seconds).

    Usage:
        shared = SharedMarketData.get_or_refresh(system, ttl_sec=120)
        items, summary = build_reserved_candidates(system, ..., shared_data=shared)
    """
    _instance: Optional["SharedMarketData"] = None
    _instance_ts: float = 0.0

    def __init__(self) -> None:
        self.ts: float = time.time()
        self.tmap: Dict[str, Dict[str, Any]] = {}
        self.obmap: Dict[str, Dict[str, Any]] = {}
        self.smap: Dict[str, Any] = {}  # MarketSnapshot
        self.universe: List[str] = []
        self.ranked_by_vol: List[str] = []
        self.names_map: Dict[str, Dict[str, str]] = {}
        self.caution_map: Dict[str, bool] = {}
        self.delisting_map: Dict[str, bool] = {}
        self.delisting_date_map: Dict[str, Optional[str]] = {}
        self.filter_stats: Dict[str, int] = {}

    def is_valid(self, ttl_sec: float = 120.0) -> bool:
        return (time.time() - self.ts) < ttl_sec

    @classmethod
    def get_or_refresh(cls, system: Any, ttl_sec: float = 120.0) -> Optional["SharedMarketData"]:
        """Reuse the cache if still valid; return None if expired (caller decides whether to refresh)."""
        if cls._instance and cls._instance.is_valid(ttl_sec):
            return cls._instance
        return None

    @classmethod
    def store(cls, data: "SharedMarketData") -> None:
        cls._instance = data
        cls._instance_ts = time.time()

    @classmethod
    def invalidate(cls) -> None:
        cls._instance = None
        cls._instance_ts = 0.0
