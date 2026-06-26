# ============================================================
# File: app/api/strategy_longshort_router.py
# Phase 1-I file diet extraction from strategy_router.py
#
# LongShort scope endpoints and helpers:
#   - _scope_calc_spread_bps, _scope_calc_depth_notional
#   - _scope_market_flow_guard
#   - longshort_scope endpoint
#   - _scope_entry_gate_from_deep_result
#   - evaluate_scope_deploy_candidate
#   - longshort_multi_scan
#   - _compute_wave_metrics
#   - _evaluate_market_for_scope
#   - longshort_scope_scan
#   - longshort_scope_deploy
#   - _longshort_scope_deploy_inner
#   - longshort_scope_slots
# ============================================================

from fastapi import APIRouter, Request, Query, Body
from typing import Dict, Any, List, Optional
import logging
import threading
from time import time as time_now
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from app.core.rate_limiter import bybit_get
from app.core.hyper_price_store import price_store
from app.core.constants import (
    BYBIT_MARKET_TICKERS,
    BYBIT_MARKET_KLINE,
    BYBIT_MARKET_INSTRUMENTS,
    bybit_v5_rest_category,
    parse_bybit_list,
    normalize_bybit_ticker,
)
from app.core.currency import Q
from app.strategy import indicators
from app.manager.oma_market_registry import MarketState
from app.monitor.btc_leading_signal import get_btc_leading_detector
from app.api.strategy_utils import (
    SNIPER_MIN_TP_PCT, SNIPER_MIN_SL_PCT, MANUAL_OVERFLOW_MAX,
    _get_cached, _set_cached, _build_cache_key,
    _to_float, _clamp_sniper_tp_sl,
    _snipers_budget_cap_by_price, _cap_snipers_budget,
    _generate_coin_warnings,
    _fetch_scope_candles_cached,
)

logger = logging.getLogger(__name__)

# ============================================================
# Endpoint-specific lock (not shared utility — stays here)
# ============================================================
_scope_deploy_lock = threading.Lock()  # prevent concurrent deploy from multiple browsers

router = APIRouter()


# ============================================================
# Helpers
# ============================================================

def _scope_calc_spread_bps(best_bid: float, best_ask: float) -> float:
    if best_bid <= 0.0 or best_ask <= 0.0:
        return 999999.0
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0.0:
        return 999999.0
    return ((best_ask - best_bid) / mid) * 10000.0


def _scope_calc_depth_notional(
    units: List[Dict[str, Any]],
    *,
    best_bid: float,
    best_ask: float,
    depth_bps: float,
) -> tuple[float, float]:
    if best_bid <= 0.0 or best_ask <= 0.0 or depth_bps <= 0.0:
        return 0.0, 0.0

    ask_lim = best_ask * (1.0 + float(depth_bps) / 10000.0)
    bid_lim = best_bid * (1.0 - float(depth_bps) / 10000.0)

    ask_notional = 0.0
    bid_notional = 0.0
    for unit in list(units or [])[:15]:
        ap = _to_float(unit.get("ask_price"), 0.0)
        asz = _to_float(unit.get("ask_size"), 0.0)
        bp = _to_float(unit.get("bid_price"), 0.0)
        bsz = _to_float(unit.get("bid_size"), 0.0)
        if ap > 0.0 and asz > 0.0 and ap <= ask_lim:
            ask_notional += ap * asz
        if bp > 0.0 and bsz > 0.0 and bp >= bid_lim:
            bid_notional += bp * bsz
    return float(ask_notional), float(bid_notional)


