# ============================================================
# File: app/strategy/indicators.py
# ------------------------------------------------------------
# Collection of technical indicator calculation functions.
# Module called by StrategyBrain when performing market analysis.
# ============================================================

from __future__ import annotations
from typing import List, Dict, Tuple, Optional

# ============================================================
# [PERF-TELEMETRY] Indicator call-count counter (2026-03-21)
# ============================================================
_call_counts: Dict[str, int] = {}

def _count(name: str) -> None:
    """Called on indicator-function entry. Reset via reset_call_counts() at tick start."""
    _call_counts[name] = _call_counts.get(name, 0) + 1

def reset_call_counts() -> None:
    """Reset the counter; call before the start of a tick cycle."""
    _call_counts.clear()

def get_call_counts() -> Dict[str, int]:
    """Return the current tick's indicator call counts (a copy)."""
    return dict(_call_counts)


# ============================================================
# [PERF] Per-tick cache integration (2026-03-21)
# ============================================================
def _cache_key(name: str, data, *params) -> tuple:
    """Content-based cache key. 3-point sampling (first/mid/last) + length for collision resistance."""
    n = len(data) if data else 0
    return (name, n, data[-1] if n else 0, data[0] if n else 0,
            data[n // 2] if n > 2 else 0, *params)

try:
    from app.strategy.indicator_cache import get_or_compute as _cache_get
except ImportError:
    import logging as _logging
    _logging.getLogger(__name__).warning("indicator_cache module not available, caching disabled", exc_info=True)
    # Bypass if the cache module is unavailable
    def _cache_get(key, fn):  # type: ignore
        return fn()

# ============================================================
# RSI
# ============================================================
def rsi(data: List[float], length: int = 14) -> float | None:
    _count("rsi")
    if not data or len(data) < length + 1:
        return None
    return _cache_get(_cache_key("rsi", data, length), lambda: _rsi_impl(data, length))

def _rsi_impl(data: List[float], length: int) -> float | None:
    # Calculate changes
    deltas = [data[i] - data[i-1] for i in range(1, len(data))]

    # First average (SMA)
    seed_gains = [d for d in deltas[:length] if d >= 0]
    seed_losses = [abs(d) for d in deltas[:length] if d < 0]

    avg_gain = sum(seed_gains) / length
    avg_loss = sum(seed_losses) / length

    # Subsequent averages (Wilder's Smoothing)
    for i in range(length, len(deltas)):
        change = deltas[i]
        g = change if change > 0 else 0
        l = abs(change) if change < 0 else 0

        avg_gain = (avg_gain * (length - 1) + g) / length
        avg_loss = (avg_loss * (length - 1) + l) / length

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ============================================================
# MACD
# ============================================================
def macd(
    data: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> Tuple[float | None, float | None, float | None]:
    _count("macd")
    if not data or len(data) < slow + signal:
        return None, None, None
    return _cache_get(_cache_key("macd", data, fast, slow, signal), lambda: _macd_impl(data, fast, slow, signal))

def _macd_impl(
    data: List[float],
    fast: int,
    slow: int,
    signal: int
) -> Tuple[float | None, float | None, float | None]:

    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)
    k_sig = 2.0 / (signal + 1)

    # Fast EMA init (SMA of first `fast` points) -> update up to the slow-1 point
    ema_fast = sum(data[:fast]) / fast
    for p in data[fast:slow]:
        ema_fast = p * k_fast + ema_fast * (1.0 - k_fast)

    # Slow EMA init (SMA of first `slow` points)
    ema_slow = sum(data[:slow]) / slow

    # Collect MACD line history over the data[slow:] range
    macd_values: List[float] = []
    for p in data[slow:]:
        ema_fast = p * k_fast + ema_fast * (1.0 - k_fast)
        ema_slow = p * k_slow + ema_slow * (1.0 - k_slow)
        macd_values.append(ema_fast - ema_slow)

    if len(macd_values) < signal:
        return None, None, None

    # Signal Line = EMA(signal) of MACD line values
    sig_ema = sum(macd_values[:signal]) / signal
    for mv in macd_values[signal:]:
        sig_ema = mv * k_sig + sig_ema * (1.0 - k_sig)

    macd_line = macd_values[-1]
    signal_line = sig_ema
    hist = macd_line - signal_line

    return macd_line, signal_line, hist


# ============================================================
# SMA
# ============================================================
def sma(data: List[float], length: int = 14) -> float | None:
    _count("sma")
    if not data or len(data) < length:
        return None
    return _cache_get(_cache_key("sma", data, length), lambda: sum(data[-length:]) / length)


# ============================================================
# EMA
# ============================================================
def ema(data: List[float], length: int = 14) -> float | None:
    _count("ema")
    if not data or len(data) < length:
        return None
    return _cache_get(_cache_key("ema", data, length), lambda: _ema_impl(data, length))

def _ema_impl(data: List[float], length: int) -> float | None:
    # EMA depends heavily on past data, so use as much as possible, or
    # here we simply compute over recent data (full history is recommended in practice)
    arr = data[-length*2:] if len(data) > length*2 else data
    k = 2 / (length + 1)
    ema_val = arr[0]
    for p in arr[1:]:
        ema_val = p * k + ema_val * (1 - k)
    return ema_val


# ============================================================
# Volatility (standard-deviation based)
# ============================================================
def volatility(data: List[float], length: int = 14) -> float | None:
    """Compute the standard deviation (%) of returns. (Matches the AI training data)"""
    _count("volatility")
    if not data or len(data) < length + 1:
        return None
    return _cache_get(_cache_key("volatility", data, length), lambda: _volatility_impl(data, length))

def _volatility_impl(data: List[float], length: int) -> float | None:
    # Calculate returns: (p_t - p_{t-1}) / p_{t-1}
    # Use length+1 price points to obtain length returns
    window = data[-(length + 1):]
    rets = [(window[i] - window[i-1]) / window[i-1] for i in range(1, len(window)) if window[i-1] > 0]

    if len(rets) < 2:
        return 0.0

    # Sample Standard Deviation (N-1)
    mean_ret = sum(rets) / len(rets)
    var_ret = sum((r - mean_ret) ** 2 for r in rets) / (len(rets) - 1)

    return (var_ret ** 0.5) * 100


# ============================================================
# Trend (% change)
# ============================================================
def trend(data: List[float], length: int = 14) -> float | None:
    _count("trend")
    if not data or len(data) < length:
        return None
    return _cache_get(_cache_key("trend", data, length),
                      lambda: (data[-1] - data[-length]) / data[-length] * 100)


# ============================================================
# Momentum
# ============================================================
def momentum(data: List[float], length: int = 14) -> float | None:
    _count("momentum")
    if not data or len(data) < length:
        return None
    return _cache_get(_cache_key("momentum", data, length),
                      lambda: data[-1] - data[-length])


# ============================================================
# Bollinger Bands
# ============================================================
def bollinger_bands(data: List[float], length: int = 20, k: float = 2.0) -> Dict[str, float] | None:
    _count("bollinger_bands")
    if not data or len(data) < length:
        return None
    return _cache_get(_cache_key("bollinger_bands", data, length, k), lambda: _bollinger_bands_impl(data, length, k))

def _bollinger_bands_impl(data: List[float], length: int, k: float) -> Dict[str, float] | None:
    arr = data[-length:]
    mid = sum(arr) / length

    # Standard deviation (population StdDev)
    var = sum((p - mid) ** 2 for p in arr) / length
    std = var ** 0.5

    upper = mid + std * k
    lower = mid - std * k

    # Bandwidth = (Upper - Lower) / Mid
    bandwidth = 0.0
    if mid > 0:
        bandwidth = (upper - lower) / mid

    return {
        "mid": mid,
        "upper": upper,
        "lower": lower,
        "std": std,
        "bandwidth": bandwidth
    }


# ============================================================
# ATR (Simplified: Close-to-Close)
# ============================================================
def atr_simplified(data: List[float], length: int = 14) -> float | None:
    """Approximate ATR computed from close prices only, without high/low data."""
    _count("atr_simplified")
    if not data or len(data) < length + 1:
        return None
    return _cache_get(_cache_key("atr_simplified", data, length), lambda: _atr_simplified_impl(data, length))

def _atr_simplified_impl(data: List[float], length: int) -> float | None:
    # True Range approximation: abs(current - prev)
    tr_list = [abs(data[i] - data[i-1]) for i in range(1, len(data))]

    # Wilder's Smoothing (RMA)
    if len(tr_list) < length:
        return None

    # First value is the SMA
    atr_val = sum(tr_list[:length]) / length

    # Subsequent values use smoothing
    for tr in tr_list[length:]:
        atr_val = (atr_val * (length - 1) + tr) / length

    return atr_val


# ============================================================
# Bollinger Band Squeeze
# ============================================================
def bollinger_squeeze(data: List[float], length: int = 20, k: float = 2.0, lookback: int = 20) -> Tuple[float, bool] | None:
    """
    Return the current bandwidth and whether a squeeze is occurring.
    Squeeze: when the current bandwidth is the lowest over the recent lookback period (minimal volatility)
    """
    _count("bollinger_squeeze")
    if not data or len(data) < length + lookback:
        return None

    # Compute the current bandwidth (using the cache)
    bb_now = bollinger_bands(data, length, k)
    if not bb_now:
        return None
    bw_now = bb_now["bandwidth"]

    # [PERF] Compute past lookback bandwidth directly (dict allocations 21 -> 0)
    # Compute only bandwidth instead of the full bollinger_bands() call
    is_squeeze = True
    for i in range(1, lookback + 1):
        end = len(data) - i
        if end < length:
            break
        window = data[end - length:end]
        mean = sum(window) / length
        if mean <= 0:
            continue
        std = (sum((p - mean) ** 2 for p in window) / length) ** 0.5
        bw = (2 * k * std) / mean
        if bw < bw_now:
            is_squeeze = False
            break

    return bw_now, is_squeeze


# ============================================================
# [PERF] Series functions (for autoloop integration, 2026-03-21 Phase 2)
# Exact port of the private implementations in autoloop_strategy.py.
# Kept separate because the seeding method differs from the existing rsi()/ema()/macd().
# ============================================================

def ema_series(data: List[float], length: int = 14) -> List[float]:
    """Full EMA series, seeded from data[0]. Returns list of length len(data).
    Port of autoloop_strategy._ema_series()."""
    _count("ema_series")
    if not data:
        return []
    if length <= 1:
        return list(data)
    return _cache_get(_cache_key("ema_series", data, length), lambda: _ema_series_impl(data, length))

def _ema_series_impl(data: List[float], length: int) -> List[float]:
    alpha = 2.0 / (length + 1.0)
    out: List[float] = []
    ema = float(data[0])
    for v in data:
        ema = alpha * float(v) + (1.0 - alpha) * ema
        out.append(ema)
    return out


def rsi_series(data: List[float], length: int = 14) -> List[float]:
    """O(N) single-pass Wilder RSI series. Returns list of RSI values.
    Port of autoloop_strategy._rsi_series_wilder()."""
    _count("rsi_series")
    if length <= 1 or len(data) <= length:
        return []
    return _cache_get(_cache_key("rsi_series", data, length), lambda: _rsi_series_impl(data, length))

def _rsi_series_impl(data: List[float], length: int) -> List[float]:
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(data)):
        d = float(data[i]) - float(data[i - 1])
        gains.append(d if d > 0 else 0.0)
        losses.append((-d) if d < 0 else 0.0)

    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    def rsi_from(ag: float, al: float) -> float:
        if al == 0.0 and ag == 0.0:
            return 50.0
        if al == 0.0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out: List[float] = [rsi_from(avg_gain, avg_loss)]
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        out.append(rsi_from(avg_gain, avg_loss))
    return out


def rsi_with_prev(data: List[float], length: int = 14) -> Tuple[float, float]:
    """(rsi_now, rsi_prev) using Wilder smoothing. Default (50, 50) if insufficient data.
    Port of autoloop_strategy._rsi_wilder()."""
    _count("rsi_with_prev")
    if length <= 1 or len(data) < length + 2:
        return 50.0, 50.0
    return _cache_get(_cache_key("rsi_with_prev", data, length), lambda: _rsi_with_prev_impl(data, length))

def _rsi_with_prev_impl(data: List[float], length: int) -> Tuple[float, float]:
    gains = 0.0
    losses = 0.0
    for i in range(1, length + 1):
        d = float(data[i]) - float(data[i - 1])
        if d >= 0:
            gains += d
        else:
            losses += -d
    avg_gain = gains / length
    avg_loss = losses / length

    def _calc_rsi(ag: float, al: float) -> float:
        if al <= 1e-12:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    rsi_prev = _calc_rsi(avg_gain, avg_loss)
    rsi_now = rsi_prev
    for i in range(length + 1, len(data)):
        d = float(data[i]) - float(data[i - 1])
        gain = d if d > 0 else 0.0
        loss = (-d) if d < 0 else 0.0
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
        rsi_prev, rsi_now = rsi_now, _calc_rsi(avg_gain, avg_loss)
    return float(rsi_now), float(rsi_prev)


def macd_hist_pair(data: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float]:
    """(hist_now, hist_prev) using ema_series seeding. Returns (0, 0) if insufficient data.
    Port of autoloop_strategy._macd_hist()."""
    _count("macd_hist_pair")
    if len(data) < max(fast, slow) + signal:
        return 0.0, 0.0
    return _cache_get(_cache_key("macd_hist_pair", data, fast, slow, signal),
                      lambda: _macd_hist_pair_impl(data, fast, slow, signal))

def _macd_hist_pair_impl(data: List[float], fast: int, slow: int, signal: int) -> Tuple[float, float]:
    ef = _ema_series_impl(data, fast)
    es = _ema_series_impl(data, slow)
    macd_line = [f - s for f, s in zip(ef, es)]
    sig_line = _ema_series_impl(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line, sig_line)]
    if len(hist) < 2:
        return 0.0, 0.0
    return float(hist[-1]), float(hist[-2])


