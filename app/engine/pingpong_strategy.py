# ============================================================
# File: app/engine/pingpong_strategy.py
# Strategy Module — PingPong (LIVE-SAFE)
# ------------------------------------------------------------
# Purpose:
# - Provide BUY / SELL / HOLD "decisions" only.
# - Execution (orders) is handled by HyperSystem (Order FSM).
#
# ✅ 2025-12-25 PATCH (Plan 1~5)
# - Fixes the negative-margin (buy high / sell low) behavior:
#   removes the old implementation's forced "always buy / always sell" logic.
# - Honors a minimum target spread (min_roundtrip_pct) that accounts for fees/spread.
# - anchor(SMA)-based mean-reversion + SL (stop-loss) support.
# ============================================================

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

from app.strategy import indicators
from app.core.currency import Q
from app.core.time_volatility import get_time_volatility_multiplier


Signal = Literal["buy", "sell", "hold"]


def _clean_prices(raw: List[Any], *, max_n: int) -> List[float]:
    out: List[float] = []
    if max_n <= 0:
        return out

    for v in raw[-max_n:]:
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            logger.warning("[PINGPONG] _clean_prices except-> continue: %s", exc, exc_info=True)
            continue
        if (not math.isfinite(f)) or f <= 0.0:
            continue
        out.append(float(f))
    return out


def _sma(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return float(sum(values) / float(len(values)))




# ------------------------------------------------------------
# Indicator helpers (self-contained; no external deps)
# ------------------------------------------------------------
def _ema_last(values: List[float], period: int) -> Optional[float]:
    """Return the last EMA value (simple seed + EMA smoothing)."""
    if period <= 0:
        return None
    if len(values) < period:
        return None
    k = 2.0 / (float(period) + 1.0)
    ema = sum(values[:period]) / float(period)
    for v in values[period:]:
        ema = float(v) * k + ema * (1.0 - k)
    return float(ema)


def _rsi_last(prices: List[float], length: int) -> Optional[float]:
    """Wilder RSI (last value)."""
    if length <= 0:
        return None
    if len(prices) < length + 1:
        return None

    # price deltas
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(prices)):
        d = float(prices[i]) - float(prices[i - 1])
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    if len(gains) < length:
        return None

    avg_gain = sum(gains[:length]) / float(length)
    avg_loss = sum(losses[:length]) / float(length)

    # Wilder smoothing
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / float(length)
        avg_loss = (avg_loss * (length - 1) + losses[i]) / float(length)

    if avg_loss <= 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # guard numeric stability
    if not math.isfinite(rsi):
        return None
    return float(max(0.0, min(100.0, rsi)))


def _macd_hist_last(prices: List[float], *, fast: int, slow: int, signal: int) -> Optional[float]:
    """Return last MACD histogram value."""
    if fast <= 0 or slow <= 0 or signal <= 0:
        return None
    if len(prices) < max(fast, slow) + signal:
        # not enough samples for stable MACD + signal
        return None

    ema_fast: Optional[float] = _ema_last(prices, fast)
    ema_slow: Optional[float] = _ema_last(prices, slow)
    if ema_fast is None or ema_slow is None:
        return None

    # build MACD line for signal EMA
    # start from where slow EMA becomes meaningful
    macd_vals: List[float] = []
    kf = 2.0 / (float(fast) + 1.0)
    ks = 2.0 / (float(slow) + 1.0)

    # seed EMAs
    ema_f = sum(prices[:fast]) / float(fast)
    ema_s = sum(prices[:slow]) / float(slow)

    for i in range(slow, len(prices)):
        # update fast EMA from i-fast? We continue EMA on the full stream:
        ema_f = float(prices[i]) * kf + ema_f * (1.0 - kf)
        ema_s = float(prices[i]) * ks + ema_s * (1.0 - ks)
        macd_vals.append(float(ema_f - ema_s))

    if len(macd_vals) < signal:
        return None

    sig = _ema_last(macd_vals, signal)
    if sig is None:
        return None
    hist = macd_vals[-1] - float(sig)
    if not math.isfinite(hist):
        return None
    return float(hist)