def _scope_market_flow_guard(
    market: str,
    *,
    current_price: float,
    atr_pct: float,
    system: Any = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    from app.core.multi_timeframe_ai import fetch_candles
    from app.core.hyper_price_store import orderbook_store

    cache_key = _build_cache_key("longshort/scope-micro", market=market)
    if not force_refresh:
        cached = _get_cached(cache_key, ttl=5.0)
        if cached is not None:
            return cached

    reasons: List[str] = []
    penalty = 0.0

    applied_budget = max(
        5.0,
        _to_float(
            getattr(system, "longshort_scope_budget_per_slot_usdt", 100.0) if system is not None else 100.0,
            100.0,
        ),
    )
    applied_budget = float(min(applied_budget, _snipers_budget_cap_by_price(current_price)))

    candles_1m = _fetch_scope_candles_cached(
        market,
        unit=1,
        count=24,
        ttl=5.0,
        force_refresh=force_refresh,
    )
    closes_1m: List[float] = []
    trade_values_1m: List[float] = []
    active_minutes = 0
    repeated_close_count = 0
    red_minutes_5 = 0
    down_trade_value_5m = 0.0
    total_trade_value_5m = 0.0
    prev_close = None
    last_window_start = max(0, len(candles_1m) - 5)

    for idx, candle in enumerate(candles_1m):
        close = _to_float(candle.get("trade_price") or candle.get("close"), 0.0)
        open_price = _to_float(candle.get("opening_price") or candle.get("open"), close)
        high = _to_float(candle.get("high_price") or candle.get("high"), close)
        low = _to_float(candle.get("low_price") or candle.get("low"), close)
        volume = _to_float(candle.get("candle_acc_trade_volume") or candle.get("volume"), 0.0)
        trade_value = _to_float(candle.get("candle_acc_trade_price"), 0.0)
        if trade_value <= 0.0 and close > 0.0 and volume > 0.0:
            trade_value = close * volume

        if close > 0.0:
            closes_1m.append(close)
            trade_values_1m.append(max(0.0, trade_value))

        moved = bool(high > low or trade_value > 0.0)
        if moved:
            active_minutes += 1

        if prev_close is not None and close > 0.0:
            if abs(close - prev_close) <= max(1e-8, current_price * 1e-8):
                repeated_close_count += 1
        prev_close = close if close > 0.0 else prev_close

        if idx >= last_window_start and close > 0.0:
            total_trade_value_5m += max(0.0, trade_value)
            ref_open = open_price if open_price > 0.0 else prev_close or close
            if close < ref_open:
                red_minutes_5 += 1
            base_prev = closes_1m[-2] if len(closes_1m) >= 2 else ref_open
            if base_prev > 0.0 and close < base_prev:
                down_trade_value_5m += max(0.0, trade_value)

    candle_count = len(candles_1m)
    active_ratio = (float(active_minutes) / float(candle_count)) if candle_count > 0 else 0.0
    repeated_close_ratio = (
        float(repeated_close_count) / float(max(1, len(closes_1m) - 1))
        if len(closes_1m) >= 2
        else 1.0
    )
    recent_trade_value_10m = float(sum(trade_values_1m[-10:])) if trade_values_1m else 0.0
    avg_trade_value_1m = recent_trade_value_10m / float(max(1, min(10, len(trade_values_1m)))) if trade_values_1m else 0.0
    sell_value_share_5m = (
        float(down_trade_value_5m) / float(total_trade_value_5m)
        if total_trade_value_5m > 0.0
        else 0.0
    )

    last3_drop_pct = 0.0
    last5_ret_pct = 0.0
    drop_from_recent_high_pct = 0.0
    if len(closes_1m) >= 4:
        base3 = closes_1m[-4]
        if base3 > 0.0:
            last3_drop_pct = ((closes_1m[-1] - base3) / base3) * 100.0
        recent_high = max(closes_1m[-4:-1]) if len(closes_1m[-4:-1]) > 0 else closes_1m[-1]
        if recent_high > 0.0:
            drop_from_recent_high_pct = ((closes_1m[-1] - recent_high) / recent_high) * 100.0
    if len(closes_1m) >= 6:
        base5 = closes_1m[-6]
        if base5 > 0.0:
            last5_ret_pct = ((closes_1m[-1] - base5) / base5) * 100.0

    if candle_count < 12 or len(closes_1m) < 12:
        reasons.append("micro_data_insufficient")
        penalty += 18.0
    else:
        if active_ratio < 0.55:
            penalty += 6.0
        if repeated_close_ratio > 0.55:
            penalty += 6.0
        if recent_trade_value_10m < applied_budget * 3.0:
            penalty += 8.0

        if active_ratio < 0.30 and repeated_close_ratio > 0.70:
            reasons.append("market_dead")
            penalty += 24.0
        elif active_ratio < 0.40 or recent_trade_value_10m < applied_budget * 1.5:
            reasons.append("trade_flow_thin")
            penalty += 16.0

        shock_threshold = max(0.35, float(atr_pct) * 0.70)
        warning_threshold = max(0.20, float(atr_pct) * 0.45)
        if last3_drop_pct <= -warning_threshold or drop_from_recent_high_pct <= -warning_threshold:
            penalty += 8.0
        if (
            (last3_drop_pct <= -shock_threshold or drop_from_recent_high_pct <= -shock_threshold)
            and red_minutes_5 >= 3
            and sell_value_share_5m >= 0.62
        ):
            reasons.append("selloff_transition")
            penalty += 18.0

    orderbook = orderbook_store.get(market) or {}
    ob_ts = _to_float((orderbook or {}).get("ts"), 0.0)
    ob_age_sec = max(0.0, float(time_now() or 0.0) - ob_ts) if ob_ts > 0.0 else 999999.0
    best_bid = _to_float((orderbook or {}).get("best_bid"), 0.0)
    best_ask = _to_float((orderbook or {}).get("best_ask"), 0.0)
    units = list((orderbook or {}).get("units") or [])
    spread_bps = _scope_calc_spread_bps(best_bid, best_ask)
    depth_bps = max(10.0, _to_float(getattr(system, "entry_ob_depth_bps", 50.0) if system is not None else 50.0, 50.0))
    depth_factor = max(1.10, _to_float(getattr(system, "entry_ob_depth_factor", 1.10) if system is not None else 1.10, 1.10))
    max_spread_bps = max(12.0, _to_float(getattr(system, "entry_ob_max_spread_bps", 25.0) if system is not None else 25.0, 25.0))
    _, bid_depth_usdt = _scope_calc_depth_notional(
        units,
        best_bid=best_bid,
        best_ask=best_ask,
        depth_bps=depth_bps,
    )
    required_bid_depth_usdt = float(applied_budget) * float(depth_factor)

    orderbook_ready = bool(best_bid > 0.0 and best_ask > 0.0 and units)
    if not orderbook_ready:
        # Websocket orderbook coverage can lag behind the market universe.
        # Missing local snapshots should not zero out otherwise valid candidates.
        penalty += 4.0
    else:
        if ob_age_sec > 45.0:
            penalty += 4.0
        if ob_age_sec > 120.0:
            reasons.append("orderbook_stale")
            penalty += 8.0
        if spread_bps > max_spread_bps * 0.85:
            penalty += 6.0
        if spread_bps > max_spread_bps * 1.15:
            reasons.append("spread_wide")
            penalty += 16.0
        if bid_depth_usdt < required_bid_depth_usdt:
            penalty += 8.0
        if bid_depth_usdt <= 0.0 or bid_depth_usdt < required_bid_depth_usdt * 0.50:
            reasons.append("bid_depth_thin")
            penalty += 16.0

    result = {
        "ok": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "penalty": round(max(0.0, penalty), 2),
        "candle_count_1m": int(candle_count),
        "active_minutes_1m": int(active_minutes),
        "active_ratio_1m": round(active_ratio, 4),
        "repeated_close_ratio_1m": round(repeated_close_ratio, 4),
        "recent_trade_value_10m": round(recent_trade_value_10m, 2),
        "avg_trade_value_1m": round(avg_trade_value_1m, 2),
        "last3_drop_pct": round(last3_drop_pct, 4),
        "last5_ret_pct": round(last5_ret_pct, 4),
        "drop_from_recent_high_pct": round(drop_from_recent_high_pct, 4),
        "red_minutes_5": int(red_minutes_5),
        "sell_value_share_5m": round(sell_value_share_5m, 4),
        "best_bid": round(best_bid, 8) if best_bid > 0.0 else 0.0,
        "best_ask": round(best_ask, 8) if best_ask > 0.0 else 0.0,
        "spread_bps": round(spread_bps, 4) if spread_bps < 999999.0 else 999999.0,
        "orderbook_age_sec": round(ob_age_sec, 3) if ob_age_sec < 999999.0 else 999999.0,
        "bid_depth_usdt": round(bid_depth_usdt, 2),
        "required_bid_depth_usdt": round(required_bid_depth_usdt, 2),
        "applied_budget_usdt": round(applied_budget, 2),
        "orderbook_ready": orderbook_ready,
    }
    _set_cached(cache_key, result)
    return result

@router.get(
    "/longshort/scope",
    summary="Precision Sniper Scope — single coin deep analysis",
    responses={200: {"description": "Deep analysis with confidence score and FIRE signal"}},
)
def longshort_scope(
    request: Request,
    market: str = Query(..., description="Market (e.g., XAUTUSDT)"),
    force_refresh: bool = Query(False, description="Force refresh (skip cache)"),
):
    """
    Precision Sniper analysis for a single coin.
    8-stage filter producing a confidence score (0-100%) + optimal parameters + FIRE signal.
    """
    import time as _time

    market_norm = Q.normalize(market.strip().upper())

    system = getattr(getattr(getattr(request, "app", None), "state", None), "system", None) if request is not None else None

    cache_key = _build_cache_key("longshort/scope", market=market_norm)
    if not force_refresh:
        cached = _get_cached(cache_key, ttl=10.0)
        if cached is not None:
            return cached

    # ── Data collection (fetch candles once → reused for both indicators and MTF AI) ──
    from app.core.multi_timeframe_ai import fetch_candles, calculate_timeframe_score, _extract_prices_from_candles, _extract_volumes_from_candles

    # Current price: prefer price_store, otherwise query the exchange ticker directly
    current_price = price_store.get_price(market_norm) or 0.0
    if current_price <= 0:
        try:
            ticker_resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
            for _t in parse_bybit_list(ticker_resp.json()):
                if isinstance(_t, dict):
                    _tc = normalize_bybit_ticker(_t)
                    if _tc.get("market", "").upper() == market_norm.upper():
                        current_price = float(_tc.get("trade_price") or 0)
                        break
        except Exception as exc:
            logger.warning("[LONGSHORT_API] current price ticker fallback failed: %s", exc, exc_info=True)
    if current_price <= 0:
        return {"ok": False, "error": "Price not available", "market": market_norm}

    # Collect candles once (only 3 API calls)
    candles_5m = _fetch_scope_candles_cached(
        market_norm,
        unit=5,
        count=100,
        ttl=10.0,
        force_refresh=force_refresh,
    )
    candles_15m = _fetch_scope_candles_cached(
        market_norm,
        unit=15,
        count=100,
        ttl=10.0,
        force_refresh=force_refresh,
    )
    candles_60m = _fetch_scope_candles_cached(
        market_norm,
        unit=60,
        count=100,
        ttl=10.0,
        force_refresh=force_refresh,
    )

    prices_5m = _extract_prices_from_candles(candles_5m)
    prices_15m = _extract_prices_from_candles(candles_15m)
    prices_60m = _extract_prices_from_candles(candles_60m)
    volumes_5m = _extract_volumes_from_candles(candles_5m)

    if len(prices_5m) < 20:
        return {"ok": False, "error": "Insufficient candle data", "market": market_norm}

    # ── Indicator calculation ──
    rsi_val = indicators.rsi(prices_5m, 14) or 50.0
    rsi_15m = indicators.rsi(prices_15m, 14) if len(prices_15m) >= 15 else None
    rsi_60m = indicators.rsi(prices_60m, 14) if len(prices_60m) >= 15 else None

    bb = indicators.bollinger_bands(prices_5m, 20, 2.0)
    bb_position = 50.0  # 0=lower, 50=mid, 100=upper
    if bb and bb["upper"] != bb["lower"]:
        bb_position = max(0, min(100, (current_price - bb["lower"]) / (bb["upper"] - bb["lower"]) * 100))

    ema_short = indicators.ema(prices_5m, 9)
    ema_mid = indicators.ema(prices_5m, 21)
    ema_long = indicators.ema(prices_5m, 50) if len(prices_5m) >= 50 else None
    ema_aligned = False
    if ema_short and ema_mid:
        ema_aligned = ema_short > ema_mid
        if ema_long:
            ema_aligned = ema_short > ema_mid > ema_long

    atr_val = indicators.atr_simplified(prices_5m, 14) or 0.0
    atr_pct = (atr_val / current_price * 100) if current_price > 0 else 0.0

    trend_val = indicators.trend(prices_5m, 20) or 0.0
    momentum_val = indicators.trend(prices_5m, 5) or 0.0
    macd_line_5, _, _ = indicators.macd(prices_5m, 12, 26, 9)
    prev_macd_line_5 = None
    if len(prices_5m) > 40:
        prev_macd_line_5, _, _ = indicators.macd(prices_5m[:-1], 12, 26, 9)
    macd_slope_5 = (
        (macd_line_5 - prev_macd_line_5)
        if macd_line_5 is not None and prev_macd_line_5 is not None
        else None
    )
    macd_line_15, _, _ = indicators.macd(prices_15m, 12, 26, 9)
    prev_macd_line_15 = None
    if len(prices_15m) > 40:
        prev_macd_line_15, _, _ = indicators.macd(prices_15m[:-1], 12, 26, 9)
    macd_slope_15 = (
        (macd_line_15 - prev_macd_line_15)
        if macd_line_15 is not None and prev_macd_line_15 is not None
        else None
    )

    # Volume surge
    vol_surge_ratio = 1.0
    if len(volumes_5m) >= 10:
        recent_vol = sum(volumes_5m[-3:]) / 3.0 if len(volumes_5m) >= 3 else 0
        avg_vol = sum(volumes_5m[-10:-3]) / 7.0 if len(volumes_5m) >= 10 else 0
        if avg_vol > 0:
            vol_surge_ratio = recent_vol / avg_vol

    # Support/resistance (based on recent 60m candles)
    lows_60m = [float(c.get("low_price") or 0) for c in candles_60m if c.get("low_price")]
    highs_60m = [float(c.get("high_price") or 0) for c in candles_60m if c.get("high_price")]
    support = min(lows_60m) if lows_60m else current_price * 0.95
    resistance = max(highs_60m) if highs_60m else current_price * 1.05

    # Proximity to the N-minute low
    lows_5m = [float(c.get("low_price") or 0) for c in candles_5m if c.get("low_price")]
    highs_5m = [float(c.get("high_price") or 0) for c in candles_5m if c.get("high_price")]
    period_low = min(lows_5m) if lows_5m else current_price
    period_high = max(highs_5m) if highs_5m else current_price
    dist_from_low_pct = ((current_price - period_low) / period_low * 100) if period_low > 0 else 999

    # Multi-TF AI (reuses already-fetched candles — 0 extra API calls)
    tf_scores = []
    for tf_unit, tf_candles in [(5, candles_5m), (15, candles_15m), (60, candles_60m)]:
        if tf_candles:
            s = calculate_timeframe_score(market_norm, tf_candles, tf_unit)
            if s:
                tf_scores.append(s)
    if tf_scores:
        best_tf = max(tf_scores, key=lambda s: abs(s.ai_score - 0.5))
        ai_score = best_tf.ai_score
        ai_signal = best_tf.signal
    else:
        ai_score = 0.5
        ai_signal = "hold"

    # BTC regime
    btc_detector = get_btc_leading_detector()
    btc_regime = "TREND"
    btc_signal = None
    if btc_detector:
        btc_regime = btc_detector.get_regime_for_lightning()
        sig = btc_detector.detect_signal()
        if sig:
            btc_signal = {
                "direction": sig.direction,
                "change_5m": round(sig.btc_change_5m, 2),
                "change_15m": round(sig.btc_change_15m, 2),
                "strength": round(sig.strength, 2),
            }

    # BB Squeeze
    squeeze_result = indicators.bollinger_squeeze(prices_5m, 20, 2.0, 20)
    bb_bandwidth = squeeze_result[0] if squeeze_result else 0.0
    bb_squeeze = squeeze_result[1] if squeeze_result else False

    # ── 8-Stage Filter ──
    stages = []
    confidence = 0

    # Stage 1: Near low (24 pts) — highest priority
    s1_pass = dist_from_low_pct <= 1.8
    s1_score = max(0, 24 - dist_from_low_pct * 8) if dist_from_low_pct <= 3.0 else 0
    stages.append({"stage": 1, "name": "Near Low", "pass": s1_pass, "score": round(s1_score, 1),
                    "detail": f"dist={dist_from_low_pct:.2f}%"})
    confidence += s1_score

    # Stage 2: RSI oversold (22 pts) — core of dip buying
    s2_pass = rsi_val < 38
    s2_score = min(22, max(0, (45 - rsi_val) * 0.9)) if rsi_val < 45 else 0
    stages.append({"stage": 2, "name": "RSI Oversold", "pass": s2_pass, "score": round(s2_score, 1),
                    "detail": f"RSI={rsi_val:.1f}"})
    confidence += s2_score

    # Stage 3: BB lower touch (14 pts)
    s3_pass = bb_position < 22
    s3_score = min(14, max(0, (30 - bb_position) * 0.7)) if bb_position < 30 else 0
    stages.append({"stage": 3, "name": "BB Lower", "pass": s3_pass, "score": round(s3_score, 1),
                    "detail": f"BB%={bb_position:.1f}"})
    confidence += s3_score

    # Stage 4: Volume surge (5 pts)
    s4_pass = vol_surge_ratio >= 1.8
    s4_score = min(5, max(0, (vol_surge_ratio - 1.0) * 3.5)) if vol_surge_ratio > 1.0 else 0
    stages.append({"stage": 4, "name": "Volume Surge", "pass": s4_pass, "score": round(s4_score, 1),
                    "detail": f"ratio={vol_surge_ratio:.2f}x"})
    confidence += s4_score

    # Stage 5: EMA inversion — downtrend = dip environment (8 pts) [FIX H5: criteria unified with compute_scope_score / multi_scan]
    ema_inverted = bool(ema_short and ema_mid and ema_short < ema_mid)
    s5_pass = ema_inverted
    s5_score = 8 if (ema_inverted and rsi_val < 40) else (4 if ema_inverted else 0)
    stages.append({"stage": 5, "name": "EMA Inversion", "pass": s5_pass, "score": round(s5_score, 1),
                    "detail": f"inverted={ema_inverted}"})
    confidence += s5_score

    # Stage 6: AI Score (7 pts) — auxiliary indicator
    s6_pass = ai_score >= 0.62
    s6_score = min(7, max(0, (ai_score - 0.45) * 40)) if ai_score > 0.45 else 0
    stages.append({"stage": 6, "name": "AI Score", "pass": s6_pass, "score": round(s6_score, 1),
                    "detail": f"ai={ai_score:.3f} ({ai_signal})"})
    confidence += s6_score

    # Stage 7: BTC regime (5 pts) — safety filter
    s7_pass = btc_regime != "SHOCK"
    s7_score = 5 if btc_regime == "TREND" else (4 if btc_regime == "RECOVERY" else (2 if btc_regime == "DRIFT" else 0))
    stages.append({"stage": 7, "name": "BTC Regime", "pass": s7_pass, "score": round(s7_score, 1),
                    "detail": f"regime={btc_regime}"})
    confidence += s7_score

    # Stage 8: Momentum + MACD (15 pts) — trend-reversal confirmation
    mom_component = min(8, max(0, (momentum_val + 0.8) * 5.0)) if momentum_val > -0.8 else 0
    macd_component = 0.0
    if macd_line_5 is not None and macd_line_5 > -0.02:
        macd_component += 4.0
    if macd_slope_5 is not None and macd_slope_5 > 0:
        macd_component += 3.0
    if macd_line_15 is not None and macd_line_15 > -0.02:
        macd_component += 2.0
    if macd_slope_15 is not None and macd_slope_15 > 0:
        macd_component += 1.0
    s8_score = min(15, mom_component + macd_component)
    s8_pass = mom_component >= 3.0 and macd_component >= 4.0
    stages.append({"stage": 8, "name": "Momentum/MACD", "pass": s8_pass, "score": round(s8_score, 1),
                    "detail": f"mom={momentum_val:.2f}% macd5={macd_line_5 if macd_line_5 is not None else 0:.4f} d5={macd_slope_5 if macd_slope_5 is not None else 0:.4f}"})
    confidence += s8_score

    confidence_raw = round(min(100, max(0, confidence)), 1)
    market_flow = _scope_market_flow_guard(
        market_norm,
        current_price=current_price,
        atr_pct=atr_pct,
        system=system,
        force_refresh=force_refresh,
    )
    confidence_penalty = _to_float(market_flow.get("penalty"), 0.0)
    confidence = round(max(0.0, min(100.0, confidence_raw - confidence_penalty)), 1)
    stages_passed = sum(1 for s in stages if s["pass"])

    # ── FIRE signal ──
    fire = confidence >= 70
    fire_level = "HOLD"
    if confidence >= 85:
        fire_level = "STRONG_FIRE"
    elif confidence >= 70:
        fire_level = "FIRE"
    elif confidence >= 50:
        fire_level = "READY"

    # ── Wave analysis [FIX L5: use shared helper _compute_wave_metrics to remove duplication] ──
    wave_analysis = _compute_wave_metrics(prices_5m, candle_min=5, fee_pct=0.10)
    avg_up_amp = wave_analysis["avg_up_amp_pct"]
    avg_down_amp = wave_analysis["avg_down_amp_pct"]


    # ── Optimal parameters (ATR-based, reflecting wave amplitude) ──
    # Also derive Entry/Exit from wave amplitude for more realistic values
    wave_entry = round(max(0.1, avg_up_amp * 0.15), 2) if avg_up_amp > 0 else 0
    wave_exit = round(max(0.1, avg_up_amp * 0.12), 2) if avg_up_amp > 0 else 0
    range_pct = ((period_high - period_low) / period_low * 100) if period_low > 0 else 2.0
    atr_entry = round(max(0.1, min(2.5, atr_pct * 0.5)), 2)
    atr_exit = round(max(0.1, min(2.5, atr_pct * 0.4)), 2)
    opt_entry_threshold = max(atr_entry, wave_entry) if wave_entry > 0 else atr_entry
    opt_exit_threshold = max(atr_exit, wave_exit) if wave_exit > 0 else atr_exit
    opt_tp = round(max(SNIPER_MIN_TP_PCT, min(8.0, avg_up_amp * 0.7 if avg_up_amp > 0.5 else atr_pct * 2.0)), 1)
    opt_sl = round(max(SNIPER_MIN_SL_PCT, min(4.0, avg_down_amp * 0.6 if avg_down_amp > 0.3 else atr_pct * 1.2)), 1)

    # Adjust TP/SL for BTC regime
    regime_mult = {"SHOCK": 0.6, "DRIFT": 0.8, "RECOVERY": 1.1, "TREND": 1.0}.get(btc_regime, 1.0)
    opt_tp = round(opt_tp * regime_mult, 1)
    opt_sl = round(opt_sl * (2.0 - regime_mult), 1)  # inverse: tighter SL in bad regime
    opt_tp, opt_sl = _clamp_sniper_tp_sl(opt_tp, opt_sl)

    result = {
        "ok": True,
        "market": market_norm,
        "price": current_price,
        "timestamp": _time.time(),
        "confidence": confidence,
        "confidence_raw": confidence_raw,
        "confidence_penalty": round(confidence_penalty, 2),
        "fire": fire,
        "fire_level": fire_level,
        "stages_passed": stages_passed,
        "stages": stages,
        "indicators": {
            "rsi_5m": round(rsi_val, 1),
            "rsi_15m": round(rsi_15m, 1) if rsi_15m is not None else None,
            "rsi_60m": round(rsi_60m, 1) if rsi_60m is not None else None,
            "bb_position": round(bb_position, 1),
            "bb_bandwidth": round(bb_bandwidth, 4),
            "bb_squeeze": bb_squeeze,
            "ema_aligned": ema_aligned,
            "ema_short": round(ema_short, 2) if ema_short else None,
            "ema_mid": round(ema_mid, 2) if ema_mid else None,
            "ema_long": round(ema_long, 2) if ema_long else None,
            "atr_pct": round(atr_pct, 3),
            "trend": round(trend_val, 2),
            "momentum": round(momentum_val, 2),
            "macd_line_5m": round(macd_line_5, 4) if macd_line_5 is not None else None,
            "macd_slope_5m": round(macd_slope_5, 4) if macd_slope_5 is not None else None,
            "macd_line_15m": round(macd_line_15, 4) if macd_line_15 is not None else None,
            "macd_slope_15m": round(macd_slope_15, 4) if macd_slope_15 is not None else None,
            "vol_surge_ratio": round(vol_surge_ratio, 2),
            "ai_score": round(ai_score, 3),
            "ai_signal": ai_signal,
        },
        "support_resistance": {
            "support": support,
            "resistance": resistance,
            "period_low": period_low,
            "period_high": period_high,
            "range_pct": round(range_pct, 2),
            "dist_from_low_pct": round(dist_from_low_pct, 2),
        },
        "btc": {
            "regime": btc_regime,
            "signal": btc_signal,
        },
        "optimal_params": {
            "entry_threshold": opt_entry_threshold,
            "exit_threshold": opt_exit_threshold,
            "tp_pct": opt_tp,
            "sl_pct": opt_sl,
            "lookback_min": 60 if atr_pct < 1.0 else 30,
        },
        "wave": wave_analysis,
        "market_flow": market_flow,
    }

    _set_cached(cache_key, result)
    return result


def _scope_entry_gate_from_deep_result(
    deep_result: Dict[str, Any],
    *,
    ai_min_score: float = 0.55,
    rsi_entry_max: float = 42.0,
) -> Dict[str, Any]:
    """Precision Scope deep result → suitability for slot placement.

    [2026-03-07 refactor]
    This function judges "is it worth seating in a watch slot", not "can it be bought right now".
    The actual buy timing is decided precisely by SniperPlugin.decide() via BPS/RSI/AI, etc.

    Block conditions (hard block):
    - market_flow issues (excessive spread, insufficient orderbook depth, etc.) → fill itself impossible
    Warning conditions (soft — slot placement allowed, the buy is decided by the plugin):
    - wave_unprofitable, entry_not_near_low, ai_gate, rsi_entry
    """
    rec = deep_result.get("optimal_params", {}) or {}
    indicators_map = deep_result.get("indicators", {}) or {}
    sr = deep_result.get("support_resistance", {}) or {}
    wave = deep_result.get("wave", {}) or {}
    market_flow = deep_result.get("market_flow", {}) or {}

    current_price = _to_float(deep_result.get("price"), 0.0)
    period_low = _to_float(sr.get("period_low"), 0.0)
    entry_threshold = max(0.1, _to_float(rec.get("entry_threshold"), 1.0))
    entry_target_price = period_low * (1.0 + entry_threshold / 100.0) if period_low > 0 else 0.0
    near_low = bool(period_low > 0 and current_price > 0 and current_price <= entry_target_price)

    ai_score = _to_float(indicators_map.get("ai_score"), 0.0)
    rsi_5m = _to_float(indicators_map.get("rsi_5m"), 50.0)
    profitable = bool(wave.get("profitable", False))

    # Collect all reasons (informational)
    reasons: List[str] = []
    if not profitable:
        reasons.append("wave_unprofitable")
    if not near_low:
        reasons.append("entry_not_near_low")
    if ai_score < ai_min_score:
        reasons.append("ai_gate")
    if rsi_5m > rsi_entry_max:
        reasons.append("rsi_entry")
    for reason in list(market_flow.get("reasons") or []):
        rs = str(reason or "").strip()
        if rs:
            reasons.append(rs)

    uniq_reasons = list(dict.fromkeys(reasons))

    # Slot placement decision: only market_flow is a hard block, the rest are soft warnings
    # → after seating in a slot, monitor confidence in real time → the plugin decides precisely at Fire time
    _market_flow_reasons = [r for r in uniq_reasons
                            if r not in ("wave_unprofitable", "entry_not_near_low",
                                         "ai_gate", "rsi_entry")]
    gate_ok = not _market_flow_reasons  # OK to place in slot if there are no market_flow issues

    return {
        "ok": gate_ok,
        "reasons": uniq_reasons,
        "hard_block_reasons": _market_flow_reasons,
        "price": round(current_price, 8),
        "period_low": round(period_low, 8),
        "entry_threshold_pct": round(entry_threshold, 4),
        "entry_target_price": round(entry_target_price, 8) if entry_target_price > 0 else 0.0,
        "dist_from_low_pct": round(_to_float(sr.get("dist_from_low_pct"), 999.0), 4),
        "ai_score": round(ai_score, 4),
        "ai_required": round(ai_min_score, 4),
        "rsi_5m": round(rsi_5m, 2),
        "rsi_required_max": round(rsi_entry_max, 2),
        "wave_profitable": profitable,
        "market_flow_ok": bool(market_flow.get("ok", True)),
        "market_flow_penalty": round(_to_float(market_flow.get("penalty"), 0.0), 2),
        "active_ratio_1m": round(_to_float(market_flow.get("active_ratio_1m"), 0.0), 4),
        "recent_trade_value_10m": round(_to_float(market_flow.get("recent_trade_value_10m"), 0.0), 2),
        "spread_bps": round(_to_float(market_flow.get("spread_bps"), 0.0), 4),
        "bid_depth_usdt": round(_to_float(market_flow.get("bid_depth_usdt"), 0.0), 2),
    }


def evaluate_scope_deploy_candidate(
    market: str,
    system: Any,
    *,
    force_refresh: bool = False,
) -> Optional[Dict[str, Any]]:
    """Final Precision Scope score shared by recommendation/deploy/autofill."""
    try:
        scope_request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(system=system),
            )
        )
        deep_result = longshort_scope(request=scope_request, market=market, force_refresh=force_refresh)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[scope/deploy] deep analysis failed %s: %s", market, exc, exc_info=True)
        return None

    if not isinstance(deep_result, dict) or not deep_result.get("ok"):
        return None

    rec = deep_result.get("optimal_params", {}) or {}
    wave = deep_result.get("wave", {}) or {}
    confidence = _to_float(deep_result.get("confidence"), 0.0)
    profit_score = min(100.0, max(0.0, _to_float(wave.get("est_daily_profit_pct"), 0.0) * 7.0))
    rank_score = round(confidence * 0.72 + profit_score * 0.28, 2)
    entry_gate = _scope_entry_gate_from_deep_result(deep_result)

    return {
        "market": str(deep_result.get("market") or market).strip().upper(),
        "price": _to_float(deep_result.get("price"), 0.0),
        "confidence": confidence,
        "confidence_raw": _to_float(deep_result.get("confidence_raw"), confidence),
        "confidence_penalty": _to_float(deep_result.get("confidence_penalty"), 0.0),
        "rank_score": rank_score,
        "fire": bool(deep_result.get("fire", False)),
        "fire_level": str(deep_result.get("fire_level") or "HOLD"),
        "stages_passed": int(_to_float(deep_result.get("stages_passed"), 0)),
        "stages": list(deep_result.get("stages") or []),
        "indicators": dict(deep_result.get("indicators") or {}),
        "support_resistance": dict(deep_result.get("support_resistance") or {}),
        "recommended_params": {
            "entry_threshold": _to_float(rec.get("entry_threshold"), 0.3),
            "exit_threshold": _to_float(rec.get("exit_threshold"), _to_float(rec.get("entry_threshold"), 0.3)),
            "tp_pct": _to_float(rec.get("tp_pct"), SNIPER_MIN_TP_PCT),
            "sl_pct": _to_float(rec.get("sl_pct"), SNIPER_MIN_SL_PCT),
            "lookback_min": int(_to_float(rec.get("lookback_min"), 60)),
        },
        "wave": dict(wave),
        "market_flow": dict(deep_result.get("market_flow") or {}),
        "btc": dict(deep_result.get("btc") or {}),
        "entry_gate": entry_gate,
        "deploy_ready": bool(entry_gate.get("ok")),
    }


