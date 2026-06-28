# ============================================================
# File: app/manager/reserved_selector_fetchers.py
# Autocoin OS v3-H — Market Data Fetching Functions
# ------------------------------------------------------------
# Extracted from reserved_selector.py
# Contains: build_watchlist, candle/highlow/ticker/orderbook
#           fetchers, and associated caches.
# ============================================================

from __future__ import annotations
import math
import os
import time
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from app.core.rate_limiter import bybit_rate_limit

from app.core.constants import (
    BYBIT_API_BASE,
    BYBIT_MARKET_INSTRUMENTS,
    BYBIT_MARKET_TICKERS,
    BYBIT_MARKET_ORDERBOOK,
    BYBIT_MARKET_RECENT_TRADE,
    BYBIT_MARKET_KLINE,
    DEFAULT_REQUEST_TIMEOUT_SEC,
    bybit_v5_rest_category,
    parse_bybit_list,
    normalize_bybit_ticker,
)
from app.core.currency import Q

from app.manager.reserved_selector_utils import (
    SharedMarketData,
    _snapshot_from_ticker_and_ob,
    _sf,
    _si,
    _norm,
    _normalize_market,
    _currency,
    _chunks,
    _csv_upper,
    _global_exclude_bases,
    _global_exclude_markets,
    _calc_spread_bps,
    _calc_depth_notional,
    _execution_quality_penalty,
    BYBIT_BASE,
    DEFAULT_TIMEOUT,
)

_logger = logging.getLogger(__name__)

# ── Module-level caches ──────────────────────────────────────

# Candle cache: reduce repeated 15m/30-candle fetch bursts from Reserved scans.
_candle_cache: Dict[str, Tuple[List[Dict[str, Any]], float, float]] = {}

# Last prefetch target list — updated by build_reserved_candidates, consumed by prefetch loop
_last_prefetch_markets: List[str] = []

# Highlow cache (300s TTL - aligned with autopilot 5-min cycle, prevents 429)
_highlow_cache: Dict[str, Tuple[Dict[str, float], float]] = {}
_HIGHLOW_CACHE_TTL = 300.0  # 300s


# ============================================================
# build_watchlist
# ============================================================

