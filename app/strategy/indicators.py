# ============================================================
# File: app/strategy/indicators.py
# ------------------------------------------------------------
# 기술적 지표 계산 함수 모음.
# StrategyBrain 이 시장 분석을 할 때 호출하는 모듈.
# ============================================================

from __future__ import annotations
from typing import List, Dict, Tuple, Optional

# ============================================================
# [PERF-TELEMETRY] 인디케이터 호출 횟수 카운터 (2026-03-21)
# ============================================================
_call_counts: Dict[str, int] = {}

def _count(name: str) -> None:
    """인디케이터 함수 진입 시 호출. 틱 시작 시 reset_call_counts()로 리셋."""
    _call_counts[name] = _call_counts.get(name, 0) + 1

def reset_call_counts() -> None:
    """틱 사이클 시작 전에 호출하여 카운터 리셋."""
    _call_counts.clear()

def get_call_counts() -> Dict[str, int]:
    """현재 틱의 인디케이터 호출 횟수 반환 (복사본)."""
    return dict(_call_counts)


# ============================================================
# [PERF] Per-Tick 캐시 연동 (2026-03-21)
# ============================================================
def _cache_key(name: str, data, *params) -> tuple:
    """Content-based 캐시 키. 3-point sampling(first/mid/last) + length로 충돌 저항."""
    n = len(data) if data else 0
    return (name, n, data[-1] if n else 0, data[0] if n else 0,
            data[n // 2] if n > 2 else 0, *params)

try:
    from app.strategy.indicator_cache import get_or_compute as _cache_get
except ImportError:
    import logging as _logging
    _logging.getLogger(__name__).warning("indicator_cache module not available, caching disabled", exc_info=True)
    # 캐시 모듈 없으면 바이패스
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

    # Fast EMA 초기화 (SMA of first `fast` points) → slow-1 시점까지 갱신
    ema_fast = sum(data[:fast]) / fast
    for p in data[fast:slow]:
        ema_fast = p * k_fast + ema_fast * (1.0 - k_fast)

    # Slow EMA 초기화 (SMA of first `slow` points)
    ema_slow = sum(data[:slow]) / slow

    # data[slow:] 구간에서 MACD line 이력 수집
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
    # EMA는 과거 데이터 의존도가 높으므로 가능한 많이 사용하거나
    # 여기서는 간단히 최근 데이터로 계산 (실제로는 전체 히스토리 권장)
    arr = data[-length*2:] if len(data) > length*2 else data
    k = 2 / (length + 1)
    ema_val = arr[0]
    for p in arr[1:]:
        ema_val = p * k + ema_val * (1 - k)
    return ema_val


# ============================================================
# 변동성(표준편차 기반)
# ============================================================
def volatility(data: List[float], length: int = 14) -> float | None:
    """수익률의 표준편차(%)를 계산한다. (AI 학습 데이터와 일치)"""
    _count("volatility")
    if not data or len(data) < length + 1:
        return None
    return _cache_get(_cache_key("volatility", data, length), lambda: _volatility_impl(data, length))

def _volatility_impl(data: List[float], length: int) -> float | None:
    # Calculate returns: (p_t - p_{t-1}) / p_{t-1}
    # length개의 수익률을 얻기 위해 length+1개의 가격 데이터 사용
    window = data[-(length + 1):]
    rets = [(window[i] - window[i-1]) / window[i-1] for i in range(1, len(window)) if window[i-1] > 0]

    if len(rets) < 2:
        return 0.0

    # Sample Standard Deviation (N-1)
    mean_ret = sum(rets) / len(rets)
    var_ret = sum((r - mean_ret) ** 2 for r in rets) / (len(rets) - 1)

    return (var_ret ** 0.5) * 100


# ============================================================
# 추세(% 변화)
# ============================================================
def trend(data: List[float], length: int = 14) -> float | None:
    _count("trend")
    if not data or len(data) < length:
        return None
    return _cache_get(_cache_key("trend", data, length),
                      lambda: (data[-1] - data[-length]) / data[-length] * 100)


# ============================================================
# 모멘텀
# ============================================================
def momentum(data: List[float], length: int = 14) -> float | None:
    _count("momentum")
    if not data or len(data) < length:
        return None
    return _cache_get(_cache_key("momentum", data, length),
                      lambda: data[-1] - data[-length])


# ============================================================
# 볼린저 밴드
# ============================================================
def bollinger_bands(data: List[float], length: int = 20, k: float = 2.0) -> Dict[str, float] | None:
    _count("bollinger_bands")
    if not data or len(data) < length:
        return None
    return _cache_get(_cache_key("bollinger_bands", data, length, k), lambda: _bollinger_bands_impl(data, length, k))

def _bollinger_bands_impl(data: List[float], length: int, k: float) -> Dict[str, float] | None:
    arr = data[-length:]
    mid = sum(arr) / length

    # 표준편차 (Population StdDev)
    var = sum((p - mid) ** 2 for p in arr) / length
    std = var ** 0.5

    upper = mid + std * k
    lower = mid - std * k

    # 밴드폭 (Bandwidth) = (Upper - Lower) / Mid
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
    """고가/저가 정보 없이 종가(Close)만으로 계산하는 근사 ATR."""
    _count("atr_simplified")
    if not data or len(data) < length + 1:
        return None
    return _cache_get(_cache_key("atr_simplified", data, length), lambda: _atr_simplified_impl(data, length))

def _atr_simplified_impl(data: List[float], length: int) -> float | None:
    # True Range 근사치: abs(current - prev)
    tr_list = [abs(data[i] - data[i-1]) for i in range(1, len(data))]

    # Wilder's Smoothing (RMA)
    if len(tr_list) < length:
        return None

    # 첫 값은 SMA
    atr_val = sum(tr_list[:length]) / length

    # 이후는 Smoothing
    for tr in tr_list[length:]:
        atr_val = (atr_val * (length - 1) + tr) / length

    return atr_val


# ============================================================
# 볼린저 밴드 스퀴즈 (Squeeze)
# ============================================================
def bollinger_squeeze(data: List[float], length: int = 20, k: float = 2.0, lookback: int = 20) -> Tuple[float, bool] | None:
    """
    현재 밴드폭과 스퀴즈(Squeeze) 발생 여부를 반환한다.
    Squeeze: 현재 밴드폭이 최근 lookback 기간 중 최저점일 때 (변동성 극소)
    """
    _count("bollinger_squeeze")
    if not data or len(data) < length + lookback:
        return None

    # 현재 밴드폭 계산 (캐시 활용)
    bb_now = bollinger_bands(data, length, k)
    if not bb_now:
        return None
    bw_now = bb_now["bandwidth"]

    # [PERF] 과거 lookback bandwidth를 직접 계산 (dict 할당 21회 → 0회)
    # bollinger_bands() 전체 호출 대신 bandwidth만 계산
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
# [PERF] Series 함수 (autoloop 통합용, 2026-03-21 Phase 2)
# autoloop_strategy.py의 private 구현을 정확히 포팅.
# 기존 rsi()/ema()/macd()와 seeding 방식이 다르므로 별도 유지.
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
# Ichimoku Cloud (일목균형표)
# WHALE 전략용: 구름대 위/아래 판단
# ============================================================
def ichimoku_cloud(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    tenkan: int = 9,
    kijun: int = 26,
    senkou_b_period: int = 52,
) -> Optional[Dict]:
    """일목균형표 구름대 계산.

    현재 봉에서 보이는 구름 (senkou_a/b는 kijun 기간 전에 계산된 값):
    - cloud_top   : max(senkou_a, senkou_b)
    - cloud_bottom: min(senkou_a, senkou_b)
    - above_cloud : 현재가 > cloud_top
    - below_cloud : 현재가 < cloud_bottom
    """
    _count("ichimoku_cloud")
    min_len = kijun + senkou_b_period  # 최소 78캔들 필요
    if (len(closes) < min_len or len(highs) < min_len or len(lows) < min_len):
        return None

    def _mid(h: List[float], l: List[float]) -> float:
        return (max(h) + min(l)) / 2.0

    # 현재 전환선/기준선
    tenkan_val = _mid(highs[-tenkan:], lows[-tenkan:])
    kijun_val  = _mid(highs[-kijun:],  lows[-kijun:])

    # 현재 봉에 표시되는 구름 = kijun 기간 전에 계산된 senkou
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
# WHALE 전략용: %K(빨강)가 %D(파랑) 위로 교차 = 진입 신호
# ============================================================
def stochastic_rsi(
    prices: List[float],
    rsi_period: int = 14,
    stoch_period: int = 14,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> Optional[Dict]:
    """Stochastic RSI 계산.

    Returns:
        k        : %K 현재값 (빨강선, 0~100)
        d        : %D 현재값 (파랑선, 0~100)
        crossover: True = %K가 %D를 방금 위로 교차 (매수 신호)
        crossunder: True = %K가 %D를 방금 아래로 교차 (매도 신호)
    """
    _count("stochastic_rsi")
    min_len = rsi_period + stoch_period + k_smooth + d_smooth + 5
    if len(prices) < min_len:
        return None

    # Step 1: RSI 시리즈 계산 — O(N) single-pass (was O(N²) loop)
    rsi_vals = rsi_series(prices, rsi_period)

    if len(rsi_vals) < stoch_period + k_smooth + d_smooth:
        return None

    # Step 2: RSI에 스토캐스틱 적용 → raw %K
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

    # Step 3: %K 스무딩 (SMA)
    k_series: List[float] = []
    for i in range(k_smooth, len(raw_k) + 1):
        k_series.append(sum(raw_k[i - k_smooth: i]) / k_smooth)

    if len(k_series) < d_smooth + 1:
        return None

    # Step 4: %D = %K의 SMA
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
        "crossover":  prev_k <= prev_d and k > d,   # 빨강이 파랑 위로 (매수)
        "crossunder": prev_k >= prev_d and k < d,   # 빨강이 파랑 아래로 (매도)
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
        H4 (또는 원하는 TF) OHLC 시리즈. 길이 동일해야 함.
    period : int
        기본 14.

    Returns
    -------
    dict | None
        {"adx": float, "plus_di": float, "minus_di": float, "adx_rising": bool}
        데이터 부족 시 None.
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
    """ADX 내부 구현 — Wilder smoothing."""
    series = _adx_series_impl(highs, lows, closes, period)
    if len(series) < 2:
        return None
    cur = series[-1]
    prev = series[-2]
    cur["adx_rising"] = cur["adx"] > prev["adx"]
    return cur


def adx_series(highs: List[float], lows: List[float], closes: List[float],
               period: int = 14) -> List[Dict]:
    """ADX 전체 시리즈.

    Returns
    -------
    List[Dict]
        각 항목: {"adx": float, "plus_di": float, "minus_di": float}
        첫 유효값은 index 0이며, 원본 대비 ``2*period`` 만큼 뒤에서 시작.
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
    """Wilder's ADX 시리즈 전체 계산.

    알고리즘:
    1) True Range, +DM, -DM 계산
    2) Wilder smoothing (period) 으로 ATR14, +DM14, -DM14
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

    # --- Step 2: 첫 period 구간 단순합 → Wilder smoothing -----------
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

    # 첫 ADX (period 번째 DX까지 사용)
    rec = di_records[period - 1].copy()
    rec["adx"] = round(adx_val, 4)
    results.append(rec)

    for k in range(period, len(dx_list)):
        adx_val = (adx_val * (period - 1) + dx_list[k]) / period
        rec = di_records[k].copy()
        rec["adx"] = round(adx_val, 4)
        results.append(rec)

    return results