# ============================================================
# LONG/SHORT Multi-Slot Precision Sniper Scanner
# [2026-02-24] scan many coins concurrently → sort by profitability rank
# ============================================================

@router.get(
    "/longshort/multi-scan",
    summary="Multi-slot Precision Sniper — scan top markets",
    responses={200: {"description": "Ranked list of markets by sniper profitability"}},
)
def longshort_multi_scan(
    request: Request,
    top_n: int = Query(10, ge=1, le=30, description="Number of top results to return"),
    scan_count: int = Query(0, ge=0, le=500, description="Number of markets to scan (0=all markets)"),
    force_refresh: bool = Query(False, description="Force refresh (skip cache)"),
    min_confidence: float = Query(20.0, ge=0.0, le=100.0, description="Minimum confidence floor"),
    focus_market: str = Query("", description="Always include this market in scan (e.g., XRPUSDT)"),
    min_price: float = Query(0, ge=0, description="Minimum coin price (USDT, 0=no limit)"),
    max_price: float = Query(0, ge=0, description="Maximum coin price (USDT, 0=no limit)"),
):
    """
    Scan many markets concurrently.
    Produce a profitability ranking via a 6-stage lightweight filter (AI/BTC excluded).
    """
    import time as _time
    system = request.app.state.system

    min_price_eff = max(0.0, float(min_price or 0.0))
    max_price_eff = max(0.0, float(max_price or 0.0))
    if min_price_eff <= 0:
        min_price_eff = max(0.0, _to_float(getattr(system, "longshort_scope_min_price", 0.0), 0.0))
    if max_price_eff <= 0:
        max_price_eff = max(0.0, _to_float(getattr(system, "longshort_scope_max_price", 0.0), 0.0))
    if min_price_eff > 0 and max_price_eff > 0 and max_price_eff < min_price_eff:
        min_price_eff, max_price_eff = max_price_eff, min_price_eff

    cache_key = _build_cache_key(
        "longshort/multi-scan",
        top_n=top_n,
        scan_count=scan_count,
        min_confidence=min_confidence,
        focus_market=focus_market,
        min_price=round(min_price_eff, 8),
        max_price=round(max_price_eff, 8),
    )
    if not force_refresh:
        cached = _get_cached(cache_key, ttl=30.0)
        if cached is not None:
            return cached

    from app.core.multi_timeframe_ai import (
        fetch_candles,
        _extract_prices_from_candles,
        _extract_volumes_from_candles,
    )

    # ── 1. Fetch market list (isDetails=true → filter delisted/caution markets) ──
    try:
        mkt_resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        all_instruments = parse_bybit_list(mkt_resp.json())
    except Exception:
        logger.error("strategy_longshort_router.longshort_multi_scan L856 except", exc_info=True)
        return {"ok": False, "error": "Failed to fetch market list"}

    markets_all = []
    excluded_delist_caution = 0
    for m in all_instruments:
        if not isinstance(m, dict):
            continue
        sym = str(m.get("symbol") or "").upper()
        if not sym:
            continue
        market = Q.normalize(sym)
        if Q.config.market_prefix and not market.startswith(Q.config.market_prefix):
            continue
        status = str(m.get("status") or "").upper()
        if status not in ("TRADING", ""):
            excluded_delist_caution += 1
            continue
        markets_all.append(market)

    if not markets_all:
        return {"ok": False, "error": "No markets found"}

    # ── 2. Fetch tickers (over all markets, chunked) ──
    # NOTE:
    # The previous implementation fetched tickers only for the first scan_count markets,
    # so the same candidates kept repeating. Changed to select the top scan_count markets
    # by trading value across the entire universe.
    tickers = []
    try:
        market_set = set(m.upper() for m in markets_all)
        ticker_resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=10.0)
        if ticker_resp.status_code == 200:
            for t in parse_bybit_list(ticker_resp.json()):
                if isinstance(t, dict):
                    tc = normalize_bybit_ticker(t)
                    if tc.get("market", "").upper() in market_set:
                        tickers.append(tc)
    except Exception:
        logger.error("strategy_longshort_router.longshort_multi_scan L893 except", exc_info=True)
        return {"ok": False, "error": "Failed to fetch tickers"}

    if not tickers:
        return {"ok": False, "error": "No ticker data"}

    ticker_map = {}
    for t in tickers:
        if not isinstance(t, dict):
            continue
        mk = t.get("market", "")
        trade_price = float(t.get("trade_price") or 0)
        if min_price_eff > 0 and trade_price < min_price_eff:
            continue
        if max_price_eff > 0 and trade_price > max_price_eff:
            continue
        ticker_map[mk] = t

    if not ticker_map:
        return {
            "ok": True,
            "timestamp": _time.time(),
            "scanned": 0,
            "profitable_count": 0,
            "results": [],
            "excluded_active": 0,
            "excluded_strategy_occupied": 0,
        }

    # Sort by 24h trading value, then select scan_count (0=all)
    sorted_by_liquidity = sorted(
        ticker_map.items(),
        key=lambda kv: float((kv[1] or {}).get("acc_trade_price_24h") or 0.0),
        reverse=True,
    )
    selected_tickers = sorted_by_liquidity if scan_count <= 0 else sorted_by_liquidity[:scan_count]
    ticker_map = dict(selected_tickers)

    filtered_markets = list(ticker_map.keys())

    # Optional user focus market: include explicitly even if it is outside liquidity top-N.
    focus = str(focus_market or "").strip().upper()
    focus = Q.normalize(str(focus_market or "").strip().upper())

    if focus:
        if focus not in ticker_map:
            try:
                f_resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
                if f_resp.status_code == 200:
                    for _t in parse_bybit_list(f_resp.json()):
                        if isinstance(_t, dict):
                            _tc = normalize_bybit_ticker(_t)
                            if _tc.get("market", "").upper() == focus.upper():
                                tp = float(_tc.get("trade_price") or 0.0)
                                if min_price_eff > 0 and tp < min_price_eff:
                                    pass
                                elif max_price_eff > 0 and tp > max_price_eff:
                                    pass
                                else:
                                    ticker_map[focus] = _tc
                                break
            except Exception as exc:
                logger.warning("[LONGSHORT_API] focus market ticker fetch failed: %s", exc, exc_info=True)
        if focus in ticker_map and focus not in filtered_markets:
            filtered_markets.append(focus)
    if not filtered_markets:
        return {
            "ok": True,
            "timestamp": _time.time(),
            "scanned": 0,
            "profitable_count": 0,
            "results": [],
            "excluded_active": 0,
            "excluded_strategy_occupied": 0,
        }

    # ── 2b. Exclude markets that are already ACTIVE/RECOVERY or occupied by another strategy (prevent inter-strategy conflicts) ──
    held_market_set: set = set()
    try:
        tc = getattr(system, "trade_client", None)
        if tc:
            for acc in tc.accounts(skip_currencies=[Q.symbol, "USDT"]):
                cur = str(acc.get("currency") or "").strip().upper()
                bal = _to_float(acc.get("balance"), 0.0)
                locked = _to_float(acc.get("locked"), 0.0)
                if cur and (bal + locked) > 0.0:
                    held_market_set.add(Q.market(cur))
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[LONGSHORT_API] exclude active/occupied markets failed: %s", exc, exc_info=True)

    contexts = getattr(system.coordinator, "contexts", {}) or {}

    active_set: set = set()
    try:
        snap = system.oma_registry.snapshot() if hasattr(system, "oma_registry") else {}
        for row in (snap.get("active") or []):
            mk = (row.get("market") if isinstance(row, dict) else row) or ""
            if mk:
                active_set.add(str(mk).strip().upper())
        for row in (snap.get("recovery") or []):
            mk = (row.get("market") if isinstance(row, dict) else row) or ""
            if not mk:
                continue
            market = str(mk).strip().upper()
            ctx = contexts.get(market)
            has_pos = bool(getattr(ctx, "position", None) or {})
            if market in held_market_set or has_pos:
                active_set.add(market)
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[LONGSHORT_API] exclude active/occupied markets failed: %s", exc, exc_info=True)

    strategy_occupied_set: set = set()
    try:
        for mk, ctx in list(contexts.items()):
            market = str(mk or "").strip().upper()
            if not market:
                continue
            ctrls = getattr(ctx, "controls", {}) or {}
            strat = ctrls.get("strategy", {}) or {}
            if not bool(strat.get("enabled")):
                continue
            has_pos = bool(getattr(ctx, "position", None) or {})
            if not has_pos and market not in active_set:
                continue
            mode = str(strat.get("mode") or "").strip().upper()
            params = strat.get("params", {}) or {}
            profile = str(params.get("profile") or "").strip().upper()
            source = str(params.get("source") or "").strip().lower()
            if mode in ("SNIPER", "SNIPER(S)") and profile == "SNIPERS" and source == "precision_scope":
                continue
            strategy_occupied_set.add(market)
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[LONGSHORT_API] strategy_longshort_router fallback: %s", exc, exc_info=True)

    excluded_active_count = 0
    excluded_strategy_occupied_count = 0
    if active_set:
        before = len(filtered_markets)
        # Keep user focus market scanable even if currently active/recovery for visibility.
        filtered_markets = [m for m in filtered_markets if (m not in active_set or (focus and m == focus))]
        excluded_active_count = before - len(filtered_markets)

    if strategy_occupied_set:
        before = len(filtered_markets)
        filtered_markets = [m for m in filtered_markets if (m not in strategy_occupied_set or (focus and m == focus))]
        excluded_strategy_occupied_count = before - len(filtered_markets)

    if not filtered_markets:
        return {
            "ok": True,
            "timestamp": _time.time(),
            "scanned": 0,
            "profitable_count": 0,
            "results": [],
            "excluded_active": excluded_active_count,
            "excluded_strategy_occupied": excluded_strategy_occupied_count,
        }

    # ── 3. Collect 5-minute candles in parallel ──
    candle_map: Dict[str, list] = {}

    def _fetch(mk: str):
        try:
            return mk, _fetch_scope_candles_cached(
                mk,
                unit=5,
                count=100,
                ttl=10.0,
                force_refresh=force_refresh,
            )
        except (ConnectionError, TimeoutError) as e:
            logger.warning("[scope/candle] %s network error: %s", mk, e)
            return mk, []
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("[scope/candle] %s fetch failed", mk, exc_info=True)
            return mk, []

    with ThreadPoolExecutor(max_workers=4) as pool:
        for mk, candles in pool.map(lambda m: _fetch(m), filtered_markets):
            if candles:
                candle_map[mk] = candles

    # ── 4. BTC regime (once) ──
    btc_detector = get_btc_leading_detector()
    btc_regime = "TREND"
    if btc_detector:
        btc_regime = btc_detector.get_regime_for_lightning()

    # ── 5. Lightweight analysis ──
    FEE_ROUND_TRIP = 0.10
    scored = []

    for mk in candle_map:
        candles_5m = candle_map[mk]
        prices = _extract_prices_from_candles(candles_5m)
        volumes = _extract_volumes_from_candles(candles_5m)
        if len(prices) < 20:
            continue

        current_price = prices[-1]
        if current_price <= 0:
            continue

        # Indicators
        rsi_val = indicators.rsi(prices, 14) or 50.0

        bb = indicators.bollinger_bands(prices, 20, 2.0)
        bb_position = 50.0
        if bb and bb["upper"] != bb["lower"]:
            bb_position = max(0, min(100, (current_price - bb["lower"]) / (bb["upper"] - bb["lower"]) * 100))

        ema_short = indicators.ema(prices, 9)
        ema_mid = indicators.ema(prices, 21)
        ema_aligned = bool(ema_short and ema_mid and ema_short > ema_mid)

        atr_val = indicators.atr_simplified(prices, 14) or 0.0
        atr_pct = (atr_val / current_price * 100) if current_price > 0 else 0.0

        momentum_val = indicators.trend(prices, 5) or 0.0
        macd_line, _, macd_hist = indicators.macd(prices, 12, 26, 9)
        prev_macd_line, prev_macd_hist = None, None
        if len(prices) > 40:
            prev_macd_line, _, prev_macd_hist = indicators.macd(prices[:-1], 12, 26, 9)
        macd_slope = (
            (macd_line - prev_macd_line)
            if macd_line is not None and prev_macd_line is not None
            else None
        )

        # Volume surge
        vol_surge_ratio = 1.0
        if len(volumes) >= 10:
            recent_vol = sum(volumes[-3:]) / 3.0
            avg_vol = sum(volumes[-10:-3]) / 7.0
            if avg_vol > 0:
                vol_surge_ratio = recent_vol / avg_vol

        # Proximity to the low
        lows = [float(c.get("low_price") or 0) for c in candles_5m if c.get("low_price")]
        highs = [float(c.get("high_price") or 0) for c in candles_5m if c.get("high_price")]
        period_low = min(lows) if lows else current_price
        period_high = max(highs) if highs else current_price
        dist_from_low_pct = ((current_price - period_low) / period_low * 100) if period_low > 0 else 999

        # ── 6-Stage lightweight filter (max 88): prioritize low/RSI/MACD ──
        confidence_raw = 0.0

        # Stage 1: Near Low (24)
        if dist_from_low_pct <= 3.0:
            confidence_raw += max(0, 24 - dist_from_low_pct * 8)

        # Stage 2: RSI Oversold (22)
        if rsi_val < 45:
            confidence_raw += min(22, max(0, (45 - rsi_val) * 0.9))

        # Stage 3: BB Lower (14)
        if bb_position < 30:
            confidence_raw += min(14, max(0, (30 - bb_position) * 0.7))

        # Stage 4: Volume Surge (5)
        if vol_surge_ratio > 1.0:
            confidence_raw += min(5, max(0, (vol_surge_ratio - 1.0) * 3.5))

        # Stage 5: EMA inversion — downtrend = dip environment (8)
        ema_inverted = bool(ema_short and ema_mid and ema_short < ema_mid)
        if ema_inverted and rsi_val < 40:
            confidence_raw += 8
        elif ema_inverted:
            confidence_raw += 4

        # Stage 6: MACD histogram reversal (prev<0 → rising) + slope (15)
        macd_hist_turn = (
            prev_macd_hist is not None
            and macd_hist is not None
            and prev_macd_hist < 0
            and macd_hist > prev_macd_hist
        )
        macd_component = 0.0
        if macd_hist_turn:
            macd_component += 10.0
        if macd_slope is not None and macd_slope > 0:
            macd_component += 5.0
        confidence_raw += min(15, macd_component)

        confidence_scaled = min(100, confidence_raw * 100.0 / 88.0)
        regime_mul = {"SHOCK": 0.75, "DRIFT": 0.9, "RECOVERY": 1.02, "TREND": 1.0}.get(btc_regime, 1.0)
        confidence_scaled = round(confidence_scaled * regime_mul, 1)

        # ── Stage 7: price-tier/liquidity/spread adjustment ──
        # Low-priced coins: tick size is large relative to price, so spread-loss risk ↑
        # Low-liquidity coins: slippage risk ↑
        tk_data = ticker_map.get(mk, {})
        _acc_vol_24h = float(tk_data.get("acc_trade_price_24h") or 0)
        _high_24h = float(tk_data.get("high_price") or 0)
        _low_24h = float(tk_data.get("low_price") or 0)

        # (a) Price penalty: disabled [2026-03-08]
        # The dynamic budget (_calc_dynamic_budget) handles low-price/low-liquidity coin risk by
        # shrinking the budget, so there is no need to double-penalize at the scoring stage. Pure signal-based.
        _price_penalty = 1.0

        # (b) Liquidity bonus/penalty: based on 24h trading value
        # under 1M USDT: -10%, 5M USDT or more: +3%, 20M USDT or more: +5%
        _liq_mul = 1.0
        if _acc_vol_24h < 1_000_000:
            _liq_mul = 0.90
        elif _acc_vol_24h >= 20_000_000:
            _liq_mul = 1.05
        elif _acc_vol_24h >= 5_000_000:
            _liq_mul = 1.03

        # (c) 24h volatility: too wide an intraday range is risky
        _daily_range_pct = ((_high_24h - _low_24h) / _low_24h * 100) if _low_24h > 0 else 0
        _vol_penalty = 1.0
        if _daily_range_pct > 15:
            _vol_penalty = 0.90  # extreme volatility
        elif _daily_range_pct > 10:
            _vol_penalty = 0.95

        confidence_scaled = round(confidence_scaled * _price_penalty * _liq_mul * _vol_penalty, 1)
        stages_passed = sum([
            dist_from_low_pct <= 1.8,
            rsi_val < 35,
            bb_position < 22,
            vol_surge_ratio >= 1.8,
            ema_inverted,
            macd_hist_turn,
        ])

        # FIRE level
        if confidence_scaled >= 85:
            fire_level = "STRONG_FIRE"
        elif confidence_scaled >= 70:
            fire_level = "FIRE"
        elif confidence_scaled >= 50:
            fire_level = "READY"
        else:
            fire_level = "HOLD"

        # ── Wave analysis ──
        waves = []
        if len(prices) >= 5:
            extremes = []
            for i in range(2, len(prices) - 2):
                window = prices[i-2:i+3]
                p = prices[i]
                if p == max(window):
                    extremes.append((i, p, 'H'))
                elif p == min(window):
                    extremes.append((i, p, 'L'))
            filtered_ex = []
            for ex in extremes:
                if not filtered_ex or filtered_ex[-1][2] != ex[2]:
                    filtered_ex.append(ex)
                else:
                    if ex[2] == 'H' and ex[1] > filtered_ex[-1][1]:
                        filtered_ex[-1] = ex
                    elif ex[2] == 'L' and ex[1] < filtered_ex[-1][1]:
                        filtered_ex[-1] = ex
            for j in range(len(filtered_ex) - 1):
                a, b = filtered_ex[j], filtered_ex[j+1]
                if a[2] == 'L' and b[2] == 'H' and a[1] > 0:
                    amp_pct = (b[1] - a[1]) / a[1] * 100
                    waves.append({"dir": "UP", "amp_pct": amp_pct, "bars": b[0] - a[0]})
                elif a[2] == 'H' and b[2] == 'L' and a[1] > 0:
                    amp_pct = (a[1] - b[1]) / a[1] * 100
                    waves.append({"dir": "DOWN", "amp_pct": amp_pct, "bars": b[0] - a[0]})

        up_waves = [w for w in waves if w["dir"] == "UP"]
        down_waves = [w for w in waves if w["dir"] == "DOWN"]
        avg_up_amp = sum(w["amp_pct"] for w in up_waves) / len(up_waves) if up_waves else 0
        avg_down_amp = sum(w["amp_pct"] for w in down_waves) / len(down_waves) if down_waves else 0
        avg_wave_bars = sum(w["bars"] for w in waves) / len(waves) if waves else 0
        avg_wave_min = avg_wave_bars * 5
        net_profit = round(avg_up_amp - FEE_ROUND_TRIP, 3) if avg_up_amp > 0 else 0
        profitable = net_profit > 0
        cycles_per_day = round(1440 / (avg_wave_min * 2), 1) if avg_wave_min > 0 else 0
        daily_est = round(net_profit * cycles_per_day, 2) if profitable else 0

        # Optimal parameters
        range_pct = ((period_high - period_low) / period_low * 100) if period_low > 0 else 2.0
        atr_entry = round(max(0.1, min(2.5, atr_pct * 0.5)), 2)
        atr_exit = round(max(0.1, min(2.5, atr_pct * 0.4)), 2)
        wave_entry = round(max(0.1, avg_up_amp * 0.15), 2) if avg_up_amp > 0 else 0
        wave_exit = round(max(0.1, avg_up_amp * 0.12), 2) if avg_up_amp > 0 else 0
        opt_entry = max(atr_entry, wave_entry) if wave_entry > 0 else atr_entry
        opt_exit = max(atr_exit, wave_exit) if wave_exit > 0 else atr_exit
        opt_tp = round(max(SNIPER_MIN_TP_PCT, min(8.0, avg_up_amp * 0.7 if avg_up_amp > 0.5 else atr_pct * 2.0)), 1)
        opt_sl = round(max(SNIPER_MIN_SL_PCT, min(4.0, avg_down_amp * 0.6 if avg_down_amp > 0.3 else atr_pct * 1.2)), 1)

        profit_score = min(100.0, max(0.0, daily_est * 7.0))
        rank_score = round(confidence_scaled * 0.72 + profit_score * 0.28, 2)

        tk = ticker_map.get(mk, {})
        scored.append({
            "market": mk,
            "price": current_price,
            "change_rate": round(float(tk.get("signed_change_rate") or 0) * 100, 2),
            "acc_trade_price_24h": float(tk.get("acc_trade_price_24h") or 0),
            "confidence": confidence_scaled,
            "rank_score": rank_score,
            "fire_level": fire_level,
            "stages_passed": stages_passed,
            "rsi": round(rsi_val, 1),
            "bb_position": round(bb_position, 1),
            "atr_pct": round(atr_pct, 3),
            "vol_surge": round(vol_surge_ratio, 2),
            "ema_aligned": ema_aligned,
            "macd_line": round(macd_line, 4) if macd_line is not None else None,
            "macd_slope": round(macd_slope, 4) if macd_slope is not None else None,
            "wave": {
                "avg_up_amp_pct": round(avg_up_amp, 3),
                "avg_down_amp_pct": round(avg_down_amp, 3),
                "avg_wave_period_min": round(avg_wave_min, 0),
                "net_profit_per_cycle_pct": net_profit,
                "est_cycles_per_day": cycles_per_day,
                "est_daily_profit_pct": daily_est,
                "profitable": profitable,
            },
            "optimal_params": {
                "entry_threshold": opt_entry,
                "exit_threshold": opt_exit,
                "tp_pct": opt_tp,
                "sl_pct": opt_sl,
                "lookback_min": 60 if atr_pct < 1.0 else 30,
            },
            "support_resistance": {
                "support": round(min(prices[-60:]) if len(prices) >= 60 else min(prices), 2),
                "resistance": round(max(prices[-60:]) if len(prices) >= 60 else max(prices), 2),
            },
        })

    # ── 6. Score cutoff + sort + top N ──
    scored_pool = [s for s in scored if float(s.get("confidence") or 0.0) >= float(min_confidence)]

    scored_pool.sort(
        key=lambda x: (
            x.get("confidence", 0.0),
            x.get("rank_score", 0.0),
            x["wave"]["est_daily_profit_pct"],
            x["wave"]["net_profit_per_cycle_pct"],
        ),
        reverse=True,
    )
    deep_pool_size = min(len(scored_pool), max(top_n, min(20, top_n + 6)))
    deep_seed = scored_pool[:deep_pool_size]
    top_results: List[Dict[str, Any]] = []
    if deep_seed:
        deep_results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(4, len(deep_seed) or 1)) as pool:
            future_map = {
                pool.submit(
                    evaluate_scope_deploy_candidate,
                    item.get("market", ""),
                    system,
                    force_refresh=False,
                ): item
                for item in deep_seed
            }
            for future, seed_item in future_map.items():
                try:
                    deploy_eval = future.result(timeout=25)
                except (ConnectionError, TimeoutError) as e:
                    logger.warning("[scope/deploy] %s network error: %s", seed_item.get("market", "?"), e)
                    deploy_eval = None
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                    logger.warning("[scope/deploy] %s eval failed", seed_item.get("market", "?"), exc_info=True)
                    deploy_eval = None
                if not deploy_eval:
                    continue

                indicators_map = deploy_eval.get("indicators", {}) or {}
                merged = dict(seed_item)
                merged["scan_confidence"] = _to_float(seed_item.get("confidence"), 0.0)
                merged["scan_rank_score"] = _to_float(seed_item.get("rank_score"), 0.0)
                merged["confidence"] = _to_float(deploy_eval.get("confidence"), 0.0)
                merged["rank_score"] = _to_float(deploy_eval.get("rank_score"), 0.0)
                merged["fire_level"] = str(deploy_eval.get("fire_level") or seed_item.get("fire_level") or "HOLD")
                merged["stages_passed"] = int(_to_float(deploy_eval.get("stages_passed"), seed_item.get("stages_passed")))
                merged["stages"] = list(deploy_eval.get("stages") or [])
                merged["rsi"] = _to_float(indicators_map.get("rsi_5m"), seed_item.get("rsi"))
                merged["bb_position"] = _to_float(indicators_map.get("bb_position"), seed_item.get("bb_position"))
                merged["atr_pct"] = _to_float(indicators_map.get("atr_pct"), seed_item.get("atr_pct"))
                merged["vol_surge"] = _to_float(indicators_map.get("vol_surge_ratio"), seed_item.get("vol_surge"))
                merged["ema_aligned"] = bool(indicators_map.get("ema_aligned", seed_item.get("ema_aligned")))
                merged["wave"] = dict(deploy_eval.get("wave") or seed_item.get("wave") or {})
                merged["optimal_params"] = dict(deploy_eval.get("recommended_params") or seed_item.get("optimal_params") or {})
                merged["support_resistance"] = dict(deploy_eval.get("support_resistance") or seed_item.get("support_resistance") or {})
                merged["entry_gate"] = dict(deploy_eval.get("entry_gate") or {})
                merged["deploy_ready"] = bool(deploy_eval.get("deploy_ready", False))
                deep_results.append(merged)

        excluded_entry_gate_count = sum(
            1 for item in deep_results
            if not bool(item.get("deploy_ready", False))
        )
        display_results = [
            item for item in deep_results
            if float(item.get("confidence") or 0.0) >= float(min_confidence)
        ]
        display_results.sort(
            key=lambda x: (
                bool(x.get("deploy_ready", False)),
                x.get("confidence", 0.0),
                x.get("rank_score", 0.0),
                (x.get("wave") or {}).get("est_daily_profit_pct", 0.0),
                (x.get("wave") or {}).get("net_profit_per_cycle_pct", 0.0),
            ),
            reverse=True,
        )
        top_results = display_results[:top_n]

    for i, item in enumerate(top_results, 1):
        item["rank"] = i

    final_eligible_count = sum(1 for item in top_results if bool(item.get("deploy_ready", False)))
    profitable_count = sum(1 for s in top_results if bool((s.get("wave") or {}).get("profitable", False)))

    result = {
        "ok": True,
        "timestamp": _time.time(),
        "btc_regime": btc_regime,
        "universe_count": len(markets_all),
        "scanned": len(scored),
        "prefilter_count": len(scored_pool),
        "eligible_count": final_eligible_count,
        "profitable_count": profitable_count,
        "excluded_active": excluded_active_count,
        "excluded_entry_gate": int(locals().get("excluded_entry_gate_count", 0)),
        "excluded_strategy_occupied": excluded_strategy_occupied_count,
        "min_price": float(min_price_eff),
        "max_price": float(max_price_eff),
        "min_confidence": float(min_confidence),
        "focus_market": focus or "",
        "results": top_results,
    }

    _set_cached(cache_key, result)

    # Unify the score source: sync multi-scan results into _scope_scan_cache
    # → so Active Slots confidence references the same source as the recommendation table
    try:
        _sys = getattr(request.app.state, "system", None)
        if _sys is not None:
            _sys._scope_scan_cache = list(top_results)
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[LONGSHORT_API] sync scope scan cache failed: %s", exc, exc_info=True)

    return result


