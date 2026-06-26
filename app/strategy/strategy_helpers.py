# ============================================================
# File: app/strategy/strategy_helpers.py
# Autocoin OS v3-H — Strategy Helper Functions
# ------------------------------------------------------------
# Extracted from strategy_plugins.py
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import statistics
import threading
import time
from typing import Any, Dict, Literal, Optional, Tuple

import json
logger = logging.getLogger(__name__)

from app.strategy import indicators
from app.core.currency import Q
from app.strategy.strategy_base import Decision, StrategyPlugin, Signal

try:
    from app.notify.telegram import send_telegram
except ImportError:
    logging.getLogger(__name__).warning("telegram module not available, send_telegram disabled", exc_info=True)
    def send_telegram(msg): pass


_SIGNAL_TELEGRAM_ENABLED = str(os.getenv("OMA_TELEGRAM_SIGNAL_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")


def send_signal_telegram(msg: str) -> None:
    """Signal (pre-fill) notifications are sent only optionally.

    Actual fill notifications are based on order_state_machine's [BUY]/[SELL] alerts.
    """
    if not _SIGNAL_TELEGRAM_ENABLED:
        return
    try:
        send_telegram(str(msg))
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning("[STRAT_HELP] strategy_helpers.send_signal_telegram except-> return: %s", exc, exc_info=True)
        return

try:
    from app.manager.reserved_queue import reserved_queue
except ImportError:
    logging.getLogger(__name__).warning("reserved_queue module not available", exc_info=True)
    reserved_queue = None

try:
    from app.ai.coin_tiers import adjust_ai_score_for_strategy, get_regime_fit
except ImportError:
    logging.getLogger(__name__).warning("coin_tiers module not available, using fallback", exc_info=True)
    def adjust_ai_score_for_strategy(ai_score, strategy=None, regime=None):
        return {
            "adjusted_score": ai_score,
            "should_buy": ai_score >= 0.4,
            "should_sell": ai_score <= 0.3,
            "tp_scale": 1.0,
            "sl_scale": 1.0,
            "confidence": 0.5,
        }
    def get_regime_fit(regime, strategy=None):
        return 0.5

try:
    from app.manager.online_calibrator import get_calibrator as _get_calibrator
except ImportError:
    logging.getLogger(__name__).warning("online_calibrator module not available", exc_info=True)
    _get_calibrator = None


# ── LongHold conversion support ──────────────────────────────────────────
_LONGHOLD_PATH = os.path.join("runtime", "longhold_config.json")
# [2026-03-15] Unified to a shared lock — same lock as ladder_manager
from app.core.longhold_file_lock import longhold_file_lock as _longhold_write_lock


def _check_btc_regime_for_longhold() -> bool:
    """Check whether the BTC regime is suitable for LongHold conversion.

    TREND / RECOVERY → True (recovery possible)
    SHOCK / DRIFT    → False (further downside risk)
    """
    try:
        from app.monitor.btc_leading_signal import get_btc_leading_detector
        det = get_btc_leading_detector()
        if det is None:
            return True  # No detector → conservatively LongHold
        regime = det.get_regime_for_lightning()
        return regime in ("TREND", "RECOVERY")
    except (ImportError, AttributeError, TypeError):
        logger.warning("[LongHold] BTC regime check failed → keeping conservative LongHold", exc_info=True)
        return True


def _register_longhold(market: str, strategy_name: str, entry_price: float,
                        current_price: float) -> bool:
    """Register the market as LongHold in runtime/longhold_config.json.

    Returns True if registration succeeded.
    """
    import json as _json
    try:
        with _longhold_write_lock:
            store: dict = {}
            if os.path.exists(_LONGHOLD_PATH):
                try:
                    with open(_LONGHOLD_PATH, "r", encoding="utf-8") as f:
                        store = _json.load(f)
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    logger.warning("[LongHold] failed to read longhold_config.json (_register)", exc_info=True)
                    store = {}

            if not isinstance(store.get("defaults"), dict):
                store["defaults"] = {
                    "enabled": True,
                    "strategy": "LADDER",
                    "target_profit_pct": 50.0,
                    "trailing_stop_pct": 2.0,
                    "notify_cooldown_sec": 3600,
                    "auto_sell_check_interval_min": 10,
                    "min_position_usdt": 10,
                    "budget_usdt": 0,
                    "repeat": True,
                    "auto_sell_on_target": False,
                }
            if not isinstance(store.get("markets"), dict):
                store["markets"] = {}
            if not isinstance(store.get("history"), list):
                store["history"] = []

            # Already-registered market → prevent duplicate registration/notification
            cur = store["markets"].get(market, {})
            if isinstance(cur, dict) and cur.get("enabled", False):
                return False

            now = time.time()
            cur = {}  # New registration
            cur.update({
                "enabled": True,
                "strategy": "LADDER",
                "note": f"SL→LongHold ({strategy_name}, entry={entry_price:.1f}, sl_price={current_price:.1f})",
                "sl_converted": True,
                "original_strategy": strategy_name,
                "entry_price_at_convert": entry_price,
                "convert_price": current_price,
                "updated_ts": now,
            })
            cur["created_ts"] = now

            store["markets"][market] = cur

            # history append (bounded)
            store["history"].append({
                "ts": now,
                "event": "SL_TO_LONGHOLD",
                "market": market,
                "strategy": strategy_name,
                "entry_price": entry_price,
                "convert_price": current_price,
            })
            if len(store["history"]) > 400:
                store["history"] = store["history"][-400:]

            from app.core.io_utils import safe_write_json
            safe_write_json(_LONGHOLD_PATH, store)
        return True
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
        logger.warning("[LongHold] registration failed: %s", market if 'market' in dir() else '?', exc_info=True)
        return False


def _try_convert_to_longhold(ctx, market: str, strategy_name: str,
                              entry_price: float, current_price: float,
                              meta: dict) -> "Decision | None":
    """Attempt LongHold conversion after SL check. Returns a hold Decision on success, None on failure."""
    if not _check_btc_regime_for_longhold():
        meta["longhold_skip"] = "btc_bear"
        return None  # BTC downtrend regime → normal SL sell

    # If current_price is 0, fix up the quote
    if current_price == 0:
        try:
            # Extract price from ctx
            current_price = float(getattr(ctx, "price", 0)) or float(getattr(ctx, "last_price", 0))
            if not current_price:
                # avg_price etc. from position
                pos = getattr(ctx, "position", None)
                if pos:
                    current_price = float(pos.get("avg_price", 0) or pos.get("entry", 0) or pos.get("price", 0))
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[STRAT_HELP] avg_price etc. from position: %s", exc, exc_info=True)
    # Already-registered market → prevent duplicate notification/registration
    import json as _json
    try:
        with _longhold_write_lock:
            store = {}
            if os.path.exists(_LONGHOLD_PATH):
                try:
                    with open(_LONGHOLD_PATH, "r", encoding="utf-8") as f:
                        store = _json.load(f)
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    logger.warning("[LongHold] failed to read longhold_config.json (_try_convert)", exc_info=True)
                    store = {}
            if isinstance(store.get("markets"), dict) and market in store["markets"] and store["markets"][market].get("enabled", False):
                return None  # Already registered
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[STRAT_HELP] already-registered market duplicate-guard: %s", exc, exc_info=True)

    if _register_longhold(market, strategy_name, entry_price, current_price):
        ctx.set_var("longhold_converted", True)
        ctx.set_var("longhold_convert_ts", time.time())
        meta["longhold"] = True
        meta["longhold_reason"] = f"SL→LongHold ({strategy_name})"
        # #3 Double safety: also set user_sell_only on context controls
        try:
            from app.manager.market_controls import apply_engine_controls
            _sys = getattr(ctx, "system", None)
            if _sys:
                apply_engine_controls(_sys, market, strategy_name,
                                      user_sell_only=True)
        except (KeyError, AttributeError, TypeError) as _lh_err:
            logger.warning("[LongHold] engine controls apply failed for %s: %s", market, _lh_err)
        try:
            send_telegram(
                f"🔒 LongHold conversion: {market}\n"
                f"Strategy: {strategy_name}\n"
                f"Entry: {entry_price:,.0f} → Current: {current_price:,.0f}\n"
                f"BTC regime favorable → convert to long-term hold instead of stop-loss"
            )
        except (AttributeError, TypeError, ValueError) as _tg_err:
            logger.warning("[LongHold] telegram notify failed: %s", _tg_err, exc_info=True)
        return Decision(signal="hold", reason=f"{strategy_name.lower()}:longhold_convert", meta=meta)
    return None


def _unregister_longhold(market: str) -> bool:
    """Remove the market from runtime/longhold_config.json."""
    import json as _json
    try:
        with _longhold_write_lock:
            if not os.path.exists(_LONGHOLD_PATH):
                return False
            with open(_LONGHOLD_PATH, "r", encoding="utf-8") as f:
                store = _json.load(f)
            if not isinstance(store.get("markets"), dict):
                return False
            if market not in store["markets"]:
                return False
            del store["markets"][market]
            if not isinstance(store.get("history"), list):
                store["history"] = []
            store["history"].append({
                "ts": time.time(),
                "event": "LONGHOLD_AUTO_RELEASE",
                "market": market,
            })
            if len(store["history"]) > 400:
                store["history"] = store["history"][-400:]
            from app.core.io_utils import safe_write_json
            safe_write_json(_LONGHOLD_PATH, store)
        return True
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
        logger.warning("[LongHold] unregister failed", exc_info=True)
        return False


def _calc_longhold_release_pct(ctx, entry_price: float) -> float:
    """[2026-05-30] Compute the dynamic ATR-based LongHold recovery threshold.

    Owner's decision: "ATR dynamic (auto-adaptive)" — recovery threshold auto-adjusts to coin volatility.
    - High-volatility coin (e.g. LINK) → larger recovery margin needed (no whipsaw recovery)
    - Low-volatility coin (e.g. BTC) → even a small recovery is meaningful

    Formula: release_pct = ATR%(14) × 1.5 (multiplier)
    Clamp: min 1.0% (prevent too-early release) ~ max 8.0% (prevent being locked forever)
    Fallback: 2.0% when data is insufficient (old hardcoded default)
    """
    _DEFAULT_PCT = 2.0
    _MULT = 1.5
    _MIN_PCT = 1.0
    _MAX_PCT = 8.0
    try:
        from app.strategy import indicators
        history = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
        if not history or len(history) < 15 or entry_price <= 0:
            return _DEFAULT_PCT
        atr = indicators.atr_simplified(history)
        if not atr or atr <= 0:
            return _DEFAULT_PCT
        atr_pct = (float(atr) / float(entry_price)) * 100.0
        release_pct = atr_pct * _MULT
        return max(_MIN_PCT, min(_MAX_PCT, release_pct))
    except (KeyError, AttributeError, TypeError, ValueError, ZeroDivisionError):
        logger.warning("[LongHold] ATR recovery threshold computation failed → using default %.1f%%", _DEFAULT_PCT, exc_info=True)
        return _DEFAULT_PCT


def _check_longhold_recovery(ctx, pos, price: float, strategy_name: str) -> bool:
    """Check whether a LongHold coin has recovered to at or above its entry price.

    On recovery, clear the LongHold flag + remove from longhold_config → return True.
    Lets the strategy resume normal operation.

    [2026-05-30] Threshold = ATR dynamic (owner's decision). See _calc_longhold_release_pct.
    """
    try:
        entry = 0.0
        if isinstance(pos, dict):
            entry = float(pos.get("entry") or pos.get("avg_price") or pos.get("entry_price") or 0)
        else:
            entry = float(getattr(pos, "entry", 0) or getattr(pos, "avg_price", 0) or 0)
        if entry <= 0:
            return False

        profit_pct = (price - entry) / entry * 100.0
        # [2026-05-30] ATR dynamic threshold (owner's decision — adapts to coin volatility)
        _LONGHOLD_RELEASE_PCT = _calc_longhold_release_pct(ctx, entry)
        if profit_pct >= _LONGHOLD_RELEASE_PCT:
            market = str(getattr(ctx, "market", "") or "")
            ctx.set_var("longhold_converted", False)
            ctx.set_var("longhold_convert_ts", 0)
            _unregister_longhold(market)
            # Release user_sell_only — resume normal automated trading
            try:
                from app.manager.market_controls import apply_engine_controls
                _sys = getattr(ctx, "system", None)
                if _sys:
                    apply_engine_controls(_sys, market, strategy_name,
                                          user_sell_only=False)
            except (KeyError, AttributeError, TypeError) as _lh_err:
                logger.warning("[LongHold] recovery controls restore failed for %s: %s", market, _lh_err)
            try:
                send_telegram(
                    f"🔓 LongHold auto-release: {market}\n"
                    f"Strategy: {strategy_name}\n"
                    f"Entry: {entry:,.0f} → Current: {price:,.0f} ({profit_pct:+.1f}%)\n"
                    f"ATR dynamic threshold: {_LONGHOLD_RELEASE_PCT:.2f}% reached\n"
                    f"Price recovered → resume normal strategy operation"
                )
            except (AttributeError, TypeError, ValueError) as _tg_err:
                logger.warning("[LongHold] recovery telegram failed: %s", _tg_err, exc_info=True)
            return True  # Released → strategy proceeds normally
    except (KeyError, AttributeError, TypeError, ValueError) as _rec_err:
        logger.warning("[LongHold] recovery check error for %s: %s", getattr(ctx, "market", "?"), _rec_err)
    return False  # Not yet recovered → keep LongHold


def _restore_longhold_flag_from_config(ctx) -> bool:
    """Restore the ctx flag from longhold_config.json on server restart.

    If in-memory longhold_converted is False but it is registered in config,
    restore the flag. Prevents duplicate _try_convert_to_longhold calls.
    """
    if ctx.get_var("longhold_converted", False):
        return True  # Already set
    market = str(getattr(ctx, "market", "") or "").strip().upper()
    if not market:
        return False
    import json as _json
    try:
        if not os.path.exists(_LONGHOLD_PATH):
            return False
        with _longhold_write_lock:
            with open(_LONGHOLD_PATH, "r", encoding="utf-8") as f:
                store = _json.load(f)
        cfg = (store.get("markets") or {}).get(market)
        if isinstance(cfg, dict) and cfg.get("sl_converted") and cfg.get("enabled", True):
            ctx.set_var("longhold_converted", True)
            ctx.set_var("longhold_convert_ts", cfg.get("ts", 0))
            return True
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as _cfg_err:
        logger.warning("[LongHold] config restore error: %s", _cfg_err, exc_info=True)
    return False


def _night_mode_adjust_sl(sl_pct: float, ctx: Any) -> float:
    """When Night Mode is active, widen SL to avoid early stop-loss on temporary dips.

    e.g. sl_pct=-2.5, multiplier=1.5 → -3.75%
    SL is negative, so multiplying by the multiplier widens it further.
    """
    try:
        system = getattr(ctx, "system", None)
        if system is None:
            return sl_pct
        if not getattr(system, 'night_mode_enabled', False):
            return sl_pct
        if not system.is_night_mode_active():
            return sl_pct
        mult = float(getattr(system, 'night_mode_sl_multiplier', 1.5) or 1.5)
        if mult <= 1.0:
            return sl_pct
        # sl_pct is negative (-2.5) → to widen, grow the absolute value
        return -(abs(sl_pct) * mult)
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[NightMode] SL adjustment failed", exc_info=True)
        return sl_pct


def adjust_order_amount_and_price(amount: float, price: float, market: str = "BTCUSDT") -> Tuple[float, float]:
    """Auto-adjust order amount and price tick to match exchange constraints."""
    from app.integrations.bybit_trade import adjust_price_to_tick
    amount = max(amount, Q.min_order)
    price = adjust_price_to_tick(price)
    return amount, price

# ----------------------------------------------------------------------
# Helper: Candle 1m Volume/Notional Injection (AI Training Telemetry)
# ----------------------------------------------------------------------
def _inject_candle_1m_telemetry(ctx: Any, telemetry: Dict[str, Any]) -> None:
    """Inject candle.1m accumulators into telemetry if available.
    Adds:
      - notional_quote_1m (candle_acc_trade_price)
      - vol_base_1m (candle_acc_trade_volume)
      - candle_1m_dt_utc (candle_date_time_utc)
    Also sets telemetry['volume'] as vol_base_1m for backward compatibility.
    """
    try:
        mkt = ""
        try:
            mkt = str(getattr(ctx, "market", "") or "")
        except (KeyError, AttributeError, TypeError):
            logger.warning("[Telemetry] failed to access ctx.market", exc_info=True)
            mkt = ""
        if not mkt:
            try:
                mkt = str(getattr(ctx, "code", "") or "")
            except (KeyError, AttributeError, TypeError):
                logger.warning("[Telemetry] failed to access ctx.code", exc_info=True)
                mkt = ""
        if not mkt:
            return
        try:
            from app.core.hyper_price_store import price_store  # type: ignore
        except (ImportError, AttributeError, TypeError) as exc:
            logger.warning("[STRAT_HELP] strategy_helpers._inject_candle_1m_telemetry except-> return: %s", exc, exc_info=True)
            return

        get_notional = getattr(price_store, "get_candle_1m_notional", None)
        get_vol = getattr(price_store, "get_candle_1m_volume", None)
        get_dt = getattr(price_store, "get_candle_1m_dt_utc", None)

        if callable(get_notional):
            telemetry["notional_quote_1m"] = float(get_notional(mkt, 0.0) or 0.0)
        if callable(get_vol):
            v = float(get_vol(mkt, 0.0) or 0.0)
            telemetry["vol_base_1m"] = v
            telemetry.setdefault("volume", v)
        if callable(get_dt):
            telemetry["candle_1m_dt_utc"] = str(get_dt(mkt) or "")
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[STRAT_HELP] strategy_helpers._inject_candle_1m_telemetry except-> return: %s", exc, exc_info=True)
        return


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    arr = sorted(float(v) for v in values)
    if not arr:
        return None
    q = max(0.0, min(1.0, float(q)))
    if len(arr) == 1:
        return arr[0]
    idx = q * (len(arr) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(arr) - 1)
    frac = idx - lo
    return arr[lo] * (1.0 - frac) + arr[hi] * frac


def _ema_series(values: list[float], length: int) -> list[float]:
    if not values:
        return []
    length = max(1, int(length))
    k = 2.0 / (length + 1.0)
    out: list[float] = []
    ema_val = float(values[0])
    for v in values:
        ema_val = (float(v) * k) + (ema_val * (1.0 - k))
        out.append(ema_val)
    return out


def _rsi_series(values: list[float], length: int = 14) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    length = max(2, int(length))
    if n < length + 1:
        return out
    deltas = [float(values[i]) - float(values[i - 1]) for i in range(1, n)]
    seed = deltas[:length]
    gains = [d for d in seed if d > 0]
    losses = [-d for d in seed if d < 0]
    avg_gain = sum(gains) / float(length)
    avg_loss = sum(losses) / float(length)

    def _to_rsi(g: float, l: float) -> float:
        if l <= 0:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    out[length] = _to_rsi(avg_gain, avg_loss)
    for i in range(length, len(deltas)):
        ch = deltas[i]
        up = ch if ch > 0 else 0.0
        dn = -ch if ch < 0 else 0.0
        avg_gain = ((avg_gain * (length - 1)) + up) / float(length)
        avg_loss = ((avg_loss * (length - 1)) + dn) / float(length)
        out[i + 1] = _to_rsi(avg_gain, avg_loss)
    return out


def _macd_turn_snapshot(
    history: list[float],
    price: float,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    zero_band_pct: float = 0.004,
) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "ready": False,
        "macd": 0.0,
        "signal": 0.0,
        "hist": 0.0,
        "hist_slope": 0.0,
        "cross_up": False,
        "zero_near": False,
        "turn_up": False,
    }
    fast = max(2, int(fast))
    slow = max(fast + 1, int(slow))
    signal = max(2, int(signal))
    if len(history) < (slow + signal + 3):
        return meta

    fast_series = _ema_series(history, fast)
    slow_series = _ema_series(history, slow)
    n = min(len(fast_series), len(slow_series))
    if n < signal + 3:
        return meta

    fast_tail = fast_series[-n:]
    slow_tail = slow_series[-n:]
    macd_series = [f - s for f, s in zip(fast_tail, slow_tail)]
    signal_series = _ema_series(macd_series, signal)
    m = min(len(macd_series), len(signal_series))
    if m < 3:
        return meta

    macd_vals = macd_series[-m:]
    sig_vals = signal_series[-m:]
    hist_vals = [a - b for a, b in zip(macd_vals, sig_vals)]
    macd_now = float(macd_vals[-1])
    sig_now = float(sig_vals[-1])
    hist_now = float(hist_vals[-1])
    hist_prev = float(hist_vals[-2])
    hist_slope = hist_now - hist_prev
    cross_up = macd_vals[-1] > sig_vals[-1] and macd_vals[-2] <= sig_vals[-2]
    zero_near = abs(macd_now) <= (abs(float(price)) * float(zero_band_pct)) if float(price) > 0 else False
    turn_up = hist_slope > 0 and (cross_up or zero_near)

    meta.update({
        "ready": True,
        "macd": macd_now,
        "signal": sig_now,
        "hist": hist_now,
        "hist_slope": hist_slope,
        "cross_up": bool(cross_up),
        "zero_near": bool(zero_near),
        "turn_up": bool(turn_up),
    })
    return meta


