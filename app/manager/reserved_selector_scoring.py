# ============================================================
# File: app/manager/reserved_selector_scoring.py
# Autocoin OS — Scoring & Confidence functions extracted from reserved_selector.py
# ------------------------------------------------------------
# Contains:
#   - Market performance scoring (_get_market_performance_score, _load_market_pnl_cache)
#   - Strategy-specific scoring (_score_pingpong, _score_ladder, _score_lightning, etc.)
#   - Multi-stage confidence scoring (_confidence_pingpong, _confidence_autoloop, etc.)
#   - AI-enhanced scoring (_score_ladder_ai, _score_lightning_ai, _score_gazua_ai, _ai_score_heuristic)
#
# NOTE: PROTECTED functions — do NOT modify _get_market_performance_score or _load_market_pnl_cache.
# ============================================================

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)
import os
import time
from typing import Any, Dict, Optional

import requests

from app.manager.reserved_selector_utils import (
    MarketSnapshot,
    _SCORE_EXCLUDED,
    _clamp,
    _execution_quality_penalty,
    _sf,
    _si,
)
from app.manager.reserved_selector_fetchers import fetch_highlow_for_lookback
from app.monitor.whale_detector import get_whale_detector

_logger = logging.getLogger(__name__)
logger = _logger

# ============================================================
# PROTECTED — Market Performance Score
# ============================================================

def _get_market_performance_score(
    market: str,
    strategy: str,
    pnl_cache: Dict[str, Dict[str, Any]],
) -> float:
    """Return a score adjustment based on past performance.

    Returns:
        Adjustment in the range -0.5 ~ +0.5
        - positive: past profit -> bonus
        - negative: past loss -> penalty (but not exclusion)
        - 0: no data
    """
    if not pnl_cache:
        return 0.0

    market_data = pnl_cache.get(market)
    if not market_data:
        return 0.0

    # Only consider trade history for this strategy
    strategy_pnl = market_data.get(strategy.upper(), {})
    if not strategy_pnl:
        # No per-strategy data -> fall back to aggregate data
        strategy_pnl = market_data.get("_total", {})

    if not strategy_pnl:
        return 0.0

    net_pnl = float(strategy_pnl.get("net_pnl_usdt", 0.0))
    trade_count = int(strategy_pnl.get("trade_count", 0))
    win_rate = float(strategy_pnl.get("win_rate", 0.5))

    if trade_count == 0:
        return 0.0

    # Score calculation
    # 1. Win-rate based (centered at 0.5, +/-0.2)
    win_bonus = (win_rate - 0.5) * 0.4  # 0.3 -> -0.08, 0.7 -> +0.08

    # 2. Net-profit based (log scale, +/-0.3)
    if net_pnl > 0:
        pnl_bonus = min(0.3, math.log1p(net_pnl / 10000) * 0.05)
    elif net_pnl < 0:
        pnl_bonus = max(-0.3, -math.log1p(abs(net_pnl) / 10000) * 0.05)
    else:
        pnl_bonus = 0.0

    # 3. Trade-count weight (experience)
    # More trades -> higher data confidence
    exp_weight = min(1.0, trade_count / 10.0)  # 10+ trades -> 100% weight

    total_adjustment = (win_bonus + pnl_bonus) * exp_weight

    # Clamp to range
    return max(-0.5, min(0.5, total_adjustment))

# ============================================================
# PROTECTED — Market PnL Cache Loader
# ============================================================

def _load_market_pnl_cache(system: Any) -> Dict[str, Dict[str, Any]]:
    """Load per-market performance data from the system's trade ledger.

    Returns:
        {
            "BTCUSDT": {
                "PINGPONG": {"net_pnl_usdt": 50000, "trade_count": 5, "win_rate": 0.6},
                "LADDER": {"net_pnl_usdt": -10000, "trade_count": 2, "win_rate": 0.5},
                "_total": {"net_pnl_usdt": 40000, "trade_count": 7, "win_rate": 0.57},
            },
            ...
        }
    """
    cache: Dict[str, Dict[str, Any]] = {}

    try:
        from app.manager.ledger_pnl import aggregate_fill_pnl

        # Last 30 days of data
        ledger = getattr(system, "ledger", None)
        if not ledger:
            return cache

        records = list(ledger.tail(5000))  # last 5000 records
        if not records:
            return cache

        now = time.time()
        since_ts = now - (30 * 24 * 3600)  # 30 days ago

        aggs = aggregate_fill_pnl(records, since_ts=since_ts, until_ts=now, markets=None)

        # market -> strategy mapping (based on OMA_ENTRY / FILL_BUY / FILL_SELL events)
        market_strategy: Dict[str, str] = {}
        for rec in records:
            try:
                ts = float(rec.get("ts", 0.0))
                if ts < since_ts:
                    continue
                event = rec.get("event", "")
                mkt = rec.get("market", "")
                data = rec.get("data") or {}
                strat = ""
                if event == "OMA_ENTRY":
                    strat = str(data.get("strategy", "") or "")
                elif event in ("FILL_BUY", "FILL_SELL"):
                    strat = str(data.get("strategy", "") or "")
                if mkt and strat:
                    market_strategy[str(mkt)] = strat.upper()
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[reserved_selector_scoring] %s: %s", 'market -> strategy mapping (based on OMA_ENTRY / FILL_BUY / FILL_SELL events) except-> continue', exc, exc_info=True)
                continue

        for market, agg in aggs.items():
            if market not in cache:
                cache[market] = {}

            net_pnl = agg.net_cash_usdt
            trade_count = agg.trade_n

            # Win-rate estimate (simplified, based on net_pnl)
            win_rate = 0.5
            if trade_count > 0:
                if net_pnl > 0:
                    win_rate = min(0.8, 0.5 + (net_pnl / (agg.buy_funds_usdt + 1)) * 0.3)
                elif net_pnl < 0:
                    win_rate = max(0.2, 0.5 + (net_pnl / (agg.buy_funds_usdt + 1)) * 0.3)

            entry = {
                "net_pnl_usdt": net_pnl,
                "trade_count": trade_count,
                "win_rate": win_rate,
            }
            cache[market]["_total"] = entry

            # Per-strategy attribution: a given market belongs to a single strategy
            strat = market_strategy.get(market, "")
            if strat and strat not in ("UNKNOWN", ""):
                cache[market][strat] = entry

        _logger.info("[reserved_selector] Loaded PnL cache for %d markets (%d with strategy)",
                     len(cache), sum(1 for v in cache.values() if len(v) > 1))

    except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
        _logger.warning(f"[reserved_selector] Failed to load PnL cache: {e}")

    return cache

# ============================================================
# Strategy Score Functions
# ============================================================

def _score_pingpong(s: MarketSnapshot) -> float:
    # [2026-03-09] Aligned with decide(): proximity to the BB lower band is key (price <= BB lower -> buy)
    # Liquidity/spread = fill feasibility; BB proximity = entry timing
    _LIQ_CAP = math.log1p(50_000_000_000.0)  # ≈ 24.7
    liq = min(math.log1p(max(0.0, s.vol24_usdt)), _LIQ_CAP)
    spread_pen = math.log1p(max(0.0, s.spread_bps))
    depth = math.log1p(max(0.0, min(s.depth_ask_usdt, s.depth_bid_usdt)))
    trades = math.log1p(float(s.recent_trades or 0))
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # -- BB lower-band proximity (decide() requires price <= bb_lower) --
    bb_entry_score = 0.0
    if s.bb_lower > 0 and s.bb_upper > s.bb_lower and s.price > 0:
        bb_range = s.bb_upper - s.bb_lower
        dist_from_lower = (s.price - s.bb_lower) / bb_range  # 0=lower, 1=upper
        if dist_from_lower <= 0.05:
            bb_entry_score = 15.0   # at/below BB lower -> decide() buys immediately
        elif dist_from_lower <= 0.15:
            bb_entry_score = 10.0   # near BB lower -> buy possible soon
        elif dist_from_lower <= 0.30:
            bb_entry_score = 5.0    # approaching BB lower
        elif dist_from_lower >= 0.80:
            bb_entry_score = -8.0   # near BB upper -> decide() never buys

    return (
        1.5 * liq
        + 0.7 * depth
        + 0.4 * trades
        - 0.8 * spread_pen
        + 3.0 * bb_entry_score     # BB proximity carries the largest weight
        + eq_pen
    )