# ============================================================
# Precision Sniper Scope — Multi-Slot System
# [2026-02-24] wave-profitability-based multi-coin scan + slot management
# ============================================================

def _compute_wave_metrics(prices: list, candle_min: int = 5, fee_pct: float = 0.10) -> dict:
    """
    Wave analysis helper: find local extremes → compute amplitude/period/net profit.
    prices: list of close prices (oldest first).
    candle_min: candle interval (minutes).
    fee_pct: round-trip fee (%).
    """
    waves = []
    if len(prices) < 5:
        return {
            "total_waves": 0, "up_waves": 0, "down_waves": 0,
            "avg_up_amp_pct": 0, "avg_down_amp_pct": 0, "avg_wave_amp_pct": 0,
            "avg_wave_period_min": 0, "fee_round_trip_pct": fee_pct,
            "net_profit_per_cycle_pct": 0, "profitable": False,
            "est_cycles_per_day": 0, "est_daily_profit_pct": 0,
        }

    extremes = []
    for i in range(2, len(prices) - 2):
        window = prices[i-2:i+3]
        p = prices[i]
        if p == max(window):
            extremes.append((i, p, 'H'))
        elif p == min(window):
            extremes.append((i, p, 'L'))

    filtered = []
    for ex in extremes:
        if not filtered or filtered[-1][2] != ex[2]:
            filtered.append(ex)
        else:
            if ex[2] == 'H' and ex[1] > filtered[-1][1]:
                filtered[-1] = ex
            elif ex[2] == 'L' and ex[1] < filtered[-1][1]:
                filtered[-1] = ex

    for j in range(len(filtered) - 1):
        a, b = filtered[j], filtered[j+1]
        if a[2] == 'L' and b[2] == 'H' and a[1] > 0:
            amp_pct = (b[1] - a[1]) / a[1] * 100
            period_bars = b[0] - a[0]
            waves.append({"dir": "UP", "amp_pct": amp_pct, "bars": period_bars})
        elif a[2] == 'H' and b[2] == 'L' and a[1] > 0:
            amp_pct = (a[1] - b[1]) / a[1] * 100
            period_bars = b[0] - a[0]
            waves.append({"dir": "DOWN", "amp_pct": amp_pct, "bars": period_bars})

    up_waves = [w for w in waves if w["dir"] == "UP"]
    down_waves = [w for w in waves if w["dir"] == "DOWN"]
    avg_up = sum(w["amp_pct"] for w in up_waves) / len(up_waves) if up_waves else 0
    avg_down = sum(w["amp_pct"] for w in down_waves) / len(down_waves) if down_waves else 0
    avg_amp = sum(w["amp_pct"] for w in waves) / len(waves) if waves else 0
    avg_bars = sum(w["bars"] for w in waves) / len(waves) if waves else 0
    avg_min = avg_bars * candle_min
    net_profit = round(avg_up - fee_pct, 3) if avg_up > 0 else 0
    profitable = net_profit > 0
    cycles_per_day = round(1440 / (avg_min * 2), 1) if avg_min > 0 else 0
    daily_est = round(net_profit * cycles_per_day, 2) if profitable else 0

    return {
        "total_waves": len(waves),
        "up_waves": len(up_waves),
        "down_waves": len(down_waves),
        "avg_up_amp_pct": round(avg_up, 3),
        "avg_down_amp_pct": round(avg_down, 3),
        "avg_wave_amp_pct": round(avg_amp, 3),
        "avg_wave_period_min": round(avg_min, 0),
        "fee_round_trip_pct": fee_pct,
        "net_profit_per_cycle_pct": net_profit,
        "profitable": profitable,
        "est_cycles_per_day": cycles_per_day,
        "est_daily_profit_pct": daily_est,
    }