def _has_bullish_divergence(history: list[float], rsi_seq: list[float | None], lookback: int = 60) -> Tuple[bool, Dict[str, Any]]:
    info: Dict[str, Any] = {"checked": False, "found": False}
    n = len(history)
    if n < 20 or len(rsi_seq) != n:
        return False, info
    start = max(1, n - max(20, int(lookback)))
    lows: list[int] = []
    for i in range(start, n - 1):
        if history[i] <= history[i - 1] and history[i] <= history[i + 1]:
            lows.append(i)
    info["checked"] = True
    info["low_count"] = len(lows)
    if len(lows) < 2:
        return False, info
    i1, i2 = lows[-2], lows[-1]
    r1 = rsi_seq[i1]
    r2 = rsi_seq[i2]
    if r1 is None or r2 is None:
        return False, info
    ok = (history[i2] < history[i1]) and (float(r2) > float(r1) + 1.0)
    info.update({
        "idx_prev": i1,
        "idx_last": i2,
        "price_prev": float(history[i1]),
        "price_last": float(history[i2]),
        "rsi_prev": float(r1),
        "rsi_last": float(r2),
        "found": bool(ok),
    })
    return bool(ok), info


def _reversal_impulse(history: list[float]) -> bool:
    if len(history) < 4:
        return False
    prev_drop = float(history[-2]) < float(history[-3])
    rebound = float(history[-1]) > float(history[-2])
    reclaim = float(history[-1]) >= float(history[-3])
    return bool(prev_drop and rebound and reclaim)