def macd_hist_series(data: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> List[float]:
    """Full MACD histogram series aligned to input length.
    Port of autoloop_strategy._macd_hist_series()."""
    _count("macd_hist_series")
    if not data:
        return []
    return _cache_get(_cache_key("macd_hist_series", data, fast, slow, signal),
                      lambda: _macd_hist_series_impl(data, fast, slow, signal))

def _macd_hist_series_impl(data: List[float], fast: int, slow: int, signal: int) -> List[float]:
    ef = _ema_series_impl(data, int(fast))
    es = _ema_series_impl(data, int(slow))
    macd_line = [f - s for f, s in zip(ef, es)]
    sig_line = _ema_series_impl(macd_line, int(signal))
    return [m - s for m, s in zip(macd_line, sig_line)]


# ============================================================
# Ichimoku Cloud
# For the WHALE strategy: determine above/below the cloud
# ============================================================
def ichimoku_cloud(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    tenkan: int = 9,
    kijun: int = 26,
    senkou_b_period: int = 52,
) -> Optional[Dict]:
    """Ichimoku cloud calculation.

    The cloud visible at the current bar (senkou_a/b are values computed kijun periods ago):
    - cloud_top   : max(senkou_a, senkou_b)
    - cloud_bottom: min(senkou_a, senkou_b)
    - above_cloud : current price > cloud_top
    - below_cloud : current price < cloud_bottom
    """
    _count("ichimoku_cloud")
    min_len = kijun + senkou_b_period  # at least 78 candles required
    if (len(closes) < min_len or len(highs) < min_len or len(lows) < min_len):
        return None

    def _mid(h: List[float], l: List[float]) -> float:
        return (max(h) + min(l)) / 2.0

    # Current tenkan/kijun lines
    tenkan_val = _mid(highs[-tenkan:], lows[-tenkan:])
    kijun_val  = _mid(highs[-kijun:],  lows[-kijun:])

    # The cloud shown at the current bar = senkou computed kijun periods ago
    # senkou_a[-kijun] = (tenkan[-kijun] + kijun[-kijun]) / 2
    t_hi = highs[-tenkan - kijun: -kijun]
    t_lo = lows [-tenkan - kijun: -kijun]
    k_hi = highs[-kijun  - kijun: -kijun]
    k_lo = lows [-kijun  - kijun: -kijun]
    if not t_hi or not k_hi:
        return None
    senkou_a = (_mid(t_hi, t_lo) + _mid(k_hi, k_lo)) / 2.0

    # senkou_b[-kijun] = midpoint over senkou_b_period ending at -kijun
    sb_hi = highs[-senkou_b_period - kijun: -kijun]
    sb_lo = lows [-senkou_b_period - kijun: -kijun]
    if not sb_hi:
        return None
    senkou_b = _mid(sb_hi, sb_lo)

    cloud_top    = max(senkou_a, senkou_b)
    cloud_bottom = min(senkou_a, senkou_b)
    price        = closes[-1]

    return {
        "tenkan":       tenkan_val,
        "kijun":        kijun_val,
        "senkou_a":     senkou_a,
        "senkou_b":     senkou_b,
        "cloud_top":    cloud_top,
        "cloud_bottom": cloud_bottom,
        "above_cloud":  price > cloud_top,
        "below_cloud":  price < cloud_bottom,
        "in_cloud":     cloud_bottom <= price <= cloud_top,
    }


# ============================================================
# Stochastic RSI
# For the WHALE strategy: %K (red) crossing above %D (blue) = entry signal
# ============================================================
def stochastic_rsi(
    prices: List[float],
    rsi_period: int = 14,
    stoch_period: int = 14,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> Optional[Dict]:
    """Stochastic RSI calculation.

    Returns:
        k        : current %K value (red line, 0~100)
        d        : current %D value (blue line, 0~100)
        crossover: True = %K just crossed above %D (buy signal)
        crossunder: True = %K just crossed below %D (sell signal)
    """
    _count("stochastic_rsi")
    min_len = rsi_period + stoch_period + k_smooth + d_smooth + 5
    if len(prices) < min_len:
        return None

    # Step 1: Compute RSI series — O(N) single-pass (was O(N²) loop)
    rsi_vals = rsi_series(prices, rsi_period)

    if len(rsi_vals) < stoch_period + k_smooth + d_smooth:
        return None

    # Step 2: Apply stochastic to RSI -> raw %K
    raw_k: List[float] = []
    for i in range(stoch_period, len(rsi_vals) + 1):
        window   = rsi_vals[i - stoch_period: i]
        min_rsi  = min(window)
        max_rsi  = max(window)
        if max_rsi == min_rsi:
            raw_k.append(50.0)
        else:
            raw_k.append((window[-1] - min_rsi) / (max_rsi - min_rsi) * 100.0)

    if len(raw_k) < k_smooth + d_smooth:
        return None

    # Step 3: %K smoothing (SMA)
    k_series: List[float] = []
    for i in range(k_smooth, len(raw_k) + 1):
        k_series.append(sum(raw_k[i - k_smooth: i]) / k_smooth)

    if len(k_series) < d_smooth + 1:
        return None

    # Step 4: %D = SMA of %K
    d_series: List[float] = []
    for i in range(d_smooth, len(k_series) + 1):
        d_series.append(sum(k_series[i - d_smooth: i]) / d_smooth)

    if len(d_series) < 2:
        return None

    k      = k_series[-1]
    d      = d_series[-1]
    prev_k = k_series[-2]
    prev_d = d_series[-2]

    return {
        "k":          k,
        "d":          d,
        "crossover":  prev_k <= prev_d and k > d,   # red above blue (buy)
        "crossunder": prev_k >= prev_d and k < d,   # red below blue (sell)
    }


# ============================================================
# ADX (Average Directional Index) — Wilder's smoothing
# ============================================================
def adx(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> Optional[Dict]:
    """Wilder's ADX.

    Parameters
    ----------
    highs, lows, closes : List[float]
        H4 (or any desired TF) OHLC series. Must be of equal length.
    period : int
        Default 14.

    Returns
    -------
    dict | None
        {"adx": float, "plus_di": float, "minus_di": float, "adx_rising": bool}
        None if insufficient data.
    """
    _count("adx")
    n = len(closes)
    min_len = 2 * period + 1          # 29 bars for period=14
    if n < min_len or len(highs) != n or len(lows) != n:
        return None
    return _cache_get(
        _cache_key("adx", closes, period, highs[-1], lows[-1]),
        lambda: _adx_impl(highs, lows, closes, period),
    )


def _adx_impl(highs: List[float], lows: List[float], closes: List[float],
              period: int) -> Optional[Dict]:
    """ADX internal implementation — Wilder smoothing."""
    series = _adx_series_impl(highs, lows, closes, period)
    if len(series) < 2:
        return None
    cur = series[-1]
    prev = series[-2]
    cur["adx_rising"] = cur["adx"] > prev["adx"]
    return cur


def adx_series(highs: List[float], lows: List[float], closes: List[float],
               period: int = 14) -> List[Dict]:
    """Full ADX series.

    Returns
    -------
    List[Dict]
        Each item: {"adx": float, "plus_di": float, "minus_di": float}
        The first valid value is at index 0 and starts ``2*period`` later than the source.
    """
    _count("adx_series")
    n = len(closes)
    if n < 2 * period + 1 or len(highs) != n or len(lows) != n:
        return []
    return _cache_get(
        _cache_key("adx_series", closes, period, highs[-1], lows[-1]),
        lambda: _adx_series_impl(highs, lows, closes, period),
    )


def _adx_series_impl(highs: List[float], lows: List[float],
                      closes: List[float], period: int) -> List[Dict]:
    """Compute the full Wilder's ADX series.

    Algorithm:
    1) Compute True Range, +DM, -DM
    2) Wilder smoothing (period) to get ATR14, +DM14, -DM14
    3) +DI = +DM14/ATR14 * 100,  -DI = -DM14/ATR14 * 100
    4) DX  = |+DI - -DI| / (+DI + -DI) * 100
    5) ADX = Wilder smooth of DX (period)
    """
    n = len(closes)

    # --- Step 1: TR, +DM, -DM (index 1 ~ n-1) ----------------------
    tr_list: List[float] = []
    plus_dm_list: List[float] = []
    minus_dm_list: List[float] = []
    for i in range(1, n):
        h = float(highs[i])
        l = float(lows[i])
        pc = float(closes[i - 1])

        tr = max(h - l, abs(h - pc), abs(l - pc))
        up_move = h - float(highs[i - 1])
        down_move = float(lows[i - 1]) - l

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    # --- Step 2: simple sum over the first period -> Wilder smoothing ---
    if len(tr_list) < 2 * period:
        return []

    atr14 = sum(tr_list[:period])
    pdm14 = sum(plus_dm_list[:period])
    mdm14 = sum(minus_dm_list[:period])

    # Wilder smooth: next = prev - prev/period + current
    smoothed_atr: List[float] = [atr14]
    smoothed_pdm: List[float] = [pdm14]
    smoothed_mdm: List[float] = [mdm14]

    for i in range(period, len(tr_list)):
        atr14 = atr14 - atr14 / period + tr_list[i]
        pdm14 = pdm14 - pdm14 / period + plus_dm_list[i]
        mdm14 = mdm14 - mdm14 / period + minus_dm_list[i]
        smoothed_atr.append(atr14)
        smoothed_pdm.append(pdm14)
        smoothed_mdm.append(mdm14)

    # --- Step 3-4: DI, DX -------------------------------------------
    dx_list: List[float] = []
    di_records: List[Dict] = []
    for j in range(len(smoothed_atr)):
        a = smoothed_atr[j]
        if a == 0:
            dx_list.append(0.0)
            di_records.append({"plus_di": 0.0, "minus_di": 0.0})
            continue
        pdi = (smoothed_pdm[j] / a) * 100.0
        mdi = (smoothed_mdm[j] / a) * 100.0
        di_sum = pdi + mdi
        dx = (abs(pdi - mdi) / di_sum * 100.0) if di_sum > 0 else 0.0
        dx_list.append(dx)
        di_records.append({"plus_di": round(pdi, 4), "minus_di": round(mdi, 4)})

    # --- Step 5: ADX = Wilder smooth of DX (period) -----------------
    if len(dx_list) < period:
        return []

    adx_val = sum(dx_list[:period]) / period
    results: List[Dict] = []

    # First ADX (uses DX up to the period-th value)
    rec = di_records[period - 1].copy()
    rec["adx"] = round(adx_val, 4)
    results.append(rec)

    for k in range(period, len(dx_list)):
        adx_val = (adx_val * (period - 1) + dx_list[k]) / period
        rec = di_records[k].copy()
        rec["adx"] = round(adx_val, 4)
        results.append(rec)

    return results