def build_watchlist(
    system: Any,
    *,
    entry_ob_depth_bps: float = 0.0,
) -> SharedMarketData:
    """Phase-1 scan: fetch market universe, tickers, orderbooks, build snapshots.

    Returns a :class:`SharedMarketData` that ``build_reserved_candidates``
    can reuse via the *shared_data* parameter, eliminating redundant API calls.

    This function is safe to call from a background thread
    (``HyperSystem._watchlist_refresh_loop``).
    """
    sess = requests.Session()

    details = fetch_markets_details(sess)

    sd = SharedMarketData()

    quote_markets: List[str] = []
    for row in details:
        m = _norm(row.get("market"))
        if not m:
            continue
        quote_markets.append(m)
        sd.names_map[m] = {
            "kr": str(row.get("korean_name") or "").strip(),
            "en": str(row.get("english_name") or "").strip(),
        }
        sd.caution_map[m] = str(row.get("market_warning") or "").strip().upper() == "CAUTION"
        market_state = str(row.get("market_state") or "").strip().upper()
        delist_date = row.get("delisting_date")
        is_delisting = (market_state == "DELISTED") or (delist_date is not None and str(delist_date).strip() != "")
        sd.delisting_map[m] = is_delisting
        sd.delisting_date_map[m] = str(delist_date).strip() if delist_date else None

    exclude_caution = os.getenv("OMA_SELECTOR_EXCLUDE_CAUTION", "1").strip() != "0"
    exclude_delisting = os.getenv("OMA_SELECTOR_EXCLUDE_DELISTING", "1").strip() != "0"
    skip_currencies = [str(x).upper() for x in (getattr(system, "skip_currencies", []) or []) if str(x).strip()]
    skip_currencies = sorted(set(skip_currencies) | set(_global_exclude_bases()))
    global_exclude_mkts = _global_exclude_markets()

    existing: set[str] = set()
    cooldown_markets: set[str] = set()
    try:
        snap = system.oma.snapshot() if hasattr(system, "oma") else system.oma_registry.snapshot()
        for bucket in ("active", "recovery"):
            for row in (snap.get(bucket) or []):
                mk = str((row if isinstance(row, dict) else {}).get("market") or row if isinstance(row, str) else "").strip().upper()
                if mk:
                    existing.add(mk)
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        _logger.warning("[SELECTOR_FETCH] OMA snapshot fallback: %s", exc, exc_info=True)
    try:
        fn = getattr(system, "get_cooldown_markets", None)
        if callable(fn):
            cooldown_markets = set(fn())
    except (KeyError, AttributeError, TypeError) as exc:
        _logger.warning("[SELECTOR_FETCH] cooldown_markets fallback: %s", exc, exc_info=True)

    _fs = {"cooldown": 0, "skip_currency": 0, "global_market": 0, "existing": 0, "caution": 0, "delisting": 0}
    for m in sorted(set(quote_markets)):
        if m in existing:
            _fs["existing"] += 1
            continue
        if m in cooldown_markets:
            _fs["cooldown"] += 1
            continue
        if global_exclude_mkts and m in global_exclude_mkts:
            _fs["global_market"] += 1
            continue
        if _currency(m) in skip_currencies:
            _fs["skip_currency"] += 1
            continue
        if exclude_caution and sd.caution_map.get(m, False):
            continue
        if exclude_delisting and sd.delisting_map.get(m, False):
            continue
        sd.universe.append(m)
    sd.filter_stats["excluded_cooldown"] = _fs["cooldown"]
    sd.filter_stats["excluded_skip_currency"] = _fs["skip_currency"]
    sd.filter_stats["excluded_global_market"] = _fs["global_market"]

    for group in _chunks(sd.universe, 100):
        try:
            ticks = fetch_tickers(sess, group)
            for t in ticks:
                mm = _norm(t.get("market"))
                if mm:
                    sd.tmap[mm] = t
        except (KeyError, AttributeError, TypeError) as e:
            _logger.warning("[SELECTOR_FETCH] ticker chunk failed: %s", e, exc_info=True)
            continue

    def _vol24(m: str) -> float:
        return _sf((sd.tmap.get(m) or {}).get("acc_trade_price_24h"), 0.0)

    sd.ranked_by_vol = sorted(
        [m for m in sd.universe if m in sd.tmap],
        key=_vol24, reverse=True,
    )

    candidate_price_min = _sf(
        getattr(system, "reserved_candidate_price_min_usdt", 0.0) or 0.0, 0.0
    )
    if candidate_price_min <= 0:
        candidate_price_min = _sf(os.getenv("OMA_SELECTOR_CANDIDATE_PRICE_MIN_USDT", "100"), 100.0)
    candidate_price_max = _sf(
        getattr(system, "reserved_candidate_price_max_usdt", 0.0) or 0.0, 0.0
    )

    # Common liquidity hard filter: applied to all strategies (ENV-adjustable)
    _vol_default = "200000" if Q.is_usdt else "500000000"
    _price_default = "0.5" if Q.is_usdt else "500"
    _global_min_vol24 = _sf(os.getenv("OMA_SELECTOR_GLOBAL_MIN_VOL24_USDT", _vol_default), float(_vol_default))
    _global_min_price = _sf(os.getenv("OMA_SELECTOR_GLOBAL_MIN_PRICE_USDT", _price_default), float(_price_default))

    # Price floor: use the larger of the config value and the global minimum
    if candidate_price_min < _global_min_price:
        candidate_price_min = _global_min_price

    filtered: List[str] = []
    _price_min_cnt = 0
    _price_max_cnt = 0
    _vol_min_cnt = 0
    for m in sd.ranked_by_vol:
        px = _sf((sd.tmap.get(m) or {}).get("trade_price"), 0.0)
        vol24 = _sf((sd.tmap.get(m) or {}).get("acc_trade_price_24h"), 0.0)
        if px <= 0:
            continue
        if candidate_price_min > 0 and px < candidate_price_min:
            _price_min_cnt += 1
            continue
        if candidate_price_max > 0 and px > candidate_price_max:
            _price_max_cnt += 1
            continue
        # Volume hard filter: exclude from candidates if 24h turnover is below threshold
        if _global_min_vol24 > 0 and vol24 < _global_min_vol24:
            _vol_min_cnt += 1
            continue
        filtered.append(m)
    sd.ranked_by_vol = filtered
    sd.filter_stats["excluded_price_min"] = _price_min_cnt
    sd.filter_stats["excluded_price_max"] = _price_max_cnt
    sd.filter_stats["excluded_vol24_min"] = _vol_min_cnt

    scan_union = sorted(set(sd.ranked_by_vol))

    for group in _chunks(scan_union, 60):
        try:
            obs = fetch_orderbooks(sess, group)
            for ob in obs:
                mm = _norm(ob.get("market"))
                if mm:
                    sd.obmap[mm] = ob
        except (requests.exceptions.RequestException, OSError) as e:
            _logger.warning("[orderbook_fetch] chunk network error: %s", e)
            continue
        except (KeyError, AttributeError, TypeError) as e:
            _logger.warning("[orderbook_fetch] chunk parse error: %s", e, exc_info=True)
            continue
    _logger.info("[orderbook_fetch] loaded %d/%d orderbooks", len(sd.obmap), len(scan_union))

    for m in scan_union:
        snap0 = _snapshot_from_ticker_and_ob(
            m,
            sd.tmap.get(m),
            sd.obmap.get(m),
            depth_bps=float(entry_ob_depth_bps),
            caution=bool(sd.caution_map.get(m, False)),
            delisting=bool(sd.delisting_map.get(m, False)),
            delisting_date=sd.delisting_date_map.get(m),
            names=sd.names_map.get(m),
        )
        if snap0 is not None:
            sd.smap[m] = snap0

    sess.close()
    sd.ts = time.time()
    SharedMarketData.store(sd)
    _logger.info("[build_watchlist] universe=%d, tickers=%d, snapshots=%d",
                 len(sd.universe), len(sd.tmap), len(sd.smap))
    return sd


