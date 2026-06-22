"""Autoloop strategy.

Design goals
------------
1) Deterministic, low-maintenance decision logic.
2) "Starts working" quickly even after process restart.
3) Minimal coupling: the strategy only needs (context, price, params).

Operational notes
-----------------
* The bot receives *ticks* (often 1s). This strategy operates on candle-like
  *bars* created from ticks (default: 3-minute closes).
* If you keep only ~2,000 ticks in memory, you cannot build 40x 3-minute bars
  (2 hours) and you'll never trade. To avoid that, this module can bootstrap
  bar history using Bybit's **public** candle endpoints (no keys required).

Signal meaning
--------------
* "buy"  : enter a position (mean-reversion entry)
* "sell" : take profit / exit hint (system TP/SL can still override)
* "hold" : do nothing

IMPORTANT
---------
No rule-set can guarantee profit. This strategy is a starting point designed
to be safe-ish and observable, and to reduce obvious failure modes like
entering during a falling knife.
"""

from __future__ import annotations
import logging
import math
import os
import threading
import time

logger = logging.getLogger(__name__)
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

# 마켓별 백그라운드 HTTP 작업 중복 방지
_bootstrap_inflight: set = set()
_autotune_inflight: set = set()
_inflight_lock = threading.Lock()
# 동시 HTTP 작업 수 제한 — GIL 경합 및 Bybit 429 방지 (1개씩 순차 처리)
_http_semaphore = threading.Semaphore(1)


from app.core.constants import BYBIT_MARKET_KLINE, bybit_v5_rest_category, parse_bybit_list
from app.core.currency import Q
from app.core.time_volatility import get_time_volatility_multiplier
from app.strategy import indicators


# =========================
# Internal state (per market)
# =========================


@dataclass
class _AutoloopState:
    bar_sec: int
    bars: Deque[float]
    cur_bar_start_ts: float = 0.0
    cur_close: float = 0.0
    bootstrapped: bool = False
    last_bootstrap_ts: float = 0.0
    last_signal: str = "hold"
    last_signal_ts: float = 0.0
    # Telemetry snapshot throttling
    last_snapshot_ts: float = 0.0
    tuned: bool = False
    tuned_ts: float = 0.0
    applied_for_tuned_ts: float = 0.0
    tuned_applied_ts: float = 0.0
    tuned_params: Dict[str, float] = field(default_factory=dict)
    tuned_summary: Dict[str, Any] = field(default_factory=dict)
    tune_emit_once: bool = False


_STATE: Dict[str, _AutoloopState] = {}


def _floor_ts(ts: float, bar_sec: int) -> float:
    if bar_sec <= 0:
        return ts
    return ts - (ts % bar_sec)


def _get_market(ctx: Any) -> str:
    return str(getattr(ctx, "market", "UNKNOWN"))


def _ensure_state(market: str, bar_sec: int, max_bars: int) -> _AutoloopState:
    st = _STATE.get(market)
    if st is None or st.bar_sec != bar_sec or (st.bars.maxlen or 0) != max_bars:
        st = _AutoloopState(bar_sec=bar_sec, bars=deque(maxlen=max_bars))
        _STATE[market] = st
    return st