def _score_ladder(s: MarketSnapshot) -> float:
    # ICAG v3: prefer moderate volatility (grid-friendly), good liquidity, tight spread
    liq = math.log1p(max(0.0, s.vol24_usdt))
    depth = math.log1p(max(0.0, min(s.depth_ask_usdt, s.depth_bid_usdt)))
    spread_pen = math.log1p(max(0.0, s.spread_bps))

    # Use real ATR% if available, otherwise fallback to range_ratio_24h proxy
    vol_pct = s.atr_pct if s.atr_pct > 0 else (s.range_ratio_24h * 100.0)

    # Moderate volatility bonus (bell curve: peak at 3-6%)
    if vol_pct < 1.0:
        vol_score = vol_pct * 0.5           # too quiet → low score
    elif vol_pct <= 8.0:
        vol_score = 2.0 + min(3.0, vol_pct * 0.4)  # sweet spot → high
    else:
        vol_score = max(0.5, 5.0 - (vol_pct - 8.0) * 0.3)  # too wild → decay

    # Bollinger width bonus: wider band = more grid opportunity
    bb_bonus = 0.0
    if s.bb_width_pct > 0:
        if 2.0 <= s.bb_width_pct <= 10.0:
            bb_bonus = 2.0                  # ideal band width
        elif s.bb_width_pct > 10.0:
            bb_bonus = max(0.0, 2.0 - (s.bb_width_pct - 10.0) * 0.2)

    # [2026-03-03] Execution-quality penalty for low price / low volume
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)
    return (1.2 * liq) + (2.5 * vol_score) + (0.6 * depth) - (0.8 * spread_pen) + bb_bonus + eq_pen

def _score_lightning(s: MarketSnapshot, ai_features: Optional[Dict[str, float]] = None) -> float:
    """LIGHTNING v2: breakout suitability + volatility/BB-aware scoring.

    [2026-02-23] Factors in ATR sweet spot, BB position, momentum acceleration.
    [2026-03-09] Aligned with decide(): added momentum spike + volume surge
    """
    # -- 1. Liquidity base --
    liq = math.log1p(max(0.0, s.vol24_usdt))

    # -- 2. ATR sweet spot (1.0~5% ideal: enough breakout room, not overheated) --
    volatility = s.atr_pct if s.atr_pct > 0 else 0.0
    if volatility < 0.8:
        vol_score = -1.0
    elif volatility <= 5.0:
        vol_score = min(4.0, volatility * 0.9)
    else:
        vol_score = max(0.0, 4.0 - (volatility - 5.0) * 0.6)

    # -- 3. BB position (mid~upper preferred = breakout-ready zone) --
    bb_score = 0.0
    if s.bb_lower > 0 and s.bb_upper > s.bb_lower and s.price > 0:
        bb_range = s.bb_upper - s.bb_lower
        position = (s.price - s.bb_lower) / bb_range
        if 0.4 <= position <= 0.7:
            bb_score = 3.0
        elif position > 0.85:
            bb_score = -2.0
        elif position < 0.2:
            bb_score = -1.0

    # -- 4. Fill feasibility --
    spread_pen = math.log1p(max(0.0, s.spread_bps)) * 0.8
    rr = math.log1p(max(0.0, s.range_ratio_24h))
    trades = math.log1p(float(s.recent_trades or 0))

    # [2026-03-03] Execution-quality penalty for low price / low volume
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # -- [2026-03-09] Momentum / volume surge (decide requires a momentum spike) --
    momentum_score = 0.0
    if ai_features:
        trend = float(ai_features.get("trend", 0.0))
        vol_surge = float(ai_features.get("volume_surge", 0.0))
        # Positive trend + volume spike = breakout signal
        if trend > 2.0 and vol_surge > 1.5:
            momentum_score = 10.0     # strong breakout sign
        elif trend > 1.0 and vol_surge > 1.0:
            momentum_score = 5.0      # breakout-ready
        elif trend > 0.5:
            momentum_score = 2.0      # mild uptrend
        elif trend < -2.0:
            momentum_score = -5.0     # falling -> unsuitable for breakout

    # -- [2026-03-18] BB squeeze detection (narrow band = breakout imminent) --
    squeeze_score = 0.0
    if s.bb_lower > 0 and s.bb_upper > s.bb_lower and s.price > 0:
        bb_width_pct = (s.bb_upper - s.bb_lower) / s.price * 100.0
        if 0.5 <= bb_width_pct <= 2.0:
            squeeze_score = 4.0
        elif 2.0 < bb_width_pct <= 4.0:
            squeeze_score = 2.0

    # ── [2026-03-18] Execution quality (depth + spread) ──
    depth_score = 0.0
    min_depth = min(float(s.depth_ask_usdt), float(s.depth_bid_usdt))
    if min_depth > 0:
        depth_score = min(3.0, math.log1p(min_depth) * 0.3)
    if s.spread_bps > 50:
        depth_score -= 2.0
    elif s.spread_bps > 30:
        depth_score -= 1.0

    return (
        1.2 * liq
        + 2.0 * vol_score
        + 2.0 * bb_score
        + 1.0 * rr
        + 0.5 * trades
        - spread_pen
        + 2.5 * momentum_score        # momentum is key
        + eq_pen
        + 1.5 * squeeze_score
        + 1.5 * depth_score
    )