# ============================================================
# Candle Fetchers
# ============================================================

@bybit_rate_limit
def fetch_candles_minutes(
    session: requests.Session,
    market: str,
    *,
    unit: int = 5,
    count: int = 30,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Fetch minute candles for AI feature extraction (Bybit candles API)."""
    m = _normalize_market(market)
    if not m:
        return []
    try:
        r = session.get(
            BYBIT_MARKET_KLINE,
            params={"category": bybit_v5_rest_category(), "symbol": m, "interval": str(unit), "limit": count},
            timeout=float(timeout),
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
        return candles
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
        _logger.warning("[candle_fetch] %s failed: %s", m, e, exc_info=True)
        return []


def _fetch_candles_minutes_cached(
    session: requests.Session,
    market: str,
    *,
    unit: int = 5,
    count: int = 30,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Fetch candles with short TTL + retry cooldown to reduce API churn."""
    m = _normalize_market(market)
    if not m:
        return []

    key = f"{m}:{int(unit)}:{int(count)}"
    now = time.time()

    ttl_sec = _sf(os.getenv("OMA_SELECTOR_CANDLE_CACHE_TTL_SEC", "120"), 120.0)
    cooldown_sec = _sf(os.getenv("OMA_SELECTOR_CANDLE_FAIL_COOLDOWN_SEC", "6"), 6.0)
    ttl_sec = max(3.0, float(ttl_sec))
    cooldown_sec = max(1.0, float(cooldown_sec))

    cached = _candle_cache.get(key)
    if cached:
        cached_data, cached_ts, retry_after_ts = cached
        if retry_after_ts > now:
            return list(cached_data or [])
        if cached_data and (now - cached_ts) < ttl_sec:
            return list(cached_data)

    data = fetch_candles_minutes(session, m, unit=unit, count=count, timeout=timeout)
    if data:
        _candle_cache[key] = (list(data), now, 0.0)
        # Opportunistic prune
        if len(_candle_cache) > 2000:
            cutoff = now - (ttl_sec * 4.0)
            for k in list(_candle_cache.keys()):
                _, ts0, _ = _candle_cache.get(k, ([], 0.0, 0.0))
                if ts0 < cutoff:
                    _candle_cache.pop(k, None)
        return list(data)

    # Empty/fail: throttle retries briefly, return stale data if available.
    stale_data: List[Dict[str, Any]] = []
    stale_ts = now
    if cached:
        stale_data = list(cached[0] or [])
        stale_ts = float(cached[1] or now)
    _candle_cache[key] = (stale_data, stale_ts, now + cooldown_sec)
    return stale_data


# ============================================================
# High/Low Fetcher
# ============================================================

@bybit_rate_limit

def fetch_highlow_for_lookback(
    session: requests.Session,
    market: str,
    lookback_min: int,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, float]:
    """Fetch actual high/low prices for the given lookback period.

    Args:
        session: requests Session
        market: Market symbol (e.g., 'BTCUSDT')
        lookback_min: Lookback period in minutes (e.g., 240 = 4 hours)
        timeout: Request timeout

    Returns:
        Dict with keys: high, low, current, range_pct, distance_from_low_pct
    """
    import time

    m = _normalize_market(market)
    if not m:
        return {
            "high": 0.0,
            "low": 0.0,
            "current": 0.0,
            "range_pct": 0.0,
            "distance_from_low_pct": 0.0,
            "candle_count": 0.0,
            "unit_min": 0.0,
        }

    # Cache check (lookback-proportional TTL: short ranges 300s, long ranges up to 1800s)
    cache_key = f"{m}:{lookback_min}"
    now = time.time()
    _effective_ttl = min(1800.0, max(_HIGHLOW_CACHE_TTL, float(lookback_min) * 0.1))
    if cache_key in _highlow_cache:
        cached_data, cached_time = _highlow_cache[cache_key]
        if now - cached_time < _effective_ttl:
            return cached_data

    # Bybit minute-candle API only supports 1, 3, 5, 10, 15, 30, 60, 240 minutes
    # Pick the unit and count matching the lookback duration
    if lookback_min <= 60:
        unit = 1
        count = lookback_min
    elif lookback_min <= 180:
        unit = 3
        count = lookback_min // 3
    elif lookback_min <= 300:
        unit = 5
        count = lookback_min // 5
    elif lookback_min <= 600:
        unit = 10
        count = lookback_min // 10
    elif lookback_min <= 900:
        unit = 15
        count = lookback_min // 15
    elif lookback_min <= 1800:
        unit = 30
        count = lookback_min // 30
    elif lookback_min <= 3600:
        unit = 60
        count = lookback_min // 60
    else:
        unit = 240
        count = min(200, lookback_min // 240)  # Bybit max 200 candles

    count = max(1, min(200, count))  # API limit

    try:
        for _attempt in range(2):
            r = session.get(
                BYBIT_MARKET_KLINE,
                params={"category": bybit_v5_rest_category(), "symbol": m, "interval": str(unit), "limit": count},
                timeout=float(timeout),
            )
            if r.status_code == 429 and _attempt == 0:
                time.sleep(1.5)
                continue
            r.raise_for_status()
            break
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
                    "timestamp": int(k[0]),
                })

        if not candles:
            return {
                "high": 0.0,
                "low": 0.0,
                "current": 0.0,
                "range_pct": 0.0,
                "distance_from_low_pct": 0.0,
                "candle_count": 0.0,
                "unit_min": float(unit),
            }

        highs = [float(c.get("high_price") or 0) for c in candles if c.get("high_price")]
        lows = [float(c.get("low_price") or 0) for c in candles if c.get("low_price")]
        current = float(candles[0].get("trade_price") or 0)  # close of the latest candle

        if not highs or not lows:
            return {
                "high": 0.0,
                "low": 0.0,
                "current": current,
                "range_pct": 0.0,
                "distance_from_low_pct": 0.0,
                "candle_count": float(len(candles)),
                "unit_min": float(unit),
            }

        high = max(highs)
        low = min(lows)

        range_pct = ((high - low) / low * 100) if low > 0 else 0.0
        distance_from_low_pct = ((current - low) / low * 100) if low > 0 else 0.0

        result = {
            "high": high,
            "low": low,
            "current": current,
            "range_pct": round(range_pct, 2),
            "distance_from_low_pct": round(distance_from_low_pct, 2),
            "candle_count": float(len(candles)),
            "unit_min": float(unit),
        }

        # Store in cache
        _highlow_cache[cache_key] = (result, now)

        return result
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
        _logger.warning(f"[fetch_highlow] Failed for {m} lookback={lookback_min}min: {e}")
        return {
            "high": 0.0,
            "low": 0.0,
            "current": 0.0,
            "range_pct": 0.0,
            "distance_from_low_pct": 0.0,
            "candle_count": 0.0,
            "unit_min": 0.0,
        }