def _evaluate_market_for_scope(market: str, system: Any) -> Optional[dict]:
    """
    Wave analysis + a simplified confidence score for a single market.
    Heavy computation, so it is called from a ThreadPoolExecutor.
    """
    from app.core.multi_timeframe_ai import fetch_candles, _extract_prices_from_candles, _extract_volumes_from_candles

    try:
        current_price = price_store.get_price(market) or 0.0
        if current_price <= 0:
            try:
                resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=3.0)
                for _t in parse_bybit_list(resp.json()):
                    if isinstance(_t, dict):
                        _tc = normalize_bybit_ticker(_t)
                        if _tc.get("market", "").upper() == market.upper():
                            current_price = float(_tc.get("trade_price") or 0)
                            break
            except Exception as exc:
                logger.warning("[LONGSHORT_API] _evaluate_market_for_scope ticker fallback failed: %s", exc, exc_info=True)
        if current_price <= 0:
            return None

        candles_5m = fetch_candles(market, unit=5, count=100)
        prices_5m = _extract_prices_from_candles(candles_5m)
        volumes_5m = _extract_volumes_from_candles(candles_5m)

        if len(prices_5m) < 20:
            return None

        wave = _compute_wave_metrics(prices_5m, candle_min=5)
        if not wave["profitable"]:
            return None

        # Simplified confidence (lightweight — full scope is for individual lookups)
        rsi_val = indicators.rsi(prices_5m, 14) or 50.0
        atr_val = indicators.atr_simplified(prices_5m, 14) or 0.0
        atr_pct = (atr_val / current_price * 100) if current_price > 0 else 0.0

        ema_s = indicators.ema(prices_5m, 9)
        ema_m = indicators.ema(prices_5m, 21)
        ema_aligned = (ema_s and ema_m and ema_s > ema_m)
        bb = indicators.bollinger_bands(prices_5m, 20, 2.0)
        bb_position = 50.0
        if bb and bb["upper"] != bb["lower"]:
            bb_position = max(0, min(100, (current_price - bb["lower"]) / (bb["upper"] - bb["lower"]) * 100))

        vol_surge = 1.0
        if len(volumes_5m) >= 10:
            recent = sum(volumes_5m[-3:]) / 3.0
            avg = sum(volumes_5m[-10:-3]) / 7.0
            if avg > 0:
                vol_surge = recent / avg

        momentum = indicators.trend(prices_5m, 5) or 0.0
        macd_line, _, macd_hist = indicators.macd(prices_5m, 12, 26, 9)
        prev_macd_line, prev_macd_hist = None, None
        if len(prices_5m) > 40:
            prev_macd_line, _, prev_macd_hist = indicators.macd(prices_5m[:-1], 12, 26, 9)
        macd_slope = (
            (macd_line - prev_macd_line)
            if macd_line is not None and prev_macd_line is not None
            else None
        )

        # Simplified score (lightweight vs the full 8-stage) — optimized for catching lows
        score = 0.0
        lows = [float(c.get("low_price") or 0) for c in candles_5m if c.get("low_price")]
        period_low = min(lows) if lows else current_price
        dist_low = ((current_price - period_low) / period_low * 100) if period_low > 0 else 999
        score += max(0, 24 - dist_low * 8) if dist_low <= 3.0 else 0
        score += min(22, max(0, (45 - rsi_val) * 0.9)) if rsi_val < 45 else 0
        score += min(14, max(0, (30 - bb_position) * 0.7)) if bb_position < 30 else 0
        score += min(5, max(0, (vol_surge - 1.0) * 3.5)) if vol_surge > 1.0 else 0
        # Stage 5: EMA inversion — downtrend = dip environment (8)
        ema_inverted = bool(ema_s and ema_m and ema_s < ema_m)
        if ema_inverted and rsi_val < 40:
            score += 8
        elif ema_inverted:
            score += 4
        # Stage 6: MACD histogram reversal (prev<0 → rising) + slope (15)
        macd_hist_turn = (
            prev_macd_hist is not None
            and macd_hist is not None
            and prev_macd_hist < 0
            and macd_hist > prev_macd_hist
        )
        macd_component = 0.0
        if macd_hist_turn:
            macd_component += 10.0
        if macd_slope is not None and macd_slope > 0:
            macd_component += 5.0
        score += min(15, macd_component)

        # Apply the same BTC regime multiplier as multi-scan — unified formula
        _regime_mul = 1.0
        try:
            btc_detector = get_btc_leading_detector()
            _regime = btc_detector.get_regime_for_lightning()
            _regime_mul = {"SHOCK": 0.75, "DRIFT": 0.9, "RECOVERY": 1.02, "TREND": 1.0}.get(_regime, 1.0)
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[LONGSHORT_API] BTC regime multiplier lookup failed: %s", exc, exc_info=True)
        confidence = round(min(100, max(0, score * 100.0 / 88.0 * _regime_mul)), 1)
        profit_score = min(100.0, max(0.0, wave["est_daily_profit_pct"] * 7.0))
        rank_score = round(confidence * 0.7 + profit_score * 0.3, 2)

        # optimal params
        wave_entry = round(max(0.1, wave["avg_up_amp_pct"] * 0.15), 2) if wave["avg_up_amp_pct"] > 0 else 0
        atr_entry = round(max(0.1, min(2.5, atr_pct * 0.5)), 2)
        opt_entry = max(atr_entry, wave_entry) if wave_entry > 0 else atr_entry
        opt_tp = round(max(SNIPER_MIN_TP_PCT, min(8.0, wave["avg_up_amp_pct"] * 0.7 if wave["avg_up_amp_pct"] > 0.5 else atr_pct * 2.0)), 1)
        opt_sl = round(max(SNIPER_MIN_SL_PCT, min(4.0, wave["avg_down_amp_pct"] * 0.6 if wave["avg_down_amp_pct"] > 0.3 else atr_pct * 1.2)), 1)

        # in_use check
        ctx = system.coordinator.contexts.get(market) if system else None
        in_use = False
        active_strategy = None
        if ctx:
            controls = getattr(ctx, "controls", {}) or {}
            strat = controls.get("strategy", {}) or {}
            if strat.get("enabled"):
                in_use = True
                active_strategy = strat.get("mode")

        return {
            "market": market,
            "price": current_price,
            "confidence": confidence,
            "wave": wave,
            "indicators": {
                "rsi": round(rsi_val, 1),
                "bb_position": round(bb_position, 1),
                "atr_pct": round(atr_pct, 3),
                "ema_aligned": ema_aligned,
                "vol_surge": round(vol_surge, 2),
                "momentum": round(momentum, 2),
                "macd_line": round(macd_line, 4) if macd_line is not None else None,
                "macd_slope": round(macd_slope, 4) if macd_slope is not None else None,
                "dist_from_low_pct": round(dist_low, 2),
            },
            "rank_score": rank_score,
            "recommended_params": {
                "entry_threshold": opt_entry,
                "tp_pct": opt_tp,
                "sl_pct": opt_sl,
                "lookback_min": 60 if atr_pct < 1.0 else 30,
            },
            "in_use": in_use,
            "active_strategy": active_strategy,
        }
    except Exception as e:
        logger.warning("[scope/scan] %s eval failed: %s", market, e, exc_info=True)
        return None