def _score_sniper(s: MarketSnapshot, ai_features: Dict[str, float], rsi: float) -> float:
    """SNIPER v2 candidate score: scoring based on bounce probability.

    [2026-02-23] Full rework based on real ATR/BB data
    - BB lower-band proximity -> structural-bottom signal (weight 0.25)
    - RSI oversold -> probabilistic bounce zone (0.15)
    - Volume reversal -> buy-side inflow signal (0.20)
    - Early trend reversal -> early EMA cross (0.15)
    - cross_exchange integration is a separate module (0.15 external)
    - Fill feasibility (depth/spread) -> execution feasibility (0.10)
    """
    # -- 1. Liquidity base (minimum bar only) --
    liq = math.log1p(max(0.0, s.vol24_usdt))
    if s.vol24_usdt > 1_000_000_000:
        liq *= 0.3
    elif s.vol24_usdt > 100_000_000:
        liq *= 0.6

    # -- 2. Volatility: prefer real ATR%, fall back to ai_features --
    volatility = s.atr_pct if s.atr_pct > 0 else float(ai_features.get("volatility", 0.0))

    # Volatility sweet spot (1.5~6%): enough sniping opportunity, not overheated
    if volatility < 0.5:
        vol_score = -2.0                                    # too quiet
    elif volatility <= 6.0:
        vol_score = min(4.0, volatility * 0.8)              # sweet spot
    else:
        vol_score = max(0.0, 4.0 - (volatility - 6.0) * 0.5)  # overheated penalty

    # -- 3. BB lower-band proximity (structural-bottom signal) --
    bb_proximity = 0.0
    if s.bb_lower > 0 and s.bb_upper > s.bb_lower and s.price > 0:
        bb_range = s.bb_upper - s.bb_lower
        dist_from_lower = (s.price - s.bb_lower) / bb_range  # 0=lower, 1=upper
        if dist_from_lower <= 0.15:
            bb_proximity = 5.0          # within 15% of BB lower -> top score
        elif dist_from_lower <= 0.30:
            bb_proximity = 3.0          # within 30% of BB lower -> good
        elif dist_from_lower >= 0.85:
            bb_proximity = -3.0         # near BB upper -> risk of buying the top

    # BB width bonus: reasonable volatility band
    bb_width_bonus = 0.0
    if s.bb_width_pct > 0:
        if 2.0 <= s.bb_width_pct <= 8.0:
            bb_width_bonus = min(2.0, s.bb_width_pct * 0.3)
        elif s.bb_width_pct > 12.0:
            bb_width_bonus = -1.0       # excessive expansion -> risk

    # -- 4. RSI oversold (bounce-probability zone) --
    rsi_bonus = 0.0
    if rsi < 25:
        rsi_bonus = 4.0                 # extreme oversold
    elif rsi < 30:
        rsi_bonus = 3.0
    elif rsi < 40:
        rsi_bonus = max(0.0, (40.0 - rsi) / 10.0) * 2.0

    # -- 5. Trend direction (favor early reversal) --
    trend = float(ai_features.get("trend", 0.0))

    uptrend_bonus = 0.0
    if trend > 0:
        uptrend_bonus = min(3.0, (trend / 5.0) * 2.5)

    # Oversold + early trend reversal = optimal sniping timing
    reversal_bonus = 0.0
    if rsi < 35 and trend > -1.0 and trend < 2.0:
        reversal_bonus = 2.5            # early bottom bounce

    # Strong-downtrend penalty (falling knife, tiered)
    # [FIX #11] When RSI is extremely oversold (< 25), halve the penalty -- preserve capitulation-bottom opportunity
    falling_knife_pen = 0.0
    if trend < -5.0:
        falling_knife_pen = 10.0
    elif trend < -3.0:
        falling_knife_pen = 5.0
    elif trend < -2.0:
        falling_knife_pen = 2.0
    if rsi < 25 and falling_knife_pen > 0:
        falling_knife_pen *= 0.5

    # -- 6. Volume reversal signal --
    volume_surge = float(ai_features.get("volume_surge", 0.0))
    vol_reversal = min(2.0, volume_surge * 0.5) if volume_surge > 1.0 else 0.0

    # -- 7. Fill feasibility (execution feasibility) --
    spread_pen = math.log1p(max(0.0, s.spread_bps)) * 0.8
    depth_score = 0.0
    min_depth = min(s.depth_ask_usdt, s.depth_bid_usdt)
    if min_depth > 50_000_000:          # 50M or more
        depth_score = 1.0
    elif min_depth > 10_000_000:
        depth_score = 0.5

    # -- 8. 24h range (range bonus) --
    range_bonus = math.log1p(max(0.0, s.range_ratio_24h * 100))

    # [2026-03-03] Execution-quality penalty for low price / low volume
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # 2026-03-10: Whale-activity adjustment (SNIPER = whale selling -> sharp drop -> sniping opportunity)
    whale_bonus = 0.0
    try:
        _wd = get_whale_detector()
        _vs = volume_surge + 1.0  # convert volume_surge to spike_ratio
        _pc = trend * 1.5
        _wi = _wd.detect(_vs, 1.0, _pc, market=s.market)
        whale_bonus = _wd.get_strategy_score_bonus(_wi, "SNIPER")
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[reserved_selector_scoring] %s: %s", '2026-03-10: whale-activity adjustment (SNIPER = whale selling -> sharp drop -> sniping opportunity)', exc, exc_info=True)

    # -- Final bounce-probability score --
    return (
        0.4 * liq
        + 2.5 * bb_proximity           # structural bottom (0.25)
        + 1.5 * bb_width_bonus
        + 2.0 * rsi_bonus              # oversold (0.15)
        + 2.0 * uptrend_bonus           # early trend (0.15)
        + 2.5 * reversal_bonus          # early reversal
        + 2.0 * vol_reversal            # volume reversal (0.20)
        + 1.5 * vol_score               # reasonable volatility
        + 1.0 * depth_score             # fill feasibility (0.10)
        + 1.2 * range_bonus
        - spread_pen
        - falling_knife_pen
        + eq_pen                        # execution quality (low price / low volume)
        + whale_bonus                   # whale activity
    )

