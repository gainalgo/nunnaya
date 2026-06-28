"""
Top-N market selector for Bybit USDT markets.

Two methods are provided:

1) public-candles:
   - Fetch historical candles (e.g., 1m candles) for all USDT markets
   - Compute simple features
   - Rank by strategy profile weights

2) live-buffer:
   - Fetch ticker prices repeatedly for all USDT markets for a fixed duration
   - Build an in-memory price buffer
   - Compute features and rank similarly

This module is intentionally standalone (public API only; no Bybit keys required).
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import statistics
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

import requests

from app.core.rate_limiter import bybit_get
from app.core.constants import (
    BYBIT_API_BASE,
    BYBIT_MARKET_INSTRUMENTS,
    BYBIT_MARKET_TICKERS,
    DEFAULT_REQUEST_TIMEOUT_SEC,
    BYBIT_MARKET_KLINE,
    bybit_v5_rest_category,
    parse_bybit_list,
    normalize_bybit_ticker,
)
from app.core.currency import Q

BYBIT_BASE = BYBIT_API_BASE
DEFAULT_TIMEOUT = int(DEFAULT_REQUEST_TIMEOUT_SEC)

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

# -------------------------
# Utilities
# -------------------------

def _chunks(seq: Sequence[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])

def _csv_upper(raw: str) -> List[str]:
    out: List[str] = []
    for part in str(raw or "").split(","):
        token = str(part or "").strip().upper()
        if token:
            out.append(token)
    return out

def _global_exclude_bases() -> set[str]:
    raw = os.getenv("OMA_SELECTOR_GLOBAL_EXCLUDE_BASES", ",".join(_DEFAULT_GLOBAL_EXCLUDE_BASES))
    vals = _csv_upper(raw)
    if not vals:
        vals = list(_DEFAULT_GLOBAL_EXCLUDE_BASES)
    return set(vals)

def _global_exclude_markets() -> set[str]:
    raw = os.getenv("OMA_SELECTOR_GLOBAL_EXCLUDE_MARKETS", "")
    vals = _csv_upper(raw)
    return {Q.normalize(v) for v in vals if v}

def _base_currency(market: str) -> str:
    m = str(market or "").strip().upper()
    if "-" in m:
        return m.split("-", 1)[1]
    return m

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        if not math.isfinite(x):
            return default
        return x
    except (TypeError, ValueError):
        logger.warning("[TopNSelector] _safe_float: conversion failed for %r", v, exc_info=True)
        return default

def _pct_returns(prices: Sequence[float]) -> List[float]:
    rets: List[float] = []
    for i in range(1, len(prices)):
        p0 = prices[i - 1]
        p1 = prices[i]
        if p0 <= 0:
            continue
        rets.append((p1 / p0) - 1.0)
    return rets

def _linear_slope(y: Sequence[float]) -> float:
    """Simple OLS slope on index vs y (no numpy)."""
    n = len(y)
    if n < 3:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(y) / n
    num = 0.0
    den = 0.0
    for i, yi in enumerate(y):
        dx = i - x_mean
        num += dx * (yi - y_mean)
        den += dx * dx
    if den == 0:
        return 0.0
    return num / den

def _zscore(values: Sequence[float]) -> List[float]:
    if not values:
        return []
    m = statistics.mean(values)
    sd = statistics.pstdev(values)
    if sd <= 0:
        return [0.0 for _ in values]
    return [(v - m) / sd for v in values]

# -------------------------
# Feature model
# -------------------------

@dataclass
class MarketFeatures:
    market: str
    samples: int
    last_price: float
    volatility: float
    momentum: float
    trend_slope: float
    range_ratio: float
    liquidity: float  # proxy: avg trade value per candle / per tick

    @property
    def trend_abs(self) -> float:
        return abs(self.momentum)

    @property
    def choppiness(self) -> float:
        # High when volatile but not trending.
        return self.volatility / (self.trend_abs + 1e-9)

# -------------------------
# Strategy profiles
# -------------------------

PROFILE_WEIGHTS: Dict[str, Dict[str, float]] = {
    # Range/mean-reversion scalping (grid/pingpong-like)
    "pingpong": {
        "volatility": 0.45,
        "liquidity": 0.35,
        "trend_abs": -0.30,
        "range_ratio": 0.10,
        "choppiness": 0.25,
    },
    # Trend-follow scaling in/out
    "ladder": {
        # DCA/ladder: avoid late momentum chasing, prefer liquid volatile pullbacks.
        "momentum": -0.20,
        "liquidity": 0.35,
        "volatility": 0.35,
        "trend_slope": -0.15,
        "trend_abs": -0.10,
        "range_ratio": 0.10,
        "choppiness": 0.20,
    },
    # Breakout / fast momentum
    "lightning": {
        "momentum": 0.35,
        "volatility": 0.35,
        "liquidity": 0.30,
        "trend_slope": 0.15,
    },
    # Trailing-ish / adaptive: prefer liquid + moving markets, avoid dead-flat
    "autorope": {
        "liquidity": 0.40,
        "volatility": 0.25,
        "momentum": 0.15,
        "range_ratio": 0.10,
        "choppiness": 0.10,
    },
    # Risk-on: strong up move + activity
    "gazua": {
        "momentum": 0.55,
        "liquidity": 0.30,
        "volatility": 0.20,
        "trend_slope": 0.20,
        "trend_abs": -0.05,
    },
}

# -------------------------
# Bybit public API
# -------------------------

def fetch_quote_markets(session: Optional[requests.Session] = None) -> List[str]:
    """Fetch all spot markets from Bybit V5 instruments-info API."""
    r = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    data = parse_bybit_list(r.json())
    blocked_bases = _global_exclude_bases()
    blocked_markets = _global_exclude_markets()
    out = []
    for m in data:
        if not isinstance(m, dict):
            continue
        sym = str(m.get("symbol") or "").upper()
        if not sym:
            continue
        market = Q.normalize(sym)
        if Q.config.market_prefix and not market.startswith(Q.config.market_prefix):
            continue
        if blocked_markets and market in blocked_markets:
            continue
        if _base_currency(market) in blocked_bases:
            continue
        out.append(market)
    return sorted(out)

def fetch_candles_minutes(
    market: str,
    unit: int = 1,
    count: int = 200,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    r = bybit_get(
        BYBIT_MARKET_KLINE,
        params={"category": bybit_v5_rest_category(), "symbol": market, "interval": str(unit), "limit": count},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    raw = parse_bybit_list(r.json())
    candles = []
    for k in raw:
        if isinstance(k, (list, tuple)) and len(k) >= 6:
            candles.append({
                "opening_price": float(k[1]),
                "high_price": float(k[2]),
                "low_price": float(k[3]),
                "trade_price": float(k[4]),
                "candle_acc_trade_volume": float(k[5]),
                "candle_acc_trade_price": float(k[4]) * float(k[5]) if float(k[5]) > 0 else 0.0,
                "timestamp": int(k[0]),
            })
    # Bybit returns newest-first; reverse to chronological.
    return list(reversed(candles))

def fetch_tickers(
    markets: Sequence[str],
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    """Fetch tickers from Bybit V5 (converted to Bybit-compatible format)."""
    if not markets:
        return []
    market_set = set(m.upper() for m in markets)

    r = bybit_get(
        BYBIT_MARKET_TICKERS,
        params={"category": bybit_v5_rest_category()},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    data = parse_bybit_list(r.json())

    out: List[Dict[str, Any]] = []
    for t in data:
        if not isinstance(t, dict):
            continue
        t = normalize_bybit_ticker(t)
        if t.get("market", "").upper() in market_set:
            out.append(t)
    return out

# -------------------------
# Feature extraction
# -------------------------

def features_from_candles(market: str, candles: Sequence[Dict[str, Any]]) -> Optional[MarketFeatures]:
    closes: List[float] = []
    ranges: List[float] = []
    trade_values: List[float] = []

    for c in candles:
        close = _safe_float(c.get("trade_price"), 0.0)
        high = _safe_float(c.get("high_price"), 0.0)
        low = _safe_float(c.get("low_price"), 0.0)
        tv = _safe_float(c.get("candle_acc_trade_price"), 0.0)
        if close <= 0:
            continue
        closes.append(close)
        if close > 0:
            ranges.append((high - low) / close)
        trade_values.append(tv)

    if len(closes) < 20:
        return None

    rets = _pct_returns(closes)
    vol = statistics.pstdev(rets) if len(rets) >= 2 else 0.0
    mom = (closes[-1] / closes[0] - 1.0) if closes[0] > 0 else 0.0
    # slope on log prices is often more stable
    logp = [math.log(p) for p in closes if p > 0]
    slope = _linear_slope(logp)
    rr = statistics.mean(ranges) if ranges else 0.0
    liq = statistics.mean(trade_values) if trade_values else 0.0

    return MarketFeatures(
        market=market,
        samples=len(closes),
        last_price=closes[-1],
        volatility=vol,
        momentum=mom,
        trend_slope=slope,
        range_ratio=rr,
        liquidity=liq,
    )

def features_from_price_buffer(market: str, prices: Sequence[float], liq_proxy: float = 0.0) -> Optional[MarketFeatures]:
    if len(prices) < 20:
        return None
    p = [float(x) for x in prices if isinstance(x, (int, float)) and x > 0 and math.isfinite(float(x))]
    if len(p) < 20:
        return None

    rets = _pct_returns(p)
    vol = statistics.pstdev(rets) if len(rets) >= 2 else 0.0
    mom = (p[-1] / p[0] - 1.0) if p[0] > 0 else 0.0
    logp = [math.log(x) for x in p if x > 0]
    slope = _linear_slope(logp)

    # Range proxy from buffer
    pmin = min(p)
    pmax = max(p)
    rr = (pmax - pmin) / p[-1] if p[-1] > 0 else 0.0

    return MarketFeatures(
        market=market,
        samples=len(p),
        last_price=p[-1],
        volatility=vol,
        momentum=mom,
        trend_slope=slope,
        range_ratio=rr,
        liquidity=float(liq_proxy),
    )

# -------------------------
# Scoring / ranking
# -------------------------

def rank_features(
    feats: Sequence[MarketFeatures],
    profile: str,
) -> List[Tuple[float, MarketFeatures]]:
    if profile not in PROFILE_WEIGHTS:
        raise ValueError(f"Unknown profile={profile}. Available: {sorted(PROFILE_WEIGHTS.keys())}")

    weights = PROFILE_WEIGHTS[profile]

    # Gather raw arrays
    def arr(name: str) -> List[float]:
        out: List[float] = []
        for f in feats:
            out.append(getattr(f, name))
        return out

    raw: Dict[str, List[float]] = {}
    for k in weights.keys():
        if hasattr(MarketFeatures, k) or hasattr(feats[0], k):
            # supports properties too by getattr
            raw[k] = [getattr(f, k) for f in feats]
        else:
            raw[k] = [0.0 for _ in feats]

    z: Dict[str, List[float]] = {k: _zscore(v) for k, v in raw.items()}

    ranked: List[Tuple[float, MarketFeatures]] = []
    for i, f in enumerate(feats):
        s = 0.0
        for k, w in weights.items():
            s += w * z.get(k, [0.0] * len(feats))[i]
        ranked.append((s, f))

    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked

# -------------------------
# Public ranking APIs
# -------------------------

def rank_topn_by_public_candles(
    n: int = 10,
    profile: str = "pingpong",
    candle_unit_minutes: int = 1,
    candle_count: int = 200,
    max_markets: Optional[int] = None,
    request_sleep: float = 0.12,
) -> List[Tuple[float, MarketFeatures]]:
    """Rank USDT markets via historical candles (public API)."""
    markets = fetch_quote_markets()
    if max_markets is not None:
        markets = markets[: int(max_markets)]

    feats: List[MarketFeatures] = []
    for i, m in enumerate(markets, 1):
        try:
            candles = fetch_candles_minutes(m, unit=candle_unit_minutes, count=candle_count)
            f = features_from_candles(m, candles)
            if f is not None:
                feats.append(f)
        except (AttributeError, TypeError) as exc:
            # Skip on any API/parse failure
            logger.warning("[topn_selector] %s: %s", 'topn_selector.rank_topn_by_public_candles fallback', exc, exc_info=True)

        # Basic rate-limit friendliness
        if request_sleep > 0:
            time.sleep(float(request_sleep))

    if not feats:
        return []

    ranked = rank_features(feats, profile=profile)
    return ranked[: int(n)]

def rank_topn_by_live_buffer(
    n: int = 10,
    profile: str = "pingpong",
    seconds: int = 180,
    interval_sec: float = 1.0,
    chunk_size: int = 100,
    max_markets: Optional[int] = None,
) -> List[Tuple[float, MarketFeatures]]:
    """Rank USDT markets by building a live price buffer from tickers (public API)."""
    markets = fetch_quote_markets()
    if max_markets is not None:
        markets = markets[: int(max_markets)]

    buf: Dict[str, List[float]] = {m: [] for m in markets}
    liq: Dict[str, float] = {m: 0.0 for m in markets}

    end = time.time() + float(seconds)
    while time.time() < end:
        for group in _chunks(markets, int(chunk_size)):
            try:
                ticks = fetch_tickers(group)
                for t in ticks:
                    m = t.get("market")
                    if not isinstance(m, str) or m not in buf:
                        continue
                    price = _safe_float(t.get("trade_price"), 0.0)
                    if price > 0:
                        buf[m].append(price)
                    # 24h trade value proxy helps filter dead markets
                    liq[m] = max(liq.get(m, 0.0), _safe_float(t.get("acc_trade_price_24h"), 0.0))
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[topn_selector] %s: %s", '24h trade value proxy helps filter dead markets', exc, exc_info=True)

        time.sleep(max(0.05, float(interval_sec)))

    feats: List[MarketFeatures] = []
    for m, prices in buf.items():
        f = features_from_price_buffer(m, prices, liq_proxy=liq.get(m, 0.0))
        if f is not None:
            feats.append(f)

    if not feats:
        return []

    ranked = rank_features(feats, profile=profile)
    return ranked[: int(n)]

# -------------------------
# CLI
# -------------------------

def _print_ranked(ranked: Sequence[Tuple[float, MarketFeatures]]) -> None:
    print("rank score market samples last_price volatility momentum trend_slope range_ratio liquidity")
    for i, (s, f) in enumerate(ranked, 1):
        print(
            f"{i:>4d} {s:>6.3f} {f.market:>10s} {f.samples:>7d} "
            f"{f.last_price:>10.4f} {f.volatility:>10.6f} {f.momentum:>9.4%} "
            f"{f.trend_slope:>10.6f} {f.range_ratio:>10.6f} {f.liquidity:>12.2f}"
        )

def main() -> int:
    ap = argparse.ArgumentParser(description="Bybit USDT Top-N selector (public API).")
    ap.add_argument("--method", choices=["candles", "buffer"], default="candles")
    ap.add_argument("--profile", choices=sorted(PROFILE_WEIGHTS.keys()), default="pingpong")
    ap.add_argument("--n", type=int, default=10)

    # candles
    ap.add_argument("--unit", type=int, default=1, help="candle unit minutes for --method candles")
    ap.add_argument("--count", type=int, default=200, help="candle count for --method candles")
    ap.add_argument("--sleep", type=float, default=0.12, help="sleep between candle requests")

    # buffer
    ap.add_argument("--seconds", type=int, default=180, help="buffer duration seconds for --method buffer")
    ap.add_argument("--interval", type=float, default=1.0, help="ticker polling interval seconds")
    ap.add_argument("--chunk", type=int, default=100, help="ticker markets chunk size per request")

    # common
    ap.add_argument("--max_markets", type=int, default=None, help="limit USDT markets scanned (debug)")

    args = ap.parse_args()

    if args.method == "candles":
        ranked = rank_topn_by_public_candles(
            n=args.n,
            profile=args.profile,
            candle_unit_minutes=args.unit,
            candle_count=args.count,
            max_markets=args.max_markets,
            request_sleep=args.sleep,
        )
    else:
        ranked = rank_topn_by_live_buffer(
            n=args.n,
            profile=args.profile,
            seconds=args.seconds,
            interval_sec=args.interval,
            chunk_size=args.chunk,
            max_markets=args.max_markets,
        )

    _print_ranked(ranked)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
