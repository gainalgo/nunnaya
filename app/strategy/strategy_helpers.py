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
    """신호(체결 전) 알림은 선택적으로만 발송한다.

    실제 체결 알림은 order_state_machine의 [BUY]/[SELL] 알림을 기준으로 본다.
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


# ── LongHold 전환 지원 ──────────────────────────────────────────
_LONGHOLD_PATH = os.path.join("runtime", "longhold_config.json")
# [2026-03-15] 공유 락으로 통일 — ladder_manager와 동일한 락 사용
from app.core.longhold_file_lock import longhold_file_lock as _longhold_write_lock


def _check_btc_regime_for_longhold() -> bool:
    """BTC 국면이 LongHold 전환에 적합한지 확인.

    TREND / RECOVERY → True (회복 가능성)
    SHOCK / DRIFT    → False (추가 하락 위험)
    """
    try:
        from app.monitor.btc_leading_signal import get_btc_leading_detector
        det = get_btc_leading_detector()
        if det is None:
            return True  # detector 없으면 보수적으로 LongHold
        regime = det.get_regime_for_lightning()
        return regime in ("TREND", "RECOVERY")
    except (ImportError, AttributeError, TypeError):
        logger.warning("[LongHold] BTC regime 확인 실패 → 보수적 LongHold 유지", exc_info=True)
        return True


def _register_longhold(market: str, strategy_name: str, entry_price: float,
                        current_price: float) -> bool:
    """runtime/longhold_config.json 에 마켓을 LongHold로 등록.

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
                    logger.warning("[LongHold] longhold_config.json 읽기 실패 (_register)", exc_info=True)
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

            # 이미 등록된 마켓이면 중복 등록/알림 방지
            cur = store["markets"].get(market, {})
            if isinstance(cur, dict) and cur.get("enabled", False):
                return False

            now = time.time()
            cur = {}  # 새로 등록
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
        logger.warning("[LongHold] 등록 실패: %s", market if 'market' in dir() else '?', exc_info=True)
        return False


def _try_convert_to_longhold(ctx, market: str, strategy_name: str,
                              entry_price: float, current_price: float,
                              meta: dict) -> "Decision | None":
    """SL 확인 후 LongHold 전환 시도. 성공하면 hold Decision 반환, 실패하면 None."""
    if not _check_btc_regime_for_longhold():
        meta["longhold_skip"] = "btc_bear"
        return None  # BTC 하락 국면 → 정상 SL 매도

    # current_price가 0이면 시세 보정
    if current_price == 0:
        try:
            # ctx에서 price 추출
            current_price = float(getattr(ctx, "price", 0)) or float(getattr(ctx, "last_price", 0))
            if not current_price:
                # position에서 avg_price 등
                pos = getattr(ctx, "position", None)
                if pos:
                    current_price = float(pos.get("avg_price", 0) or pos.get("entry", 0) or pos.get("price", 0))
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[STRAT_HELP] position에서 avg_price 등: %s", exc, exc_info=True)
    # 이미 등록된 마켓이면 중복 알림/등록 방지
    import json as _json
    try:
        with _longhold_write_lock:
            store = {}
            if os.path.exists(_LONGHOLD_PATH):
                try:
                    with open(_LONGHOLD_PATH, "r", encoding="utf-8") as f:
                        store = _json.load(f)
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    logger.warning("[LongHold] longhold_config.json 읽기 실패 (_try_convert)", exc_info=True)
                    store = {}
            if isinstance(store.get("markets"), dict) and market in store["markets"] and store["markets"][market].get("enabled", False):
                return None  # 이미 등록됨
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[STRAT_HELP] 이미 등록된 마켓이면 중복 알림/등록 방지: %s", exc, exc_info=True)

    if _register_longhold(market, strategy_name, entry_price, current_price):
        ctx.set_var("longhold_converted", True)
        ctx.set_var("longhold_convert_ts", time.time())
        meta["longhold"] = True
        meta["longhold_reason"] = f"SL→LongHold ({strategy_name})"
        # #3 이중 안전: context controls에도 user_sell_only 설정
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
                f"🔒 LongHold 전환: {market}\n"
                f"전략: {strategy_name}\n"
                f"진입가: {entry_price:,.0f} → 현재가: {current_price:,.0f}\n"
                f"BTC 국면 양호 → 손절 대신 장기보유 전환"
            )
        except (AttributeError, TypeError, ValueError) as _tg_err:
            logger.warning("[LongHold] telegram notify failed: %s", _tg_err, exc_info=True)
        return Decision(signal="hold", reason=f"{strategy_name.lower()}:longhold_convert", meta=meta)
    return None


def _unregister_longhold(market: str) -> bool:
    """runtime/longhold_config.json 에서 마켓 제거."""
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
        logger.warning("[LongHold] unregister 실패", exc_info=True)
        return False