def _evaluate_reversal_buy_guard(
    history: list[float],
    price: float,
    *,
    strategy_tag: str,
    rsi_value: float | None = None,
    neutral_min: float = 45.0,
    neutral_max: float = 55.0,
    rsi_low_static: float = 35.0,
    rsi_dist_lookback: int = 180,
    rsi_low_pct: float = 0.10,
    zscore_threshold: float = -1.2,
    macd_zero_band_pct: float = 0.004,
    min_score: float = 2.0,
    require_macd_turn: bool = True,
    require_extreme_rsi: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    meta: Dict[str, Any] = {
        "reversal_guard": True,
        "reversal_guard_strategy": str(strategy_tag or ""),
    }
    if len(history) < 40:
        meta["reversal_guard_skipped"] = "insufficient_history"
        return True, meta

    rsi_now = float(rsi_value) if rsi_value is not None else float(indicators.rsi(history, 14) or 50.0)
    meta["reversal_rsi"] = round(rsi_now, 3)
    # [2026-03-07] Sync the neutral-zone (45~55) gate with rsi_low_static.
    # SNIPER(S) allows RSI 42~48, so it overlaps the neutral zone.
    # If rsi_low_static >= neutral_min, the strategy has explicitly allowed neutral RSI,
    # so skip the neutral-zone block.
    if float(rsi_low_static) < float(neutral_min):
        if float(neutral_min) < rsi_now < float(neutral_max):
            meta["reversal_blocked"] = "rsi_neutral"
            meta["reversal_rsi_neutral_band"] = [float(neutral_min), float(neutral_max)]
            return False, meta

    rsi_seq = _rsi_series(history, 14)
    rsi_tail = [float(v) for v in rsi_seq[-max(40, int(rsi_dist_lookback)):] if v is not None]
    dyn_low = _percentile(rsi_tail, float(rsi_low_pct)) if len(rsi_tail) >= 20 else None
    if dyn_low is None:
        dyn_low = float(rsi_low_static)
    dyn_low = max(20.0, min(50.0, float(dyn_low)))
    rsi_gate = max(20.0, min(50.0, max(float(rsi_low_static), float(dyn_low))))
    oversold = rsi_now <= rsi_gate
    meta["reversal_rsi_gate"] = round(rsi_gate, 3)
    meta["reversal_oversold"] = bool(oversold)

    bb = indicators.bollinger_bands(history, 20, 2.0)
    bb_touch = False
    zscore = 0.0
    if bb:
        lower = float(bb.get("lower", 0.0) or 0.0)
        mid = float(bb.get("mid", 0.0) or 0.0)
        std = float(bb.get("std", 0.0) or 0.0)
        bb_touch = price <= lower if lower > 0 else False
        if std > 0:
            zscore = (float(price) - mid) / std
    zscore_ok = zscore <= float(zscore_threshold)
    meta["reversal_bb_touch"] = bool(bb_touch)
    meta["reversal_zscore"] = round(zscore, 4)
    meta["reversal_zscore_ok"] = bool(zscore_ok)

    macd_meta = _macd_turn_snapshot(history, price, zero_band_pct=macd_zero_band_pct)
    macd_turn = bool(macd_meta.get("turn_up", False))
    meta["reversal_macd_ready"] = bool(macd_meta.get("ready", False))
    meta["reversal_macd_turn"] = bool(macd_turn)
    meta["reversal_macd_hist_slope"] = round(float(macd_meta.get("hist_slope", 0.0) or 0.0), 6)
    meta["reversal_macd_cross_up"] = bool(macd_meta.get("cross_up", False))
    meta["reversal_macd_zero_near"] = bool(macd_meta.get("zero_near", False))

    divergence_ok, divergence_info = _has_bullish_divergence(history, rsi_seq, lookback=60)
    meta["reversal_divergence"] = bool(divergence_ok)
    if divergence_info:
        meta["reversal_divergence_info"] = divergence_info

    impulse_ok = _reversal_impulse(history)
    meta["reversal_impulse"] = bool(impulse_ok)

    score = 0.0
    if oversold:
        score += 1.0
    if bb_touch or zscore_ok:
        score += 1.0
    if divergence_ok:
        score += 1.0
    if impulse_ok:
        score += 1.0
    if macd_turn:
        score += 1.0
    meta["reversal_score"] = round(score, 3)
    meta["reversal_min_score"] = round(float(min_score), 3)

    if require_extreme_rsi and not oversold:
        meta["reversal_blocked"] = "rsi_not_extreme"
        return False, meta
    if require_macd_turn and not macd_turn:
        meta["reversal_blocked"] = "macd_not_turning"
        return False, meta
    if score < float(min_score):
        meta["reversal_blocked"] = "score_low"
        return False, meta
    return True, meta


# ----------------------------------------------------------------------
# Helper: Unified Buy Timing
# ----------------------------------------------------------------------
def should_buy_global_default(ctx: Any, price: float, params: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """Unified entry check: buy_now, bounce + EMA cross + RSI + momentum + AI."""
    buy_now = bool(params.get("buy_now", False))
    history = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
    meta: Dict[str, Any] = {}
    if buy_now:
        meta["buy_now"] = True
        return True, meta
    if len(history) >= 6:
        recent = history[-3:]
        lowest = min(recent)
        bounce_pct = ((price - lowest) / lowest * 100.0) if lowest > 0 else 0.0
        ema5 = indicators.ema(history, 5)
        ema12 = indicators.ema(history, 12)
        ema20 = indicators.ema(history, 20)
        ema_cross = ema5 > ema12 and ema12 > ema20
        rsi = indicators.rsi(history, 14)
        momentum = (price - history[-3]) / history[-3] * 100.0 if history[-3] > 0 else 0.0
        ai_score = 0.5
        if hasattr(ctx, "current_ai") and isinstance(ctx.current_ai, dict):
            brain = ctx.current_ai.get("brain", {})
            ai_score = float(brain.get("ai_prediction", 0.5))
        bounce_min = float(params.get("bounce_pct_min", 0.3))
        rsi_min = int(params.get("rsi_min", 30))
        rsi_max = int(params.get("rsi_max", 40))
        mom_min = float(params.get("momentum_min", 0.3))
        ai_min = float(params.get("ai_score_min", 0.7))
        ema_req_raw = params.get("ema_cross_required", True)
        if isinstance(ema_req_raw, str):
            ema_required = ema_req_raw.strip().lower() not in ("0", "false", "no", "off")
        else:
            ema_required = bool(ema_req_raw)
        ema_ok = ema_cross or (not ema_required)
        if bounce_pct >= bounce_min and ema_ok and rsi_min <= rsi <= rsi_max and momentum >= mom_min and ai_score >= ai_min:
            meta.update({
                "bounce_pct": bounce_pct,
                "ema_cross": ema_cross,
                "ema_required": ema_required,
                "rsi": rsi,
                "momentum": momentum,
                "ai_score": ai_score,
            })
            return True, meta
    return False, meta


# ----------------------------------------------------------------------
# Helper: Regime Detection (shared by PP/AL)
# ----------------------------------------------------------------------
def _detect_regime(history: list[float], fast: int = 20, slow: int = 60) -> str:
    """Simple regime classification based on EMA spread.

    Returns: "TREND" | "RANGE" | "UNKNOWN"
    - |spread| >= 0.4% → TREND
    - |spread| < 0.4%  → RANGE
    """
    if not history or len(history) < slow + 5:
        return "UNKNOWN"
    ema_f = indicators.ema(history, fast)
    ema_s = indicators.ema(history, slow)
    if ema_s <= 0:
        return "UNKNOWN"
    spread_pct = abs((ema_f / ema_s - 1.0) * 100.0)
    return "TREND" if spread_pct >= 0.4 else "RANGE"


def _check_regime_hysteresis(ctx: Any, regime: str, prefix: str, required: int = 3) -> bool:
    """Hysteresis: returns True when the same regime occurs N times in a row.

    Tracks the consecutive count via ctx.set_var.
    Cooldown (30 min): blocks re-switching for 30 min after the last switch.
    """
    key_dir = f"{prefix}_regime_dir"
    key_cnt = f"{prefix}_regime_cnt"
    key_ts = f"{prefix}_regime_switch_ts"
    try:
        prev_dir = str(ctx.get_var(key_dir) or "")
        prev_cnt = int(ctx.get_var(key_cnt) or 0)
        switch_ts = float(ctx.get_var(key_ts) or 0.0)
    except (TypeError, ValueError):
        logger.warning("[Regime] failed to parse hysteresis state: prefix=%s", prefix, exc_info=True)
        prev_dir, prev_cnt, switch_ts = "", 0, 0.0

    if regime == prev_dir:
        cnt = prev_cnt + 1
    else:
        cnt = 1
        # Cooldown: block re-switching within 30 min
        if switch_ts > 0 and (time.time() - switch_ts) < 1800:
            try:
                ctx.set_var(key_cnt, 0)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[STRAT_HELP] cooldown: block re-switch within 30 min: %s", exc, exc_info=True)
            return False

    try:
        ctx.set_var(key_dir, regime)
        ctx.set_var(key_cnt, cnt)
        if cnt >= required and prev_dir != regime:
            ctx.set_var(key_ts, time.time())
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        logger.warning("[STRAT_HELP] cooldown: block re-switch within 30 min: %s", exc, exc_info=True)

    return cnt >= required


def _is_breakout(history: list[float], price: float) -> bool:
    """Breakout detection: Bollinger upper band + upward momentum both satisfied.

    Used to suppress band take-profit and switch to trailing on a breakout while holding a PP position.
    """
    if not history or len(history) < 30:
        return False
    window = history[-20:]
    sma = sum(window) / len(window)
    if sma <= 0:
        return False
    std = statistics.stdev(window) if len(window) > 1 else 0.0
    upper = sma + 2.0 * std
    if price < upper:
        return False
    # Upward momentum: last 5 ticks rising + above EMA20
    ema20 = indicators.ema(history, 20)
    if price <= ema20:
        return False
    recent = history[-5:]
    if recent[-1] <= recent[0]:
        return False
    return True


# ----------------------------------------------------------------------
# Helper: ATR Dynamic Limits (TP/SL)
# ----------------------------------------------------------------------
def _apply_atr_dynamic_limits(ctx: Any, params: Dict[str, Any], price: float, history: list[float], meta: Dict[str, Any], prefix: str) -> None:
    """Compute ATR-based dynamic TP/SL and handle locking.

    If tp_sl_mode="manual", skip entirely (protect user fixed values).
    If tp_sl_mode="auto" (default), compute dynamically from ATR.
    """
    if not history or len(history) < 20:
        return

    tp_sl_mode = str(params.get("tp_sl_mode", "auto")).strip().lower()
    if tp_sl_mode == "manual":
        return

    atr_period = int(params.get("atr_period", 14))
    atr_sl_mult = float(params.get("atr_sl_mult", 3.0))
    atr_tp_mult = float(params.get("atr_tp_mult", 3.0))

    dynamic_sl = None
    dynamic_tp = None

    pos = getattr(ctx, "position", None)
    has_pos = (pos is not None and float(pos.get("qty", 0.0) or 0.0) > 0)

    key_sl = f"{prefix}_locked_sl"
    key_tp = f"{prefix}_locked_tp"

    if has_pos:
        dynamic_sl = ctx.get_var(key_sl)
        dynamic_tp = ctx.get_var(key_tp)

        if dynamic_sl is None or dynamic_tp is None:
            atr = indicators.atr_simplified(history, atr_period)
            if atr and price > 0:
                if dynamic_sl is None:
                    dynamic_sl = -abs((atr * atr_sl_mult) / price * 100.0)
                    ctx.set_var(key_sl, dynamic_sl)
                if dynamic_tp is None:
                    dynamic_tp = abs((atr * atr_tp_mult) / price * 100.0)
                    ctx.set_var(key_tp, dynamic_tp)
    else:
        ctx.set_var(key_sl, None)
        ctx.set_var(key_tp, None)
        atr = indicators.atr_simplified(history, atr_period)
        if atr and price > 0:
            dynamic_sl = -abs((atr * atr_sl_mult) / price * 100.0)
            dynamic_tp = abs((atr * atr_tp_mult) / price * 100.0)

    if dynamic_sl is not None:
        meta["dynamic_sl"] = float(dynamic_sl)
    if dynamic_tp is not None:
        meta["dynamic_tp"] = float(dynamic_tp)

    # ── GreenPen Cycle TP/SL hook (only when greenpen_enabled=True) ──
    if bool(params.get("greenpen_enabled", False)) and price > 0:
        try:
            from app.strategy.greenpen.cycle_tp import compute_cycle_targets
            atr_val = indicators.atr_simplified(history, atr_period) if history and len(history) >= atr_period else 0
            if atr_val and atr_val > 0:
                gp = compute_cycle_targets(
                    price, "LONG", atr_val,
                    tp1_mult=float(params.get("greenpen_tp1_mult", 2.5)),
                    tp2_mult=float(params.get("greenpen_tp2_mult", 5.0)),
                    sl_mult=float(params.get("greenpen_sl_mult", 1.0)),
                )
                meta["gp_tp1"] = gp.tp1
                meta["gp_tp2"] = gp.tp2
                meta["gp_sl"] = gp.sl
                meta["gp_rr"] = gp.rr_ratio
                meta["gp_atr"] = atr_val
        except Exception:
            pass  # On GreenPen import failure, keep existing logic


# ── Common DCA helper (shared by PINGPONG/AUTOLOOP/LIGHTNING/CONTRARIAN) ──
def _common_dca_check(
    ctx, price: float, entry_price: float, params: dict,
    strategy_prefix: str, meta: dict,
) -> "Decision | None":
    """
    Common DCA averaging-down logic. A simplified version of SNIPER DCA.

    [2026-05-30] Owner's decision 4️⃣ "Strengthen C (aligned with LongHold spirit)" — old 4 steps / depth 2% → 8 steps / 4%
    - Endure-the-lockup spirit: hold deeper on the way to SL while lowering the average price
    - 6️⃣ budget cap (plugin budget) automatic safety net → blocks over-allocation

    Defaults (strengthened):
    - dca_step_pct: keep 0.5% (triggers often)
    - dca_add_ratio: 0.25 (old 0.30 → slightly ↓, more steps spread the size out)
    - dca_max_depth_pct: 4.0% (old 2.0 → 2x, endure deeper)
    - pyramid 1.0 → 2.5 max (old 2.0), +0.20 per step (old 0.25, gentler)
    - automatic step count = depth/step = 8 (old 4)
    Returns: Decision("buy") or None (DCA not applicable)
    """
    if entry_price <= 0 or price <= 0:
        return None

    dca_enabled = str(params.get(f"{strategy_prefix}_dca_enabled",
                      params.get("dca_enabled", "true"))).strip().lower() in ("1", "true", "yes", "on")
    if not dca_enabled:
        return None

    dca_step_pct = float(params.get(f"{strategy_prefix}_dca_step_pct",
                         params.get("dca_step_pct", 0.5)))
    if dca_step_pct <= 0:
        dca_step_pct = 0.5
    dca_add_ratio = float(params.get(f"{strategy_prefix}_dca_add_ratio",
                          params.get("dca_add_ratio", 0.25)))  # [2026-05-30] 0.30 → 0.25
    dca_max_depth_pct = float(params.get(f"{strategy_prefix}_dca_max_depth_pct",
                              params.get("dca_max_depth_pct", 4.0)))  # [2026-05-30] 2.0 → 4.0

    dca_var = f"{strategy_prefix}_dca_count"
    dca_entry_var = f"{strategy_prefix}_dca_initial_entry"

    dca_count = int(ctx.get_var(dca_var, 0))
    dca_initial_entry = float(ctx.get_var(dca_entry_var, 0.0))
    if dca_initial_entry <= 0:
        dca_initial_entry = entry_price
        ctx.set_var(dca_entry_var, dca_initial_entry)

    max_dca_steps = int(dca_max_depth_pct / dca_step_pct) if dca_step_pct > 0 else 0
    drop_from_initial = ((dca_initial_entry - price) / dca_initial_entry * 100) if dca_initial_entry > 0 else 0.0
    next_dca_level = (dca_count + 1) * dca_step_pct

    # [2026-05-30] Gentler pyramid multiplier — +20% per add, max 2.5x (old 25% / 2.0)
    pyramid_mult = min(1.0 + dca_count * 0.20, 2.5)
    effective_ratio = round(dca_add_ratio * pyramid_mult, 4)

    if (dca_count < max_dca_steps
            and drop_from_initial >= next_dca_level
            and price < dca_initial_entry):
        ctx.set_var(dca_var, dca_count + 1)
        dca_meta = dict(meta)
        dca_meta["buy_reason"] = f"{strategy_prefix}:dca"
        dca_meta["size_scale"] = effective_ratio
        dca_meta["dca_count"] = dca_count + 1
        dca_meta["dca_max_steps"] = max_dca_steps
        dca_meta["dca_initial_entry"] = dca_initial_entry
        dca_meta["dca_drop_pct"] = round(drop_from_initial, 2)
        dca_meta["dca_effective_ratio"] = effective_ratio
        return Decision(signal="buy", reason=f"{strategy_prefix}:dca", meta=dca_meta)

    return None


def _reset_dca_state(ctx, strategy_prefix: str):
    """Reset DCA state (on new position entry)."""
    try:
        ctx.set_var(f"{strategy_prefix}_dca_count", 0)
        ctx.set_var(f"{strategy_prefix}_dca_initial_entry", 0.0)
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning("[STRAT_HELP] strategy_helpers._reset_dca_state fallback: %s", exc, exc_info=True)
