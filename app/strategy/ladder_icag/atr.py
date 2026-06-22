# ============================================================
# ICAG ATR & VWAP Calculator — Bybit candle-based
# ============================================================
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from app.core.constants import BYBIT_MARKET_KLINE

logger = logging.getLogger(__name__)

# ---- SSL context for Windows (avoid CERTIFICATE_VERIFY_FAILED) ----
try:
    import certifi
    _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except (ImportError, AttributeError, TypeError):
    logging.getLogger(__name__).warning("certifi not available, using insecure SSL context", exc_info=True)
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE

# ---- In-memory cache to avoid spamming Bybit ----
_candle_cache: Dict[str, Tuple[float, List[dict]]] = {}
_CACHE_TTL_SEC = 600.0  # refresh every ~10min (429 방지 — ATR/VWAP는 느리게 변함)

# ---- Global cooldown: 429 발생 시 일정 시간 전체 요청 차단 ----
_last_fetch_ts: float = 0.0
_FETCH_MIN_INTERVAL: float = 0.15  # 최소 150ms 간격
_FETCH_BOOT_INTERVAL: float = 1.0  # 부팅 후 60초간 요청 간격 (429 방지)
_backoff_until: float = 0.0  # 429 발생 시 백오프 종료 시점
_module_init_ts: float = time.time()  # 모듈 로드 시점 (부팅 감지용)


def _fetch_candles(
    market: str,
    timeframe_minutes: int = 5,
    count: int = 50,
) -> List[dict]:
    """Fetch candles from Bybit REST API.  Returns newest-first."""
    global _last_fetch_ts, _backoff_until
    normalized_market = market if market.endswith("USDT") else f"{market}USDT"
    cache_key = f"{normalized_market}:{timeframe_minutes}:{count}"
    now = time.time()

    cached = _candle_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    # 429 백오프 중이면 캐시(stale) 반환
    if now < _backoff_until:
        if cached:
            return cached[1]
        return []

    # 최소 간격 유지 (부팅 60초간은 넓은 간격 → 429 방지)
    _min_interval = _FETCH_BOOT_INTERVAL if (now - _module_init_ts) < 60.0 else _FETCH_MIN_INTERVAL
    elapsed = now - _last_fetch_ts
    if elapsed < _min_interval:
        time.sleep(_min_interval - elapsed)

    url = f"{BYBIT_MARKET_KLINE}/{timeframe_minutes}?market={normalized_market}&count={count}"
    try:
        _last_fetch_ts = time.time()
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list) and len(data) > 0:
            _candle_cache[cache_key] = (time.time(), data)
            return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        if "429" in str(e):
            _backoff_until = time.time() + 10.0  # 429 → 10초 전체 차단
            logger.warning("_fetch_candles(%s, %dm) 429 → 10s backoff", market, timeframe_minutes)
        else:
            logger.warning("_fetch_candles(%s, %dm) failed: %s", market, timeframe_minutes, e)

    # stale cache fallback
    if cached:
        return cached[1]
    return []


# ============================================================
# ATR
# ============================================================

def calc_atr(candles: List[dict], period: int = 14) -> float:
    """Calculate ATR.  *candles* must be newest-first (Bybit default)."""
    needed = period + 1
    if len(candles) < 2:
        return 0.0
    if len(candles) < needed:
        period = len(candles) - 1

    # oldest-first for sequential calc
    ordered = list(reversed(candles[: period + 1]))

    true_ranges: List[float] = []
    for i in range(1, len(ordered)):
        h = float(ordered[i].get("high_price") or 0)
        lo = float(ordered[i].get("low_price") or 0)
        pc = float(ordered[i - 1].get("trade_price") or 0)
        if h <= 0 or lo <= 0 or pc <= 0:
            continue
        tr = max(h - lo, abs(h - pc), abs(lo - pc))
        true_ranges.append(tr)

    if not true_ranges:
        return 0.0
    return sum(true_ranges) / len(true_ranges)


def calc_atr_multi_tf(
    market: str,
    period: int = 14,
) -> float:
    """Multi-timeframe blended ATR (5m / 60m / 240m).

    Returns an ATR in *price units* that smooths out short-term noise
    while still reacting to sudden spikes.
    """
    candles_5m = _fetch_candles(market, 5, count=period + 5)
    candles_1h = _fetch_candles(market, 60, count=period + 5)
    candles_4h = _fetch_candles(market, 240, count=period + 5)

    atr_5m = calc_atr(candles_5m, period)
    atr_1h = calc_atr(candles_1h, period)
    atr_4h = calc_atr(candles_4h, period)

    # Normalise longer TF ATRs to 5m scale (approximate)
    atr_1h_scaled = atr_1h / (12 ** 0.5) if atr_1h else 0.0   # √12
    atr_4h_scaled = atr_4h / (48 ** 0.5) if atr_4h else 0.0   # √48

    # spike detection: if 5m ATR is much larger → weight it more
    if atr_5m > atr_1h_scaled * 1.5 and atr_1h_scaled > 0:
        return 0.50 * atr_5m + 0.30 * atr_1h_scaled + 0.20 * atr_4h_scaled
    # normal
    if atr_1h_scaled > 0:
        return 0.30 * atr_5m + 0.40 * atr_1h_scaled + 0.30 * atr_4h_scaled
    # fallback: only 5m available
    return atr_5m


# ============================================================
# VWAP
# ============================================================

def calc_vwap(candles: List[dict]) -> float:
    """Volume-weighted average price from candle list (any order)."""
    total_vp = 0.0
    total_vol = 0.0
    for c in candles:
        vol = float(c.get("candle_acc_trade_volume") or 0)
        tp = (
            float(c.get("high_price") or 0)
            + float(c.get("low_price") or 0)
            + float(c.get("trade_price") or 0)
        ) / 3.0
        if vol > 0 and tp > 0:
            total_vp += tp * vol
            total_vol += vol
    return total_vp / total_vol if total_vol > 0 else 0.0


# ============================================================
# Public helpers
# ============================================================

def get_market_atr(
    market: str,
    period: int = 14,
    timeframe_minutes: int = 5,
    multi_tf: bool = False,
) -> Tuple[float, float]:
    """Return (atr_absolute, atr_pct) for a market."""
    if multi_tf:
        atr = calc_atr_multi_tf(market, period)
    else:
        candles = _fetch_candles(market, timeframe_minutes, count=period + 5)
        atr = calc_atr(candles, period)

    # current price from latest candle
    candles_latest = _fetch_candles(market, 5, count=1)
    price = float(candles_latest[0].get("trade_price") or 0) if candles_latest else 0.0
    atr_pct = (atr / price * 100.0) if price > 0 else 0.0
    return atr, atr_pct


def get_market_vwap(market: str, hours: int = 24) -> float:
    """Get VWAP over *hours* using 5-minute candles."""
    count = min(200, hours * 12)
    candles = _fetch_candles(market, 5, count=count)
    if not candles:
        return 0.0
    return calc_vwap(candles)


def get_current_price(market: str) -> float:
    """Latest trade price from 5m candle cache (fast, no extra API call)."""
    candles = _fetch_candles(market, 5, count=1)
    if candles:
        return float(candles[0].get("trade_price") or 0)
    return 0.0
