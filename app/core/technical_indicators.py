# ============================================================
# Technical Indicators — ATR & Bollinger Bands
# Pure computation from candle data (no HTTP calls)
# ============================================================
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

# ---- In-memory cache (10-minute TTL) ----
_indicator_cache: Dict[str, Tuple[float, Dict[str, float]]] = {}
_CACHE_TTL_SEC = 600.0  # 10 minutes


def calc_atr_from_candles(candles: List[Dict[str, Any]], period: int = 14) -> float:
    """ATR from candle list (newest-first, Bybit format).

    Returns ATR in absolute price units.
    """
    if len(candles) < 2:
        return 0.0
    needed = period + 1
    if len(candles) < needed:
        period = len(candles) - 1

    ordered = list(reversed(candles[:needed]))
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


def calc_bollinger_from_candles(
    candles: List[Dict[str, Any]],
    period: int = 20,
    num_std: float = 2.0,
) -> Dict[str, float]:
    """Bollinger Bands from candle list (newest-first).

    Returns: {"upper": ..., "middle": ..., "lower": ..., "width_pct": ...}
    """
    if len(candles) < period:
        return {"upper": 0.0, "middle": 0.0, "lower": 0.0, "width_pct": 0.0}

    closes = [float(c.get("trade_price") or 0) for c in candles[:period]]
    closes = [p for p in closes if p > 0]
    if len(closes) < period:
        return {"upper": 0.0, "middle": 0.0, "lower": 0.0, "width_pct": 0.0}

    sma = sum(closes) / len(closes)
    variance = sum((p - sma) ** 2 for p in closes) / len(closes)
    std = variance ** 0.5

    upper = sma + num_std * std
    lower = sma - num_std * std
    width_pct = ((upper - lower) / sma * 100.0) if sma > 0 else 0.0

    return {
        "upper": upper,
        "middle": sma,
        "lower": lower,
        "width_pct": width_pct,
    }


def compute_indicators(
    market: str,
    candles: List[Dict[str, Any]],
    atr_period: int = 14,
    bb_period: int = 20,
    use_cache: bool = True,
) -> Dict[str, float]:
    """Compute ATR + Bollinger for a market (with cache).

    Returns dict with keys:
        atr, atr_pct, bb_upper, bb_middle, bb_lower, bb_width_pct
    """
    now = time.time()
    if use_cache:
        cached = _indicator_cache.get(market)
        if cached and (now - cached[0]) < _CACHE_TTL_SEC:
            return cached[1]

    if not candles or len(candles) < 3:
        return {
            "atr": 0.0, "atr_pct": 0.0,
            "bb_upper": 0.0, "bb_middle": 0.0, "bb_lower": 0.0, "bb_width_pct": 0.0,
        }

    price = float(candles[0].get("trade_price") or 0)
    atr = calc_atr_from_candles(candles, atr_period)
    atr_pct = (atr / price * 100.0) if price > 0 else 0.0

    bb = calc_bollinger_from_candles(candles, bb_period)

    result = {
        "atr": atr,
        "atr_pct": atr_pct,
        "bb_upper": bb["upper"],
        "bb_middle": bb["middle"],
        "bb_lower": bb["lower"],
        "bb_width_pct": bb["width_pct"],
    }

    if use_cache:
        _indicator_cache[market] = (now, result)

    return result
