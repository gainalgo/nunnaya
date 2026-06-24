# ============================================================
# File: app/api/strategy_router.py
# Autocoin OS v3-H — Strategy Router (Unified / Readable)
#
# Role summary:
# - Exposes the Strategy system's decisions/scores/state to the outside (UI/OMA) as read-only "queries"
# - Calculation, selection, and execution authority are never handled here
#
# Section layout:
#  A. Last Strategy State        (single latest state per market)
#  B. Strategy Score Table View  (admin/decision table after READY)
# ============================================================

from fastapi import APIRouter, Request, Query
from typing import Dict, Any, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from pydantic import BaseModel
from app.manager.oma_market_registry import MarketState
from app.core.hyper_price_store import price_store
from app.core.constants import (
    BYBIT_MARKET_TICKERS,
    BYBIT_MARKET_KLINE,
    BYBIT_MARKET_INSTRUMENTS,
)
import requests
from concurrent.futures import ThreadPoolExecutor
from app.strategy import indicators
from app.manager.ai_trainer import ai_trainer
from app.api.strategy_longshort_router import _compute_wave_metrics
from app.core.currency import Q
from app.manager.topn_selector import (
    PROFILE_WEIGHTS,
    rank_topn_by_public_candles,
    MarketFeatures,
)
try:
    from app.ai.coin_tiers import adjust_ai_score_for_strategy, get_regime_fit
except ImportError:
    logger.warning("strategy_router.unknown L41 except", exc_info=True)
    def adjust_ai_score_for_strategy(ai_score, strategy=None, regime=None):
        return {"adjusted_score": ai_score, "should_buy": ai_score >= 0.4, "tp_scale": 1.0, "sl_scale": 1.0, "confidence": 0.5}
    def get_regime_fit(regime, strategy=None):
        return 0.5

# [2026-02-01] [PROTECTED] strategy -> topn_selector profile mapping
# DO NOT MODIFY - each strategy must be scored with the profile matching its characteristics
# Changing this mapping breaks per-strategy coin selection
STRATEGY_TO_PROFILE: Dict[str, str] = {
    "PINGPONG": "pingpong",    # range-bound, sideways, volatility
    "AUTOLOOP": "autorope",    # liquidity + moderate volatility
    "LADDER": "ladder",        # trend following, scaled buying
    "LIGHTNING": "lightning",  # breakout, momentum
    "GAZUA": "gazua",          # strong upward momentum
    "CONTRARIAN": "pingpong",  # contrarian = similar to volatility + range-bound
    "SNIPER": "lightning",     # pump sniping = similar to momentum
}
import threading
from time import time as time_now
from app.monitor.btc_leading_signal import get_btc_leading_detector
from app.api.strategy_utils import (
    _cache, _cache_lock, CACHE_TTL,
    SNIPER_MIN_TP_PCT, SNIPER_MIN_SL_PCT, MANUAL_OVERFLOW_MAX,
    _get_cached, _set_cached, _build_cache_key,
    _to_float, _clamp_sniper_tp_sl,
    _snipers_budget_cap_by_price, _cap_snipers_budget,
    _count_strategy_active_slots, _get_strategy_slot_target,
    _check_manual_overflow, _generate_coin_warnings,
    _fetch_scope_candles_cached, _sync_policy_tp_sl,
    StrategyStopRequest,
)

# ============================================================
# Endpoint-specific locks (not shared utilities — stay here)
# ============================================================
_scope_deploy_lock = threading.Lock()  # prevent concurrent deploy across multiple browsers
_sniper_setup_lock = threading.Lock()  # [FIX M11] prevent concurrent setup_sniper calls (duplicate position risk)
_recommend_semaphore = threading.Semaphore(1)  # limit recommendation computation to 1 at a time (protects tick loop thread pool)