@router.get(
    "/longshort/scope/scan",
    summary="Precision Sniper multi-slot scan — wave profitability ranking",
    responses={200: {"description": "Ranked list of coins by wave profitability"}},
)
def longshort_scope_scan(
    request: Request,
    limit: int = Query(10, ge=1, le=30, description="Number of top coins to return"),
    min_profit: float = Query(0.0, description="Minimum daily profit % to include"),
    exclude_active: bool = Query(True, description="Exclude already-active markets"),
    min_price: float = Query(0, ge=0, description="Minimum coin price (USDT, 0=no limit)"),
    max_price: float = Query(0, ge=0, description="Maximum coin price (USDT, 0=no limit)"),
):
    """
    Wave-profitability-based multi-coin scan.
    1) Pre-filter the top 50 exchange markets by trading volume
    2) Wave analysis per coin (parallel)
    3) Sort by net_profit_per_cycle → return top N
    """
    import time as _time
    system = request.app.state.system

    min_price_eff = max(0.0, float(min_price or 0.0))
    max_price_eff = max(0.0, float(max_price or 0.0))
    if min_price_eff <= 0:
        min_price_eff = max(0.0, _to_float(getattr(system, "longshort_scope_min_price", 0.0), 0.0))
    if max_price_eff <= 0:
        max_price_eff = max(0.0, _to_float(getattr(system, "longshort_scope_max_price", 0.0), 0.0))
    if min_price_eff > 0 and max_price_eff > 0 and max_price_eff < min_price_eff:
        min_price_eff, max_price_eff = max_price_eff, min_price_eff

    cache_key = _build_cache_key(
        "longshort/scope/scan",
        limit=limit,
        min_profit=min_profit,
        exclude_active=exclude_active,
        min_price=round(min_price_eff, 8),
        max_price=round(max_price_eff, 8),
    )
    cached = _get_cached(cache_key, ttl=60.0)
    if cached is not None:
        return cached

    # Phase 1: pre-filter — top markets by volume
    try:
        markets_resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        all_markets = [Q.normalize(str(m.get("symbol") or "")) for m in parse_bybit_list(markets_resp.json()) if isinstance(m, dict) and str(m.get("symbol") or "")]
        if not all_markets:
            return {"ok": True, "items": [], "scanned": 0}

        _market_set = set(m.upper() for m in all_markets[:200])
        ticker_resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        tickers = [normalize_bybit_ticker(t) for t in parse_bybit_list(ticker_resp.json()) if isinstance(t, dict) and Q.normalize(str(t.get("symbol") or "")).upper() in _market_set] if ticker_resp.status_code == 200 else []
    except Exception as e:
        logger.warning("strategy_longshort_router.longshort_scope_scan L1724: %s", e)
        return {"ok": False, "error": f"Market fetch failed: {e}"}

    # Sort by trading value → top 50 (limit API load)
    tickers.sort(key=lambda t: float(t.get("acc_trade_price_24h") or 0), reverse=True)
    prefilter_tickers = tickers[:50]
    if min_price_eff > 0:
        prefilter_tickers = [t for t in prefilter_tickers if float(t.get("trade_price") or 0) >= min_price_eff]
    if max_price_eff > 0:
        prefilter_tickers = [t for t in prefilter_tickers if float(t.get("trade_price") or 0) <= max_price_eff]
    prefilter_markets = [t.get("market") for t in prefilter_tickers if t.get("market")]

    # exclude_active filter
    if exclude_active and system:
        active_set = set()
        try:
            for m in system.oma_registry.get_all_markets():
                st = system.oma_registry.get_state(m)
                if st in (MarketState.ACTIVE, MarketState.RECOVERY):
                    active_set.add(m)
        except (AttributeError, TypeError) as exc:
            logger.warning("[LONGSHORT_API] exclude_active filter failed: %s", exc, exc_info=True)
        prefilter_markets = [m for m in prefilter_markets if m not in active_set]

    # Phase 2: parallel wave analysis
    results = []
    with ThreadPoolExecutor(max_workers=min(4, len(prefilter_markets) or 1)) as pool:
        futures = {pool.submit(_evaluate_market_for_scope, m, system): m for m in prefilter_markets}
        for future in futures:
            try:
                r = future.result(timeout=15)
                if r and r["wave"]["profitable"]:
                    if r["wave"]["est_daily_profit_pct"] >= min_profit:
                        results.append(r)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[LONGSHORT_API] Phase 2 parallel wave analysis failed: %s", exc, exc_info=True)

    # Phase 3: composite sort by recommendation criteria (technical score + wave profit)
    results.sort(
        key=lambda x: (
            x.get("rank_score", 0.0),
            x["wave"]["net_profit_per_cycle_pct"],
            x["wave"]["est_daily_profit_pct"],
        ),
        reverse=True,
    )
    items = results[:limit]

    # Assign ranks
    for i, item in enumerate(items, 1):
        item["rank"] = i

    # Add BTC regime info
    btc_detector = get_btc_leading_detector()
    btc_regime = "TREND"
    if btc_detector:
        btc_regime = btc_detector.get_regime_for_lightning()

    result = {
        "ok": True,
        "scanned": len(prefilter_markets),
        "found": len(results),
        "limit": limit,
        "btc_regime": btc_regime,
        "min_price": float(min_price_eff),
        "max_price": float(max_price_eff),
        "items": items,
        "timestamp": _time.time(),
    }

    _set_cached(cache_key, result)
    return result


@router.post(
    "/longshort/scope/deploy",
    summary="Deploy multiple Precision Sniper slots",
    responses={200: {"description": "Deployment results per market"}},
)
def longshort_scope_deploy(
    request: Request,
    markets: List[str] = Body(..., description="List of markets to deploy"),
    budget_per_slot: float = Body(100, description="Budget per slot (USDT)"),
    auto_reentry: bool = Body(True, description="Enable auto re-entry"),
    no_demote: bool = Body(True, description="Prevent autopilot demote"),
    use_limit: bool = Body(True, description="Use limit orders"),
    fallback_to_market: bool = Body(True, description="Fallback to market order"),
    slicing: bool = Body(False, description="Enable order slicing for large orders"),
):
    """
    Deploy the markets selected from scan results as Precision Sniper slots in bulk.
    Internally reuses the existing SNIPER setup logic.
    """
    from app.manager.sniper_position_store import sniper_store, generate_sniper_id

    # Prevent race conditions on concurrent calls from multiple browsers
    if not _scope_deploy_lock.acquire(blocking=False):
        return {"ok": False, "error": "deploy_in_progress", "results": []}

    try:
        return _longshort_scope_deploy_inner(
            request, sniper_store, generate_sniper_id,
            markets, budget_per_slot, auto_reentry,
            no_demote, use_limit, fallback_to_market, slicing,
        )
    finally:
        _scope_deploy_lock.release()


