# ============================================================
# File: app/api/strategy_recommend_router.py
# Phase 1-G file diet extraction
#
# Extracted from strategy_router.py — recommendation endpoints:
#   - STRATEGY_TIMEFRAMES constant
#   - _ai_candle_cache / _AI_CANDLE_CACHE_TTL
#   - _get_strategy_timeframe() helper
#   - _fetch_candles_for_ai() helper
#   - _recommend_semaphore
#   - get_rich_recommendations() endpoint
#   - prewarm_recommendation() function
#   - recommend_strategy() endpoint
# ============================================================

from fastapi import APIRouter, Request, Query
from typing import Dict, Any, List, Optional, Tuple
import logging
import threading
from types import SimpleNamespace
from datetime import datetime, timezone
from time import time as time_now
from concurrent.futures import ThreadPoolExecutor
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from app.core.rate_limiter import bybit_get
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
from app.manager.ai_trainer import ai_trainer
from app.manager.topn_selector import (
    PROFILE_WEIGHTS,
    rank_topn_by_public_candles,
    MarketFeatures,
)
try:
    from app.ai.coin_tiers import adjust_ai_score_for_strategy, get_regime_fit
except ImportError:
    logger.warning("strategy_recommend_router.unknown L45 except", exc_info=True)
    def adjust_ai_score_for_strategy(ai_score, strategy=None, regime=None):
        return {"adjusted_score": ai_score, "should_buy": ai_score >= 0.4, "tp_scale": 1.0, "sl_scale": 1.0, "confidence": 0.5}
    def get_regime_fit(regime, strategy=None):
        return 0.5

from app.api.strategy_utils import (
    SNIPER_MIN_TP_PCT, SNIPER_MIN_SL_PCT,
    _get_cached, _set_cached, _build_cache_key,
    _clamp_sniper_tp_sl,
)

# [2026-02-01] [PROTECTED] strategy -> topn_selector profile mapping
# DO NOT MODIFY - each strategy must be scored with a profile matching its characteristics
# Changing this mapping breaks per-strategy coin selection
STRATEGY_TO_PROFILE: Dict[str, str] = {
    "PINGPONG": "pingpong",    # range-bound, sideways, volatility
    "AUTOLOOP": "autorope",    # liquidity + moderate volatility
    "LADDER": "ladder",        # trend following, scaled buys
    "LIGHTNING": "lightning",  # breakout, momentum
    "GAZUA": "gazua",          # strong upward momentum
    "CONTRARIAN": "pingpong",  # contrarian = volatility + range-bound similar
    "SNIPER": "lightning",     # pump sniping = momentum-like
}

logger = logging.getLogger(__name__)

router = APIRouter()

# ============================================================
# Endpoint-specific lock (not shared — stays with the endpoints)
# ============================================================
_recommend_semaphore = threading.Semaphore(1)  # limit recommendation compute to 1 concurrent (protects tick loop thread pool)


# Per-strategy timeframe settings (multi-timeframe analysis)
STRATEGY_TIMEFRAMES = {
    # strategy: (candle unit (min), candle count, description)
    "LIGHTNING": (1, 60, "1min x 60 = 1h - scalping/pumps"),
    "PINGPONG": (5, 60, "5min x 60 = 5h - range swing"),
    "AUTOLOOP": (15, 60, "15min x 60 = 15h - scaled buys"),
    "LADDER": (60, 48, "1h x 48 = 2d - DCA downtrend"),
    "GAZUA": (240, 42, "4h x 42 = 7d - trend following"),
    "SNIPER": (60, 24, "1h x 24 = 1d - sniping timing"),
    "CONTRARIAN": (15, 60, "15min x 60 = 15h - contrarian trading"),
}

def _get_strategy_timeframe(strategy: str) -> tuple:
    """Return the candle timeframe for a strategy (unit_min, count)."""
    s = str(strategy).upper()
    if s in STRATEGY_TIMEFRAMES:
        return STRATEGY_TIMEFRAMES[s][0], STRATEGY_TIMEFRAMES[s][1]
    # default: 5min x 30
    return 5, 30

# ---- recommendation API candle cache (avoid 429) ----
_ai_candle_cache: Dict[str, Tuple[float, List[float]]] = {}
_AI_CANDLE_CACHE_TTL = 300.0  # 5min cache


def _fetch_candles_for_ai(market: str, strategy: str = "AUTOLOOP") -> List[float]:
    """Fetch recent candles for on-the-fly AI analysis.

    Per-strategy multi-timeframe support:
    - LIGHTNING: 1min candles (scalping)
    - PINGPONG: 5min candles (range-bound)
    - AUTOLOOP: 15min candles (scaled buys)
    - LADDER: 1h candles (DCA)
    - GAZUA: 4h candles (trend following)
    - SNIPER: 1h candles (sniping)
    """
    try:
        # Normalize market format
        if "/" in market:
            base = market.split("/")[0]
            exchange_market = Q.market(base)
        elif not market.startswith(Q.config.market_prefix):
            exchange_market = Q.market(market)
        else:
            exchange_market = market

        # get the per-strategy timeframe
        unit_min, count = _get_strategy_timeframe(strategy)

        # check cache
        cache_key = f"{exchange_market}:{unit_min}:{count}"
        now = time_now()
        cached = _ai_candle_cache.get(cache_key)
        if cached and (now - cached[0]) < _AI_CANDLE_CACHE_TTL:
            return cached[1]

        # Bybit V5 kline API
        resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": exchange_market, "interval": str(unit_min), "limit": count}, timeout=5.0)
        if resp.status_code == 200:
            raw = parse_bybit_list(resp.json())
            result = [float(k[4]) for k in reversed(raw) if isinstance(k, (list, tuple)) and len(k) >= 5 and float(k[4]) > 0]
            _ai_candle_cache[cache_key] = (time_now(), result)
            return result
        elif resp.status_code == 429:
            if cached:
                return cached[1]
    except (ConnectionError, OSError) as e:
        logger.warning("[AI candle] %s connection failed: %s", exchange_market, e)
    except Exception as exc:
        logger.warning("[RECOMMEND_API] Bybit V5 kline API: %s", exc, exc_info=True)
    return []