# ============================================================
# [2026-03-03] 6-Stage Scope Score — unified recommendation/active score utility
# Extracts the core confidence/rank_score logic of longshort_multi_scan
# into a standalone function so reserved_selector can reuse it.
# ============================================================
def compute_scope_score(
    market: str,
    *,
    btc_regime: str = "TREND",
) -> Optional[Dict[str, Any]]:
    """Compute 6-stage confidence + rank_score for a single market based on 5-minute candles.

    Returns dict with keys: confidence, rank_score, rsi, bb_position,
    atr_pct, vol_surge, ema_aligned, fire_level, stages_passed,
    optimal_params, support_resistance, wave   — or None on failure.
    """
    try:
        from app.core.multi_timeframe_ai import (
            fetch_candles,
            _extract_prices_from_candles,
            _extract_volumes_from_candles,
        )
        from app.strategy import indicators as _ind
    except (ImportError, AttributeError, TypeError):
        logger.warning("strategy_router.compute_scope_score L105 except", exc_info=True)
        return None

    try:
        candles_5m = _fetch_scope_candles_cached(market, unit=5, count=100, ttl=10.0, force_refresh=False)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
        logger.warning("strategy_router.compute_scope_score L110 except", exc_info=True)
        return None
    if not candles_5m:
        return None

    prices = _extract_prices_from_candles(candles_5m)
    volumes = _extract_volumes_from_candles(candles_5m)
    if len(prices) < 20:
        return None

    current_price = prices[-1]
    if current_price <= 0:
        return None

    FEE_ROUND_TRIP = 0.10

    rsi_val = _ind.rsi(prices, 14) or 50.0
    bb = _ind.bollinger_bands(prices, 20, 2.0)
    bb_position = 50.0
    if bb and bb["upper"] != bb["lower"]:
        bb_position = max(0, min(100, (current_price - bb["lower"]) / (bb["upper"] - bb["lower"]) * 100))

    ema_short = _ind.ema(prices, 9)
    ema_mid = _ind.ema(prices, 21)
    ema_aligned = bool(ema_short and ema_mid and ema_short > ema_mid)

    atr_val = _ind.atr_simplified(prices, 14) or 0.0
    atr_pct = (atr_val / current_price * 100) if current_price > 0 else 0.0

    macd_line, _, macd_hist = _ind.macd(prices, 12, 26, 9)
    prev_macd_line, prev_macd_hist = None, None
    if len(prices) > 40:
        prev_macd_line, _, prev_macd_hist = _ind.macd(prices[:-1], 12, 26, 9)
    macd_slope = (
        (macd_line - prev_macd_line)
        if macd_line is not None and prev_macd_line is not None
        else None
    )

    vol_surge_ratio = 1.0
    if len(volumes) >= 10:
        recent_vol = sum(volumes[-3:]) / 3.0
        avg_vol = sum(volumes[-10:-3]) / 7.0
        if avg_vol > 0:
            vol_surge_ratio = recent_vol / avg_vol

    lows = [float(c.get("low_price") or 0) for c in candles_5m if c.get("low_price")]
    highs = [float(c.get("high_price") or 0) for c in candles_5m if c.get("high_price")]
    period_low = min(lows) if lows else current_price
    period_high = max(highs) if highs else current_price
    dist_from_low_pct = ((current_price - period_low) / period_low * 100) if period_low > 0 else 999

    # ── 6-Stage lightweight filter (max 88) ──
    confidence_raw = 0.0
    if dist_from_low_pct <= 3.0:
        confidence_raw += max(0, 24 - dist_from_low_pct * 8)
    if rsi_val < 45:
        confidence_raw += min(22, max(0, (45 - rsi_val) * 0.9))
    if bb_position < 30:
        confidence_raw += min(14, max(0, (30 - bb_position) * 0.7))
    if vol_surge_ratio > 1.0:
        confidence_raw += min(5, max(0, (vol_surge_ratio - 1.0) * 3.5))

    ema_inverted = bool(ema_short and ema_mid and ema_short < ema_mid)
    if ema_inverted and rsi_val < 40:
        confidence_raw += 8
    elif ema_inverted:
        confidence_raw += 4

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

    stages_passed = sum([
        dist_from_low_pct <= 1.8,
        rsi_val < 35,
        bb_position < 22,
        vol_surge_ratio >= 1.8,
        ema_inverted,
        macd_hist_turn,
    ])

    if confidence_scaled >= 85:
        fire_level = "STRONG_FIRE"
    elif confidence_scaled >= 70:
        fire_level = "FIRE"
    elif confidence_scaled >= 50:
        fire_level = "READY"
    else:
        fire_level = "HOLD"

    # ── Wave analysis [FIX L5: use shared helper _compute_wave_metrics to remove duplication] ──
    _wm = _compute_wave_metrics(prices, candle_min=5, fee_pct=FEE_ROUND_TRIP)
    avg_up_amp = _wm["avg_up_amp_pct"]
    avg_down_amp = _wm["avg_down_amp_pct"]
    avg_wave_min = _wm["avg_wave_period_min"]
    net_profit = _wm["net_profit_per_cycle_pct"]
    profitable = _wm["profitable"]
    cycles_per_day = _wm["est_cycles_per_day"]
    daily_est = _wm["est_daily_profit_pct"]

    opt_entry = round(max(0.1, min(2.5, atr_pct * 0.5)), 2)
    opt_exit = round(max(0.1, min(2.5, atr_pct * 0.4)), 2)
    wave_entry = round(max(0.1, avg_up_amp * 0.15), 2) if avg_up_amp > 0 else 0
    wave_exit = round(max(0.1, avg_up_amp * 0.12), 2) if avg_up_amp > 0 else 0
    if wave_entry > 0:
        opt_entry = max(opt_entry, wave_entry)
    if wave_exit > 0:
        opt_exit = max(opt_exit, wave_exit)
    opt_tp = round(max(SNIPER_MIN_TP_PCT, min(8.0, avg_up_amp * 0.7 if avg_up_amp > 0.5 else atr_pct * 2.0)), 1)
    opt_sl = round(max(SNIPER_MIN_SL_PCT, min(4.0, avg_down_amp * 0.6 if avg_down_amp > 0.3 else atr_pct * 1.2)), 1)

    profit_score = min(100.0, max(0.0, daily_est * 7.0))
    rank_score = round(confidence_scaled * 0.72 + profit_score * 0.28, 2)

    return {
        "market": market,
        "price": current_price,
        "confidence": confidence_scaled,
        "rank_score": rank_score,
        "fire_level": fire_level,
        "stages_passed": stages_passed,
        "rsi": round(rsi_val, 1),
        "bb_position": round(bb_position, 1),
        "atr_pct": round(atr_pct, 3),
        "vol_surge": round(vol_surge_ratio, 2),
        "ema_aligned": ema_aligned,
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
    }