def _longshort_scope_deploy_inner(
    request, sniper_store, generate_sniper_id,
    markets, budget_per_slot, auto_reentry,
    no_demote, use_limit, fallback_to_market, slicing,
):
    system = request.app.state.system
    results = []
    try:
        scope_min_conf = max(
            0.0,
            min(100.0, float(getattr(system, "longshort_scope_min_conf", 0.0) or 0.0)),
        )
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("strategy_longshort_router._longshort_scope_deploy_inner L1845 except", exc_info=True)
        scope_min_conf = 0.0
    scope_target = max(
        0,
        int(
            getattr(
                system,
                "autopilot_scope_target_n",
                getattr(system, "reserved_sniper_n", 0),
            )
            or 0
        ),
    )
    try:
        scope_overflow_ttl_min = max(0, int(getattr(system, "autopilot_scope_cooldown_min", 60) or 60))
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("strategy_longshort_router._longshort_scope_deploy_inner L1860 except", exc_info=True)
        scope_overflow_ttl_min = 60

    def _current_scope_slot_count() -> int:
        """Count active SNIPER(s) scope slots (profile=SNIPERS + source=precision_scope)."""
        seen_markets = set()
        try:
            for stored in sniper_store.get_all_as_list():
                market = str(stored.get("market") or "").strip().upper()
                if not market:
                    continue
                params = (stored.get("params") or {})
                profile = str(params.get("profile") or "").strip().upper()
                source = str(params.get("source") or "").strip().lower()
                if profile == "SNIPERS" and source == "precision_scope":
                    seen_markets.add(market)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router._current_scope_slot_count fallback: %s", exc, exc_info=True)

        try:
            contexts = getattr(system.coordinator, "contexts", {}) or {}
            for market, ctx in contexts.items():
                mk = str(market or "").strip().upper()
                if not mk:
                    continue
                ctrls = getattr(ctx, "controls", {}) or {}
                strat = ctrls.get("strategy", {}) or {}
                if not bool(strat.get("enabled")):
                    continue
                mode_upper = str(strat.get("mode") or "").strip().upper()
                if mode_upper not in ("SNIPER", "SNIPER(S)"):
                    continue
                params = strat.get("params", {}) or {}
                profile = str(params.get("profile") or "").strip().upper()
                source = str(params.get("source") or "").strip().lower()
                if profile == "SNIPERS" and source == "precision_scope":
                    seen_markets.add(mk)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router._current_scope_slot_count fallback: %s", exc, exc_info=True)

        return len(seen_markets)

    def _is_scope_market_deployed(market: str) -> bool:
        mk = str(market or "").strip().upper()
        if not mk:
            return False
        try:
            for stored in sniper_store.get_all_as_list():
                s_mk = str(stored.get("market") or "").strip().upper()
                if s_mk != mk:
                    continue
                params = (stored.get("params") or {})
                profile = str(params.get("profile") or "").strip().upper()
                source = str(params.get("source") or "").strip().lower()
                if profile == "SNIPERS" and source == "precision_scope":
                    return True
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router._is_scope_market_deployed fallback: %s", exc, exc_info=True)
        try:
            ctx = (getattr(system.coordinator, "contexts", {}) or {}).get(mk)
            if not ctx:
                return False
            ctrls = getattr(ctx, "controls", {}) or {}
            strat = ctrls.get("strategy", {}) or {}
            if not bool(strat.get("enabled")):
                return False
            mode_upper = str(strat.get("mode") or "").strip().upper()
            if mode_upper not in ("SNIPER", "SNIPER(S)"):
                return False
            params = strat.get("params", {}) or {}
            profile = str(params.get("profile") or "").strip().upper()
            source = str(params.get("source") or "").strip().lower()
            return profile == "SNIPERS" and source == "precision_scope"
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("strategy_longshort_router._is_scope_market_deployed L1933 except", exc_info=True)
            return False

    def _collect_scope_slots_for_swap() -> List[Dict[str, Any]]:
        slots_by_market: Dict[str, Dict[str, Any]] = {}
        try:
            for stored in sniper_store.get_all_as_list():
                market = str(stored.get("market") or "").strip().upper()
                if not market:
                    continue
                params = (stored.get("params") or {})
                profile = str(params.get("profile") or "").strip().upper()
                source = str(params.get("source") or "").strip().lower()
                if profile != "SNIPERS" or source != "precision_scope":
                    continue
                row = slots_by_market.get(market) or {"market": market, "sniper_id": "", "ctx": None}
                sid = str(stored.get("sniper_id") or "").strip()
                if sid and not row.get("sniper_id"):
                    row["sniper_id"] = sid
                slots_by_market[market] = row
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router._collect_scope_slots_for_swap fallback: %s", exc, exc_info=True)

        try:
            contexts = getattr(system.coordinator, "contexts", {}) or {}
            for market, ctx in contexts.items():
                mk = str(market or "").strip().upper()
                if not mk:
                    continue
                ctrls = getattr(ctx, "controls", {}) or {}
                strat = ctrls.get("strategy", {}) or {}
                if not bool(strat.get("enabled")):
                    continue
                mode_upper = str(strat.get("mode") or "").strip().upper()
                if mode_upper not in ("SNIPER", "SNIPER(S)"):
                    continue
                params = strat.get("params", {}) or {}
                profile = str(params.get("profile") or "").strip().upper()
                source = str(params.get("source") or "").strip().lower()
                if profile != "SNIPERS" or source != "precision_scope":
                    continue
                row = slots_by_market.get(mk) or {"market": mk, "sniper_id": "", "ctx": None}
                row["ctx"] = ctx
                slots_by_market[mk] = row
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router fallback: %s", exc, exc_info=True)

        if not slots_by_market:
            return []

        score_map: Dict[str, tuple] = {}
        try:
            for item in list(getattr(system, "_scope_scan_cache", []) or []):
                mk = str(item.get("market") or "").strip().upper()
                if not mk:
                    continue
                rank = _to_float(item.get("rank_score"), 0.0)
                conf = _to_float(item.get("confidence"), 0.0)
                prev = score_map.get(mk)
                if prev is None or (rank, conf) > prev:
                    score_map[mk] = (rank, conf)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router fallback: %s", exc, exc_info=True)

        now_ts = float(time_now() or 0.0)
        rows: List[Dict[str, Any]] = []
        for market, row in slots_by_market.items():
            ctx = row.get("ctx") or (getattr(system.coordinator, "contexts", {}) or {}).get(market)
            has_pos = False
            try:
                pos = getattr(ctx, "position", None) or {}
                has_pos = float(pos.get("qty", 0) or 0) > 0
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("strategy_longshort_router._collect_scope_slots_for_swap L2005 except", exc_info=True)
                has_pos = False

            age_sec = 0.0
            try:
                since_active = float(system.oma_registry.get_active_since_ts(market) or 0.0)
                if since_active > 0 and now_ts > 0:
                    age_sec = max(0.0, now_ts - since_active)
            except (TypeError, ValueError):
                logger.warning("strategy_longshort_router._collect_scope_slots_for_swap L2013 except", exc_info=True)
                age_sec = 0.0

            rank_score = 0.0
            confidence = 0.0
            cached = score_map.get(market)
            if cached is not None:
                rank_score, confidence = cached
            else:
                try:
                    eval_result = evaluate_scope_deploy_candidate(market, system, force_refresh=False)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                    logger.warning("strategy_longshort_router._collect_scope_slots_for_swap L2024 except", exc_info=True)
                    eval_result = None
                if eval_result:
                    rank_score = _to_float(eval_result.get("rank_score"), 0.0)
                    confidence = _to_float(eval_result.get("confidence"), 0.0)

            rows.append({
                "market": market,
                "sniper_id": str(row.get("sniper_id") or "").strip(),
                "has_pos": bool(has_pos),
                "rank_score": float(rank_score),
                "confidence": float(confidence),
                "active_age_sec": float(age_sec),
            })
        return rows

    def _pick_scope_swap_out(*, target_market: str) -> Optional[Dict[str, Any]]:
        target = str(target_market or "").strip().upper()
        slots = _collect_scope_slots_for_swap()
        replaceable = [
            s for s in slots
            if not bool(s.get("has_pos"))
            and str(s.get("market") or "").strip().upper() != target
        ]
        if not replaceable:
            return None
        replaceable.sort(
            key=lambda s: (
                float(s.get("rank_score") or 0.0),
                float(s.get("confidence") or 0.0),
                -float(s.get("active_age_sec") or 0.0),
            )
        )
        return replaceable[0]

    def _release_scope_slot_for_manual(*, old_slot: Dict[str, Any], new_market: str) -> bool:
        old_market = str(old_slot.get("market") or "").strip().upper()
        old_sniper_id = str(old_slot.get("sniper_id") or "").strip()
        if not old_market:
            return False

        try:
            if old_sniper_id:
                sniper_store.remove_position(old_sniper_id)
            else:
                sniper_store.remove_positions_by_market(old_market)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router._release_scope_slot_for_manual fallback: %s", exc, exc_info=True)

        try:
            system.oma_set_market(
                market=old_market,
                state=MarketState.WATCH,
                reason=["precision_scope_manual_swap_out", f"to:{new_market}"],
            )
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning(f"[scope/deploy] manual swap release failed {old_market}: {exc}")
            return False

        try:
            ctx = system.coordinator.get_context(old_market)
            if ctx:
                ctx.update_controls({"strategy": {"enabled": False, "mode": ""}})
                if hasattr(ctx, "strategy_mode"):
                    ctx.strategy_mode = ""
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router._release_scope_slot_for_manual fallback: %s", exc, exc_info=True)

        try:
            system._save_context_state()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router._release_scope_slot_for_manual fallback: %s", exc, exc_info=True)

        logger.info(f"[scope/deploy] manual swap-out {old_market} -> {new_market}")
        return True

    def _is_market_in_use_by_other_strategy(market: str) -> Optional[str]:
        mk = str(market or "").strip().upper()
        if not mk:
            return None
        try:
            oma_state = system.oma_registry.get_state(mk)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("strategy_longshort_router._is_market_in_use_by_other_strategy L2106 except", exc_info=True)
            oma_state = None
        if oma_state not in (MarketState.ACTIVE, MarketState.RECOVERY):
            return None
        try:
            ctx = system.coordinator.contexts.get(mk)
            if not ctx:
                return None
            ctrls = getattr(ctx, "controls", {}) or {}
            strat = ctrls.get("strategy", {}) or {}
            if not bool(strat.get("enabled")):
                return None
            mode = str(strat.get("mode") or "").strip()
            params = strat.get("params", {}) or {}
            profile = str(params.get("profile") or "").strip().upper()
            source = str(params.get("source") or "").strip().lower()
            if mode.upper() in ("SNIPER", "SNIPER(S)") and profile == "SNIPERS" and source == "precision_scope":
                return None
            return mode or "UNKNOWN"
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("strategy_longshort_router._is_market_in_use_by_other_strategy L2125 except", exc_info=True)
            return None

    def _scope_eval_from_cache(market: str) -> Optional[Dict[str, Any]]:
        mk = str(market or "").strip().upper()
        if not mk:
            return None
        try:
            for item in list(getattr(system, "_scope_scan_cache", []) or []):
                item_mk = str(item.get("market") or "").strip().upper()
                if item_mk != mk:
                    continue
                rec = item.get("optimal_params", {}) or {}
                wave = item.get("wave", {}) or {}
                rec_tp, rec_sl = _clamp_sniper_tp_sl(
                    _to_float(rec.get("tp_pct"), SNIPER_MIN_TP_PCT),
                    _to_float(rec.get("sl_pct"), SNIPER_MIN_SL_PCT),
                )
                return {
                    "market": mk,
                    "price": _to_float(item.get("price"), _to_float(price_store.get_price(mk), 0.0)),
                    "confidence": _to_float(item.get("confidence"), 0.0),
                    "rank_score": _to_float(item.get("rank_score"), 0.0),
                    "wave": wave,
                    "recommended_params": {
                        "entry_threshold": _to_float(rec.get("entry_threshold"), 0.3),
                        "tp_pct": rec_tp,
                        "sl_pct": rec_sl,
                        "lookback_min": int(_to_float(rec.get("lookback_min"), 60)),
                    },
                }
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router._scope_eval_from_cache fallback: %s", exc, exc_info=True)
        return None

    for raw_market in markets[:20]:  # cap at 20
        market = Q.normalize(raw_market.strip().upper())

        if _is_scope_market_deployed(market):
            results.append({"market": market, "ok": False, "reason": "already_deployed"})
            continue

        manual_overflow_add = False

        # 1) First check deploy feasibility (on failure, do not touch existing slots)
        in_use_by = _is_market_in_use_by_other_strategy(market)  # [FIX N1] fix unassigned NameError
        if in_use_by:
            results.append({"market": market, "ok": False, "reason": f"in_use_by_{in_use_by}"})
            continue

        eval_result = evaluate_scope_deploy_candidate(market, system, force_refresh=True)
        if not eval_result:
            results.append({"market": market, "ok": False, "reason": "analysis_failed"})
            continue
        confidence_now = _to_float(eval_result.get("confidence"), 0.0)
        if scope_min_conf > 0 and confidence_now < scope_min_conf:
            results.append({
                "market": market,
                "ok": False,
                "reason": "confidence_below_min",
                "confidence": round(confidence_now, 1),
                "min_confidence": round(scope_min_conf, 1),
            })
            continue
        entry_gate = eval_result.get("entry_gate", {}) or {}
        if not bool(entry_gate.get("ok")):
            results.append({
                "market": market,
                "ok": False,
                "reason": "entry_gate_blocked",
                "confidence": round(confidence_now, 1),
                "entry_gate": entry_gate,
            })
            continue

        # Slot policy:
        # - below target: normal add
        # - at or above target: allow manual overflow add (up to +2)
        # [2026-03-07] apply manual overflow cap MANUAL_OVERFLOW_MAX(+2)
        if scope_target > 0:
            current_scope_slots = _current_scope_slot_count()
            if current_scope_slots >= scope_target:
                scope_overflow = current_scope_slots - scope_target
                if scope_overflow >= MANUAL_OVERFLOW_MAX:
                    scope_coin_warnings = _generate_coin_warnings(system, market, "SNIPER(S)")
                    results.append({
                        "market": market, "ok": False, "reason": "scope_slot_overflow",
                        "current_slots": current_scope_slots, "target": scope_target,
                        "overflow": scope_overflow, "max_overflow": MANUAL_OVERFLOW_MAX,
                        "warnings": scope_coin_warnings,
                    })
                    continue
                manual_overflow_add = True

        try:
            # Skip if already running as SNIPER
            existing = sniper_store.get_positions_by_market(market)
            if existing:
                results.append({"market": market, "ok": False, "reason": "already_deployed"})
                continue

            rec = eval_result.get("recommended_params", {})
            rec_tp, rec_sl = _clamp_sniper_tp_sl(
                rec.get("tp_pct", SNIPER_MIN_TP_PCT),
                rec.get("sl_pct", SNIPER_MIN_SL_PCT),
            )
            sniper_id = generate_sniper_id(market)
            current_price = _to_float(eval_result.get("price") or price_store.get_price(market) or 0.0, 0.0)
            min_price_eff = max(0.0, _to_float(getattr(system, "longshort_scope_min_price", 0.0), 0.0))
            max_price_eff = max(0.0, _to_float(getattr(system, "longshort_scope_max_price", 0.0), 0.0))
            if min_price_eff <= 0:
                min_price_eff = max(0.0, _to_float(getattr(system, "reserved_candidate_price_min_usdt", 0.0), 0.0))
            if max_price_eff <= 0:
                max_price_eff = max(0.0, _to_float(getattr(system, "reserved_candidate_price_max_usdt", 0.0), 0.0))
            if min_price_eff > 0 and max_price_eff > 0 and max_price_eff < min_price_eff:
                min_price_eff, max_price_eff = max_price_eff, min_price_eff

            if current_price <= 0:
                results.append({"market": market, "ok": False, "reason": "price_unavailable"})
                continue
            if min_price_eff > 0 and current_price < min_price_eff:
                results.append({
                    "market": market,
                    "ok": False,
                    "reason": "price_below_min",
                    "price": current_price,
                    "min_price": min_price_eff,
                })
                continue
            if max_price_eff > 0 and current_price > max_price_eff:
                results.append({
                    "market": market,
                    "ok": False,
                    "reason": "price_above_max",
                    "price": current_price,
                    "max_price": max_price_eff,
                })
                continue

            requested_budget = max(5.0, _to_float(budget_per_slot, 100.0))
            cap_budget = _snipers_budget_cap_by_price(current_price)
            # scope/deploy uses a manual budget set by the user, so the cap is not enforced.
            applied_budget = requested_budget

            # Set OMA state to ACTIVE
            system.oma_set_market(
                market=market,
                state=MarketState.ACTIVE,
                reason=["precision_scope_deploy"],
            )

            # Set budget
            current_state = system.oma_registry.get_state(market) or MarketState.ACTIVE
            system.oma_registry.set_state(market, current_state, reason=["precision_scope_budget"], budget_usdt=applied_budget)

            # Build SNIPER parameters
            params = {
                "profile": "SNIPERS",
                "side": "LONG",
                "entry_enabled": True,
                "entry_lookback_min": rec.get("lookback_min", 60),
                "entry_threshold_pct": rec.get("entry_threshold", 0.3),
                "exit_enabled": True,
                "exit_lookback_min": rec.get("lookback_min", 60),
                "exit_threshold_pct": rec.get("entry_threshold", 0.3),
                "expiry_min": 30,
                "tp_pct": rec_tp,
                "sl_pct": rec_sl,
                "trail_tp": False,
                "trail_dist_pct": 1.5,
                "ai_gate_enabled": True,
                "ai_min_score": 0.55,
                "ai_min_score_scope": 0.55,
                "scope_relaxed_ai_min": 0.50,
                "rsi_entry_enabled": True,
                "rsi_entry_max_scope": 42.0,
                "scope_relaxed_rsi_entry_max": 48.0,
                "scope_relax_after_min": 20.0,
                "rsi_exit_enabled": True,
                "vol_spike_enabled": False,
                "vol_spike_mult": 2.0,
                "auto_reentry": auto_reentry,
                "atr_auto": False,
                "watch_sec": 90,
                "confirm_window_sec": 240,
                "dca_step_pct": max(0.1, min(5.0, _to_float(getattr(system, "sniper_dca_step_pct", 0.2), 0.2))),
                "dca_add_ratio": max(0.1, min(2.0, _to_float(getattr(system, "sniper_dca_add_ratio", 0.5), 0.5))),
                "dca_max_depth_pct": max(0.2, min(10.0, _to_float(getattr(system, "sniper_dca_max_depth_pct", 1.0), 1.0))),
                # Leave some budget at probe/confirm so DCA can actually be executed.
                "dca_reserve_ratio": max(0.0, min(0.6, _to_float(getattr(system, "sniper_dca_reserve_ratio", 0.2), 0.2))),
                "time_stop_min": 45,
                "time_filter_enabled": False,
                "time_start": "09:00",
                "time_end": "18:00",
                "use_limit": use_limit,
                "fallback_to_market": fallback_to_market,
                "slicing": slicing,
                "hold_sell": False,
                "cycle_mode": "UP",
                "no_demote": no_demote,
                "mode": "near_low",
                "source": "precision_scope",
                "deploy_confidence": _to_float(eval_result.get("confidence"), 0.0),
                "deploy_rank_score": _to_float(eval_result.get("rank_score"), 0.0),
                "scope_deploy_ts": float(time_now() or 0.0),  # [FIX M1] record deploy time
                "buy_now": False,  # [FIX L4] remove manual_swap_out dead code (always False)
            }
            if manual_overflow_add:
                now_ts = float(time_now() or 0.0)
                params.update({
                    "scope_overflow_manual": True,
                    "scope_overflow_started_ts": now_ts,
                    "scope_overflow_ttl_min": scope_overflow_ttl_min,
                })

            # Set up the strategy context
            ctx = system.coordinator.get_context(market)
            if not ctx:
                ctx = system.coordinator.ensure_market(market)

            # Sync context capital immediately so buy sizing follows input budget.
            try:
                pos0 = getattr(ctx, "position", None) or {}
                qty0 = float(pos0.get("qty", 0.0) or 0.0)
                has_pos0 = qty0 > 0.0
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("strategy_longshort_router._scope_eval_from_cache L2350 except", exc_info=True)
                qty0 = 0.0
                has_pos0 = False

            used_usdt = 0.0
            if has_pos0:
                try:
                    entry0 = float(
                        pos0.get("avg_price", 0.0)
                        or pos0.get("entry_price", 0.0)
                        or pos0.get("entry", 0.0)
                        or 0.0
                    )
                    if entry0 > 0.0 and qty0 > 0.0:
                        used_usdt = float(entry0 * qty0)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("strategy_longshort_router._scope_eval_from_cache L2365 except", exc_info=True)
                    used_usdt = 0.0

            ctx.allocated_capital = float(applied_budget)
            if has_pos0:
                ctx.usable_capital = max(0.0, float(applied_budget) - float(used_usdt))
            else:
                ctx.usable_capital = float(applied_budget)
            ctx.wallet_mode = bool(getattr(system, "wallet_mode", False))

            ctx.update_controls({
                "strategy": {
                    "enabled": True,
                    "mode": "SNIPER(s)",
                    "params": params,
                }
            })
            ctx.strategy_mode = "SNIPER(s)"
            system._save_context_state()

            # Save to position store
            sniper_store.save_position(sniper_id, {
                "budget_usdt": applied_budget,
                "params": params,
            })

            # Precision Scope slots are selected by recommendation score, so attempt to buy immediately after placement.
            # User request: enter right after slot placement with no SCANNING delay.
            buy_now_result = None
            buy_now_reason = "scope_deploy_buy_now"  # [FIX L4] remove manual_swap_out dead code
            should_buy_now = True
            if should_buy_now:
                has_pos = False
                try:
                    pos = getattr(ctx, "position", None) or {}
                    has_pos = float(pos.get("qty", 0) or 0) > 0
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("strategy_longshort_router._scope_eval_from_cache L2401 except", exc_info=True)
                    has_pos = False

                if has_pos:
                    buy_now_result = {"ok": False, "msg": "already_has_position"}
                else:
                    current_price = _to_float(eval_result.get("price") or price_store.get_price(market) or 0.0, 0.0)
                    fsm = getattr(system, "order_fsm", None)
                    if fsm:
                        ok, msg = fsm.submit_market_buy(
                            ctx=ctx,
                            market=market,
                            quote_amount=applied_budget,
                            expected_price=current_price,
                            reason=f"sniper:{buy_now_reason}",
                        )
                        buy_now_result = {
                            "ok": bool(ok),
                            "msg": str(msg),
                            "price": current_price,
                        }
                        # [FIX M12] LIVE fill: the position is recorded by FSM apply_fill_buy() at the actual fill price
                        # Pre-calling open_position() records a duplicate at the expected price, skewing the entry price
                    elif str(getattr(system, "trading_mode", "")).upper() == "PAPER":
                        if current_price > 0:
                            try:
                                ctx.open_position(
                                    entry_price=current_price,
                                    usdt_amount=applied_budget,
                                    source="paper_scope_swap_buy_now",
                                )
                                system.ledger.append(
                                    "PAPER_BUY_NOW",
                                    market=market,
                                    price=current_price,
                                    usdt=applied_budget,
                                )
                                system._save_context_state()
                            except (AttributeError, TypeError) as exc:
                                logger.warning("[LONGSHORT_API] paper open_position failed: %s", exc, exc_info=True)
                            buy_now_result = {"ok": True, "msg": "paper_filled", "price": current_price}
                        else:
                            buy_now_result = {"ok": False, "msg": "no_price_for_paper"}
                    else:
                        buy_now_result = {"ok": False, "msg": "order_fsm_unavailable"}

            results.append({
                "market": market,
                "ok": True,
                "sniper_id": sniper_id,
                "swap_out_market": "",  # [FIX L4] remove manual_swap_out dead code
                "manual_overflow_add": manual_overflow_add,
                "buy_now_result": buy_now_result,
                "buy_now_reason": buy_now_reason,
                "params": {
                    "tp_pct": params["tp_pct"],
                    "sl_pct": params["sl_pct"],
                    "entry_threshold": params["entry_threshold_pct"],
                    "auto_reentry": params["auto_reentry"],
                },
                "confidence": eval_result.get("confidence", 0),
                "wave_daily_profit": eval_result["wave"]["est_daily_profit_pct"],
                "budget_requested_usdt": requested_budget,
                "budget_cap_usdt": cap_budget,
                "budget_applied_usdt": applied_budget,
                "budget_above_cap": bool(applied_budget > cap_budget),
                "warnings": _generate_coin_warnings(system, market, "SNIPER(S)"),
            })
        except Exception as e:
            logger.warning(f"[scope/deploy] {market} failed: {e}")
            results.append({"market": market, "ok": False, "reason": str(e)})

    deployed = sum(1 for r in results if r.get("ok"))
    return {
        "ok": True,
        "deployed": deployed,
        "total": len(results),
        "results": results,
    }