# ============================================================
# Market / Ticker / Orderbook / Trades Fetchers
# ============================================================

def fetch_markets_details(session: requests.Session, *, timeout: float = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    """Fetch all spot markets from Bybit V5 instruments-info API."""
    last_err = None
    for _attempt in range(3):
        try:
            r = session.get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=float(timeout))
            r.raise_for_status()
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            _logger.warning("[fetch_markets_details] attempt %d/3 failed: %s", _attempt + 1, e)
            import time as _t; _t.sleep(2.0 * (_attempt + 1))
    else:
        raise last_err  # type: ignore[misc]
    data = parse_bybit_list(r.json())

    result: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        quote_coin = str(row.get("quoteCoin") or "").upper()
        if quote_coin != Q.symbol:
            continue
        market = Q.normalize(sym)
        status = str(row.get("status") or "").upper()
        base_coin = str(row.get("baseCoin") or "").upper()
        result.append({
            "market": market,
            "korean_name": base_coin,
            "english_name": base_coin,
            "market_warning": "",
            "market_state": "ACTIVE" if status == "TRADING" else status,
            "delisting_date": None,
        })

    return result


def fetch_tickers(session: requests.Session, markets: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    """Fetch tickers from Bybit V5 (converted to Bybit-compatible format)."""
    if not markets:
        return []

    market_set = set(_norm(m).upper() for m in markets if m)
    if not market_set:
        return []

    r = session.get(
        BYBIT_MARKET_TICKERS,
        params={"category": bybit_v5_rest_category()},
        timeout=float(timeout),
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


def fetch_orderbooks(session: requests.Session, markets: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    """Fetch orderbooks from Bybit V5 (one symbol at a time, converted to Bybit format).

    NOTE: Bybit orderbook API returns ``{"result": {"s": ..., "b": [...], "a": [...]}}``,
    NOT ``{"result": {"list": [...]}}``.  Do NOT use ``parse_bybit_list()`` here.
    """
    if not markets:
        return []

    results: List[Dict[str, Any]] = []
    _fail_count = 0
    for sym in markets:
        sym_norm = _norm(sym)
        if not sym_norm:
            continue
        try:
            r = session.get(
                BYBIT_MARKET_ORDERBOOK,
                params={"category": bybit_v5_rest_category(), "symbol": sym_norm, "limit": 15},
                timeout=float(timeout),
            )
            r.raise_for_status()
            body = r.json()
            # Bybit V5 orderbook: result is the orderbook object itself (not result.list)
            ob = body.get("result") if isinstance(body, dict) else None
            if not isinstance(ob, dict) or ob.get("retCode", body.get("retCode", 0)) != 0:
                _logger.warning("[orderbook_fetch] %s: unexpected response: retCode=%s",
                                sym_norm, body.get("retCode"))
                _fail_count += 1
                continue
            bids = ob.get("b", [])
            asks = ob.get("a", [])
            if not bids or not asks:
                _logger.warning("[orderbook_fetch] %s: empty bids/asks", sym_norm)
                _fail_count += 1
                continue
            units = []
            for i in range(min(len(bids), len(asks), 15)):
                units.append({
                    "bid_price": float(bids[i][0]),
                    "bid_size": float(bids[i][1]),
                    "ask_price": float(asks[i][0]),
                    "ask_size": float(asks[i][1]),
                })
            results.append({"market": sym_norm, "orderbook_units": units})
        except (requests.exceptions.RequestException, OSError) as e:
            _logger.warning("[orderbook_fetch] %s network error: %s", sym_norm, e)
            _fail_count += 1
            continue
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
            _logger.warning("[orderbook_fetch] %s parse error: %s", sym_norm, e, exc_info=True)
            _fail_count += 1
            continue
    if _fail_count:
        _logger.warning("[orderbook_fetch] %d/%d symbols failed", _fail_count, len(markets))
    return results


def fetch_recent_trades_count(
    session: requests.Session,
    market: str,
    *,
    minutes: int = 5,
    max_count: int = 200,
    timeout: float = DEFAULT_TIMEOUT,
) -> int:
    """Count trades within the last N minutes using Bybit trades/ticks endpoint.

    WARNING: Expensive if called for many markets; caller must throttle/limit.
    """
    m = _normalize_market(market)
    if not m:
        return 0
    minutes = max(1, int(minutes))
    max_count = max(1, min(int(max_count), 500))  # Bybit allows up to 500

    try:
        _last_err = None
        for _attempt in range(2):
            try:
                r = session.get(
                    BYBIT_MARKET_RECENT_TRADE,
                    params={"category": bybit_v5_rest_category(), "symbol": m, "limit": str(max_count)},
                    timeout=float(timeout),
                )
                r.raise_for_status()
                _last_err = None
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as _e:
                _last_err = _e
                import time as _t; _t.sleep(1.0)
        if _last_err:
            _logger.warning("[trades_count] %s failed after 2 attempts: %s", m, _last_err)
            return 0
        data = parse_bybit_list(r.json())
        if not data:
            return 0

        now_ms = int(time.time() * 1000)
        cutoff = now_ms - (minutes * 60 * 1000)
        c = 0
        for row in data:
            if not isinstance(row, dict):
                continue
            # Bybit uses "time" (string ms) for trade timestamp
            ts = row.get("time") or row.get("timestamp", 0)
            tsm = _si(ts, 0)
            if tsm >= cutoff:
                c += 1
        return int(c)
    except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
        _logger.warning("[trades_count] %s failed: %s", m, e, exc_info=True)
        return 0