# (Moved to app/api/strategy_utils.py: _clamp_sniper_tp_sl, cache functions,
#  slot management, coin warnings, candle fetching, policy sync)

router = APIRouter(
    prefix="/api/strategy",
    tags=["strategy"]
)

# --- Sub-routers (Phase 1 file diet) ---
from app.api.strategy_ladder_router import router as _ladder_router
from app.api.strategy_lightning_router import router as _lightning_router
from app.api.strategy_gazua_router import router as _gazua_router
from app.api.strategy_contrarian_router import router as _contrarian_router
from app.api.strategy_sniper_router import router as _sniper_router
from app.api.strategy_recommend_router import router as _recommend_router
from app.api.strategy_ranking_router import router as _ranking_router
from app.api.strategy_longshort_router import router as _longshort_router
router.include_router(_ladder_router)
router.include_router(_lightning_router)
router.include_router(_gazua_router)
router.include_router(_contrarian_router)
router.include_router(_sniper_router)
router.include_router(_recommend_router)
router.include_router(_ranking_router)
router.include_router(_longshort_router)

# ============================================================
# A. Last Strategy State
# ------------------------------------------------------------
# Purpose:
# - Query the "most recent strategy decision state" for a specific market
# - Pure decision info, independent of whether the engine is running
#
# Used by:
# - Debugging
# - Per-market detail view
# ============================================================
@router.get(
    "/last",
    summary="Get last strategy state for a market",
    responses={
        200: {"description": "Latest strategy decision state for the market"},
    },
)
def last_strategy_state(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
):
    """
    Retrieve the most recent strategy decision state for a market.

    - Independent of engine execution status
    - Includes last signal, policy, profit data, and AI brain analysis
    """
    system = request.app.state.system
    ctx = system.coordinator.get_context(market)

    # Use the ai/brain from the last tick result if present
    # - the coordinator is supposed to fill ctx.last_ai, but access defensively
    brain = None

    # 1) Case where the engine puts it in context.strategy_reason["engine_ai"]
    sr = getattr(ctx, "strategy_reason", None)
    if isinstance(sr, dict):
        brain = sr.get("engine_ai")

    # 2) legacy: ctx.last_ai
    if brain is None:
        last = getattr(ctx, "last_ai", None)
        if isinstance(last, dict):
            # Account for the case where the whole engine_out["ai"] is stored
            brain = last.get("brain") if isinstance(last.get("brain"), dict) else last

    return {
        "ok": True,
        "market": market,
        "last_signal": ctx.last_signal,
        "policy": ctx.policy,
        "unrealized_profit": ctx.unrealized_profit,
        "total_profit": ctx.total_profit,
        # ⬇️ Supplementary decision data (indicators/AI)
        "brain": brain  # { rsi, macd_histogram, volatility, ... }
    }