@router.get(
    "/longshort/scope/slots",
    summary="Get active Precision Sniper scope slots status",
    responses={200: {"description": "Active scope slot status list"}},
)
def longshort_scope_slots(request: Request):
    """
    Current status of running LONG/SHORT SNIPER(s) slots.

    Criteria:
    - include only slots where profile == SNIPERS and source == precision_scope
    - sniper_store first + supplement from context when missing in the store
    """
    from app.manager.sniper_position_store import sniper_store

    system = request.app.state.system
    all_positions = sniper_store.get_all_as_list()
    contexts = getattr(system.coordinator, "contexts", {}) or {}
    _scope_scan_map: Dict[str, Dict[str, float]] = {}
    try:
        for item in list(getattr(system, "_scope_scan_cache", []) or []):
            mk = str(item.get("market") or "").strip().upper()
            if not mk:
                continue
            _scope_scan_map[mk] = {
                "confidence": _to_float(item.get("confidence"), 0.0),
                "rank_score": _to_float(item.get("rank_score"), 0.0),
            }
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[LONGSHORT_API] strategy_longshort_router.longshort_scope_slots fallback: %s", exc, exc_info=True)

    # Query exchange balances once → cache as {currency: {avg_buy_price, balance, locked}}
    _exchange_balances: Dict[str, Dict[str, Any]] = {}
    try:
        tc = getattr(system, "trade_client", None)
        if tc:
            for acc in tc.accounts(skip_currencies=[Q.symbol, "USDT"]):
                cur = acc.get("currency", "")
                if cur:
                    _exchange_balances[cur] = acc
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[LONGSHORT_API] exchange balance fetch failed: %s", exc, exc_info=True)

    # Collect current prices for all slot markets from price_store first,
    # then supplement markets missing from price_store with one exchange ticker API call
    _all_markets = list({
        str(p.get("market") or "").strip().upper()
        for p in all_positions if p.get("market")
    })
    _price_cache: Dict[str, float] = {}
    _missing_markets: List[str] = []
    for _m in _all_markets:
        _p = float(price_store.get_price(_m) or 0)
        if _p > 0:
            _price_cache[_m] = _p
        else:
            _missing_markets.append(_m)
    if _missing_markets:
        try:
            from app.integrations.bybit_markets import fetch_bybit_tickers
            tickers = fetch_bybit_tickers(_missing_markets, timeout=5.0)
            for t in tickers:
                _mk = str(t.get("market") or "")
                _tp = float(t.get("trade_price") or 0)
                if _mk and _tp > 0:
                    _price_cache[_mk] = _tp
                    # Also populate price_store so it is immediately usable on the next call
                    price_store.set_price(_mk, _tp)
            _still_missing = [m for m in _missing_markets if m not in _price_cache]
            if _still_missing:
                logger.warning(f"[scope_slots] price unavailable after ticker fallback: {_still_missing}")
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning(f"[scope_slots] ticker fallback failed for {_missing_markets}: {exc}")

    def _build_slot(sniper_id: str, market: str, params: Dict[str, Any], budget: float) -> Dict[str, Any]:
        ctx = contexts.get(market)
        tp_pct, sl_pct = _clamp_sniper_tp_sl(
            params.get("tp_pct", SNIPER_MIN_TP_PCT),
            params.get("sl_pct", SNIPER_MIN_SL_PCT),
        )

        pos = {}
        pnl_pct = 0.0
        pnl_amount = 0.0
        entry_price = 0.0
        invested_usdt = 0.0
        qty = 0.0
        current_price = _price_cache.get(market, 0.0)
        live_cached_conf = _to_float((_scope_scan_map.get(market) or {}).get("confidence"), 0.0)
        live_cached_rank = _to_float((_scope_scan_map.get(market) or {}).get("rank_score"), 0.0)
        param_live_conf = _to_float(params.get("live_confidence"), 0.0)
        param_live_rank = _to_float(params.get("live_rank_score"), 0.0)
        deploy_conf = _to_float(params.get("deploy_confidence"), 0.0)
        deploy_rank = _to_float(params.get("deploy_rank_score"), 0.0)
        confidence = live_cached_conf if live_cached_conf > 0 else (param_live_conf if param_live_conf > 0 else deploy_conf)
        rank_score = live_cached_rank if live_cached_rank > 0 else (param_live_rank if param_live_rank > 0 else deploy_rank)

        # 1) Look up qty/entry from the context position
        if ctx:
            pos = getattr(ctx, "position", None) or {}
            if pos:
                qty = float(pos.get("qty", 0) or 0)
                entry_price = float(pos.get("entry", 0) or 0)

        # 2) No context or entry=0 → fall back to exchange balance
        coin = Q.extract_base(market)
        if coin in _exchange_balances:
            bal = _exchange_balances[coin]
            if qty <= 0:
                qty = float(bal.get("balance", 0) or 0) + float(bal.get("locked", 0) or 0)
            if entry_price <= 0:
                entry_price = float(bal.get("avg_buy_price", 0) or 0)

        # 3) PnL calculation — cannot compute if current_price is 0 (price_store not received)
        if qty > 0 and entry_price > 0:
            invested_usdt = qty * entry_price
            if current_price > 0:
                current_val = qty * current_price
                pnl_amount = current_val - invested_usdt
                pnl_pct = (pnl_amount / invested_usdt * 100) if invested_usdt > 0 else 0

        # [2026-03-08] Slot state definitions:
        # ACTIVE = holding (fill complete, operating normally)
        # HOLDING = waiting for order fill (slippage/unfilled state)
        # WAITING = empty slot (should not appear in the buy-immediately design)
        state_str = "WAITING"
        _has_pending_order = False
        if ctx:
            _os = getattr(ctx, "order_state", None)
            if _os and isinstance(_os, dict):
                _has_pending_order = str(_os.get("state", "")).lower() in ("pending", "submitted", "open")
        if qty > 0:
            state_str = "ACTIVE"
        elif _has_pending_order:
            state_str = "HOLDING"
        elif ctx:
            controls = getattr(ctx, "controls", {}) or {}
            strat = controls.get("strategy", {}) or {}
            if strat.get("enabled"):
                state_str = "SCANNING"

        is_scope = str(params.get("source") or "").strip().lower() == "precision_scope"
        if is_scope and qty <= 0:
            try:
                live_eval = evaluate_scope_deploy_candidate(market, system, force_refresh=False)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.warning("strategy_longshort_router._build_slot L2627 except", exc_info=True)
                live_eval = None
            if live_eval:
                confidence = _to_float(live_eval.get("confidence"), confidence)
                rank_score = _to_float(live_eval.get("rank_score"), rank_score)
        return {
            "sniper_id": sniper_id,
            "market": market,
            "state": state_str,
            "source": "precision_scope" if is_scope else "manual",
            "budget_usdt": float(budget or 0),
            "invested_usdt": round(invested_usdt, 0),
            "entry_price": entry_price,
            "current_price": current_price,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_amount": round(pnl_amount, 0),
            "confidence": round(confidence, 2),
            "rank_score": round(rank_score, 6),
            "params": {
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "auto_reentry": params.get("auto_reentry", False),
                "entry_threshold": params.get("entry_threshold_pct"),
            },
            "_debug": {
                "has_ctx": ctx is not None,
                "has_pos": bool(pos),
                "pos_entry_raw": pos.get("entry") if pos else None,
                "pos_qty_raw": pos.get("qty") if pos else None,
                "exchange_bal": bool(_exchange_balances.get(Q.extract_base(market))),
                "price_from_store": float(price_store.get_price(market) or 0),
                "price_from_cache": _price_cache.get(market),
                "missing_markets": _missing_markets,
            },
        }

    def _prune_waiting_scope_slot(*, market: str, sniper_id: str) -> None:
        """Remove stale WAITING scope slot so autofill can reuse the slot immediately."""
        try:
            if sniper_id:
                removed = bool(sniper_store.remove_position(sniper_id))
            else:
                removed = bool(sniper_store.remove_positions_by_market(market))
        except (AttributeError, TypeError):
            logger.warning("strategy_longshort_router._prune_waiting_scope_slot L2670 except", exc_info=True)
            removed = False

        try:
            ctx = contexts.get(market)
            if ctx:
                controls = getattr(ctx, "controls", {}) or {}
                strat = controls.get("strategy", {}) or {}
                if bool(strat.get("enabled")):
                    ctx.update_controls({"strategy": {"enabled": False, "mode": ""}})
                    if hasattr(ctx, "strategy_mode"):
                        ctx.strategy_mode = ""
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[LONGSHORT_API] strategy_longshort_router._prune_waiting_scope_slot fallback: %s", exc, exc_info=True)

        # Avoid immediate same-market re-pick after exit; keep replacement opportunities open.
        try:
            cooldown_min = int(getattr(system, "autopilot_scope_cooldown_min", 60) or 0)
            mgr = getattr(system, "autopilot_manager", None)
            if mgr is not None and hasattr(mgr, "mark_cooldown"):
                mgr.mark_cooldown(market, minutes=max(0, cooldown_min), reason="scope_waiting_slot_gc")
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LONGSHORT_API] scope waiting slot cooldown failed: %s", exc, exc_info=True)

        if removed:
            try:
                system._save_context_state()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[LONGSHORT_API] save context state failed: %s", exc, exc_info=True)

    slots: List[Dict[str, Any]] = []
    seen_keys = set()
    seen_markets = set()

    # 1) Based on sniper_store: include only profile=SNIPERS + source=precision_scope
    # [2026-03-08] Orphan position GC: strategy disabled + no balance → remove from store
    _gc_remove_ids: List[str] = []
    for stored in all_positions:
        market = str(stored.get("market") or "").strip().upper()
        if not market:
            continue
        params = stored.get("params", {}) or {}
        profile = str(params.get("profile") or "").strip().upper()
        source = str(params.get("source") or "").strip().lower()
        if profile != "SNIPERS" or source != "precision_scope":
            continue

        sniper_id = str(stored.get("sniper_id") or "").strip() or market
        if sniper_id in seen_keys or market in seen_markets:
            continue

        # Check whether the strategy is enabled
        ctx = contexts.get(market)
        _strat_enabled = False
        if ctx:
            _ctrl = getattr(ctx, "controls", {}) or {}
            _st = _ctrl.get("strategy", {}) or {}
            _strat_enabled = bool(_st.get("enabled"))

        # Check for balance (HOLDING)
        coin = Q.extract_base(market)
        _has_balance = False
        if coin in _exchange_balances:
            _bal = float(_exchange_balances[coin].get("balance", 0) or 0)
            _locked = float(_exchange_balances[coin].get("locked", 0) or 0)
            _has_balance = (_bal + _locked) > 0

        # Strategy disabled + no balance → orphan position, remove from store
        if not _strat_enabled and not _has_balance:
            _gc_remove_ids.append(sniper_id)
            logger.info(f"[scope_slots GC] removing orphan: {sniper_id} ({market})")
            continue

        budget = float(stored.get("budget_usdt") or 0)
        slot = _build_slot(sniper_id=sniper_id, market=market, params=params, budget=budget)
        slots.append(slot)
        seen_keys.add(sniper_id)
        seen_markets.add(market)

    # Remove orphan positions in bulk
    for _rid in _gc_remove_ids:
        try:
            sniper_store.remove_position(_rid)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[LONGSHORT_API] orphan position removal failed: %s", exc, exc_info=True)

    # 2) Supplement store gaps: add active strategies from context with profile=SNIPERS + source=precision_scope
    for market, ctx in list(contexts.items()):
        if market in seen_markets:
            continue

        controls = getattr(ctx, "controls", {}) or {}
        strat = controls.get("strategy", {}) or {}
        if not strat.get("enabled"):
            continue
        mode_upper = str(strat.get("mode") or "").strip().upper()
        if mode_upper not in ("SNIPER", "SNIPER(S)"):
            continue

        params = strat.get("params", {}) or {}
        profile = str(params.get("profile") or "").strip().upper()
        source = str(params.get("source") or "").strip().lower()
        if profile != "SNIPERS" or source != "precision_scope":
            continue

        budget = float(system.oma_registry.get_budget_usdt(market) or 0)
        slots.append(_build_slot(sniper_id=market, market=market, params=params, budget=budget))
        seen_markets.add(market)

    # Sort by PnL percentage
    slots.sort(key=lambda s: s["pnl_pct"], reverse=True)

    total_budget = sum(s["budget_usdt"] for s in slots)
    total_pnl = sum(s["pnl_amount"] for s in slots)

    return {
        "ok": True,
        "count": len(slots),
        "total_budget_usdt": total_budget,
        "total_pnl": round(total_pnl, 0),
        "slots": slots,
    }


# ============================================================
# END OF FILE
# ============================================================