# ============================================================
# E. Rich Recommendations (Candidate Search)
# ------------------------------------------------------------
@router.get(
    "/recommendations",
    summary="Get strategy-specific market recommendations",
    responses={
        200: {"description": "List of recommended markets with AI analysis"},
    },
)
def get_rich_recommendations(
    request: Request,
    strategy: str = Query("LADDER", description="Strategy type (PINGPONG, AUTOLOOP, LADDER, LIGHTNING, GAZUA, CONTRARIAN, SNIPER, SNIPERS)"),
    n: int = Query(10, ge=1, le=50, description="Number of recommendations"),
    min_price: float = Query(0, ge=0, description="Minimum coin price (USDT, 0=no limit)"),
    max_price: float = Query(0, ge=0, description="Maximum coin price (USDT, 0=no limit)"),
):
    """
    Search for promising coins matching the strategy characteristics.

    - [2026-02-01] Uses topn_selector profile-based ranking for each strategy
    - Each strategy uses its own profile weights (volatility, momentum, liquidity, etc.)
    - Returns markets ranked by strategy-specific features + AI analysis
    """
    st_raw = strategy.strip().upper()
    snipers_mode = (st_raw == "SNIPERS")
    # SNIPER(s) shares SNIPER's execution logic but separates only the recommendation/label.
    st = "SNIPER" if snipers_mode else st_raw
    strategy_label = "SNIPERS" if snipers_mode else st
    min_price_eff = max(0.0, float(min_price or 0.0))
    max_price_eff = max(0.0, float(max_price or 0.0))
    if min_price_eff > 0 and max_price_eff > 0 and max_price_eff < min_price_eff:
        min_price_eff, max_price_eff = max_price_eff, min_price_eff

    # --- CACHE CHECK (900s read-TTL — longer than the prewarm cycle (n=20 raises compute, ~540-610s) ─
    #     shorter than the cycle creates a cold window each cycle, slowing down with a 200-market full fetch.
    #     n must equal the prewarm default (20) so the cache_key hits -> the dashboard should call with n=20) ---
    cache_key = _build_cache_key(
        "recommendations",
        strategy=strategy_label,
        n=n,
        min_price=round(min_price_eff, 8),
        max_price=round(max_price_eff, 8),
    )
    cached = _get_cached(cache_key, ttl=900)
    if cached:
        return cached

    system = request.app.state.system

    # [2026-03-03] serialize: rank_topn_by_public_candles runs only 1 at a time in the thread pool
    # if concurrent requests arrive, return stale cache (protects tick loop thread pool)
    _recommend_acquired = _recommend_semaphore.acquire(blocking=False)
    if not _recommend_acquired:
        stale = _get_cached(cache_key, ttl=86400)  # return even an old cache
        if stale:
            return stale
        profile = STRATEGY_TO_PROFILE.get(st, "ladder")
        return {"ok": True, "items": [], "profile": profile, "strategy": strategy_label, "computing": True}

    # [2026-02-01] use per-strategy profile-based ranking
    profile = STRATEGY_TO_PROFILE.get(st, "ladder")  # fallback to ladder

    try:
        # call topn_selector's profile-based ranking
        # score using characteristics matching each strategy (volatility, momentum, liquidity, etc.)
        ranked_n = int(n * 2)
        if min_price_eff > 0 or max_price_eff > 0:
            ranked_n = int(max(n * 4, 50))
        ranked = rank_topn_by_public_candles(
            n=ranked_n,  # fetch generously then filter
            profile=profile,
            candle_unit_minutes=5,  # 5min candles (speed vs accuracy balance)
            candle_count=60,        # 5h of data
            max_markets=200,
            request_sleep=0.05,     # API rate throttling
        )

        # MarketFeatures -> market list + score
        ranked_markets = {f.market: (score, f) for score, f in ranked}

    except (TypeError, ValueError) as e:
        logger.warning(f"[recommendations] topn_selector failed for {st}/{profile}: {e}")
        ranked_markets = {}

    # enrich with current price info from tickers
    try:
        if ranked_markets:
            wanted_set = set(m.upper() for m in ranked_markets.keys())
        else:
            all_markets = list(system._known_markets) if system._known_markets else [Q.market("BTC"), Q.market("ETH"), Q.market("XRP"), Q.market("SOL"), Q.market("DOGE")]
            wanted_set = set(m.upper() for m in all_markets[:50])

        if not wanted_set:
            _recommend_semaphore.release()
            return {"ok": True, "items": [], "profile": profile}

        resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=3.0)
        raw_tickers = parse_bybit_list(resp.json()) if resp.status_code == 200 else []
        ticker_map = {}
        for t in raw_tickers:
            if not isinstance(t, dict):
                continue
            t = normalize_bybit_ticker(t)
            if t.get("market", "").upper() in wanted_set:
                ticker_map[t["market"]] = t

    except (ConnectionError, TimeoutError, OSError) as e:
        logger.warning("[recommend] ticker fetch network error: %s", e)
        ticker_map = {}
    except Exception:
        logger.error("[recommend] ticker fetch unexpected error", exc_info=True)
        ticker_map = {}

    # build candidates in profile ranking order (highest score first)
    if ranked_markets:
        top_candidates = []
        for market in ranked_markets.keys():
            t = ticker_map.get(market)
            if t:
                top_candidates.append(t)
    else:
        # fallback: by trading value
        top_candidates = list(ticker_map.values())
        top_candidates.sort(key=lambda x: float(x.get("acc_trade_price_24h") or 0), reverse=True)
        top_candidates = top_candidates[:n * 2]

    # Calculate median volume for budget scaling
    vols = [float(x.get("acc_trade_price_24h") or 0) for x in top_candidates]
    vols = [v for v in vols if v > 0]
    median_vol = 1.0
    if vols:
        vols.sort()
        median_vol = vols[len(vols) // 2]

    # ★ equity-linked base — fixes a bug where old KRW residue (base_budget=100,000 KRW) was just
    #   relabeled as USDT and recommended $200K on a $0.13 coin. Based on real equity (USDT), 200 conservative if absent.
    _acct_eq = float(getattr(system, "_last_equity_usdt", 0) or 0)
    if _acct_eq <= 0:
        _acct_eq = float(getattr(system, "equity_usdt", 0) or 0)
    if _acct_eq <= 0:
        _acct_eq = 200.0
    _budget_cap = max(10.0, _acct_eq * 0.5)   # never recommend more than half of equity on one coin

    # Get AI Model Info (Training Data Size)
    model_info = ai_trainer.get_info()
    model_rows = model_info.get("rows", 0)

    # --- OPTIMIZATION: Pre-fetch candle data in parallel ---
    # Previously candle data was requested sequentially per candidate coin, which was slow.
    # Now we request all in parallel at once, greatly improving response speed.
    markets_to_fetch = []
    for t in top_candidates:
        market = t.get("market")
        if market and not system.coordinator.contexts.get(market):
            markets_to_fetch.append(market)

    candle_histories = {}
    if markets_to_fetch:
        # excessive parallelism causes 429/failures that flatten ai_score to 0.5, so limit workers.
        max_workers = min(4, len(markets_to_fetch)) or 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = executor.map(lambda m: _fetch_candles_for_ai(m, st), markets_to_fetch)
            candle_histories = {market: hist for market, hist in zip(markets_to_fetch, results)}
    # --- END OPTIMIZATION ---

    items = []
    for t in top_candidates:
        market = t.get("market")
        if not market: continue
        trade_price = float(t.get("trade_price") or 0.0)
        if min_price_eff > 0 and trade_price < min_price_eff:
            continue
        if max_price_eff > 0 and trade_price > max_price_eff:
            continue

        profile_score = 0.0
        profile_features = None
        if ranked_markets and market in ranked_markets:
            profile_score, pf = ranked_markets[market]
            profile_features = {
                "volatility": round(pf.volatility, 6),
                "momentum": round(pf.momentum, 4),
                "liquidity": round(pf.liquidity, 2),
                "trend_slope": round(pf.trend_slope, 6),
                "range_ratio": round(pf.range_ratio, 6),
            }

        # 2. Check Duplicate / Active Strategy
        active_strategy = None
        oma_state = "NONE"
        ctx = None

        if system.oma_registry.has_market(market):
            oma_state = system.oma_registry.get_state(market).value

        # [PATCH] Only flag as "Active Strategy" if actually ACTIVE or RECOVERY.
        # WATCH markets are considered "Available" for re-deployment/modification.
        if oma_state in ("ACTIVE", "RECOVERY"):
            # check context
            ctx = system.coordinator.contexts.get(market)
            if ctx:
                # check strategy mode
                ctrls = getattr(ctx, "controls", {}) or {}
                strat = ctrls.get("strategy", {}) or {}
                if strat.get("enabled"):
                    active_strategy = str(strat.get("mode") or "CUSTOM").upper()
                else:
                    active_strategy = "AI (AUTOCOIN)" # default engine
            else:
                active_strategy = f"{oma_state} (NO_CTX)"

        # AI Score (if available)
        ai_score = 0.5
        volatility = 0.0
        trend = 0.0

        # RSI, momentum initial values (previously hardcoded 50, now real calculation)
        rsi = 50.0
        momentum = 0.0

        ai_from_live = False
        if ctx and hasattr(ctx, "current_ai"):
            brain = ctx.current_ai.get("brain", {})
            ai_score = float(brain.get("ai_prediction", 0.5))
            volatility = float(brain.get("volatility", 0.0))
            trend = float(brain.get("trend", 0.0))
            rsi = float(brain.get("rsi", 50.0))
            momentum = float(brain.get("momentum", 0.0))
            ai_from_live = True
        else:
            # 3. On-the-fly AI Analysis for new candidates
            # (Use pre-fetched data from _fetch_candles_for_ai)
            hist = candle_histories.get(market)
            # RSI calculation needs at least 15 data points (length 14 + 1)
            if hist and len(hist) >= 15:
                try:
                    # hist is already a price list (float list) - returned by _fetch_candles_for_ai
                    prices = hist  # already a list
                    current_price = prices[-1] if prices else 0

                    brain_module = getattr(system.engine, "pipeline", None)
                    if brain_module and hasattr(brain_module, "brain"):
                        b_out = brain_module.brain.analyze(market, current_price, price_history=prices)
                        ai_score = b_out.ai_prediction
                        volatility = b_out.volatility
                        trend = b_out.trend
                        rsi = getattr(b_out, "rsi", 50.0) or 50.0
                        momentum = getattr(b_out, "momentum", 0.0) or 0.0
                        ai_from_live = True
                    else:
                        # if no Brain, compute directly with indicators
                        volatility = indicators.volatility(prices, 20) or 0.0
                        trend = indicators.trend(prices, 20) or 0.0
                        momentum = indicators.trend(prices, 3) or 0.0
                        rsi_val = indicators.rsi(prices, 14)
                        rsi = float(rsi_val) if rsi_val is not None else 50.0
                        ai_from_live = True
                except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
                    logger.warning(f"[recommendations] {market} indicator calc failed: {e}")

        # when AI/indicators flatten (0.5/0.0), use profile features as auxiliary input to preserve strategy character.
        if (
            not ai_from_live
            and profile_features is not None
            and abs(volatility) < 1e-9
            and abs(trend) < 1e-9
            and abs(momentum) < 1e-9
        ):
            try:
                pf_vol = float(profile_features.get("volatility") or 0.0) * 100.0
                pf_mom = float(profile_features.get("momentum") or 0.0) * 100.0
                pf_slope = float(profile_features.get("trend_slope") or 0.0)
                volatility = pf_vol
                momentum = pf_mom
                trend = pf_mom if abs(pf_mom) > 0.01 else (pf_slope * 10000.0)
                if abs(ai_score - 0.5) < 1e-9:
                    ai_score = max(0.42, min(0.72, 0.52 + float(profile_score) * 0.06))
                if abs(rsi - 50.0) < 1e-9:
                    if momentum <= -1.5:
                        rsi = 35.0
                    elif momentum >= 1.5:
                        rsi = 65.0
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[RECOMMEND_API] profile feature fallback: %s", exc, exc_info=True)

        # Budget suggestion logic
        # 1) trading-value based factor
        vol24 = float(t.get("acc_trade_price_24h") or 0)
        vol_factor = (vol24 / median_vol) ** 0.5 if median_vol > 0 else 1.0
        vol_factor = max(0.5, min(2.0, vol_factor))

        # 2) coin-price based minimum budget (so at least 0.001 units are tradable)
        coin_price = float(t.get("trade_price") or 0)
        # minimum budget by price tier: high-priced coins need more capital (USDT)
        if coin_price >= 50_000:    # BTC tier (>= $50K)
            min_budget = 500
        elif coin_price >= 1_000:   # ETH tier (>= $1K)
            min_budget = 200
        elif coin_price >= 100:     # >= $100
            min_budget = 100
        elif coin_price >= 10:      # >= $10
            min_budget = 50
        else:                       # low-priced coins
            min_budget = 30

        base_budget = max(10.0, _acct_eq * 0.15)   # per-deploy ≈ 15% of equity (assumes multiple slots, USDT)

        # 3) RSI-based budget adjustment
        # RSI < 30 (oversold): buy opportunity -> increase budget
        # RSI > 70 (overbought): risk zone -> decrease budget
        rsi_factor = 1.0
        if rsi < 30:
            rsi_factor = 1.3  # +30% when oversold
        elif rsi < 40:
            rsi_factor = 1.15
        elif rsi > 70:
            rsi_factor = 0.7  # -30% when overbought
        elif rsi > 60:
            rsi_factor = 0.85

        suggested_budget = int((base_budget * vol_factor * rsi_factor) * 100) / 100  # USDT 0.01 units
        # guarantee minimum budget (but never exceed equity)
        suggested_budget = max(suggested_budget, min(min_budget, _budget_cap))
        suggested_budget = min(suggested_budget, _budget_cap)  # ★ never exceed half of equity (prevent old KRW residue recurrence)

        # Price Prediction (Heuristic based on Volatility & AI Score)
        # predict upward direction when AI Score is >= 0.5
        # derive target by dividing volatility (Daily Range) into time units
        pred = {}
        rec = {}
        ladder_params = {}
        gazua_params = {}
        lightning_params = {}

        # volatility calculation (defined outside the try block)
        curr = float(t.get("trade_price") or 0)
        high = float(t.get("high_price") or curr)
        low = float(t.get("low_price") or curr)
        daily_vol_pct = (high - low) / low if low > 0 else 0.05
        change_rate_pct = float(t.get("signed_change_rate") or 0) * 100.0

        # SNIPER(s)-only candidate filter:
        # - swing range must be sufficient
        # - exclude sharply falling coins
        if snipers_mode:
            if daily_vol_pct < 0.025:  # below 2.5%: insufficient swing
                continue
            if change_rate_pct <= -4.5:  # excessive drop: exclude falling knives
                continue

        try:

            # AI confidence (0.5~1.0 -> 0.0~1.0 scaling)
            # keep at least ~0.2 weight so the target comes out above the current price
            confidence = max(0.2, (ai_score - 0.5) * 2.0)

            # expected upside per time horizon (simplified square-root-of-time rule)
            pred["1h"] = curr * (1.0 + (daily_vol_pct / 4.9) * confidence)  # sqrt(24) approx 4.9
            pred["6h"] = curr * (1.0 + (daily_vol_pct / 2.0) * confidence)  # sqrt(4) = 2
            pred["24h"] = curr * (1.0 + daily_vol_pct * confidence)

            rec = {
                "entry": curr,
                "target": pred["24h"],
                "stop_loss": curr * (1.0 - daily_vol_pct * 0.5) # Default SL heuristic
            }

            # Strategy specific adjustments
            if st == "LIGHTNING":
                # Lightning: shorter horizon, tighter SL
                rec["target"] = pred["1h"]
                rec["stop_loss"] = curr * (1.0 - daily_vol_pct * 0.2)

                # Lightning specific params: short-term high-volatility scalping
                # TP/SL proportional to volatility, short holding time
                rec_tp = 1.5  # default 1.5%
                rec_sl = -1.0  # default -1%

                # volatility-based adjustment
                if daily_vol_pct > 0.08:
                    rec_tp = 3.0
                    rec_sl = -2.0
                elif daily_vol_pct > 0.05:
                    rec_tp = 2.5
                    rec_sl = -1.5
                elif daily_vol_pct > 0.03:
                    rec_tp = 2.0
                    rec_sl = -1.2

                # AI confidence adjustment
                if ai_score >= 0.7:
                    rec_tp += 0.5  # raise target on high confidence
                elif ai_score < 0.5:
                    rec_tp -= 0.3  # conservative on low confidence
                    rec_sl = min(rec_sl, -SNIPER_MIN_SL_PCT)  # keep SL floor

                # RSI-based adjustment (LIGHTNING)
                if rsi < 30:
                    rec_tp += 0.5  # oversold -> rebound expected -> raise TP
                    rec_sl -= 0.3  # give SL more room
                elif rsi > 70:
                    rec_tp -= 0.5  # overbought -> conservative
                    rec_sl = min(rec_sl + 0.3, -SNIPER_MIN_SL_PCT)  # keep SL floor

                # max holding time (min) - shorter the higher the volatility
                hold_minutes = 60
                if daily_vol_pct > 0.08:
                    hold_minutes = 30
                elif daily_vol_pct > 0.05:
                    hold_minutes = 45

                lightning_params = {
                    "tp": round(rec_tp, 2),
                    "sl": round(rec_sl, 2),
                    "max_hold_minutes": hold_minutes,
                    "volatility_pct": round(daily_vol_pct * 100, 2)
                }

            # Ladder specific suggestions
            if st == "LADDER":
                # Volatility-based tuning
                # defaults
                base_step = 1.0
                sl_recommend = -5.0 # default fallback

                # attempt ATR calculation (only if hist exists)
                atr_14h = None
                try:
                    hist = candle_histories.get(market) if 'candle_histories' in dir() or 'candle_histories' in locals() else None
                    if hist and len(hist) >= 100:
                        atr_14h = indicators.atr_simplified(hist, min(840, len(hist)))
                except (TypeError, ValueError) as exc:
                    logger.warning("[RECOMMEND_API] ATR calc: %s", exc, exc_info=True)

                if atr_14h and curr > 0:
                    atr_pct = (atr_14h / curr) * 100.0
                    # stop loss = ATR(14h) * 1.5
                    sl_recommend = -(atr_pct * 1.5)

                    # also adjust Step Gap to the ATR ratio (e.g. 0.5x ATR)
                    base_step = max(0.5, min(5.0, atr_pct * 0.5))
                else:
                    # Fallback to daily vol
                    if daily_vol_pct > 0.05:
                        base_step = 1.5
                    elif daily_vol_pct < 0.02:
                        base_step = 0.5

                # AI & Volatility based tuning
                # 1. Steps: higher volatility -> split into more steps to spread risk
                rec_steps = 10
                if daily_vol_pct > 0.10: rec_steps = 20
                elif daily_vol_pct > 0.05: rec_steps = 15

                # 2. ATR Mode: enable when volatility is very high or AI confidence is low
                rec_atr_enabled = (daily_vol_pct > 0.08) or (ai_score < 0.4)

                # 3. Martingale: aggressive when AI is good
                rec_martingale = 1.05 if daily_vol_pct > 0.03 else 1.0
                if ai_score > 0.7: rec_martingale = 1.15
                elif ai_score > 0.6: rec_martingale = 1.10
                elif ai_score < 0.4: rec_martingale = 1.0

                # 4. TP: volatility + AI Score
                rec_tp = 2.0
                if daily_vol_pct > 0.05: rec_tp = 3.0
                if ai_score > 0.7: rec_tp += 1.0

                # 5. RSI-based adjustment (LADDER)
                if rsi < 30:
                    rec_steps = min(rec_steps + 5, 25)  # oversold -> more scaled buys
                    rec_martingale = min(rec_martingale + 0.05, 1.25)  # stronger martingale
                elif rsi > 70:
                    rec_steps = max(rec_steps - 3, 5)  # overbought -> hold back entry
                    rec_tp = max(rec_tp - 0.5, 1.0)  # conservative TP

                ladder_params = {"step_pct": base_step, "martingale": rec_martingale, "max_steps": rec_steps, "step_gap_atr_enabled": rec_atr_enabled, "tp": rec_tp}

            # Gazua specific suggestions
            if st == "GAZUA":
                rec_tp = 10.0
                rec_sl = -5.0

                # AI Adjustments
                if ai_score >= 0.8:
                    rec_tp = 15.0
                    rec_sl = -4.0  # High confidence -> tighter SL? or standard. Let's keep it slightly tight.
                elif ai_score >= 0.6:
                    rec_tp = 12.0
                elif ai_score < 0.4:
                    rec_tp = 5.0   # Low confidence -> take profit early
                    rec_sl = -3.0

                # Volatility adjustment
                if daily_vol_pct > 0.10:
                    rec_sl = min(rec_sl, -8.0) # High vol -> widen SL
                    rec_tp = max(rec_tp, 20.0) # Aim for moon

                # RSI-based adjustment (GAZUA)
                if rsi < 30:
                    rec_tp += 3.0  # oversold -> expect a big rebound
                    rec_sl -= 1.0  # give SL room
                elif rsi < 40:
                    rec_tp += 1.5
                elif rsi > 70:
                    rec_tp = max(rec_tp - 3.0, 5.0)  # overbought -> take profit early
                    rec_sl = max(rec_sl + 1.5, -3.0)  # tighter SL

                gazua_params = {"tp": rec_tp, "sl": rec_sl}

        except Exception as e:
            import traceback
            logger.warning(f"[recommendations] {market} params calculation failed: {e}\n{traceback.format_exc()}")

        # consolidate recommended_params (frontend compatible)
        recommended_params = {}
        if st == "LADDER" and ladder_params:
            recommended_params = {
                "step_pct": ladder_params.get("step_pct"),
                "steps": ladder_params.get("max_steps"),
                "martingale": ladder_params.get("martingale"),
                "tp_pct": ladder_params.get("tp"),
                "use_atr": ladder_params.get("step_gap_atr_enabled"),
            }
        elif st == "LIGHTNING" and lightning_params:
            recommended_params = {
                "tp_pct": lightning_params.get("tp"),
                "sl_pct": lightning_params.get("sl"),
                "max_hold_minutes": lightning_params.get("max_hold_minutes"),
            }
        elif st == "GAZUA" and gazua_params:
            recommended_params = {
                "tp_pct": gazua_params.get("tp"),
                "sl_pct": gazua_params.get("sl"),
            }
        elif st == "CONTRARIAN":
            # CONTRARIAN params (operational defaults: TP 15 / SL -50)
            ct_tp = 15.0
            ct_sl = -50.0
            recommended_params = {
                "tp_pct": round(ct_tp, 2),
                "sl_pct": round(ct_sl, 2),
                "trail_tp_enabled": False,
                "trail_dist_pct": 0.3,
                "use_atr": False,
                "rsi_filter": False,
                "ema_cross_enabled": False,
                "min_score": 1,
                "cooldown_sec": 300,
                "entry_ob_guard_enabled": False,
            }
        elif st == "SNIPER":
            # SNIPER: snipe buy/sell at the low/high
            # [2026-02-02] raised lookback defaults (to handle lulls/downturns)
            #
            # volatility ranges (raised):
            # - ultra-high vol (>10%): 1~2h (scalping)
            # - high vol (5~10%): 2~4h (swing)
            # - mid vol (2~5%): 4~8h (mid-term)
            # - low vol (<2%): 12~24h (daily low/high)

            sn_lookback = 240  # default 4h (raised from old 1h)
            sn_threshold = 0.3
            sn_expiry = 360  # default 6h (raised from old 1h)
            sn_tp = max(2.0, SNIPER_MIN_TP_PCT)
            sn_sl = SNIPER_MIN_SL_PCT

            # determine lookback/expiry from volatility (all raised)
            if daily_vol_pct > 0.10:
                # ultra-high vol: 1~2h (old 5~15min)
                sn_lookback = 60
                sn_expiry = 120
                sn_threshold = 0.5
                sn_tp = 3.0
                sn_sl = max(SNIPER_MIN_SL_PCT, 2.0)
            elif daily_vol_pct > 0.08:
                # high vol: 2~3h (old 15~30min)
                sn_lookback = 120
                sn_expiry = 180
                sn_threshold = 0.4
                sn_tp = 2.5
                sn_sl = max(SNIPER_MIN_SL_PCT, 1.8)
            elif daily_vol_pct > 0.05:
                # mid-high vol: 3~4h (old 30min~1h)
                sn_lookback = 180
                sn_expiry = 240
                sn_threshold = 0.35
                sn_tp = 2.2
                sn_sl = max(SNIPER_MIN_SL_PCT, 1.6)
            elif daily_vol_pct > 0.03:
                # mid vol: 4~6h (old 1~3h)
                sn_lookback = 240
                sn_expiry = 360
                sn_threshold = 0.3
                sn_tp = 2.0
                sn_sl = max(SNIPER_MIN_SL_PCT, 1.5)
            elif daily_vol_pct > 0.02:
                # low vol: 6~12h (old 3~6h)
                sn_lookback = 360
                sn_expiry = 720
                sn_threshold = 0.25
                sn_tp = 1.8
                sn_sl = max(SNIPER_MIN_SL_PCT, 1.2)
            else:
                # ultra-low vol: 12~24h (search daily low/high)
                sn_lookback = 720  # 12h
                sn_expiry = 1440  # 24h
                sn_threshold = 0.2
                sn_tp = 1.5
                sn_sl = max(SNIPER_MIN_SL_PCT, 1.0)

            # AI confidence-based adjustment
            if ai_score >= 0.7:
                sn_tp += 0.5
            elif ai_score < 0.4:
                sn_tp = max(sn_tp - 0.5, SNIPER_MIN_TP_PCT)
                sn_sl = max(sn_sl - 0.3, SNIPER_MIN_SL_PCT)

            # RSI-based fine tuning
            if rsi < 30:
                sn_threshold = max(sn_threshold - 0.05, 0.1)
                sn_tp += 0.3
            elif rsi > 70:
                sn_threshold += 0.05
                sn_expiry = max(sn_expiry // 2, 15)

            # SNIPER(s): prevent values stretching into swing/long-hold (cap for short-term cycling)
            if snipers_mode:
                sn_lookback = min(sn_lookback, 180)   # max 3h
                sn_expiry = min(sn_expiry, 120)       # max 2h
                sn_threshold = max(sn_threshold, 0.25)

            sn_tp, sn_sl = _clamp_sniper_tp_sl(sn_tp, sn_sl)

            recommended_params = {
                "expiry_min": sn_expiry,
                "tp_pct": round(sn_tp, 1),
                "sl_pct": round(sn_sl, 1),
                # Entry (snipe buy)
                "entry_enabled": True,
                "entry_lookback_min": sn_lookback,
                "entry_threshold_pct": round(max(sn_threshold, 0.1), 2),
                # Exit (snipe sell)
                "exit_enabled": True,
                "exit_lookback_min": sn_lookback,
                "exit_threshold_pct": round(max(sn_threshold, 0.1), 2),
                # Guards
                "trail_tp": True,
                "trail_dist_pct": 1.5,
            }
            if snipers_mode:
                side = "SHORT" if trend < -0.8 else "LONG"
                recommended_params.update({
                    "profile": "SNIPERS",
                    "side": side,
                    "cycle_mode": "DOWN" if side == "SHORT" else "UP",
                    "auto_reentry": True,
                    "no_demote": False,
                    "hold_sell": False,
                })
        elif st == "PINGPONG":
            # PINGPONG: range-bound trading - volatility based
            pp_tp = max(2.0, min(6.0, daily_vol_pct * 80 + 2.0)) if daily_vol_pct else 3.0
            pp_sl = -(pp_tp * 0.7)
            # RSI-based adjustment
            if rsi < 30:
                pp_tp += 0.5
            elif rsi > 70:
                pp_tp = max(pp_tp - 0.5, 2.0)
            recommended_params = {
                "tp_pct": round(pp_tp, 1),
                "sl_pct": round(pp_sl, 1),
                "rsi_buy": 30,
                "rsi_sell": 70,
            }
        elif st == "AUTOLOOP":
            # AUTOLOOP: scaled buys + take profit
            al_tp = max(1.5, min(4.0, daily_vol_pct * 60 + 1.5)) if daily_vol_pct else 2.5
            # AI confidence-based multiplier
            conf_tier = "high" if ai_score >= 0.8 else ("medium" if ai_score >= 0.6 else "low")
            budget_mult = 1.3 if conf_tier == "high" else (1.0 if conf_tier == "medium" else 0.8)
            # RSI-based adjustment
            if rsi < 30:
                al_tp += 0.3
                budget_mult = min(budget_mult + 0.1, 1.5)
            elif rsi > 70:
                al_tp = max(al_tp - 0.3, 1.5)
            recommended_params = {
                "tp_pct": round(al_tp, 1),
                "steps": max(3, min(10, int(suggested_budget // 30))),
                "budget_multiplier": round(budget_mult, 2),
                "confidence_tier": conf_tier,
            }

        # [2026-01-30] apply per-strategy AI adjustment + Regime fit
        # Regime estimation: based on trend + volatility
        est_regime = "NEUTRAL"
        if trend > 1.0 and volatility < 3.0:
            est_regime = "BULL"
        elif trend < -1.0 and volatility > 1.5:
            est_regime = "BEAR"

        ai_adjustment = adjust_ai_score_for_strategy(ai_score, strategy=st, regime=est_regime)
        regime_fit = get_regime_fit(est_regime, strategy=st)

        # AI-adjusted score (reflects strategy-regime fit)
        adjusted_score = ai_adjustment.get("adjusted_score", ai_score)
        ai_should_buy = ai_adjustment.get("should_buy", True)

        # global TP/SL floor correction
        if isinstance(recommended_params, dict):
            if "tp_pct" in recommended_params:
                try:
                    recommended_params["tp_pct"] = round(max(SNIPER_MIN_TP_PCT, float(recommended_params.get("tp_pct"))), 2)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("strategy_recommend_router.get_rich_recommendations L848 except", exc_info=True)
                    recommended_params["tp_pct"] = SNIPER_MIN_TP_PCT
            if "sl_pct" in recommended_params:
                try:
                    _sl = float(recommended_params.get("sl_pct"))
                    if _sl < 0:
                        recommended_params["sl_pct"] = round(min(_sl, -SNIPER_MIN_SL_PCT), 2)
                    else:
                        recommended_params["sl_pct"] = round(max(abs(_sl), SNIPER_MIN_SL_PCT), 2)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("strategy_recommend_router.get_rich_recommendations L857 except", exc_info=True)
                    recommended_params["sl_pct"] = -SNIPER_MIN_SL_PCT

        items.append({
            "market": market,
            "strategy": strategy_label,  # requested strategy label (incl. SNIPERS)
            "profile": strategy_label,
            "profile_score": round(float(profile_score), 4),
            "profile_features": profile_features,
            "price": float(t.get("trade_price") or 0),
            "change_rate": change_rate_pct,
            "high_price": float(t.get("high_price") or 0),
            "low_price": float(t.get("low_price") or 0),
            "acc_trade_price_24h": float(t.get("acc_trade_price_24h") or 0),
            "active_strategy": active_strategy, # None means unused
            "oma_state": oma_state,
            "ai_score": ai_score,
            "ai_adjusted_score": adjusted_score,  # strategy-regime adjusted score
            "ai_should_buy": ai_should_buy,       # whether AI permits buying
            "regime": est_regime,                 # estimated regime
            "regime_fit": regime_fit,             # strategy-regime fit
            "ai_model_rows": model_rows,
            "volatility": volatility,
            "trend": trend,
            "rsi": rsi,
            "momentum": momentum,
            "suggested_budget_usdt": suggested_budget,
            "budget": suggested_budget,
            "predictions": pred,
            "recommendation": rec,
            "recommended_params": recommended_params,
            "ladder_params": ladder_params,
            "gazua_params": gazua_params,
            "lightning_params": lightning_params,
        })

    # --------------------------------------------------------
    # Strategy-Specific Sorting & Filtering (integrates Strategy Advisor logic)
    # --------------------------------------------------------
    from app.manager.strategy_graduator import suggest_strategy_for_ai_features

    # st is already defined above (strategy.strip().upper())

    # compute a recommended strategy for each coin
    for item in items:
        try:
            rec_strategy, confidence, reason = suggest_strategy_for_ai_features(
                momentum=float(item.get("momentum") or 0),
                volatility=float(item.get("volatility") or 0) / 100.0,  # percent -> ratio
                trend=float(item.get("trend") or 0),
                ai_prediction=float(item.get("ai_score") or 0.5),
                rsi=float(item.get("rsi") or 50.0),
            )
            item["recommended_strategy"] = rec_strategy
            item["strategy_confidence"] = round(confidence, 3)
            # suggest_strategy only classifies the 5 core strategies.
            # (SNIPER/SNIPERS/CONTRARIAN) are not used as the primary sort key for recommendations.
            if st in ("LADDER", "LIGHTNING", "GAZUA", "PINGPONG", "AUTOLOOP"):
                item["strategy_match"] = (rec_strategy == st)
            else:
                item["strategy_match"] = False
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("strategy_recommend_router.get_rich_recommendations L918 except", exc_info=True)
            item["recommended_strategy"] = "AUTOLOOP"
            item["strategy_confidence"] = 0.5
            item["strategy_match"] = False

    # 1) strategy profile score first (core)
    # 2) whether AI permits buying
    # 3) adjusted AI score / regime fit
    # 4) trading value
    # strategy_match is kept as auxiliary info only and excluded from the primary sort.
    items.sort(key=lambda x: (
        -float(x.get("profile_score") or 0),            # profile fit
        -1 if x.get("ai_should_buy") else 0,            # AI buy-permitted first
        -float(x.get("ai_adjusted_score") or 0),        # higher adjusted score first
        -float(x.get("regime_fit") or 0),               # higher regime fit first
        -float(x.get("acc_trade_price_24h") or 0),      # higher trading value first
    ))

    # prioritize AI should_buy=True, but still show False ones (as a warning)
    # don't fully exclude them; should_buy=False ones go to the back
    items = [x for x in items if x.get("ai_score", 0) >= 0.3]  # lowered minimum threshold

    # keep matched-strategy coin stats, but simplify the return to top-N by profile score.
    matched = [x for x in items if x.get("strategy_match")]
    final_items = items[:n]

    # [2026-02-01] enrich profile-based score (only if missing)
    for item in final_items:
        mkt = item.get("market")
        if mkt and mkt in ranked_markets and item.get("profile_features") is None:
            score, features = ranked_markets[mkt]
            item["profile_score"] = round(score, 4)
            item["profile_features"] = {
                "volatility": round(features.volatility, 6),
                "momentum": round(features.momentum, 4),
                "liquidity": round(features.liquidity, 2),
                "trend_slope": round(features.trend_slope, 6),
                "range_ratio": round(features.range_ratio, 6),
            }

    result = {
        "ok": True,
        "items": final_items,
        "profile": profile,
        "strategy": strategy_label,
        "min_price": min_price_eff,
        "max_price": max_price_eff,
        "matched_count": len(matched),
        "total_analyzed": len(items),
    }

    # --- CACHE SET ---
    _set_cached(cache_key, result)
    _recommend_semaphore.release()
    return result


# ============================================================
# G-pre. Background pre-warm helper (called from hyper_system._strategy_recommend_loop)
# ============================================================
def prewarm_recommendation(system: Any, strategy: str, n: int = 20) -> None:
    """Serially refresh the strategy recommendation cache in the background.

    Called from hyper_system._strategy_recommend_loop at 45s intervals per strategy.
    - skips if the cache is alive within 120s (cycle ~540-610s > 120s, so it refreshes each cycle)
    - the semaphore is managed inside get_rich_recommendations, so nothing extra here
    - execution failures (semaphore contention) are retried on the next cycle
    """
    import types

    st_raw = strategy.strip().upper()
    snipers_mode = (st_raw == "SNIPERS")
    strategy_label = "SNIPERS" if snipers_mode else st_raw
    cache_key = _build_cache_key(
        "recommendations",
        strategy=strategy_label,
        n=n,
        min_price=round(0.0, 8),
        max_price=round(0.0, 8),
    )
    if _get_cached(cache_key, ttl=120):
        return  # still fresh - skip

    # fake request: get_rich_recommendations only needs request.app.state.system
    state = types.SimpleNamespace(system=system)
    app_ns = types.SimpleNamespace(state=state)
    req = types.SimpleNamespace(app=app_ns)
    try:
        get_rich_recommendations(req, strategy=strategy, n=n, min_price=0.0, max_price=0.0)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[RECOMMEND_API] prewarm get_rich_recommendations: %s", exc, exc_info=True)


# ============================================================
# F. Strategy Recommendation (which coin fits which strategy)
# ============================================================
@router.get(
    "/recommend",
    summary="Recommend best strategy for each coin",
    responses={
        200: {"description": "Strategy recommendation for all or specific markets"},
    },
)
def recommend_strategy(
    request: Request,
    market: Optional[str] = Query(None, description="Specific market (all if omitted)"),
    top_n: int = Query(20, description="Top N only"),
):
    """
    Recommend which strategy best fits each coin.

    - LADDER: high volatility + downtrend (scaled buys)
    - LIGHTNING: strong upward momentum (scalping)
    - GAZUA: AI upward prediction + sideways/up (trend following)
    - PINGPONG: stable range-bound (range trading)
    """
    from app.manager.strategy_graduator import suggest_strategy_for_ai_features

    system = request.app.state.system

    # 0) query markets scheduled for delisting
    # Note: Binance does not provide a delisting API, so use an empty dict
    delisting_markets = {}

    # 1) market list
    if market:
        markets = [market.upper()]
    else:
        try:
            all_markets_resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=5)
            if all_markets_resp.ok:
                all_market_data = parse_bybit_list(all_markets_resp.json())
                markets = []
                for m in all_market_data:
                    if not isinstance(m, dict):
                        continue
                    sym = str(m.get("symbol") or "").upper()
                    mk = Q.normalize(sym)
                    if not Q.config.market_prefix or mk.startswith(Q.config.market_prefix):
                        markets.append(mk)
                markets = markets[:50]
            else:
                markets = [Q.market("BTC"), Q.market("ETH"), Q.market("XRP")]
        except Exception as exc:
            logger.error("[RECOMMEND] market list fetch FAILED, using minimal fallback: %s", exc, exc_info=True)
            markets = [Q.market("BTC"), Q.market("ETH"), Q.market("XRP")]

    # 2) fetch candle data (parallel)
    def fetch_candles(m):
        try:
            # Market format (already in correct format)
            exchange_market = m if not Q.config.market_prefix or m.startswith(Q.config.market_prefix) else Q.normalize(m)
            r = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": exchange_market, "interval": "3", "limit": 100}, timeout=5)
            if r.ok:
                raw = parse_bybit_list(r.json())
                candles = []
                for k in raw:
                    if isinstance(k, (list, tuple)) and len(k) >= 6:
                        candles.append({
                            "trade_price": float(k[4]),
                            "high_price": float(k[2]),
                            "low_price": float(k[3]),
                            "opening_price": float(k[1]),
                            "candle_acc_trade_volume": float(k[5]),
                            "timestamp": int(k[0]),
                        })
                return m, candles
        except Exception as exc:
            logger.warning("[RECOMMEND_API] market format: %s", exc, exc_info=True)
        return m, None

    candle_map = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(fetch_candles, markets[:50]))  # max 50
    for m, data in results:
        if data:
            candle_map[m] = data

    # 3) Brain analysis + strategy recommendation
    recommendations = []
    brain = getattr(system.engine, "pipeline", None)
    if brain and hasattr(brain, "brain"):
        brain = brain.brain
    else:
        brain = None

    import logging
    logger = logging.getLogger("strategy_router")
    logger.info(f"[recommend] candle_map has {len(candle_map)} markets")

    for m, candles in candle_map.items():
        # RSI calculation needs at least 15 data points (length 14 + 1)
        if not candles or len(candles) < 15:
            logger.warning(f"[recommend] {m}: skipped (candles={len(candles) if candles else 0})")
            continue

        try:
            # price history (convert newest-first -> oldest-first)
            # Binance klines may come as lists, so handle that
            if candles and isinstance(candles[0], list):
                # Raw candle format: [timestamp, open, high, low, close, volume, ...]
                prices = [float(c[4]) for c in reversed(candles) if len(c) >= 5]
            else:
                # Dict format (our internal format)
                prices = [float(c.get("trade_price") or 0) for c in reversed(candles)]
            current_price = prices[-1] if prices else 0

            # Brain analysis
            if brain:
                b_out = brain.analyze(m, current_price, price_history=prices)
                ai_score = b_out.ai_prediction
                volatility = b_out.volatility
                trend = b_out.trend
                momentum = b_out.momentum
                rsi = b_out.rsi
            else:
                ai_score = 0.5
                volatility = indicators.volatility(prices, 20) or 0.0
                trend = indicators.trend(prices, 20) or 0.0
                momentum = indicators.trend(prices, 3) or 0.0
                rsi = indicators.rsi(prices, 14) or 50.0

            # strategy recommendation
            strategy, confidence, reason = suggest_strategy_for_ai_features(
                momentum=momentum,
                volatility=volatility,
                trend=trend,
                ai_prediction=ai_score,
                rsi=rsi,
            )

            # check delisting warning
            delist_info = delisting_markets.get(m)
            is_delisting = delist_info is not None

            recommendations.append({
                "market": m,
                "price": current_price,
                "recommended_strategy": strategy,
                "confidence": round(confidence, 3),
                "reason": reason,
                "ai_score": round(ai_score, 3),
                "volatility": round(volatility, 4),
                "trend": round(trend, 4),
                "momentum": round(momentum, 4),
                "rsi": round(rsi, 1),
                "delisting": is_delisting,
                "delisting_date": delist_info.get("delisting_date") if delist_info else None,
                "warning": "⚠️ Delisting scheduled" if is_delisting else None,
            })
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
            logger.warning("[RECOMMEND_API] %s: %s", m, e, exc_info=True)
            continue

    # 4) sort by confidence
    recommendations.sort(key=lambda x: x["confidence"], reverse=True)

    # 5) group by strategy
    by_strategy = {}
    for r in recommendations:
        s = r["recommended_strategy"]
        if s not in by_strategy:
            by_strategy[s] = []
        by_strategy[s].append(r)

    return {
        "ok": True,
        "total": len(recommendations),
        "recommendations": recommendations[:top_n],
        "by_strategy": {k: v[:5] for k, v in by_strategy.items()},  # top 5 per strategy
        "summary": {
            "LADDER": len(by_strategy.get("LADDER", [])),
            "LIGHTNING": len(by_strategy.get("LIGHTNING", [])),
            "GAZUA": len(by_strategy.get("GAZUA", [])),
            "AUTOLOOP": len(by_strategy.get("AUTOLOOP", [])),
            "PINGPONG": len(by_strategy.get("PINGPONG", [])),
        }
    }