def _calc_longhold_release_pct(ctx, entry_price: float) -> float:
    """[2026-05-30] ATR 기반 동적 LongHold 회복 임계 계산.

    부모님 결단: "ATR 동적 (자동 적응)" — 코인 변동성에 따라 회복 임계 자동 조절.
    - 변동성 큰 코인 (예: LINK) → 큰 회복 폭 필요 (휩쏘 회복 X)
    - 변동성 작은 코인 (예: BTC) → 작은 회복도 의미 있음

    산식: release_pct = ATR%(14) × 1.5 (multiplier)
    Clamp: min 1.0% (너무 빠른 해제 방지) ~ max 8.0% (영원 묶임 방지)
    Fallback: 데이터 부족 시 2.0% (옛 hardcoded default)
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
        logger.warning("[LongHold] ATR 회복 임계 계산 실패 → default %.1f%% 사용", _DEFAULT_PCT, exc_info=True)
        return _DEFAULT_PCT


def _check_longhold_recovery(ctx, pos, price: float, strategy_name: str) -> bool:
    """LongHold 코인이 진입가 이상으로 회복했는지 확인.

    회복 시 LongHold 플래그 해제 + longhold_config에서 제거 → True 반환.
    전략이 정상 운영 재개하도록 한다.

    [2026-05-30] 임계값 = ATR 동적 (부모님 결단). _calc_longhold_release_pct 참고.
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
        # [2026-05-30] ATR 동적 임계 (부모님 결단 — 코인 변동성에 적응)
        _LONGHOLD_RELEASE_PCT = _calc_longhold_release_pct(ctx, entry)
        if profit_pct >= _LONGHOLD_RELEASE_PCT:
            market = str(getattr(ctx, "market", "") or "")
            ctx.set_var("longhold_converted", False)
            ctx.set_var("longhold_convert_ts", 0)
            _unregister_longhold(market)
            # user_sell_only 해제 — 정상 자동매매 복귀
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
                    f"🔓 LongHold 자동 해제: {market}\n"
                    f"전략: {strategy_name}\n"
                    f"진입가: {entry:,.0f} → 현재가: {price:,.0f} ({profit_pct:+.1f}%)\n"
                    f"ATR 동적 임계: {_LONGHOLD_RELEASE_PCT:.2f}% 도달\n"
                    f"가격 회복 → 정상 전략 운영 복귀"
                )
            except (AttributeError, TypeError, ValueError) as _tg_err:
                logger.warning("[LongHold] recovery telegram failed: %s", _tg_err, exc_info=True)
            return True  # 해제됨 → 전략 정상 진행
    except (KeyError, AttributeError, TypeError, ValueError) as _rec_err:
        logger.warning("[LongHold] recovery check error for %s: %s", getattr(ctx, "market", "?"), _rec_err)
    return False  # 아직 미회복 → LongHold 유지