def _calc_sniper_params(
    s: MarketSnapshot,
    ai_features: Dict[str, float],
    rsi: float,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Auto-compute SNIPER v2 parameters.

    [2026-02-23] Precise ATR/BB-based parameters + 2-phase entry support.
    - ATR%-based lookback/threshold (volatility-adaptive)
    - BB upper -> TP target reference
    - Probe/Confirm two-stage entry parameters
    - Time-stop parameters (escape sideways chop)
    """
    # Prefer real ATR%, fall back to ai_features
    volatility = s.atr_pct if s.atr_pct > 0 else float(ai_features.get("volatility", 2.0))
    range_24h = s.range_ratio_24h * 100

    # ATR-based lookback (higher volatility -> shorter)
    if volatility > 5.0:
        lookback_min = 240      # 4 hours
    elif volatility > 3.0:
        lookback_min = 360      # 6 hours
    elif volatility > 1.5:
        lookback_min = 720      # 12 hours
    else:
        lookback_min = 1440     # 24 hours

    # Fetch real high/low
    highlow_data: Dict[str, float] = {}
    if session is not None:
        highlow_data = fetch_highlow_for_lookback(session, s.market, lookback_min)

    actual_high = highlow_data.get("high", 0.0)
    actual_low = highlow_data.get("low", 0.0)
    actual_range_pct = highlow_data.get("range_pct", 0.0)
    distance_from_low = highlow_data.get("distance_from_low_pct", 0.0)

    # -- Threshold: prefer ATR-based, fall back to highlow --
    if volatility > 0.5:
        # Use 30~40% of ATR as the threshold (volatility-adaptive)
        threshold_pct = max(0.3, min(2.5, volatility * 0.35))
    elif actual_range_pct > 0:
        threshold_pct = max(0.3, min(2.5, actual_range_pct * 0.20))
    else:
        threshold_pct = max(0.3, min(2.0, range_24h * 0.15))

    # -- TP: prefer distance to BB upper, fall back to highlow --
    trend = float(ai_features.get("trend", 0.0))

    # Use distance to BB upper as the TP reference
    bb_tp = 0.0
    if s.bb_upper > 0 and s.price > 0:
        bb_tp = (s.bb_upper - s.price) / s.price * 100.0

    # [2026-03-18] TP/SL: keep base lower bounds low; real tuning happens in UI Guards
    # TP: 0.8% ~ 15%, SL: 1.5% ~ 6% -- trailing grows the profit
    if bb_tp > 1.0:
        base_tp = max(0.8, min(15.0, bb_tp * 0.80))
    elif actual_range_pct > 0:
        base_tp = max(0.8, min(15.0, actual_range_pct * 0.45))
    else:
        base_tp = max(0.8, min(8.0, range_24h * 0.45))

    # Trend-based TP adjustment
    if trend > 3.0:
        base_tp = min(base_tp * 1.5, 15.0)
    elif trend < -3.0:
        base_tp = max(base_tp * 0.7, 0.8)

    # Near-low + uptrend bonus
    if distance_from_low < 15 and trend > 0:
        base_tp = min(base_tp * 1.4, 15.0)

    # RSI oversold bonus (expect a strong bounce)
    if rsi < 30 and trend > -2.0:
        base_tp = min(base_tp + 2.5, 15.0)
    elif rsi < 40 and trend > 0:
        base_tp = min(base_tp + 1.5, 12.0)

    # -- SL: ATR-based dynamic (wider when volatility is high) --
    if volatility > 0.5:
        base_sl = max(1.5, min(6.0, volatility * 1.0))
    else:
        base_sl = max(1.5, min(5.0, base_tp * 0.6))

    # Trail: 30% of TP (wider, to ride the trend)
    trail_dist = max(0.8, base_tp * 0.30)

    # -- Time-stop: ATR-based (shorter wait when volatility is high) --
    if volatility > 4.0:
        time_stop_min = 30          # high volatility: 30 min
    elif volatility > 2.0:
        time_stop_min = 60          # mid volatility: 1 hour
    else:
        time_stop_min = 120         # low volatility: 2 hours

    return {
        "entry_enabled": True,
        "entry_lookback_min": lookback_min,
        "entry_threshold_pct": round(threshold_pct, 2),
        "exit_enabled": True,
        "exit_lookback_min": lookback_min,
        "exit_threshold_pct": round(threshold_pct, 2),
        "tp_pct": round(base_tp, 1),
        "sl_pct": round(base_sl, 1),
        "trail_tp": True,
        "trail_dist_pct": round(trail_dist, 1),
        "ai_gate_enabled": True,
        "ai_min_score": 0.45,
        # [2026-03-07] rsi_entry_max: sync RSI threshold between selector and plugin
        # Selector allows RSI<55 + plugin grace zone (+15%) -> 42*1.15 approx 48.3 effective allowance
        "rsi_entry_max": 42.0,
        "rsi_entry_enabled": True,
        "rsi_exit_enabled": True,
        "use_limit": True,
        "fallback_to_market": True,
        "expiry_min": max(30, lookback_min // 2),
        "trend_protect_enabled": True,
        "ema_cross_enabled": False,
        # Real high/low info
        "actual_high": actual_high,
        "actual_low": actual_low,
        "actual_range_pct": round(actual_range_pct, 2),
        "distance_from_low_pct": round(distance_from_low, 2),
        # [2026-02-23] SNIPER v2 parameters
        "sniper_schema_ver": 2,
        "probe_ratio": 0.3,             # Probe entry ratio (30%)
        "confirm_ratio": 0.7,           # Confirm entry ratio (70%)
        "watch_sec": 180,               # Phase 0 observation time (sec)
        "confirm_window_sec": 300,      # Probe->Confirm confirmation window (5 min)
        "time_stop_min": time_stop_min, # sideways-chop timeout (min)
        "atr_pct": round(volatility, 2),
        "bb_upper": round(s.bb_upper, 2) if s.bb_upper > 0 else 0.0,
        "bb_lower": round(s.bb_lower, 2) if s.bb_lower > 0 else 0.0,
        "bb_middle": round(s.bb_middle, 2) if s.bb_middle > 0 else 0.0,
        # [2026-03-02] DCA averaging-down settings (UI-adjustable)
        "dca_step_pct": float(os.getenv("SNIPER_DCA_STEP_PCT", 0.2)),
        "dca_add_ratio": float(os.getenv("SNIPER_DCA_ADD_RATIO", 0.5)),
        "dca_max_depth_pct": float(os.getenv("SNIPER_DCA_MAX_DEPTH_PCT", 1.0)),
    }

def _score_gazua(s: MarketSnapshot, ai_features: Optional[Dict[str, float]] = None) -> float:
    # [2026-03-09] Aligned with decide(): AI >= 0.65 + rising trend are the key entry conditions
    liq = math.log1p(max(0.0, s.vol24_usdt))
    rr = math.log1p(max(0.0, s.range_ratio_24h))
    spread_pen = math.log1p(max(0.0, s.spread_bps))
    depth = math.log1p(max(0.0, min(s.depth_ask_usdt, s.depth_bid_usdt)))
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # -- AI score + trend (decide requires ai >= 0.65 + RS relative strength) --
    ai_entry_score = 0.0
    trend_score = 0.0
    if ai_features:
        trend = float(ai_features.get("trend", 0.0))
        vol_surge = float(ai_features.get("volume_surge", 0.0))
        # Strong uptrend = good fit for GAZUA
        if trend > 3.0:
            trend_score = 10.0
        elif trend > 1.5:
            trend_score = 5.0
        elif trend > 0:
            trend_score = 2.0
        elif trend < -2.0:
            trend_score = -8.0     # downtrend -> unsuitable for GAZUA
        # Volume-surge bonus
        if vol_surge > 2.0:
            trend_score += 3.0

    return (
        1.8 * liq
        + 0.5 * rr
        + 0.5 * depth
        - 0.7 * spread_pen
        + 2.5 * trend_score            # trend/AI is key
        + eq_pen
    )

def _score_autoloop(s: MarketSnapshot, rsi_macd: Optional[Dict[str, Any]] = None) -> float:
    # [2026-03-09] Aligned with decide(): RSI <= 28 + MACD upward reversal are the key entry conditions
    liq = math.log1p(max(0.0, s.vol24_usdt))
    rr = math.log1p(max(0.0, s.range_ratio_24h))
    spread_bps = float(s.spread_bps)
    _max_spread = float(os.getenv("OMA_SELECTOR_AUTOLOOP_MAX_SPREAD_BPS", "80"))
    if spread_bps > _max_spread > 0:
        return _SCORE_EXCLUDED
    spread_pen = math.log1p(max(0.0, spread_bps))
    range_pct = s.range_ratio_24h * 100.0
    range_bonus = 2.0 if 1.0 <= range_pct <= 3.0 else 0.0
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # -- RSI/MACD entry suitability (decide requires rsi<=28 + macd_turning_up) --
    rsi_entry_score = 0.0
    macd_entry_score = 0.0
    if rsi_macd:
        rsi = float(rsi_macd.get("rsi") or 50.0)
        macd_hist = float(rsi_macd.get("macd_hist") or 0.0)
        macd_hist_prev = float(rsi_macd.get("macd_hist_prev") or 0.0)
        # RSI oversold zone (decide: rsi <= rsi_buy=28)
        if rsi <= 28:
            rsi_entry_score = 12.0    # immediate-buy zone
        elif rsi <= 35:
            rsi_entry_score = 6.0     # close -- entry possible soon
        elif rsi <= 42:
            rsi_entry_score = 2.0     # approaching
        elif rsi >= 65:
            rsi_entry_score = -5.0    # overbought -> decide never buys
        # MACD upward reversal (decide: macd_turning_up = hist > hist_prev)
        if macd_hist > macd_hist_prev:
            macd_entry_score = 5.0    # upward reversal confirmed
        elif macd_hist < macd_hist_prev and macd_hist < 0:
            macd_entry_score = -3.0   # accelerating decline -> no buy

    return (
        1.8 * liq
        + 0.5 * rr
        - 0.25 * spread_pen
        + range_bonus
        + 2.5 * rsi_entry_score       # RSI oversold is key
        + 2.0 * macd_entry_score       # MACD reversal confirmation
        + eq_pen
    )

def _score_contrarian(s: MarketSnapshot, rsi_macd: Optional[Dict[str, Any]] = None) -> float:
    """CONTRARIAN strategy suitability score.

    [2026-03-09] Aligned with decide(): RSI oversold reversal + counter-trend signal are key
    """
    liq = math.log1p(max(0.0, s.vol24_usdt))
    rr = math.log1p(max(0.0, s.range_ratio_24h))
    spread_pen = math.log1p(max(0.0, s.spread_bps))
    depth = math.log1p(max(0.0, min(s.depth_ask_usdt, s.depth_bid_usdt)))
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # -- RSI oversold (decide requires RSI < 50 + reversal) --
    rsi_score = 0.0
    if rsi_macd:
        rsi = float(rsi_macd.get("rsi") or 50.0)
        if rsi < 30:
            rsi_score = 10.0      # extreme oversold -> ideal for contrarian
        elif rsi < 40:
            rsi_score = 6.0       # oversold zone
        elif rsi < 50:
            rsi_score = 2.0       # CONTRARIAN entry possible
        elif rsi >= 60:
            rsi_score = -5.0      # overbought -> unsuitable for contrarian

    return (
        1.5 * liq
        + 0.8 * rr
        + 0.6 * depth
        - 0.5 * spread_pen
        + 2.5 * rsi_score              # RSI oversold is key
        + eq_pen
    )

def _score_contrarian_live(
    s: MarketSnapshot,
    contrarian_score: int,
    contrarian_data: Dict[str, Any],
    coin_ret_24h: float = 0.0,
    btc_ret_24h: float = 0.0,
    rsi_macd: Optional[Dict[str, Any]] = None,
) -> float:
    """CONTRARIAN strategy suitability score (with the live counter-trend scanner).

    Args:
        s: market snapshot
        contrarian_score: counter-trend score (0-3)
        contrarian_data: counter-trend scanner data (volume_spike, tf_score, etc.)
        coin_ret_24h: coin 24h return (%)
        btc_ret_24h: BTC 24h return (%) -- for the internal fallback RS calculation
        rsi_macd: cached RSI/MACD data

    Returns:
        Final score (higher is better)
    """
    base_score = _score_contrarian(s, rsi_macd=rsi_macd)

    bonus = 0.0
    if contrarian_score >= 3:
        bonus += 30.0
    elif contrarian_score >= 2:
        bonus += 15.0
    elif contrarian_score >= 1:
        bonus += 5.0

    if contrarian_data.get("volume_spike"):
        bonus += 20.0

    tf_score = contrarian_data.get("tf_score", 0)
    if tf_score >= 2:
        bonus += 15.0
    elif tf_score >= 1:
        bonus += 5.0

    ai_score = contrarian_data.get("ai_score")
    if ai_score and ai_score > 0.7:
        bonus += (ai_score - 0.5) * 30.0

    # Internal fallback RS: coin's counter-move vs BTC (negative = weaker than BTC = preferred by CONTRARIAN)
    rs_actual = coin_ret_24h - btc_ret_24h
    if rs_actual < -5.0:
        bonus += 15.0   # strong counter-move -> optimal
    elif rs_actual < -2.0:
        bonus += 8.0    # moderate counter-move
    elif rs_actual < 0:
        bonus += 3.0    # mild counter-move
    elif rs_actual > 5.0:
        bonus -= 10.0   # stronger than BTC -> unsuitable for CONTRARIAN

    # -- [2026-03-18] Detailed scoring of contrarian_data fields --
    _vr = float(contrarian_data.get("volume_ratio") or 0.0)
    if _vr >= 3.0:
        bonus += 15.0
    elif _vr >= 2.0:
        bonus += 8.0
    elif contrarian_data.get("volume_spike"):
        bonus += 5.0

    _rs = float(contrarian_data.get("rs") or 0.0)
    if _rs > 2.0:
        bonus += 15.0
    elif _rs > 1.5:
        bonus += 10.0
    elif _rs > 1.2:
        bonus += 5.0

    _corr = float(contrarian_data.get("corr") or 0.0)
    if _corr < -0.3:
        bonus += 15.0
    elif _corr < 0.0:
        bonus += 8.0
    elif _corr < 0.3:
        bonus += 4.0

    _rs_diff = float(contrarian_data.get("rs_diff") or 0.0)
    if _rs_diff > 3.0:
        bonus += 12.0
    elif _rs_diff > 1.5:
        bonus += 8.0
    elif _rs_diff > 0.5:
        bonus += 3.0
    elif _rs_diff < -2.0:
        bonus -= 8.0

    _rs_mom = float(contrarian_data.get("rs_momentum") or 0.0)
    if _rs_mom >= 0.5:
        bonus += 10.0
    elif _rs_mom >= 0.3:
        bonus += 5.0

    _accel = float(contrarian_data.get("acceleration") or 0.0)
    if _accel >= 0.2:
        bonus += 8.0
    elif _accel >= 0.1:
        bonus += 4.0

    if contrarian_data.get("early_signal"):
        bonus += 8.0

    bonus = min(bonus, 100.0)

    return base_score + bonus

# ============================================================
# Multi-Stage Confidence Scoring (per-strategy multi-stage confidence)
# [2026-03-08] Extended SNIPER's 6-stage compute_scope_score pattern to all strategies
# Each condition is an independent score -> summed -> confidence scaled to 0~100
# ============================================================

def _confidence_pingpong(
    s: MarketSnapshot,
    ai_features: Dict[str, float],
    rsi_macd: Dict[str, Any],
) -> Dict[str, Any]:
    """PINGPONG multi-stage confidence: fast-rotation suitability (max 90).

    Key: high liquidity + tight spread + reasonable volatility + active trading.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: Liquidity (turnover 5B+ = full marks, max 20)
    vol_b = s.vol24_usdt / 1e9  # in billions
    s1 = min(20.0, max(0.0, vol_b * 4.0))  # 5B -> 20
    stages["liquidity"] = round(s1, 1)
    conf += s1

    # Stage 2: Spread (5bps or less = full marks, max 20)
    sp = max(0.0, s.spread_bps)
    s2 = max(0.0, 20.0 - sp * 0.8)  # 0bps->20, 25bps->0
    stages["spread"] = round(s2, 1)
    conf += s2

    # Stage 3: Order-book depth ($50K+ on both sides = full marks, max 15)
    min_depth = min(s.depth_ask_usdt, s.depth_bid_usdt)
    s3 = min(15.0, max(0.0, min_depth / 1e7 * 3.0))  # 50M -> 15
    stages["depth"] = round(s3, 1)
    conf += s3

    # Stage 4: Volatility-range TP feasibility (range 3~7% = optimal, max 15)
    range_pct = s.range_ratio_24h * 100.0
    if 3.0 <= range_pct <= 7.0:
        s4 = 15.0
    elif 1.5 <= range_pct < 3.0:
        s4 = range_pct * 5.0  # 1.5%->7.5, 3%->15
    elif range_pct > 7.0:
        s4 = max(5.0, 15.0 - (range_pct - 7.0) * 2.0)
    else:
        s4 = max(0.0, range_pct * 3.0)
    stages["volatility_range"] = round(s4, 1)
    conf += s4

    # Stage 5: RSI neutral band (40~60 = optimal entry, max 10)
    rsi = float(rsi_macd.get("rsi", 50.0))
    if 40.0 <= rsi <= 60.0:
        s5 = 10.0
    elif 30.0 <= rsi < 40.0 or 60.0 < rsi <= 70.0:
        s5 = 5.0
    else:
        s5 = 0.0
    stages["rsi_neutral"] = round(s5, 1)
    conf += s5

    # Stage 6: Trade activity (20+ recent fills, max 10)
    trades = float(s.recent_trades or 0)
    s6 = min(10.0, max(0.0, trades * 0.5))  # 20 -> 10
    stages["trade_activity"] = round(s6, 1)
    conf += s6

    confidence = min(100.0, conf * 100.0 / 90.0)
    return {
        "confidence": round(confidence, 1),
        "stages": stages,
        "stages_passed": sum(1 for v in stages.values() if v > 5.0),
    }

def _confidence_autoloop(
    s: MarketSnapshot,
    ai_features: Dict[str, float],
    rsi_macd: Dict[str, Any],
) -> Dict[str, Any]:
    """AUTOLOOP multi-stage confidence: medium-speed rotation suitability (max 90).

    Key: liquidity + reasonable move range (1~5%) + trend-neutral + wide spread tolerance.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: Liquidity (turnover, max 20)
    vol_b = s.vol24_usdt / 1e9
    s1 = min(20.0, max(0.0, vol_b * 4.0))
    stages["liquidity"] = round(s1, 1)
    conf += s1

    # Stage 2: Move range (1~5% optimal, max 20)
    range_pct = s.range_ratio_24h * 100.0
    if 1.0 <= range_pct <= 5.0:
        s2 = 20.0
    elif 5.0 < range_pct <= 10.0:
        s2 = max(8.0, 20.0 - (range_pct - 5.0) * 2.4)
    elif range_pct < 1.0:
        s2 = max(0.0, range_pct * 10.0)
    else:
        s2 = max(0.0, 20.0 - (range_pct - 5.0) * 2.0)
    stages["range_optimal"] = round(s2, 1)
    conf += s2

    # Stage 3: Spread (AUTOLOOP tolerates up to 30bps, max 15)
    sp = max(0.0, s.spread_bps)
    s3 = max(0.0, 15.0 - sp * 0.5)  # 0->15, 30->0
    stages["spread"] = round(s3, 1)
    conf += s3

    # Stage 4: Trend-neutral (|trend| < 0.3 = optimal, max 15)
    trend = float(ai_features.get("trend", 0.0))
    abs_trend = abs(trend)
    if abs_trend < 0.15:
        s4 = 15.0
    elif abs_trend < 0.3:
        s4 = 10.0
    elif abs_trend < 0.5:
        s4 = 5.0
    else:
        s4 = 0.0
    stages["trend_neutral"] = round(s4, 1)
    conf += s4

    # Stage 5: RSI cycle room (35~65 = cycling possible, max 10)
    rsi = float(rsi_macd.get("rsi", 50.0))
    if 35.0 <= rsi <= 65.0:
        s5 = 10.0
    elif 25.0 <= rsi < 35.0 or 65.0 < rsi <= 75.0:
        s5 = 5.0
    else:
        s5 = 0.0
    stages["rsi_cycle_room"] = round(s5, 1)
    conf += s5

    # Stage 6: Order-book depth (max 10)
    min_depth = min(s.depth_ask_usdt, s.depth_bid_usdt)
    s6 = min(10.0, max(0.0, min_depth / 1e7 * 2.5))
    stages["depth"] = round(s6, 1)
    conf += s6

    confidence = min(100.0, conf * 100.0 / 90.0)
    return {
        "confidence": round(confidence, 1),
        "stages": stages,
        "stages_passed": sum(1 for v in stages.values() if v > 5.0),
    }

def _confidence_ladder(
    s: MarketSnapshot,
    ai_features: Dict[str, float],
    rsi_macd: Dict[str, Any],
) -> Dict[str, Any]:
    """LADDER multi-stage confidence: grid/DCA suitability (max 90).

    Key: sideways + reasonable volatility (ATR 2~6%) + not near highs + stable volume.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: Sideways (|trend| < 0.15 = optimal, max 22)
    trend = float(ai_features.get("trend", 0.0))
    abs_trend = abs(trend)
    if abs_trend < 0.1:
        s1 = 22.0
    elif abs_trend < 0.2:
        s1 = 15.0
    elif abs_trend < 0.35:
        s1 = 8.0
    else:
        s1 = max(0.0, 8.0 - (abs_trend - 0.35) * 20.0)
    stages["sideways"] = round(s1, 1)
    conf += s1

    # Stage 2: Reasonable volatility ATR 2~6% (max 20)
    vol_pct = s.atr_pct if s.atr_pct > 0 else float(ai_features.get("volatility", 0.0))
    if 2.0 <= vol_pct <= 6.0:
        s2 = 20.0
    elif 1.0 <= vol_pct < 2.0:
        s2 = vol_pct * 10.0  # 1%->10, 2%->20
    elif vol_pct > 6.0:
        s2 = max(5.0, 20.0 - (vol_pct - 6.0) * 3.0)
    else:
        s2 = max(0.0, vol_pct * 5.0)
    stages["volatility_grid"] = round(s2, 1)
    conf += s2

    # Stage 3: Liquidity (max 15)
    vol_b = s.vol24_usdt / 1e9
    s3 = min(15.0, max(0.0, vol_b * 5.0))
    stages["liquidity"] = round(s3, 1)
    conf += s3

    # Stage 4: BB bandwidth (2~10% = grid-optimal, max 13)
    if s.bb_width_pct > 0:
        if 2.0 <= s.bb_width_pct <= 10.0:
            s4 = 13.0
        elif s.bb_width_pct > 10.0:
            s4 = max(3.0, 13.0 - (s.bb_width_pct - 10.0) * 1.5)
        else:
            s4 = max(0.0, s.bb_width_pct * 4.0)
    else:
        s4 = 0.0
    stages["bb_bandwidth"] = round(s4, 1)
    conf += s4

    # Stage 5: Spread (max 10)
    sp = max(0.0, s.spread_bps)
    s5 = max(0.0, 10.0 - sp * 0.4)
    stages["spread"] = round(s5, 1)
    conf += s5

    # Stage 6: Momentum stability (|momentum| < 0.5 = stable, max 10)
    mom = float(ai_features.get("momentum", 0.0))
    abs_mom = abs(mom)
    if abs_mom < 0.3:
        s6 = 10.0
    elif abs_mom < 0.8:
        s6 = 5.0
    else:
        s6 = 0.0
    stages["momentum_stable"] = round(s6, 1)
    conf += s6

    confidence = min(100.0, conf * 100.0 / 90.0)
    return {
        "confidence": round(confidence, 1),
        "stages": stages,
        "stages_passed": sum(1 for v in stages.values() if v > 5.0),
    }

def _confidence_lightning(
    s: MarketSnapshot,
    ai_features: Dict[str, float],
    rsi_macd: Dict[str, Any],
) -> Dict[str, Any]:
    """LIGHTNING multi-stage confidence: volatility-breakout suitability (max 90).

    Key: strong momentum + volume surge + uptrend + reasonable ATR + BB breakout-ready.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: Momentum acceleration (momentum > 1.0 = strong rise, max 22)
    mom = float(ai_features.get("momentum", 0.0))
    if mom > 2.0:
        s1 = 22.0
    elif mom > 1.0:
        s1 = 15.0 + (mom - 1.0) * 7.0
    elif mom > 0.5:
        s1 = mom * 10.0
    elif mom > 0:
        s1 = mom * 5.0
    else:
        s1 = max(0.0, 5.0 + mom * 5.0)  # negative-momentum penalty
    stages["momentum_acceleration"] = round(min(22.0, s1), 1)
    conf += stages["momentum_acceleration"]

    # Stage 2: Volume surge (volume_surge > 1.0 = breakout authenticity, max 20)
    surge = float(ai_features.get("volume_surge", 0.0))
    if surge > 1.5:
        s2 = 20.0
    elif surge > 1.0:
        s2 = 12.0 + (surge - 1.0) * 16.0
    elif surge > 0.5:
        s2 = surge * 12.0
    else:
        s2 = max(0.0, surge * 6.0)
    stages["volume_surge"] = round(min(20.0, s2), 1)
    conf += stages["volume_surge"]

    # Stage 3: Uptrend (trend > 0 = directionality, max 18)
    trend = float(ai_features.get("trend", 0.0))
    if trend > 0.3:
        s3 = 18.0
    elif trend > 0.1:
        s3 = 10.0 + (trend - 0.1) * 40.0
    elif trend > 0:
        s3 = trend * 50.0
    else:
        s3 = max(0.0, 5.0 + trend * 10.0)
    stages["uptrend"] = round(min(18.0, s3), 1)
    conf += stages["uptrend"]

    # Stage 4: Reasonable ATR (1.5~5%, max 12)
    vol_pct = s.atr_pct if s.atr_pct > 0 else float(ai_features.get("volatility", 0.0))
    if 1.5 <= vol_pct <= 5.0:
        s4 = 12.0
    elif vol_pct > 5.0:
        s4 = max(3.0, 12.0 - (vol_pct - 5.0) * 2.0)
    else:
        s4 = max(0.0, vol_pct * 6.0)
    stages["atr_sweetspot"] = round(s4, 1)
    conf += s4

    # Stage 5: BB breakout-ready (mid~upper, max 10)
    bb_score = 0.0
    if s.bb_lower > 0 and s.bb_upper > s.bb_lower and s.price > 0:
        bb_range = s.bb_upper - s.bb_lower
        position = (s.price - s.bb_lower) / bb_range
        if 0.4 <= position <= 0.75:
            bb_score = 10.0
        elif 0.75 < position <= 0.9:
            bb_score = 6.0
        elif 0.2 <= position < 0.4:
            bb_score = 4.0
    stages["bb_breakout_ready"] = round(bb_score, 1)
    conf += bb_score

    # Stage 6: MACD upward turn (max 8)
    macd_hist = float(rsi_macd.get("macd_histogram", 0.0))
    macd_trend = rsi_macd.get("macd_trend", "neutral")
    if macd_trend == "bullish":
        s6 = 8.0
    elif macd_hist > 0:
        s6 = 4.0
    else:
        s6 = 0.0
    stages["macd_bullish"] = round(s6, 1)
    conf += s6

    confidence = min(100.0, conf * 100.0 / 90.0)
    return {
        "confidence": round(confidence, 1),
        "stages": stages,
        "stages_passed": sum(1 for v in stages.values() if v > 3.0),
    }

def _confidence_gazua(
    s: MarketSnapshot,
    ai_features: Dict[str, float],
    rsi_macd: Dict[str, Any],
    coin_ret_24h: float = 0.0,
    btc_ret_24h: float = 0.0,
) -> Dict[str, Any]:
    """GAZUA multi-stage confidence: long-term uptrend-hold suitability (max 90).

    Key: strong upward momentum + relative strength vs BTC + trend direction + liquidity.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: Uptrend (trend > 0.2 = clear rise, max 22)
    trend = float(ai_features.get("trend", 0.0))
    if trend > 0.5:
        s1 = 22.0
    elif trend > 0.2:
        s1 = 12.0 + (trend - 0.2) * 33.3
    elif trend > 0:
        s1 = trend * 30.0
    else:
        s1 = max(0.0, 5.0 + trend * 10.0)
    stages["uptrend"] = round(min(22.0, s1), 1)
    conf += stages["uptrend"]

    # Stage 2: Relative strength vs BTC, RS (coin return - BTC return, max 20)
    rs = coin_ret_24h - btc_ret_24h
    if rs > 5.0:
        s2 = 20.0
    elif rs > 2.0:
        s2 = 10.0 + (rs - 2.0) * 3.3
    elif rs > 0:
        s2 = rs * 5.0
    elif rs > -3.0:
        s2 = max(0.0, 5.0 + rs * 1.7)
    else:
        s2 = 0.0
    stages["rs_vs_btc"] = round(s2, 1)
    conf += s2

    # Stage 3: Momentum (momentum > 0.5 = buy-side confirmation, max 18)
    mom = float(ai_features.get("momentum", 0.0))
    if mom > 1.5:
        s3 = 18.0
    elif mom > 0.5:
        s3 = 8.0 + (mom - 0.5) * 10.0
    elif mom > 0:
        s3 = mom * 10.0
    else:
        s3 = max(0.0, 3.0 + mom * 3.0)
    stages["momentum"] = round(min(18.0, s3), 1)
    conf += stages["momentum"]

    # Stage 4: Liquidity (exit feasibility on long-term holds, max 12)
    vol_b = s.vol24_usdt / 1e9
    s4 = min(12.0, max(0.0, vol_b * 3.0))
    stages["liquidity"] = round(s4, 1)
    conf += s4

    # Stage 5: MACD rising (bullish = trend confirmation, max 10)
    macd_trend = rsi_macd.get("macd_trend", "neutral")
    macd_hist = float(rsi_macd.get("macd_histogram", 0.0))
    if macd_trend == "bullish" and macd_hist > 0:
        s5 = 10.0
    elif macd_trend == "bullish" or macd_hist > 0:
        s5 = 5.0
    else:
        s5 = 0.0
    stages["macd_bullish"] = round(s5, 1)
    conf += s5

    # Stage 6: Volume surge (confirms accompanying buy-side, max 8)
    surge = float(ai_features.get("volume_surge", 0.0))
    if surge > 1.0:
        s6 = 8.0
    elif surge > 0.5:
        s6 = surge * 8.0
    else:
        s6 = max(0.0, surge * 4.0)
    stages["volume_surge"] = round(min(8.0, s6), 1)
    conf += stages["volume_surge"]

    confidence = min(100.0, conf * 100.0 / 90.0)
    return {
        "confidence": round(confidence, 1),
        "stages": stages,
        "stages_passed": sum(1 for v in stages.values() if v > 3.0),
    }

def _confidence_contrarian(
    s: MarketSnapshot,
    ai_features: Dict[str, float],
    rsi_macd: Dict[str, Any],
    contrarian_score: int = 0,
) -> Dict[str, Any]:
    """CONTRARIAN multi-stage confidence: contrarian suitability (max 90).

    Key: counter-move during a market drop + RSI oversold + sufficient liquidity + trend-reversal signal.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: Contrarian Score (from external scanner, max 25)
    s1 = min(25.0, max(0.0, float(contrarian_score) * 8.0))
    stages["contrarian_signal"] = round(s1, 1)
    conf += s1

    # Stage 2: RSI oversold (< 40 = reversal opportunity, max 20)
    rsi = float(rsi_macd.get("rsi", 50.0))
    if rsi < 30:
        s2 = 20.0
    elif rsi < 40:
        s2 = 10.0 + (40.0 - rsi)
    elif rsi < 50:
        s2 = (50.0 - rsi) * 1.0
    else:
        s2 = 0.0
    stages["rsi_oversold"] = round(s2, 1)
    conf += s2

    # Stage 3: Liquidity (avoid slippage on counter-trend trades, max 15)
    vol_b = s.vol24_usdt / 1e9
    s3 = min(15.0, max(0.0, vol_b * 5.0))
    stages["liquidity"] = round(s3, 1)
    conf += s3

    # Stage 4: Reversal within a downtrend (trend < 0 + momentum > 0 = bounce, max 15)
    trend = float(ai_features.get("trend", 0.0))
    mom = float(ai_features.get("momentum", 0.0))
    if trend < -0.1 and mom > 0.3:
        s4 = 15.0  # bounce momentum during a decline
    elif trend < -0.1 and mom > 0:
        s4 = 8.0
    elif trend < 0:
        s4 = 4.0
    else:
        s4 = 0.0
    stages["reversal_signal"] = round(s4, 1)
    conf += s4

    # Stage 5: Spread (max 8)
    sp = max(0.0, s.spread_bps)
    s5 = max(0.0, 8.0 - sp * 0.32)
    stages["spread"] = round(s5, 1)
    conf += s5

    # Stage 6: MACD reversal (bearish->neutral or rising histogram, max 7)
    macd_hist = float(rsi_macd.get("macd_histogram", 0.0))
    if macd_hist > 0:
        s6 = 7.0
    elif macd_hist > -0.5:
        s6 = 3.0
    else:
        s6 = 0.0
    stages["macd_reversal"] = round(s6, 1)
    conf += s6

    confidence = min(100.0, conf * 100.0 / 90.0)
    return {
        "confidence": round(confidence, 1),
        "stages": stages,
        "stages_passed": sum(1 for v in stages.values() if v > 3.0),
    }

# ============================================================
# AI-Enhanced Scoring Functions (Strategy-Specific Classification)
# ============================================================

def _score_ladder_ai(s: MarketSnapshot, ai_features: Dict[str, float]) -> float:
    """LADDER ICAG v3 suitability score (AI features + ATR/BB based).

    Preferred conditions (ICAG grid trading):
    - Reasonable volatility (ATR 2~6% or AI volatility 1.5~5%) -> optimal grid spacing
    - Sideways or mild decline (|trend| < 0.3) -> mean-reversion opportunity
    - Strong rise/fall -> penalized (unsuitable for grid trading)
    """
    base_score = _score_ladder(s)

    trend = float(ai_features.get("trend", 0.0))
    # Use real ATR% if enriched, otherwise fallback to AI volatility feature
    volatility = s.atr_pct if s.atr_pct > 0 else float(ai_features.get("volatility", 0.0))

    ai_bonus = 0.0

    # Prefer reasonable volatility (bell curve: 1.5~5% sweet spot)
    if volatility < 0.5:
        ai_bonus -= 5.0                     # too quiet -> no opportunity
    elif volatility <= 5.0:
        ai_bonus += min(15.0, volatility * 4.0)  # sweet spot
    else:
        ai_bonus += max(0.0, 15.0 - (volatility - 5.0) * 2.0)  # overheated -> decay

    # Prefer sideways / mild decline (core of ICAG mean reversion)
    abs_trend = abs(trend)
    # [2026-03-08] Guard: trend=0 AND volatility=0 means data unavailable -> block the bonus
    if abs_trend == 0.0 and volatility == 0.0:
        pass                                # no data -> no bonus
    elif abs_trend < 0.15:
        ai_bonus += 10.0                    # sideways -> best
    elif abs_trend < 0.3:
        ai_bonus += 5.0                     # mild trend -> good
    elif abs_trend < 0.5:
        pass                                # moderate trend -> neutral
    else:
        ai_bonus -= 8.0 * abs_trend         # strong trend -> penalty

    # Mild decline gets a small bonus (DCA entry opportunity)
    if -0.4 < trend < -0.1:
        ai_bonus += 3.0

    return base_score + ai_bonus

def _score_lightning_ai(s: MarketSnapshot, ai_features: Dict[str, float], *, price_change_pct: float = 0.0) -> float:
    """LIGHTNING v2 scoring based on AI features.

    [2026-02-23] Factors in momentum acceleration + volume surge + trend direction.
    """
    base_score = _score_lightning(s, ai_features=ai_features)

    momentum = float(ai_features.get("momentum", 0.0))
    volume_surge = float(ai_features.get("volume_surge", 0.0))
    volatility = float(ai_features.get("volatility", 0.0))
    trend = float(ai_features.get("trend", 0.0))

    ai_bonus = 0.0

    # Momentum acceleration: strong bonus when the rise is accelerating
    if momentum > 1.0 and trend > 0:
        ai_bonus += min(25.0, momentum * 8.0)
    elif momentum > 1.0:
        ai_bonus += min(15.0, momentum * 5.0)

    # Volume surge: key to judging breakout authenticity
    if volume_surge > 1.0:
        ai_bonus += min(20.0, volume_surge * 12.0)
    elif volume_surge > 0.5:
        ai_bonus += min(10.0, volume_surge * 8.0)

    # Reasonable-volatility bonus (penalize overheating)
    if 1.5 <= volatility <= 5.0:
        ai_bonus += min(10.0, volatility * 2.5)
    elif volatility > 6.0:
        ai_bonus -= min(8.0, (volatility - 6.0) * 2.0)

    # Downward-momentum penalty
    if momentum < -1.0:
        ai_bonus -= min(15.0, abs(momentum) * 5.0)
    # Strong-downtrend penalty
    if trend < -3.0:
        ai_bonus -= min(10.0, abs(trend) * 2.0)

    # 2026-03-10: Whale-activity adjustment (LIGHTNING = volatility strategy, whale activity itself is opportunity)
    try:
        _wd = get_whale_detector()
        # Use volume_surge as spike_ratio (2x or more = suspect a whale)
        _vs = volume_surge + 1.0  # volume_surge=1.0 -> spike_ratio=2.0
        _pc = price_change_pct if price_change_pct != 0.0 else trend * 1.5
        _wi = _wd.detect(_vs, 1.0, _pc, market=s.market)
        ai_bonus += _wd.get_strategy_score_bonus(_wi, "LIGHTNING")
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as _wh_err:
        logger.warning("[LIGHTNING] whale detector error: %s", _wh_err, exc_info=True)

    return base_score + ai_bonus

def _score_gazua_ai(
    s: MarketSnapshot,
    ai_features: Dict[str, float],
    ai_heuristic: float,
    coin_ret_24h: float = 0.0,
    btc_ret_24h: float = 0.0,
) -> float:
    """GAZUA V2 strategy suitability score (momentum + relative strength vs BTC + AI).

    V2 key: strong upward momentum + independent buy-side (RS) + AI-confidence-based selection.
    RS is computed from coin 24h return - BTC 24h return (positive = stronger than BTC).
    """
    base_score = _score_gazua(s, ai_features=ai_features)

    momentum = float(ai_features.get("momentum", 0.0))
    volume_surge = float(ai_features.get("volume_surge", 0.0))

    # 1. Momentum multiplier
    if momentum > 1.0:
        momentum_mult = 1.20
    elif momentum > 0.5:
        momentum_mult = 1.10
    elif momentum > 0:
        momentum_mult = 1.00
    elif momentum > -0.5:
        momentum_mult = 0.90
    else:
        momentum_mult = 0.75

    # 2. Volume bonus
    vol_bonus = min(max(0.0, volume_surge * 0.10), 0.15)

    # 3. Real relative strength vs BTC (coin 24h return - BTC 24h return)
    rs_actual = coin_ret_24h - btc_ret_24h
    if rs_actual > 5.0:
        rs_mult = 1.15
    elif rs_actual > 2.0:
        rs_mult = 1.08
    elif rs_actual > 0:
        rs_mult = 1.00
    elif rs_actual > -3.0:
        rs_mult = 0.90
    else:
        rs_mult = 0.75

    # 4. AI Confidence
    if ai_heuristic >= 0.85:
        ai_mult = 1.10
    elif ai_heuristic >= 0.75:
        ai_mult = 1.00
    else:
        ai_mult = 0.80

    # 2026-03-10: Whale-activity adjustment (GAZUA = whale buying is a long-term positive)
    _gz_whale = 0.0
    try:
        _wd = get_whale_detector()
        _vs = volume_surge + 1.0
        _pc = (coin_ret_24h if coin_ret_24h else 0.0)
        _wi = _wd.detect(_vs, 1.0, _pc, market=s.market)
        _gz_whale = _wd.get_strategy_score_bonus(_wi, "GAZUA")
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as _wh_err:
        logger.warning("[GAZUA] whale detector error: %s", _wh_err, exc_info=True)

    return base_score * momentum_mult * (1 + vol_bonus) * rs_mult * ai_mult + _gz_whale

def _ai_score_heuristic(s: MarketSnapshot) -> float:
    """Heuristic 0~1 score for candidate quality (phase-1 AI gate).

    High liquidity + tight spread + good depth -> higher score.
    This is not ML yet; it is a stable 'market suitability' score for gating.
    """
    try:
        # Spread term: 0 bps -> 1.0, 40 bps -> ~0
        spr = max(0.0, float(s.spread_bps))
        spr_s = max(0.0, 1.0 - (spr / 40.0))
        spr_s = min(1.0, spr_s)

        # Depth term: log scaled; 0 -> 0, 50K USDT -> ~1
        d = max(0.0, float(min(s.depth_ask_usdt, s.depth_bid_usdt)))
        d_s = math.log1p(d) / math.log1p(50_000.0)
        d_s = min(1.0, max(0.0, d_s))

        # Liquidity term: 0 -> 0, 20M USDT -> ~1
        v = max(0.0, float(s.vol24_usdt))
        v_s = math.log1p(v) / math.log1p(20_000_000.0)
        v_s = min(1.0, max(0.0, v_s))

        # Range term: 0 -> 0, 8% -> ~1
        rr = max(0.0, float(s.range_ratio_24h))
        rr_s = min(1.0, max(0.0, rr / 0.08))

        score = (0.45 * v_s) + (0.25 * d_s) + (0.20 * spr_s) + (0.10 * rr_s)
        return float(min(1.0, max(0.0, score)))
    except (TypeError, ValueError):
        logger.warning("[Selector] _whale_score_simple failed", exc_info=True)
        return 0.0