# ============================================================
# B. Strategy Score Table View (READ ONLY)
# ------------------------------------------------------------
# Purpose:
# - After READY state, expose the "strategy score results" computed by
#   StrategySelector in table form
#
# Key principles:
# - Read-only, for OMA/UI admin and decision making
# - No strategy modification / recomputation / execution trigger
#
# Used by:
# - OMA Admin UI
# - Strategy Score Table
# ============================================================
@router.get(
    "/scores",
    summary="Get strategy scores for all markets",
    responses={
        200: {"description": "Strategy score table sorted by score descending"},
    },
)
def get_strategy_scores(
    request: Request,
    strategy: Optional[str] = Query(None, description="Filter by strategy name (e.g., AUTOLOOP)"),
):
    """
    Retrieve strategy scores for all READY markets.

    - Read-only view for OMA/Admin decision making
    - Sorted by score descending
    - Optionally filter by strategy name
    """
    system = request.app.state.system
    target_strategy = strategy.strip().upper() if strategy else None

    items: List[Dict[str, Any]] = []

    # Iterate over all market contexts
    for market, ctx in system.coordinator.contexts.items():
        # Markets before READY (= warm-up complete) are not eligible for decisions
        try:
            if not ctx.is_ready():
                continue
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[strategy_router] %s: %s", 'markets before READY (= warm-up complete) are not eligible except-> continue', exc, exc_info=True)
            continue

        st = getattr(ctx, "strategy_state", None)
        if not isinstance(st, dict) or not st:
            continue

        selected = st.get("selected")

        # [PATCH] Filter by strategy if requested (e.g. ?strategy=AUTOLOOP)
        if target_strategy:
            # 1. Strategy match
            if str(selected or "").upper() != target_strategy:
                continue
            # 2. Hide WATCH markets to reduce noise (show only ACTIVE/RECOVERY)
            ms = getattr(ctx, "market_state", "WATCH")
            if ms not in ("ACTIVE", "RECOVERY"):
                continue

        scores = st.get("scores") or {}

        # Score of the selected strategy (or the highest if absent)
        score_val = None
        if isinstance(scores, dict):
            if selected in scores:
                score_val = scores.get(selected)
            elif scores:
                try:
                    score_val = max(float(v) for v in scores.values())
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("strategy_router.get_strategy_scores L434 except", exc_info=True)
                    score_val = None

        # opinion may live inside reason (None if absent)
        opinion = None
        rsn = st.get("reason")
        if isinstance(rsn, dict):
            opinion = rsn.get("opinion")

        items.append({
            "market": market,
            "strategy": selected,
            "score": float(score_val) if score_val is not None else 0.0,
            "confidence": st.get("confidence") if st.get("confidence") is not None else getattr(ctx, "confidence", None),
            "opinion": opinion,
            "ts": st.get("ts"),
        })

    # Descending by score (for OMA decision convenience)
    # Descending by score (for OMA decision convenience)
    items.sort(key=lambda x: x["score"], reverse=True)

    return {
        "ok": True,
        "phase": "READY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "items": items
    }

# (Section C: Ladder Factory API → strategy_ladder_router.py)

# (Section D: Lightning Factory API → strategy_lightning_router.py)


# (Section E: Gazua Factory API → strategy_gazua_router.py)


# (Recommendation endpoints → strategy_recommend_router.py)

# (Rankings/scoring endpoints → strategy_ranking_router.py)
# (LongShort scope endpoints → strategy_longshort_router.py)

# ============================================================
# Re-exports for backward compatibility (external imports)
# ============================================================
from app.api.strategy_recommend_router import prewarm_recommendation  # noqa: F401 (hyper_system.py)
from app.api.strategy_longshort_router import (  # noqa: F401 (autopilot_manager.py)
    longshort_multi_scan,
    evaluate_scope_deploy_candidate,
)