def _restore_longhold_flag_from_config(ctx) -> bool:
    """서버 재시작 시 longhold_config.json에서 ctx 플래그 복원.

    in-memory longhold_converted가 False인데 config에 등록되어 있으면
    플래그를 복원. 중복 _try_convert_to_longhold 호출 방지.
    """
    if ctx.get_var("longhold_converted", False):
        return True  # 이미 설정됨
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
    """Night Mode 활성 시 SL을 넓혀서 일시 하락에 조기 손절 방지.

    예: sl_pct=-2.5, multiplier=1.5 → -3.75%
    SL은 음수이므로 multiplier를 곱하면 더 넓어짐.
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
        # sl_pct는 음수 (-2.5) → 더 넓히려면 절대값을 키움
        return -(abs(sl_pct) * mult)
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[NightMode] SL 조정 실패", exc_info=True)
        return sl_pct


def adjust_order_amount_and_price(amount: float, price: float, market: str = "BTCUSDT") -> Tuple[float, float]:
    """거래소 조건에 맞게 주문 금액과 가격 단위를 자동 보정."""
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
            logger.warning("[Telemetry] ctx.market 접근 실패", exc_info=True)
            mkt = ""
        if not mkt:
            try:
                mkt = str(getattr(ctx, "code", "") or "")
            except (KeyError, AttributeError, TypeError):
                logger.warning("[Telemetry] ctx.code 접근 실패", exc_info=True)
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
    # [2026-03-07] neutral zone(45~55) 게이트와 rsi_low_static 동기화
    # SNIPER(S)는 RSI 42~48까지 허용하므로 neutral zone과 겹침.
    # rsi_low_static >= neutral_min이면 전략이 명시적으로 중립 RSI를 허용한 것이므로
    # neutral zone 차단을 건너뛴다.
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
# Helper: Unified Buy Timing (통일 매수 타이밍)
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
# Helper: Regime Detection (국면 감지 — PP/AL 공용)
# ----------------------------------------------------------------------
def _detect_regime(history: list[float], fast: int = 20, slow: int = 60) -> str:
    """EMA spread 기반 간단 국면 판별.

    Returns: "TREND" | "RANGE" | "UNKNOWN"
    - |spread| >= 0.4% → TREND (추세)
    - |spread| < 0.4%  → RANGE (횡보)
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
    """히스테리시스: 동일 국면이 N회 연속이면 True 반환.

    ctx.set_var 기반으로 연속 카운트를 추적한다.
    쿨다운(30분): 마지막 전환 이후 30분간 재전환 차단.
    """
    key_dir = f"{prefix}_regime_dir"
    key_cnt = f"{prefix}_regime_cnt"
    key_ts = f"{prefix}_regime_switch_ts"
    try:
        prev_dir = str(ctx.get_var(key_dir) or "")
        prev_cnt = int(ctx.get_var(key_cnt) or 0)
        switch_ts = float(ctx.get_var(key_ts) or 0.0)
    except (TypeError, ValueError):
        logger.warning("[Regime] 히스테리시스 상태 파싱 실패: prefix=%s", prefix, exc_info=True)
        prev_dir, prev_cnt, switch_ts = "", 0, 0.0

    if regime == prev_dir:
        cnt = prev_cnt + 1
    else:
        cnt = 1
        # 쿨다운: 30분 이내 재전환 차단
        if switch_ts > 0 and (time.time() - switch_ts) < 1800:
            try:
                ctx.set_var(key_cnt, 0)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[STRAT_HELP] 쿨다운: 30분 이내 재전환 차단: %s", exc, exc_info=True)
            return False

    try:
        ctx.set_var(key_dir, regime)
        ctx.set_var(key_cnt, cnt)
        if cnt >= required and prev_dir != regime:
            ctx.set_var(key_ts, time.time())
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        logger.warning("[STRAT_HELP] 쿨다운: 30분 이내 재전환 차단: %s", exc, exc_info=True)

    return cnt >= required


def _is_breakout(history: list[float], price: float) -> bool:
    """돌파 감지: 볼린저 상단 + 상승 모멘텀 동시 충족.

    PP 포지션 보유 중 돌파 시 밴드 익절을 억제하고 trailing으로 전환하기 위한 판단.
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
    # 상승 모멘텀: 최근 5틱 상승 + EMA20 위
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
    """ATR 기반 동적 TP/SL 계산 및 Lock 처리.

    tp_sl_mode="manual"이면 전체 건너뜀 (사용자 고정값 보호).
    tp_sl_mode="auto"(기본)이면 ATR 기반으로 동적 계산.
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

    # ── GreenPen Cycle TP/SL 훅 (greenpen_enabled=True 일 때만) ──
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
            pass  # GreenPen import 실패 시 기존 로직 유지


# ── 공통 DCA 헬퍼 (PINGPONG/AUTOLOOP/LIGHTNING/CONTRARIAN 공유) ──
def _common_dca_check(
    ctx, price: float, entry_price: float, params: dict,
    strategy_prefix: str, meta: dict,
) -> "Decision | None":
    """
    공통 DCA 물타기 로직.  SNIPER DCA의 간소화 버전.

    [2026-05-30] 부모님 결단 4️⃣ "C 강화 (LongHold 정신 일치)" — 옛 4단계/깊이 2% → 8단계/4%
    - 묶임 견딤 정신: SL 까지 가는 도중 더 깊이 견디며 평단가 낮춤
    - 6️⃣ budget cap (plugin budget) 자동 안전망 → over-allocation 차단

    Defaults (강화):
    - dca_step_pct: 0.5% 유지 (자주 발동)
    - dca_add_ratio: 0.25 (옛 0.30 → 약간 ↓, 단계 늘어서 사이즈 분산)
    - dca_max_depth_pct: 4.0% (옛 2.0 → 2배, 깊이 견딤)
    - pyramid 1.0 → 2.5 max (옛 2.0), 단계당 0.20 증가 (옛 0.25, 완만하게)
    - 자동 단계 수 = depth/step = 8 (옛 4)
    반환: Decision("buy") 또는 None (DCA 해당 없음)
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

    # [2026-05-30] 피라미드 배율 완만하게 — 횟수마다 20% 증가, 최대 2.5배 (옛 25% / 2.0)
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
    """DCA 상태 초기화 (새 포지션 진입 시)."""
    try:
        ctx.set_var(f"{strategy_prefix}_dca_count", 0)
        ctx.set_var(f"{strategy_prefix}_dca_initial_entry", 0.0)
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning("[STRAT_HELP] strategy_helpers._reset_dca_state fallback: %s", exc, exc_info=True)