def _update_bars(st: _AutoloopState, price: float, ts: float) -> None:
    """Update rolling close bars from tick price."""
    if price <= 0:
        return

    if st.cur_bar_start_ts <= 0.0:
        st.cur_bar_start_ts = _floor_ts(ts, st.bar_sec)
        st.cur_close = price
        return

    # Clock skew / reset
    if ts < st.cur_bar_start_ts:
        st.cur_bar_start_ts = _floor_ts(ts, st.bar_sec)
        st.cur_close = price
        return

    elapsed = ts - st.cur_bar_start_ts
    if elapsed >= st.bar_sec:
        n = int(elapsed // st.bar_sec)
        if st.cur_close > 0:
            st.bars.append(st.cur_close)
        st.cur_bar_start_ts += float(st.bar_sec) * n
        st.cur_close = price
    else:
        st.cur_close = price


# =========================
# Indicators
# =========================


def _ema_series(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    if period <= 1:
        return list(values)
    alpha = 2.0 / (period + 1.0)
    out: List[float] = []
    ema = float(values[0])
    for v in values:
        ema = alpha * float(v) + (1.0 - alpha) * ema
        out.append(ema)
    return out


def _macd_hist(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float]:
    """Return (hist_now, hist_prev). If insufficient data -> (0, 0)."""
    if len(values) < max(fast, slow) + signal:
        return 0.0, 0.0
    ema_fast = _ema_series(values, fast)
    ema_slow = _ema_series(values, slow)
    macd = [f - s for f, s in zip(ema_fast, ema_slow)]
    sig = _ema_series(macd, signal)
    hist = [m - s for m, s in zip(macd, sig)]
    if len(hist) < 2:
        return 0.0, 0.0
    return float(hist[-1]), float(hist[-2])


def _rsi_wilder(values: List[float], period: int = 14) -> Tuple[float, float]:
    """Return (rsi_now, rsi_prev). If insufficient data -> (50, 50)."""
    if period <= 1 or len(values) < period + 2:
        return 50.0, 50.0

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = float(values[i]) - float(values[i - 1])
        if d >= 0:
            gains += d
        else:
            losses += -d

    avg_gain = gains / period
    avg_loss = losses / period

    def _calc_rsi(ag: float, al: float) -> float:
        if al <= 1e-12:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    rsi_prev = _calc_rsi(avg_gain, avg_loss)
    rsi_now = rsi_prev

    # Continue smoothing to the end.
    for i in range(period + 1, len(values)):
        d = float(values[i]) - float(values[i - 1])
        gain = d if d > 0 else 0.0
        loss = (-d) if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi_prev, rsi_now = rsi_now, _calc_rsi(avg_gain, avg_loss)

    return float(rsi_now), float(rsi_prev)

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _quantile(xs: List[float], q: float) -> float:
    """Linear-interpolated quantile (q in [0,1])."""
    if not xs:
        return 0.0
    ys = sorted(float(v) for v in xs)
    if len(ys) == 1:
        return ys[0]
    q = _clamp(float(q), 0.0, 1.0)
    pos = q * (len(ys) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ys[lo]
    return ys[lo] + (ys[hi] - ys[lo]) * (pos - lo)


def _rsi_series_wilder(values: List[float], period: int) -> List[float]:
    """RSI series using Wilder smoothing.

    Returns a list of RSI values (length ~= len(values)-period).
    """
    period = int(period)
    if period <= 1 or len(values) <= period:
        return []

    # Deltas
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(values)):
        d = float(values[i]) - float(values[i - 1])
        gains.append(d if d > 0 else 0.0)
        losses.append((-d) if d < 0 else 0.0)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def rsi_from(ag: float, al: float) -> float:
        if al == 0.0 and ag == 0.0:
            return 50.0
        if al == 0.0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out: List[float] = [rsi_from(avg_gain, avg_loss)]

    # Continue smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(rsi_from(avg_gain, avg_loss))

    return out


def _macd_hist_series(values: List[float], fast: int, slow: int, signal: int) -> List[float]:
    """MACD histogram series aligned to input length."""
    if not values:
        return []
    fast = int(fast)
    slow = int(slow)
    signal = int(signal)
    ema_fast = _ema_series(values, fast)
    ema_slow = _ema_series(values, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    sig_line = _ema_series(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line, sig_line)]
    return hist


def _sma(values: List[float], n: int) -> float:
    if not values:
        return 0.0
    n = max(1, min(int(n), len(values)))
    return float(sum(values[-n:])) / float(n)


def _std(values: List[float], n: int) -> float:
    if not values:
        return 0.0
    n = max(2, min(int(n), len(values)))
    m = _sma(values, n)
    var = sum((float(v) - m) ** 2 for v in values[-n:]) / float(n)
    return float(math.sqrt(max(0.0, var)))


def _returns(values: List[float], n: int) -> List[float]:
    if len(values) < 2:
        return []
    n = max(2, min(int(n), len(values)))
    window = values[-n:]
    out: List[float] = []
    for i in range(1, len(window)):
        prev = float(window[i - 1])
        cur = float(window[i])
        if prev <= 0:
            continue
        out.append((cur / prev) - 1.0)
    return out


def _infer_regime(values: List[float], fast: int, slow: int, bull_pct: float, bear_pct: float) -> Tuple[str, float]:
    """Simple regime inference based on EMA spread. Returns (regime, spread_pct)."""
    if len(values) < max(fast, slow) + 5:
        return "NEUTRAL", 0.0
    ema_f = indicators.ema_series(values, fast)[-1]
    ema_s = indicators.ema_series(values, slow)[-1]
    if ema_s <= 0:
        return "NEUTRAL", 0.0
    spread_pct = (ema_f / ema_s - 1.0) * 100.0
    if spread_pct >= float(bull_pct):
        return "BULL", float(spread_pct)
    if spread_pct <= -float(bear_pct):
        return "BEAR", float(spread_pct)
    return "NEUTRAL", float(spread_pct)


# =========================
# Bootstrap (optional)
# =========================


def _fetch_candle_closes(market: str, unit_min: int, count: int, timeout: float) -> Optional[List[float]]:
    """Fetch Bybit public minute candles and return closes oldest->newest."""
    if unit_min <= 0:
        return None

    try:
        from app.core.rate_limiter import bybit_get
    except (ImportError, AttributeError, TypeError):
        logger.info("[AUTOLOOP] bybit_get not available for candle fetch")
        return None

    try:
        resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": str(unit_min), "limit": int(count)}, timeout=float(timeout))
    except Exception as exc:
        logger.warning("[AUTOLOOP] _fetch_candle_closes HTTP failed for %s: %s", market, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        raw = parse_bybit_list(resp.json())
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
        logger.warning("[AUTOLOOP] parse_bybit_list failed for %s", market, exc_info=True)
        return None

    closes: List[float] = []
    for k in reversed(raw or []):
        try:
            if isinstance(k, (list, tuple)) and len(k) >= 5:
                closes.append(float(k[4]))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning("[AUTOLOOP] _fetch_candle_closes except-> continue: %s", exc, exc_info=True)
            continue
    closes = [c for c in closes if c > 0]
    return closes or None


def _maybe_bootstrap(st: _AutoloopState, market: str, *, bar_sec: int, bootstrap: bool, bootstrap_count: int, bootstrap_min_bars: int, timeout: float) -> Dict[str, Any]:
    if not bootstrap:
        return {"boot": "disabled"}
    if st.bootstrapped:
        return {"boot": "already"}

    now = time.time()
    if st.last_bootstrap_ts > 0 and (now - st.last_bootstrap_ts) < 60.0:
        return {"boot": "cooldown"}

    unit_min = int(round(bar_sec / 60.0))
    if unit_min <= 0:
        return {"boot": "invalid_unit", "unit_min": unit_min}
    if unit_min not in {1, 3, 5, 10, 15, 30, 60, 240}:
        return {"boot": "unsupported_unit", "unit_min": unit_min}

    # [PERF] 백그라운드 스레드에서 HTTP 호출 — tick 이벤트 루프 블로킹 방지
    with _inflight_lock:
        if market in _bootstrap_inflight:
            return {"boot": "pending"}
        _bootstrap_inflight.add(market)

    st.last_bootstrap_ts = now  # 쿨다운 시작

    def _bg(_st=st, _m=market, _u=unit_min, _cnt=bootstrap_count, _min=bootstrap_min_bars, _to=timeout):
        with _http_semaphore:  # 동시 HTTP 2개 제한
            try:
                closes = _fetch_candle_closes(_m, unit_min=_u, count=int(_cnt), timeout=float(_to))
                if closes:
                    for c in closes[-(_st.bars.maxlen or len(closes)):]:
                        _st.bars.append(float(c))
                    _st.bootstrapped = len(_st.bars) >= int(_min)
            except (TypeError, ValueError) as exc:
                logger.warning("[AUTOLOOP] _bg fallback: %s", exc, exc_info=True)
            finally:
                with _inflight_lock:
                    _bootstrap_inflight.discard(_m)

    threading.Thread(target=_bg, daemon=True).start()
    return {"boot": "pending"}



# =========================
# Auto-tuning (multi-timeframe)
# =========================

def _autotune_from_timeframes(
    market: str,
    *,
    rsi_len: int,
    macd_fast: int,
    macd_slow: int,
    macd_signal: int,
    tune_units_min: List[int],
    tune_count: int,
    timeout_sec: float,
) -> Dict[str, Any]:
    """Fetch multi-timeframe candles and summarize RSI/MACD ranges.

    Intended to run once at (or shortly after) market registration.
    """
    per: List[Dict[str, Any]] = []

    for unit in tune_units_min:
        try:
            unit_i = int(unit)
        except (TypeError, ValueError) as exc:
            logger.warning("[AUTOLOOP] _autotune_from_timeframes except-> continue: %s", exc, exc_info=True)
            continue

        closes = _fetch_candle_closes(
            market, unit_min=unit_i, count=int(tune_count), timeout=float(timeout_sec)
        )

        if not closes:
            continue
        if len(closes) < max(int(rsi_len) + 2, int(macd_slow) + int(macd_signal) + 5):
            continue

        rsi_vals = indicators.rsi_series(closes, int(rsi_len))
        hist = indicators.macd_hist_series(closes, int(macd_fast), int(macd_slow), int(macd_signal))

        # Normalize MACD histogram to percent (scale-invariant across BTC/ETH/etc)
        warm = max(int(macd_slow), int(macd_signal)) * 2
        hist_pct: List[float] = []
        for i in range(min(len(hist), len(closes))):
            if i < warm:
                continue
            p = float(closes[i])
            if p > 0:
                hist_pct.append((float(hist[i]) / p) * 100.0)

        if not rsi_vals:
            continue

        per.append(
            {
                "unit_min": unit_i,
                "n": len(closes),
                "rsi_min": float(min(rsi_vals)),
                "rsi_max": float(max(rsi_vals)),
                "rsi_p10": float(_quantile(rsi_vals, 0.10)),
                "rsi_p90": float(_quantile(rsi_vals, 0.90)),
                "macd_pct_p10": float(_quantile(hist_pct, 0.10)) if hist_pct else 0.0,
                "macd_pct_p90": float(_quantile(hist_pct, 0.90)) if hist_pct else 0.0,
            }
        )

    return {"ok": bool(per), "per": per}


def _maybe_autotune(
    st: _AutoloopState,
    *,
    market: str,
    base_rsi_buy: float,
    base_rsi_sell: float,
    rsi_len: int,
    macd_fast: int,
    macd_slow: int,
    macd_signal: int,
    tune_on_boot: bool,
    tune_units_min: List[int],
    tune_count: int,
    tune_timeout_sec: float,
    tune_alpha: float,
    tune_strength: float,
    tune_min_gap: float,
    attempt_cooldown_sec: float,
    context: Any = None,
) -> None:
    """One-shot per-market auto-tuning.

    Updates st.tuned_params / st.tuned_summary in-place.
    context가 있으면 튠 결과를 ctx.set_var()로 저장하여 재기동 시 즉시 복원.
    """
    if not bool(tune_on_boot):
        return

    now = time.time()

    # Already tuned in this process.
    if st.tuned:
        return

    # 재기동 복원: ctx에 저장된 튠 결과가 있으면 즉시 적용 (API 호출 불필요)
    if context is not None and not st.tuned:
        try:
            saved = context.get_var("al_tuned_params")
            saved_ts = float(context.get_var("al_tuned_ts") or 0.0)
            if saved and isinstance(saved, dict) and saved_ts > 0:
                max_age_sec = float(os.getenv("AUTOLOOP_TUNE_PERSIST_MAX_AGE_SEC", "86400"))
                if (now - saved_ts) < max_age_sec:
                    st.tuned = True
                    st.tuned_applied_ts = saved_ts
                    st.tuned_params = dict(saved)
                    st.tuned_summary = {
                        "enabled": True,
                        "applied": True,
                        "restored_from_ctx": True,
                        "saved_ts": saved_ts,
                        "age_sec": round(now - saved_ts, 1),
                    }
                    st.tune_emit_once = True
                    return
        except (TypeError, ValueError) as exc:
            logger.warning("[AUTOLOOP] 재기동 복원: ctx에 저장된 튠 결과가 있으면 즉시 적용 (API 호출 불필요): %s", exc, exc_info=True)

    # Avoid spamming public endpoints on repeated ticks.
    if st.tuned_ts and (now - float(st.tuned_ts)) < float(attempt_cooldown_sec):
        return

    # [PERF] 백그라운드 스레드에서 HTTP 호출 — tick 이벤트 루프 블로킹 방지
    with _inflight_lock:
        if market in _autotune_inflight:
            return
        _autotune_inflight.add(market)

    st.tuned_ts = now  # record attempt time (cooldown 시작)

    def _bg_tune(
        _st=st, _m=market, _now=now,
        _rsi_len=rsi_len, _macd_fast=macd_fast, _macd_slow=macd_slow, _macd_signal=macd_signal,
        _units=list(tune_units_min), _cnt=tune_count, _to=tune_timeout_sec,
        _alpha=tune_alpha, _strength=tune_strength, _min_gap=tune_min_gap,
        _base_buy=base_rsi_buy, _base_sell=base_rsi_sell, _context=context,
    ):
        with _http_semaphore:  # 동시 HTTP 2개 제한 — GIL 경합 방지
            try:
                res = _autotune_from_timeframes(
                    _m,
                    rsi_len=int(_rsi_len),
                    macd_fast=int(_macd_fast),
                    macd_slow=int(_macd_slow),
                    macd_signal=int(_macd_signal),
                    tune_units_min=list(_units),
                    tune_count=int(_cnt),
                    timeout_sec=float(_to),
                )

                if not res.get("ok"):
                    _st.tuned_summary = {"enabled": True, "applied": False, "reason": "no_data"}
                    return

                per = list(res.get("per") or [])
                rsi_lo = min(p.get("rsi_p10", 0.0) for p in per)
                rsi_hi = max(p.get("rsi_p90", 0.0) for p in per)
                span = max(1.0, float(rsi_hi) - float(rsi_lo))

                macd_span_abs = 0.0
                for p in per:
                    macd_span_abs = max(macd_span_abs, abs(float(p.get("macd_pct_p10", 0.0))), abs(float(p.get("macd_pct_p90", 0.0))))

                alpha = float(_alpha)
                if macd_span_abs > 0.0:
                    alpha = _clamp(alpha + (0.10 - macd_span_abs) * 1.5, 0.15, 0.35)

                buy_cand = float(rsi_lo) + alpha * span
                sell_cand = float(rsi_hi) - alpha * span

                strength = _clamp(float(_strength), 0.0, 1.0)
                buy = float(_base_buy) + strength * (buy_cand - float(_base_buy))
                sell = float(_base_sell) + strength * (sell_cand - float(_base_sell))

                buy = _clamp(buy, 15.0, 55.0)
                sell = _clamp(sell, 45.0, 90.0)

                min_gap = max(5.0, float(_min_gap))
                if (sell - buy) < min_gap:
                    mid = (buy + sell) / 2.0
                    buy = _clamp(mid - (min_gap / 2.0), 15.0, 55.0)
                    sell = _clamp(mid + (min_gap / 2.0), 45.0, 90.0)

                _st.tuned = True
                _st.tuned_applied_ts = _now
                _st.tuned_params = {
                    "rsi_buy_base": float(round(buy, 3)),
                    "rsi_sell_base": float(round(sell, 3)),
                }
                _st.tuned_summary = {
                    "applied": True,
                    "rsi_buy_base": _st.tuned_params["rsi_buy_base"],
                    "rsi_sell_base": _st.tuned_params["rsi_sell_base"],
                }
                _st.tune_emit_once = True

                if _context is not None:
                    try:
                        _context.set_var("al_tuned_params", dict(_st.tuned_params))
                        _context.set_var("al_tuned_ts", float(_now))
                    except (TypeError, ValueError) as exc:
                        logger.warning("[AUTOLOOP] autoloop_strategy fallback: %s", exc, exc_info=True)
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.warning("[AUTOLOOP] autotune failed for %s: %s", _m, e, exc_info=True)
                _st.tuned_summary = {"enabled": True, "applied": False, "error": str(e)}
            finally:
                with _inflight_lock:
                    _autotune_inflight.discard(_m)

    threading.Thread(target=_bg_tune, daemon=True).start()


# =========================
# Public API
# =========================


def decide_detail(context: Any, price: float, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a detailed decision dict.

    The caller may use:
      result["signal"] in {"buy","sell","hold"}
      result["meta"] for debugging/telemetry
    """

    params = dict(params or {})

    market = _get_market(context)
    now = time.time()
    # ---- Config needed early ----
    # NOTE:
    # - periodic retune (below) references `st`, so state MUST be initialized first.
    # - Values can be edited via dashboard; keep parsing defensive.
    try:
        bar_sec = int(params.get("bar_sec", 180))
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[AUTOLOOP] bar_sec parse failed, using default 180", exc_info=True)
        bar_sec = 180
    try:
        max_bars = int(params.get("max_bars", 600))
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[AUTOLOOP] max_bars parse failed, using default 600", exc_info=True)
        max_bars = 600

    if bar_sec <= 0:
        bar_sec = 180
    if max_bars <= 0:
        max_bars = 600

    st = _ensure_state(market, bar_sec=bar_sec, max_bars=max_bars)

    # ------------------------------------------------------------
    # PERIODIC RETUNE (runtime)
    # - ENV: AUTOLOOP_RETUNE_INTERVAL_SEC (default 3600)
    # - also allow per-market override: params.retune_interval_sec
    # - interval <= 0  => disable periodic retune
    # ------------------------------------------------------------
    try:
        env_iv = float(os.getenv("AUTOLOOP_RETUNE_INTERVAL_SEC", "3600") or 3600)
    except (TypeError, ValueError):
        logger.warning("[AUTOLOOP] AUTOLOOP_RETUNE_INTERVAL_SEC parse failed", exc_info=True)
        env_iv = 3600.0

    try:
        iv = float(params.get("retune_interval_sec", env_iv))
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[AUTOLOOP] retune_interval_sec parse failed", exc_info=True)
        iv = env_iv

    if iv > 0:
        last_ok = float(getattr(st, "tuned_applied_ts", 0.0) or 0.0)
        if last_ok > 0 and (now - last_ok) >= iv:
            # 재튜닝 만료 처리
            st.tuned = False
            st.tune_emit_once = False
            st.tuned_ts = 0.0
            st.tuned_summary = {
                "enabled": True,
                "applied": False,
                "reason": "periodic_retune_due",
                "interval_sec": iv,
            }
            # ✅ 다음 튜닝 결과가 나오면 controls에 다시 반영되도록 리셋
            try:
                st.applied_for_tuned_ts = 0.0
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[AUTOLOOP] 다음 튜닝 결과가 나오면 controls에 다시 반영되도록 리셋: %s", exc, exc_info=True)


    # ---- Config (safe defaults) ----
    bootstrap = bool(params.get("bootstrap", True))
    bootstrap_count = int(params.get("bootstrap_count", 200))
    bootstrap_min_bars = int(params.get("bootstrap_min_bars", 60))
    bootstrap_timeout = float(params.get("bootstrap_timeout_sec", 3.0))

    rsi_len = int(params.get("rsi_len", 14))
    # [2026-05-30] 부모님 결단 5️⃣ A 보수화 — RSI buy 28 → 26 (더 깊은 oversold만 진입)
    rsi_buy_base = float(params.get("rsi_buy", 26.0))
    rsi_sell_base = float(params.get("rsi_sell", 58.0))

    macd_fast = int(params.get("macd_fast", 12))
    macd_slow = int(params.get("macd_slow", 26))
    macd_signal = int(params.get("macd_signal", 9))

    # Autotune (multi-timeframe RSI/MACD range)
    tune_on_boot = bool(params.get("tune_on_boot", True))
    tune_units_min = params.get("tune_units_min") or [240, 60, 30, 10]
    tune_count = int(params.get("tune_count", 200))
    tune_timeout_sec = float(params.get("tune_timeout_sec", 3.0))
    tune_alpha = float(params.get("tune_alpha", 0.25))
    tune_strength = float(params.get("tune_strength", 0.75))
    tune_min_gap = float(params.get("tune_min_gap", 10.0))
    tune_attempt_cooldown_sec = float(params.get("tune_attempt_cooldown_sec", 60.0))

    anchor_len = int(params.get("anchor_len", 50))
    z_len = int(params.get("z_len", 20))
    z_buy_base = float(params.get("z_buy", 1.5))
    dev_buy_pct_base = float(params.get("dev_buy_pct", 0.35))

    # Volatility / knife filters (bar-based)
    vol_len = int(params.get("vol_len", 20))
    max_vol_pct = float(params.get("max_vol_pct", 1.8))
    knife_lookback = int(params.get("knife_lookback", 4))
    knife_drop_pct = float(params.get("knife_drop_pct", 1.2))

    # Regime filter (EMA spread)
    regime_fast = int(params.get("regime_fast", 20))
    regime_slow = int(params.get("regime_slow", 60))
    regime_bull_pct = float(params.get("regime_bull_pct", 0.4))
    regime_bear_pct = float(params.get("regime_bear_pct", 0.4))
    momentum_lookback = int(params.get("momentum_lookback", 10))
    momentum_require = bool(params.get("momentum_require", True))

    repeat_cooldown_sec = float(params.get("repeat_cooldown_sec", 2.0))

    # Trend pullback (BULL regime tactic)
    pb_enabled = bool(params.get("pb_enabled", True))
    pb_rsi_min = float(params.get("pb_rsi_min", 38.0))
    pb_rsi_max = float(params.get("pb_rsi_max", 55.0))
    pb_dev_min_pct = float(params.get("pb_dev_min_pct", 0.15))
    pb_dev_max_pct = float(params.get("pb_dev_max_pct", 0.80))
    pb_slope_bars = max(1, int(params.get("pb_slope_bars", 5)))
    pb_min_slope_pct = float(params.get("pb_min_slope_pct", 0.05))
    pb_macd_floor = float(params.get("pb_macd_floor", 0.0))
    pb_z_buy = float(params.get("pb_z_buy", 0.6))
    pb_require_bounce = bool(params.get("pb_require_bounce", True))

    # Bear Rebound (BEAR regime tactic)
    br_enabled = bool(params.get("br_enabled", True))
    br_rsi_max = float(params.get("br_rsi_max", 35.0))
    br_dev_min_pct = float(params.get("br_dev_min_pct", 2.0))
    br_z_buy = float(params.get("br_z_buy", 2.0))

    # Telemetry (AUTOLOOP_SNAPSHOT) throttling
    telemetry_interval_sec = float(params.get("telemetry_interval_sec", 60.0))

    st = _ensure_state(market, bar_sec=bar_sec, max_bars=max_bars)

    # Auto-tune thresholds (per market, one-shot)
    _maybe_autotune(
        st,
        market=market,
        base_rsi_buy=rsi_buy_base,
        base_rsi_sell=rsi_sell_base,
        rsi_len=rsi_len,
        macd_fast=macd_fast,
        macd_slow=macd_slow,
        macd_signal=macd_signal,
        tune_on_boot=tune_on_boot,
        tune_units_min=tune_units_min,
        tune_count=tune_count,
        tune_timeout_sec=tune_timeout_sec,
        tune_alpha=tune_alpha,
        tune_strength=tune_strength,
        tune_min_gap=tune_min_gap,
        attempt_cooldown_sec=tune_attempt_cooldown_sec,
        context=context,
    )
    if st.tuned_params:
        rsi_buy_base = float(st.tuned_params.get("rsi_buy_base", rsi_buy_base))
        rsi_sell_base = float(st.tuned_params.get("rsi_sell_base", rsi_sell_base))

    boot_meta = _maybe_bootstrap(
        st,
        market,
        bar_sec=bar_sec,
        bootstrap=bootstrap,
        bootstrap_count=bootstrap_count,
        bootstrap_min_bars=bootstrap_min_bars,
        timeout=bootstrap_timeout,
    )

    _update_bars(st, float(price), now)

    # Build close series: completed bars + current forming bar (close).
    closes = list(st.bars)
    if st.cur_close > 0:
        closes.append(float(st.cur_close))

    pos = getattr(context, "position", None)
    has_pos = bool(pos) and float(pos.get("qty", 0) or 0) > 0
    # ------------------------------------------------------------
    # APPLY TUNED PARAMS INTO CONTROLS (persist + staged entry params)
    # - st.tuned_params(rsi_buy_base/rsi_sell_base) -> controls.strategy.params 저장
    # - 분할 매수(2~3단) 파라미터도 함께 저장:
    #   * buy_splits: [1차, 2차, 3차] 비중 (합 1.0)
    #   * add_buy_drop_pcts: [2차 트리거, 3차 트리거] (%; 음수)
    # - 주기 재튜닝 시각을 next_retune_ts로 기록 → 대시보드에서 변동 확인
    # ------------------------------------------------------------
    try:
        apply_to_controls = bool(params.get("apply_tuned_to_controls", True))
        if apply_to_controls and (not has_pos) and bool(getattr(st, "tuned", False)) and isinstance(getattr(st, "tuned_params", None), dict):

            tuned_ts = float(getattr(st, "tuned_ts", 0.0) or 0.0)
            applied_for = float(getattr(st, "applied_for_tuned_ts", 0.0) or 0.0)

            # 새로 튜닝된 경우에만 반영
            if tuned_ts > 0.0 and tuned_ts != applied_for:

                buy_tuned = float(st.tuned_params.get("rsi_buy_base", rsi_buy_base))
                sell_tuned = float(st.tuned_params.get("rsi_sell_base", rsi_sell_base))

                # 안전 클램프
                buy_tuned = max(15.0, min(55.0, buy_tuned))
                sell_tuned = max(max(buy_tuned + 6.0, 45.0), min(90.0, sell_tuned))

                # 변동성 proxy: 튜닝 요약의 macd_span_abs_pct 사용(추가 계산 없음)
                vol_proxy = None
                try:
                    vol_proxy = float((st.tuned_summary or {}).get("macd_span_abs_pct"))
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[AUTOLOOP] macd_span_abs_pct parse failed", exc_info=True)
                    vol_proxy = None

                # --------------------------------------------------------
                # PATCH: Martingale Support
                # --------------------------------------------------------
                martingale = float(params.get("martingale", 1.0))
                
                if martingale > 1.0:
                    # Martingale: 1, m, m^2 ... (3 stages)
                    w = [pow(martingale, i) for i in range(3)]
                    total_w = sum(w)
                    buy_splits = [x / total_w for x in w]
                    
                    # Drop pcts: keep deeper staged adds in weak markets
                    if vol_proxy is not None and vol_proxy >= 0.020:
                        add_buy_drop_pcts = [-1.5, -3.5]
                    elif vol_proxy is not None and vol_proxy <= 0.006:
                        add_buy_drop_pcts = [-1.0, -2.6]
                    else:
                        add_buy_drop_pcts = [-1.2, -3.0]
                else:
                    # Conservative staged add-buy defaults
                    if vol_proxy is not None and vol_proxy >= 0.020:
                        buy_splits = [0.25, 0.30, 0.45]
                        add_buy_drop_pcts = [-1.4, -3.2]
                    elif vol_proxy is not None and vol_proxy <= 0.006:
                        buy_splits = [0.35, 0.30, 0.35]
                        add_buy_drop_pcts = [-1.0, -2.6]
                    else:
                        buy_splits = [0.30, 0.30, 0.40]
                        add_buy_drop_pcts = [-1.2, -3.0]

                # 과매매 방지: RSI gap이 좁으면 1차 더 줄이고 트리거 더 깊게
                gap = float(sell_tuned - buy_tuned)
                if gap < 10.0:
                    buy_splits = [max(0.12, buy_splits[0] * 0.90), buy_splits[1], buy_splits[2]]
                    s = sum(buy_splits)
                    buy_splits = [x / s for x in buy_splits]
                    add_buy_drop_pcts = [add_buy_drop_pcts[0] - 0.2, add_buy_drop_pcts[1] - 0.4]

                # 주기 재튜닝 정보 기록(대시보드 표시용)
                try:
                    env_iv = float(os.getenv("AUTOLOOP_RETUNE_INTERVAL_SEC", "3600") or 3600)
                except (TypeError, ValueError):
                    logger.warning("[AUTOLOOP] AUTOLOOP_RETUNE_INTERVAL_SEC parse failed (tuning apply)", exc_info=True)
                    env_iv = 3600.0
                try:
                    iv = float(params.get("retune_interval_sec", env_iv))
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[AUTOLOOP] retune_interval_sec parse failed (tuning apply)", exc_info=True)
                    iv = env_iv
                next_retune_ts = (time.time() + iv) if iv > 0 else None

                # controls 저장
                context.update_controls({
                    "strategy": {
                        "params": {
                            "rsi_buy": round(buy_tuned, 2),
                            "rsi_sell": round(sell_tuned, 2),
                            "buy_splits": buy_splits,
                            "add_buy_drop_pcts": add_buy_drop_pcts,
                            "entry_stage_max": 3,
                            "add_buy_cooldown_sec": 120.0,
                            "tuned_at": float(time.time()),
                            "retune_interval_sec": float(iv),
                            "next_retune_ts": float(next_retune_ts) if next_retune_ts is not None else None,
                            "tuned_vol_proxy_pct": float(vol_proxy) if vol_proxy is not None else None,
                        }
                    }
                })

                # 적용 마킹
                st.applied_for_tuned_ts = tuned_ts
                st.tuned_applied_ts = float(time.time())

    except (OSError, KeyError, IndexError, AttributeError, TypeError, ValueError, OverflowError) as exc:
        logger.warning("[AUTOLOOP] 적용 마킹: %s", exc, exc_info=True)



    def _finish(signal: str, reason: str, meta: Dict[str, Any], tactic: str) -> Dict[str, Any]:
        """Attach tactic + telemetry snapshot metadata and return a decision dict."""

        meta = dict(meta or {})
        meta.setdefault("market", market)
        meta.setdefault("bar_sec", bar_sec)
        meta.setdefault("bars", len(closes))
        meta.setdefault("boot", boot_meta)
        meta.setdefault("tune", st.tuned_summary or {"enabled": tune_on_boot, "applied": st.tuned})
        meta.setdefault("has_pos", has_pos)
        meta["tactic"] = str(tactic or "")

        # Snapshot throttling: always emit on buy/sell, else sample by interval.
        emit = False
        try:
            interval = float(telemetry_interval_sec)
        except (TypeError, ValueError):
            logger.warning("[AUTOLOOP] telemetry_interval_sec parse failed", exc_info=True)
            interval = 0.0

        if interval > 0.0:
            if signal in ("buy", "sell"):
                emit = True
            elif (now - float(st.last_snapshot_ts or 0.0)) >= interval:
                emit = True

        meta["telemetry_emit"] = bool(emit)
        if emit:
            st.last_snapshot_ts = now

            snap: Dict[str, Any] = {
                "signal": signal,
                "reason": reason,
                "tactic": str(tactic or ""),
                "price": float(price),
                "bar_sec": int(bar_sec),
                "bars": int(meta.get("bars") or len(closes)),
                "has_pos": bool(has_pos),
            }

            # Selected fields (keep payload compact)
            for k in (
                "regime",
                "regime_spread_pct",
                "momentum_pct",
                "rsi",
                "rsi_prev",
                "macd_hist",
                "macd_hist_prev",
                "anchor",
                "dev_pct",
                "dev_prev_pct",
                "z",
                "vol_pct",
                "pb_slope_pct",
            ):
                if k in meta:
                    snap[k] = meta[k]

            # Filters/diagnostics (compact)
            f = meta.get("filters")
            if isinstance(f, dict):
                snap["filters"] = {k: bool(f.get(k)) for k in ("vol_ok", "knife_ok", "momentum_ok") if k in f}

            d = meta.get("diagnostics")
            if isinstance(d, dict):
                snap["diagnostics"] = {k: d.get(k) for k in ("entry_ok", "exit_ok", "pullback_ok") if k in d}

            meta["telemetry"] = snap
            if st.tune_emit_once:
                meta["telemetry"]["tune"] = st.tuned_summary
                meta["telemetry"]["tuned_params"] = st.tuned_params
                st.tune_emit_once = False

        return {"signal": signal, "reason": reason, "meta": meta}

    # Repeat-cooldown to avoid log spam / repeated blocked entries.
    if st.last_signal in ("buy", "sell") and (now - st.last_signal_ts) < repeat_cooldown_sec:
        return _finish(
            "hold",
            "repeat_cooldown",
            {
                "market": market,
                "bar_sec": bar_sec,
                "bars": len(closes),
                "boot": boot_meta,
                "last_signal": st.last_signal,
                "last_signal_age_sec": now - st.last_signal_ts,
            },
            tactic="cooldown",
        )

    # Need enough bars for indicators.
    min_bars = max(macd_slow + macd_signal + 2, anchor_len + 2, rsi_len + 2, z_len + 2, regime_slow + 2)
    if len(closes) < min_bars:
        return _finish(
            "hold",
            "warmup_bars",
            {
                "market": market,
                "bar_sec": bar_sec,
                "bars": len(closes),
                "min_bars": min_bars,
                "boot": boot_meta,
            },
            tactic="warmup",
        )

    # ---- Indicators ----
    rsi_now, rsi_prev = indicators.rsi_with_prev(closes, rsi_len)
    hist_now, hist_prev = indicators.macd_hist_pair(closes, fast=macd_fast, slow=macd_slow, signal=macd_signal)

    anchor_series = indicators.ema_series(closes, anchor_len)
    anchor = float(anchor_series[-1]) if anchor_series else 0.0
    anchor_prev = float(anchor_series[-2]) if len(anchor_series) >= 2 else anchor
    dev_pct = 0.0 if anchor <= 0 else (float(price) / float(anchor) - 1.0) * 100.0

    dev_prev_pct = dev_pct
    if len(closes) >= 2 and anchor_prev > 0:
        dev_prev_pct = (float(closes[-2]) / float(anchor_prev) - 1.0) * 100.0

    # Trend slope (anchor EMA change over pb_slope_bars)
    slope_ref = anchor_prev
    if len(anchor_series) >= pb_slope_bars + 1:
        slope_ref = float(anchor_series[-(pb_slope_bars + 1)])
    pb_slope_pct = 0.0 if slope_ref <= 0 else (float(anchor) / float(slope_ref) - 1.0) * 100.0

    z_mean = _sma(closes, z_len)
    z_std = _std(closes, z_len)
    z = 0.0 if z_std <= 1e-12 else (float(price) - z_mean) / z_std

    rets = _returns(closes, vol_len)
    vol = 0.0
    if len(rets) >= 2:
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / len(rets)
        vol = math.sqrt(max(0.0, var))
    vol_pct = vol * 100.0

    regime, regime_spread_pct = _infer_regime(
        closes,
        fast=regime_fast,
        slow=regime_slow,
        bull_pct=regime_bull_pct,
        bear_pct=regime_bear_pct,
    )

    momentum_pct = 0.0
    if len(closes) >= momentum_lookback + 1 and closes[-(momentum_lookback + 1)] > 0:
        momentum_pct = (closes[-1] / closes[-(momentum_lookback + 1)] - 1.0) * 100.0

    # ---- Adaptive thresholds (regime-aware) ----
    rsi_buy = rsi_buy_base
    rsi_sell = rsi_sell_base
    z_buy = z_buy_base
    dev_buy_pct = dev_buy_pct_base

    if regime == "BULL":
        rsi_buy = min(45.0, rsi_buy_base + 5.0)
        rsi_sell = min(80.0, rsi_sell_base + 5.0)
        z_buy = max(0.9, z_buy_base - 0.2)
        dev_buy_pct = max(0.15, dev_buy_pct_base - 0.10)
    elif regime == "BEAR":
        rsi_buy = max(15.0, rsi_buy_base - 3.0)
        rsi_sell = max(45.0, rsi_sell_base - 3.0)
        z_buy = z_buy_base + 0.4
        dev_buy_pct = dev_buy_pct_base + 0.15

    # ---- Filters ----
    # [2026-05-30] 부모님 결단 5️⃣ C ATR 동적 — 변동성 큰 코인 = 가드 강화 / 작은 코인 = 관용
    _atr_val = None
    _atr_pct = 0.0
    try:
        _atr_val = indicators.atr_simplified(closes)
        if _atr_val and price > 0:
            _atr_pct = (float(_atr_val) / float(price)) * 100.0
    except (KeyError, AttributeError, TypeError, ValueError, ZeroDivisionError):
        _atr_pct = 0.0

    # Volatility max: max(default 1.8%, ATR%×1.5) — 변동성 큰 코인은 더 관용
    effective_max_vol = max(max_vol_pct, _atr_pct * 1.5) if _atr_pct > 0 else max_vol_pct
    vol_ok = vol_pct <= effective_max_vol

    knife_drop = 0.0
    if len(closes) >= knife_lookback + 1 and closes[-(knife_lookback + 1)] > 0:
        knife_drop = (closes[-1] / closes[-(knife_lookback + 1)] - 1.0) * 100.0
    # Knife guard: max(보수화 1.5%, ATR%×1.0) — 변동성 큰 코인 = 더 강한 컷 보호
    effective_knife_pct = max(1.5, max(knife_drop_pct, _atr_pct * 1.0)) if _atr_pct > 0 else max(1.5, knife_drop_pct)
    knife_ok = knife_drop >= -abs(effective_knife_pct)

    momentum_ok = True
    if momentum_require and regime == "BEAR":
        # In a BEAR regime, require non-negative short momentum to avoid catching a falling knife.
        momentum_ok = momentum_pct >= 0.0

    macd_turning_up = hist_now > hist_prev
    macd_turning_down = hist_now < hist_prev

    # ---- Entry/Exit Logic ----
    signal = "hold"
    reason = "hold"

    # Mean-reversion entry (baseline)
    # [2026-05-30] 부모님 결단 5️⃣ A 보수화 — BEAR regime mean-reversion 진입 차단
    # BEAR 에서는 bear_rebound 만 허용 (line 1165~ br_enabled). 일반 MR 진입 X
    mr_entry_ok = (
        (regime != "BEAR")
        and (rsi_now <= rsi_buy)
        and macd_turning_up
        and (z <= -abs(z_buy))
        and (dev_pct <= -abs(dev_buy_pct))
        and vol_ok
        and knife_ok
        and momentum_ok
    )

    # Trend pullback entry (BULL regime): buy controlled dips in an uptrend.
    pullback_ok = False
    pullback_debug: Dict[str, Any] = {}
    if pb_enabled and (not has_pos) and regime == "BULL":
        dev_min = abs(float(pb_dev_min_pct))
        dev_max = abs(float(pb_dev_max_pct))
        if dev_max < dev_min:
            dev_min, dev_max = dev_max, dev_min

        dev_in_band = (dev_pct <= -dev_min) and (dev_pct >= -dev_max)
        rsi_in_band = (rsi_now >= pb_rsi_min) and (rsi_now <= pb_rsi_max)
        macd_ok_pb = (hist_now >= hist_prev) and (hist_now >= pb_macd_floor)
        z_ok_pb = True if float(pb_z_buy) <= 0 else (z <= -abs(float(pb_z_buy)))
        slope_ok = pb_slope_pct >= pb_min_slope_pct
        bounce_ok = True if (not pb_require_bounce) else (dev_pct > dev_prev_pct)

        pullback_ok = (
            dev_in_band
            and rsi_in_band
            and macd_ok_pb
            and z_ok_pb
            and slope_ok
            and bounce_ok
            and vol_ok
            and knife_ok
            and momentum_ok
        )

        pullback_debug = {
            "dev_in_band": dev_in_band,
            "dev_min_pct": dev_min,
            "dev_max_pct": dev_max,
            "rsi_in_band": rsi_in_band,
            "macd_ok": macd_ok_pb,
            "z_ok": z_ok_pb,
            "slope_ok": slope_ok,
            "bounce_ok": bounce_ok,
        }

    # Bear Rebound (BEAR regime tactic): buy deep oversold bounces in downtrend.
    bear_rebound_ok = False
    bear_debug: Dict[str, Any] = {}
    if br_enabled and (not has_pos) and regime == "BEAR":
        dev_ok_br = dev_pct <= -abs(br_dev_min_pct)
        rsi_ok_br = rsi_now <= br_rsi_max
        z_ok_br = z <= -abs(br_z_buy)
        macd_ok_br = (hist_now > hist_prev)

        bear_rebound_ok = (
            dev_ok_br
            and rsi_ok_br
            and z_ok_br
            and macd_ok_br
            and vol_ok
            and knife_ok
            # momentum_ok skipped to catch turn early
        )

        bear_debug = {
            "dev_ok": dev_ok_br,
            "rsi_ok": rsi_ok_br,
            "z_ok": z_ok_br,
            "macd_ok": macd_ok_br,
        }


    # ---- Trailing Stop (시간대별 배율 적용) ----
    trailing_base = float(params.get("trailing_pct", 0.8))  # 기본값 0.8%
    time_mult = get_time_volatility_multiplier()
    trailing_pct = trailing_base * time_mult

    exit_trailing_hit = False
    if has_pos:
        # 최고가 대비 하락률 계산
        try:
            entry_price = float(pos.get("entry_price", 0))
            high_price = float(pos.get("high_price", 0))
            cur_price = float(price)
            if high_price > 0:
                drawdown = (high_price - cur_price) / high_price * 100.0
                exit_trailing_hit = drawdown >= trailing_pct
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[AUTOLOOP] 최고가 대비 하락률 계산: %s", exc, exc_info=True)

    exit_ok = (
        ((rsi_now >= rsi_sell) and macd_turning_down and (z >= 0.0) and vol_ok)
        or exit_trailing_hit
    )

    tactic = "hold"
    if not has_pos:
        if pullback_ok:
            signal = "buy"
            reason = "entry_trend_pullback"
            tactic = "trend_pullback"
        elif bear_rebound_ok:
            signal = "buy"
            reason = "entry_bear_rebound"
            tactic = "bear_rebound"
        elif mr_entry_ok:
            signal = "buy"
            reason = "entry_mean_reversion"
            tactic = "mean_reversion"
    else:
        # Exit hint only. Real exits may be driven by TP/SL and profit guard.
        if exit_ok:
            signal = "sell"
            reason = "exit_rsi_macd"
            tactic = "exit"

    if signal == "buy":
        # --------------------------------------------------------
        # PATCH: 잔고 부족 시 매수 신호 억제 (로그 스팸 방지)
        # --------------------------------------------------------
        capital = 0.0
        try:
            c = getattr(context, "usable_capital", None)
            if c is None:
                c = getattr(context, "allocated_capital", None)
            capital = float(c or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[AUTOLOOP] capital read failed for buy gate", exc_info=True)
            capital = 0.0

        min_order = Q.min_order
        if params:
            try:
                min_order = float(params.get("min_order_usdt") or Q.min_order)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[AUTOLOOP] min_order_usdt 파싱 실패: %s", exc, exc_info=True)

        if capital < min_order:
            signal = "hold"
            reason = "insufficient_capital"
            tactic = "hold"

    if signal in ("buy", "sell"):
        st.last_signal = signal
        st.last_signal_ts = now

    meta: Dict[str, Any] = {
        "market": market,
        "bar_sec": bar_sec,
        "bars": len(closes),
        "boot": boot_meta,
        "has_pos": has_pos,
        "rsi": rsi_now,
        "rsi_prev": rsi_prev,
        "macd_hist": hist_now,
        "macd_hist_prev": hist_prev,
        "anchor": anchor,
        "dev_pct": dev_pct,
        "dev_prev_pct": dev_prev_pct,
        "z": z,
        "z_mean": z_mean,
        "z_std": z_std,
        "vol_pct": vol_pct,
        "pb_slope_pct": pb_slope_pct,
        "regime": regime,
        "regime_spread_pct": regime_spread_pct,
        "momentum_pct": momentum_pct,
        "tactic": tactic,
        "thresholds": {
            "rsi_buy": rsi_buy,
            "rsi_sell": rsi_sell,
            "z_buy": z_buy,
            "dev_buy_pct": dev_buy_pct,
            "max_vol_pct": max_vol_pct,
            "knife_drop_pct": knife_drop_pct,
            # [2026-05-30] ATR 동적 적용 후 실효 임계 (부모님 검증용)
            "atr_pct": round(_atr_pct, 3),
            "effective_max_vol_pct": round(effective_max_vol, 3),
            "effective_knife_pct": round(effective_knife_pct, 3),
            "pb_enabled": pb_enabled,
            "pb_rsi_min": pb_rsi_min,
            "pb_rsi_max": pb_rsi_max,
            "pb_dev_min_pct": pb_dev_min_pct,
            "pb_dev_max_pct": pb_dev_max_pct,
            "pb_slope_bars": pb_slope_bars,
            "pb_min_slope_pct": pb_min_slope_pct,
            "pb_macd_floor": pb_macd_floor,
            "pb_z_buy": pb_z_buy,
            "pb_require_bounce": pb_require_bounce,
            "br_enabled": br_enabled,
            "br_rsi_max": br_rsi_max,
            "br_dev_min_pct": br_dev_min_pct,
            "br_z_buy": br_z_buy,
            "telemetry_interval_sec": telemetry_interval_sec,
        },
        "filters": {
            "vol_ok": vol_ok,
            "knife_ok": knife_ok,
            "momentum_ok": momentum_ok,
        },
        "diagnostics": {
            "knife_drop_pct": knife_drop,
            "entry_ok": bool(pullback_ok or mr_entry_ok or bear_rebound_ok),
            "mean_reversion_ok": bool(mr_entry_ok),
            "pullback_ok": bool(pullback_ok),
            "pullback": pullback_debug,
            "bear_rebound_ok": bool(bear_rebound_ok),
            "bear_rebound": bear_debug,
            "exit_ok": exit_ok,
        },
    }

    return _finish(signal, reason, meta, tactic=tactic)


def decide(context: Any, price: float, params: Optional[Dict[str, Any]] = None) -> str:
    """Compatibility wrapper: returns only the signal."""
    return str(decide_detail(context, price, params).get("signal", "hold"))