def _volatility_pct(prices: List[float], *, window: int) -> Optional[float]:
    """Std dev of percent returns (in % units)."""
    if window <= 1:
        return None
    if len(prices) < window + 1:
        return None
    win = [float(x) for x in prices[-(window + 1):]]
    rets: List[float] = []
    for i in range(1, len(win)):
        prev = float(win[i - 1])
        cur = float(win[i])
        if prev <= 0.0:
            continue
        rets.append((cur / prev - 1.0) * 100.0)

    if len(rets) < 2:
        return None

    mean = sum(rets) / float(len(rets))
    var = sum((r - mean) ** 2 for r in rets) / float(len(rets) - 1)
    v = float(math.sqrt(max(0.0, var)))
    if not math.isfinite(v):
        return None
    return v


def compute_levels(context: Any, price: float, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute PingPong levels (entry/exit/stop-loss) + (optional) peak-proximal exit meta from the current price.

    Purpose:
    - Compute the 'levels' that decide() uses, and provide UI/ledger observations (meta) alongside.
    - Execution (orders) is handled by HyperSystem (Order FSM).

    params (supported keys: baseline)
      - pp_anchor_window / anchor_window (int)    : anchor(SMA) computation window (default=20)
      - pp_entry_gap_pct / gap_pct / gap (float) : entry gap % (default=0.35)
      - pp_exit_gap_pct / pp_tp_pct (float)      : exit gap % (default=entry_gap)
      - pp_min_roundtrip_pct / min_roundtrip_pct : minimum round-trip target % (default=0.25)
      - pp_sl_pct / sl (float)                   : stop-loss % (default=-0.8)

    params (additional: PingPong ExitPolicy v1 — peak-proximal)
      - pp_exit_enabled (bool)                   : default True
      - pp_exit_lookback (int)                   : indicator/high computation window (default=60)
      - pp_exit_dampen_need (int 1~3)            : dampen hits N-of-3 (default=2)
      - pp_exit_trail_min_pct / max_pct (float)  : trail% min/max (default 0.4~1.0)
      - pp_exit_trail_vol_mult (float)           : trail% = vol_mult * vol_pct (clamp) (default=0.6)
      - pp_exit_trail_vol_window (int)           : volatility computation window (default=30)
      - pp_exit_rsi_len (int)                    : RSI period (default=14)
      - pp_exit_rsi_drop_ratio (float)           : drop ratio vs RSI_peak (default=0.08)
      - pp_exit_macd_fast/slow/signal (int)      : MACD parameters (default=12/26/9)
      - pp_exit_macd_down_streak (int)           : MACD hist down-streak (default=2)
      - pp_exit_band_len / band_k                : Bollinger parameters (default=20/2.0)
      - pp_exit_min_profit_pct (float)           : minimum profit condition (optional) (default=0.0)

    params (additional: Entry Enhancement)
      - pp_buy_band_enabled (bool)               : enable lower-Bollinger buy (default=False)
      - pp_buy_band_len / k                      : buy-side band settings (default=20/2.0)
      - pp_check_squeeze (bool)                  : whether to detect squeeze (default=True)
      - pp_squeeze_lookback (int)                : squeeze detection window (default=20)
      - pp_squeeze_action (str)                  : action on squeeze "suspend"|"ignore" (default="suspend")

    Returns:
      dict(valid, price, buy_price, sell_price, stop_price, exit, ...)
    """
    params = dict(params or {})

    # -----------------------------
    # price sanitize
    # -----------------------------
    try:
        p = float(price)
    except (TypeError, ValueError):
        logger.warning("[PingPong] price float conversion failed: %r", price, exc_info=True)
        p = 0.0

    if (not math.isfinite(p)) or p <= 0.0:
        return {"valid": False, "price": p}

    now = time.time()

    # -----------------------------
    # (defensive) reentry cooldown
    # -----------------------------
    reentry_until = 0.0
    try:
        reentry_until = float(getattr(context, "reentry_block_until_ts", 0.0) or 0.0)
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[PingPong] reentry_block_until_ts read failed", exc_info=True)
        reentry_until = 0.0
    reentry_blocked = bool(reentry_until and now < reentry_until)

    # -----------------------------
    # params normalize
    # -----------------------------
    try:
        anchor_window = int(params.get("pp_anchor_window", params.get("anchor_window", 20)) or 20)
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[PingPong] anchor_window parse failed, using default 20", exc_info=True)
        anchor_window = 20

    def _f(key: str, default: float) -> float:
        try:
            return float(params.get(key, default))
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[PingPong] param '%s' parse failed, using default %s", key, default, exc_info=True)
            return float(default)

    entry_gap_pct = _f("pp_entry_gap_pct", _f("gap_pct", _f("gap", 0.35)))
    exit_gap_pct = _f("pp_exit_gap_pct", _f("pp_tp_pct", float(entry_gap_pct)))
    min_roundtrip_pct = _f("pp_min_roundtrip_pct", _f("min_roundtrip_pct", 0.25))

    # [2026-03-30] PINGPONG Regime-Aware: adjust entry/exit gap per BTC regime
    # ON/OFF via the pp_regime_aware parameter (default ON)
    _regime_aware = bool(params.get("pp_regime_aware", True))
    if _regime_aware:
        try:
            _regime = str(params.get("_btc_regime", "")).upper()
            if not _regime:
                # Read BTC regime from price_store (no HTTP call, cache only)
                from app.core.market_regime import RegimeDetector
                _det = getattr(compute_levels, "_regime_det", None)
                if _det is None:
                    _det = RegimeDetector()
                    compute_levels._regime_det = _det  # singleton cache
                _regime = str(_det.detect("BTCUSDT") or "SIDEWAYS").upper()
            if _regime == "BULL":
                entry_gap_pct *= 0.8   # buy sooner
                exit_gap_pct *= 1.3    # hold longer
            elif _regime == "BEAR":
                entry_gap_pct *= 1.3   # buy after a deeper dip
                exit_gap_pct *= 0.7    # exit sooner
            elif _regime == "VOLATILE":
                entry_gap_pct *= 1.2   # ignore noise
                exit_gap_pct *= 1.2
            # SIDEWAYS: no change
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[PINGPONG] regime-aware gap adjust failed: %s", exc, exc_info=True)

    # Stop-loss percent accepts both negative and positive input: 2.5 or -2.5 → normalized to -2.5
    # 2026-01-30: default relaxed from -0.8% → -2.5% (too tight stops out right after buying)
    sl_raw = params.get("pp_sl_pct", params.get("sl", -2.5))
    try:
        sl_pct = float(sl_raw)
    except (TypeError, ValueError):
        logger.warning("[PingPong] sl_pct parse failed: %r, using default -2.5", sl_raw, exc_info=True)
        sl_pct = -2.5
    if sl_pct > 0:
        sl_pct = -abs(sl_pct)

    # minimum-value guard
    entry_gap_eff = max(abs(float(entry_gap_pct)), 0.01)
    exit_gap_eff = max(abs(float(exit_gap_pct)), abs(float(min_roundtrip_pct)), 0.01)

    # default-insertion helper (works even when missing from engine/config)
    def _get_param(k: str, default: Any) -> Any:
        try:
            return params.get(k, default)
        except (KeyError, AttributeError, TypeError):
            logger.warning("[PingPong] _get_param('%s') failed, using default", k, exc_info=True)
            return default

    # -----------------------------
    # anchor(SMA)
    # -----------------------------
    anchor = float(p)
    try:
        hist = getattr(context, "_tick_prices", None) or list(getattr(context, "price_history", []) or [])
        cleaned = _clean_prices(hist, max_n=max(anchor_window, 30))
        if len(cleaned) >= max(5, min(10, anchor_window)):
            win = cleaned[-anchor_window:] if anchor_window > 0 else cleaned
            a = _sma(win)
            if a and a > 0:
                anchor = float(a)
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[PingPong] anchor SMA computation failed, using price", exc_info=True)
        anchor = float(p)

    # -----------------------------
    # position extract
    # -----------------------------
    pos = getattr(context, "position", None)
    entry = 0.0
    qty = 0.0
    has_position = False
    if isinstance(pos, dict):
        try:
            entry = float(pos.get("entry") or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[PingPong] position entry parse failed", exc_info=True)
            entry = 0.0
        try:
            qty = float(pos.get("qty") or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[PingPong] position qty parse failed", exc_info=True)
            qty = 0.0
        has_position = bool(qty > 0.0)

    # -----------------------------
    # level computation (classic)
    # -----------------------------
    buy_price = float(anchor) * (1.0 - (entry_gap_eff / 100.0))

    sell_price = None
    stop_price = None

    # -----------------------------
    # Bollinger Band Entry / Squeeze Logic
    # -----------------------------
    buy_band_enabled = bool(_get_param("pp_buy_band_enabled", False))
    buy_band_len = int(_get_param("pp_buy_band_len", 20) or 20)
    buy_band_k = float(_get_param("pp_buy_band_k", 2.0) or 2.0)
    
    check_squeeze = bool(_get_param("pp_check_squeeze", True))
    squeeze_lookback = int(_get_param("pp_squeeze_lookback", 20) or 20)

    squeeze_info = None
    bb_lower_entry = None

    # Prepare history including the current price for band computation
    if buy_band_enabled or check_squeeze:
        try:
            hist_full = getattr(context, "_tick_prices", None) or list(getattr(context, "price_history", []) or [])
            # Include the current price p to reflect the latest state
            full_series = _clean_prices(hist_full + [float(p)], max_n=max(200, squeeze_lookback + buy_band_len))

            if buy_band_enabled:
                bb_res = indicators.bollinger_bands(full_series, length=buy_band_len, k=buy_band_k)
                if bb_res:
                    bb_lower_entry = float(bb_res["lower"])
                    # Between the SMA-based buy price and the band lower, pick the 'higher (buys sooner) price'
                    # (aggressive entry: when the band tightens and the lower rises, buy there)
                    buy_price = max(buy_price, bb_lower_entry)

            if check_squeeze:
                sq_res = indicators.bollinger_squeeze(full_series, length=buy_band_len, k=buy_band_k, lookback=squeeze_lookback)
                if sq_res:
                    bw, is_sq = sq_res
                    squeeze_info = {"bandwidth": bw, "is_squeeze": is_sq}
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[PINGPONG] band entry/squeeze computation failed: %s", exc, exc_info=True)

    if has_position and entry > 0.0:
        sell_price = float(entry) * (1.0 + (exit_gap_eff / 100.0))
        stop_price = float(entry) * (1.0 + (sl_pct / 100.0))

    # -----------------------------
    # (optional) ExitPolicy v1 (peak-proximal)
    # -----------------------------
    exit_meta: Dict[str, Any] = {"enabled": False, "triggered": False}

    exit_enabled = bool(_get_param("pp_exit_enabled", True))

    if has_position and exit_enabled and entry > 0.0:
        exit_meta["enabled"] = True

        # 1) since-entry high (persisted on ctx)
        try:
            prev_high = float(context.get_var("pp_high_since_entry", 0.0))
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_high_since_entry read failed", exc_info=True)
            prev_high = 0.0
        high = max(prev_high, float(p))
        try:
            context.set_var("pp_high_since_entry", float(high))
        except (TypeError, ValueError) as exc:
            logger.warning("[PINGPONG] since-entry high persist failed: %s", exc, exc_info=True)

        # 2) indicator input series
        try:
            lookback = int(_get_param("pp_exit_lookback", 60) or 60)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_lookback parse failed", exc_info=True)
            lookback = 60
        try:
            hist2 = getattr(context, "_tick_prices", None) or list(getattr(context, "price_history", []) or [])
        except (KeyError, AttributeError, TypeError):
            logger.warning("[PingPong] price history read failed for exit policy", exc_info=True)
            hist2 = []
        series = _clean_prices(hist2 + [float(p)], max_n=max(lookback, 200))

        # 3) optional profit gate (strategy-side)
        _entry_f = float(entry)
        change_pct = (float(p) - _entry_f) / _entry_f * 100.0 if _entry_f != 0.0 else 0.0
        try:
            min_profit = float(_get_param("pp_exit_min_profit_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_min_profit_pct parse failed", exc_info=True)
            min_profit = 0.0
        allow_exit = bool(change_pct >= float(min_profit))

        # --- RSI dampen ---
        try:
            rsi_len = int(_get_param("pp_exit_rsi_len", 14) or 14)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_rsi_len parse failed", exc_info=True)
            rsi_len = 14
        rsi_now = indicators.rsi(series, rsi_len)

        try:
            prev_rsi_peak = float(context.get_var("pp_rsi_peak_since_entry", 0.0))
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_rsi_peak_since_entry read failed", exc_info=True)
            prev_rsi_peak = 0.0
        rsi_peak = max(prev_rsi_peak, float(rsi_now or 0.0))
        try:
            context.set_var("pp_rsi_peak_since_entry", float(rsi_peak))
        except (TypeError, ValueError) as exc:
            logger.warning("[PINGPONG] RSI dampen: %s", exc, exc_info=True)

        try:
            rsi_drop_ratio = float(_get_param("pp_exit_rsi_drop_ratio", 0.08))
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_rsi_drop_ratio parse failed", exc_info=True)
            rsi_drop_ratio = 0.08
        rsi_dampen = False
        if rsi_now is not None and rsi_peak > 0:
            rsi_dampen = bool(float(rsi_now) < float(rsi_peak) * (1.0 - abs(float(rsi_drop_ratio))))

        # --- MACD dampen (hist down-streak) ---
        try:
            mf = int(_get_param("pp_exit_macd_fast", 12) or 12)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_macd_fast parse failed", exc_info=True)
            mf = 12
        try:
            ms = int(_get_param("pp_exit_macd_slow", 26) or 26)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_macd_slow parse failed", exc_info=True)
            ms = 26
        try:
            msi = int(_get_param("pp_exit_macd_signal", 9) or 9)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_macd_signal parse failed", exc_info=True)
            msi = 9

        macd_hist = _macd_hist_last(series, fast=mf, slow=ms, signal=msi)

        try:
            prev_hist = context.get_var("pp_macd_hist_prev", None)
            prev_hist_f = float(prev_hist) if prev_hist is not None else None
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_macd_hist_prev read failed", exc_info=True)
            prev_hist_f = None

        try:
            down_streak = int(context.get_var("pp_macd_down_streak", 0))
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_macd_down_streak read failed", exc_info=True)
            down_streak = 0

        macd_dampen = False
        if macd_hist is not None:
            if prev_hist_f is not None and float(macd_hist) < float(prev_hist_f):
                down_streak += 1
            else:
                down_streak = 0
            try:
                context.set_var("pp_macd_hist_prev", float(macd_hist))
                context.set_var("pp_macd_down_streak", int(down_streak))
            except (TypeError, ValueError) as exc:
                logger.warning("[PINGPONG] MACD dampen (hist down-streak): %s", exc, exc_info=True)

            try:
                need = int(_get_param("pp_exit_macd_down_streak", 2) or 2)
            except (TypeError, ValueError):
                logger.warning("[PingPong] pp_exit_macd_down_streak parse failed", exc_info=True)
                need = 2
            macd_dampen = bool(down_streak >= max(1, need))

        # --- Bollinger upper-band reject ---
        try:
            band_len = int(_get_param("pp_exit_band_len", 20) or 20)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_band_len parse failed", exc_info=True)
            band_len = 20
        try:
            band_k = float(_get_param("pp_exit_band_k", 2.0) or 2.0)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_band_k parse failed", exc_info=True)
            band_k = 2.0

        bb = indicators.bollinger_bands(series, length=band_len, k=band_k)

        try:
            was_above = bool(context.get_var("pp_band_was_above", False))
        except (AttributeError, TypeError):
            logger.warning("[PingPong] pp_band_was_above read failed", exc_info=True)
            was_above = False

        band_reject = False
        upper = None
        if bb:
            upper = float(bb.get("upper") or 0.0)
            if upper > 0 and float(p) > upper:
                was_above = True
            elif was_above and upper > 0 and float(p) <= upper:
                band_reject = True
                was_above = False

        try:
            context.set_var("pp_band_was_above", bool(was_above))
        except (AttributeError, TypeError) as exc:
            logger.warning("[PINGPONG] Bollinger upper-band reject: %s", exc, exc_info=True)

        # --- volatility-adaptive trailing exit ---
        try:
            vol_win = int(_get_param("pp_exit_trail_vol_window", 30) or 30)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_trail_vol_window parse failed", exc_info=True)
            vol_win = 30

        vol_pct = indicators.volatility(series, vol_win)
        try:
            vol_mult = float(_get_param("pp_exit_trail_vol_mult", 0.6) or 0.6)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_trail_vol_mult parse failed", exc_info=True)
            vol_mult = 0.6

        def _ff(key: str, default: float) -> float:
            try:
                return float(_get_param(key, default))
            except (TypeError, ValueError):
                logger.warning("[PingPong] param '%s' parse failed, using default %s", key, default, exc_info=True)
                return float(default)

        trail_min = _ff("pp_exit_trail_min_pct", 0.4)
        trail_max = _ff("pp_exit_trail_max_pct", 1.0)


        # Apply time-of-day volatility multiplier
        time_mult = get_time_volatility_multiplier()
        if vol_pct is not None:
            trail_pct = max(trail_min, min(trail_max, float(vol_mult) * float(vol_pct) * time_mult))
        else:
            trail_pct = max(trail_min, min(trail_max, 0.6 * time_mult))

        dist_from_high_pct = ((float(high) - float(p)) / float(high) * 100.0) if high > 0 else 0.0
        trail_hit = bool(dist_from_high_pct >= float(trail_pct))

        # --- N-of-3 dampen rule ---
        try:
            dampen_need = int(_get_param("pp_exit_dampen_need", 2) or 2)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_exit_dampen_need parse failed", exc_info=True)
            dampen_need = 2
        dampen_need = max(1, min(3, int(dampen_need)))
        dampen_hits = int(bool(rsi_dampen)) + int(bool(macd_dampen)) + int(bool(band_reject))
        dampen_hit = bool(dampen_hits >= dampen_need)

        triggered = bool(allow_exit and (trail_hit or dampen_hit))

        # Assemble meta (UI/ledger friendly)
        exit_meta = {
            "enabled": True,
            "triggered": bool(triggered),
            "mode": "TRAIL" if trail_hit else ("DAMPEN" if dampen_hit else "NONE"),
            "reason": (
                "TRAIL"
                if trail_hit
                else (
                    "DAMPEN(" + "+".join([x for x, ok in (("RSI", rsi_dampen), ("MACD", macd_dampen), ("BAND", band_reject)) if ok]) + ")"
                    if dampen_hit
                    else ""
                )
            ),
            "trigger": {
                "trail": bool(trail_hit),
                "rsi_dampen": bool(rsi_dampen),
                "macd_dampen": bool(macd_dampen),
                "band_reject": bool(band_reject),
            },
            "entry": float(entry),
            "price": float(p),
            "change_pct": float(change_pct),
            "high_since_entry": float(high),
            "dist_from_high_pct": float(dist_from_high_pct),
            "trail_pct": float(trail_pct),
            "rsi_now": float(rsi_now) if rsi_now is not None else None,
            "rsi_peak": float(rsi_peak) if rsi_peak > 0 else None,
            "macd_hist": float(macd_hist) if macd_hist is not None else None,
            "macd_down_streak": int(down_streak),
            "bb_upper": float(upper) if upper is not None else None,
            "vol_pct": float(vol_pct) if vol_pct is not None else None,
            "min_profit_pct": float(min_profit),
        }

    # -----------------------------
    # Sell lock (pairing) — never move sell line down
    # -----------------------------
    sell_lock_price = None
    lock_mode = str(_get_param("sell_lock_mode", "TRAIL_UP") or "TRAIL_UP").upper()
    lock_enabled = lock_mode not in ("OFF", "DISABLED", "NONE", "0", "FALSE")
    if has_position and entry > 0.0 and lock_enabled:
        try:
            lock_entry = float(context.get_var("pp_locked_entry", 0.0) or 0.0)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_locked_entry read failed", exc_info=True)
            lock_entry = 0.0
        try:
            lock_price = float(context.get_var("pp_locked_sell_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            logger.warning("[PingPong] pp_locked_sell_price read failed", exc_info=True)
            lock_price = 0.0

        if lock_entry != entry:
            lock_entry = entry
            lock_price = 0.0

        base_target = float(entry) * (1.0 + (exit_gap_eff / 100.0))
        if base_target > lock_price:
            lock_price = base_target

        try:
            high = float((exit_meta or {}).get("high_since_entry") or 0.0)
            trail_pct = float((exit_meta or {}).get("trail_pct") or 0.0)
            if high > 0 and trail_pct > 0:
                trail_price = high * (1.0 - trail_pct / 100.0)
                if trail_price > lock_price:
                    lock_price = trail_price
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[PINGPONG] trail price calc: %s", exc, exc_info=True)

        if lock_price > 0:
            sell_price = float(lock_price)
            sell_lock_price = float(lock_price)
        try:
            context.set_var("pp_locked_entry", float(lock_entry))
            context.set_var("pp_locked_sell_price", float(lock_price))
        except (TypeError, ValueError) as exc:
            logger.warning("[PINGPONG] set locked vars: %s", exc, exc_info=True)
    else:
        try:
            context.set_var("pp_locked_entry", 0.0)
            context.set_var("pp_locked_sell_price", 0.0)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[PINGPONG] fallback: %s", exc, exc_info=True)

    return {
        "valid": True,
        "price": float(p),
        "now": float(now),
        "reentry_blocked": bool(reentry_blocked),
        "reentry_until_ts": float(reentry_until),
        "anchor": float(anchor),
        "anchor_window": int(anchor_window),
        "entry_gap_pct": float(entry_gap_eff),
        "exit_gap_pct": float(exit_gap_eff),
        "min_roundtrip_pct": float(min_roundtrip_pct),
        "sl_pct": float(sl_pct),
        "has_position": bool(has_position),
        "entry": float(entry),
        "qty": float(qty),
        "buy_price": float(buy_price),
        "sell_price": float(sell_price) if sell_price is not None else None,
        "sell_lock_price": float(sell_lock_price) if sell_lock_price is not None else None,
        "stop_price": float(stop_price) if stop_price is not None else None,
        "exit": dict(exit_meta) if isinstance(exit_meta, dict) else None,
        "squeeze": squeeze_info,
        "bb_lower_entry": bb_lower_entry,
    }


def decide(context: Any, price: float, params: Optional[Dict[str, Any]] = None) -> Signal:
    """Decide the PingPong signal."""
    levels = compute_levels(context, price, params)

    if not levels.get("valid"):
        return "hold"

    # (defensive) reentry cooldown is enforced by the System in principle, but
    # the strategy level also applies a first-pass hold to reduce noise/over-trading.
    if levels.get("reentry_blocked"):
        return "hold"

    # --------------------------------------------------------
    # Squeeze Guard
    # --------------------------------------------------------
    # During a squeeze (volatility contraction) a range strategy can be risky
    # (breakout counter-trade), so new entries are held off.
    squeeze = levels.get("squeeze")
    if squeeze and squeeze.get("is_squeeze"):
        action = "suspend"
        if params:
            action = params.get("pp_squeeze_action", "suspend")
        
        # Restrict entry only when there is no position (existing positions follow exit logic)
        if action == "suspend" and not levels.get("has_position"):
            return "hold"

    p = float(levels["price"])

    if not levels.get("has_position"):
        buy_price = float(levels.get("buy_price") or 0.0)
        if buy_price > 0 and p <= buy_price:
            # --------------------------------------------------------
            # PATCH: suppress buy signal when capital is insufficient (avoid log spam)
            # --------------------------------------------------------
            capital = 0.0
            try:
                c = getattr(context, "usable_capital", None)
                if c is None:
                    c = getattr(context, "allocated_capital", None)
                capital = float(c or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[PingPong] capital read failed for buy gate", exc_info=True)
                capital = 0.0

            min_order = Q.min_order
            if params:
                try:
                    min_order = float(params.get("min_order_usdt") or Q.min_order)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[PINGPONG] min_order parse: %s", exc, exc_info=True)

            if capital < min_order:
                return "hold"

            return "buy"
        return "hold"

    # has position

    # 1) stop-loss (hard)
    stop_price = levels.get("stop_price")
    if stop_price is not None:
        if p <= float(stop_price):
            return "sell"

    # 2) effective exits (trail/dampen) — optional
    ex = levels.get("exit")
    sell_lock_price = levels.get("sell_lock_price") or levels.get("sell_price")
    if isinstance(ex, dict) and bool(ex.get("enabled")) and bool(ex.get("triggered")):
        try:
            if sell_lock_price is not None and p < float(sell_lock_price):
                return "hold"
        except (TypeError, ValueError) as exc:
            logger.warning("[PINGPONG] effective exits (trail/dampen): %s", exc, exc_info=True)
        return "sell"

    # 3) take-profit band (classic)
    sell_price = levels.get("sell_price")
    if sell_price is not None:
        if p >= float(sell_price):
            return "sell"

    return "hold"
