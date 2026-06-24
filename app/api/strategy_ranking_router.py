# ============================================================
# File: app/api/strategy_ranking_router.py
# Phase 1-H file diet extraction from strategy_router.py
#
# Contains ranking/scoring/analysis endpoints:
#   - holdings_upside_ranking, _get_upside_reason
#   - market_upside_ranking, _get_market_upside_reason
#   - market_rebound_ranking
#   - market_rsi_ranking
#   - market_tech_score
#   - market_rankings_unified
#   - calc_params
#   - get_daily_pnl
#   - get_multi_timeframe_analysis, get_multi_timeframe_batch
#   - _get_market_benchmark
#   - market_surge_scanner
# ============================================================

from fastapi import APIRouter, Request, Query
from typing import Dict, Any, List, Optional
import logging

import json
from app.core.rate_limiter import bybit_get
logger = logging.getLogger(__name__)

from app.core.constants import (
    BYBIT_MARKET_TICKERS,
    BYBIT_MARKET_KLINE,
    BYBIT_MARKET_INSTRUMENTS,
    bybit_v5_rest_category,
    parse_bybit_list,
    normalize_bybit_ticker,
)
from app.strategy import indicators
from app.core.currency import Q
from app.manager.oma_market_registry import MarketState
from app.api.strategy_utils import (
    _get_cached, _set_cached, _build_cache_key,
)

router = APIRouter()


# ============================================================
# Holdings Upside Ranking - upside potential ranking of held coins
# ============================================================

@router.get(
    "/holdings/upside",
    summary="Get upside potential ranking of current holdings",
    responses={
        200: {"description": "Holdings ranked by upside potential"},
    },
)
def holdings_upside_ranking(
    request: Request,
    top_n: int = Query(3, ge=1, le=20, description="Number of top coins to return"),
):
    """
    Upside potential ranking of currently held coins.

    Combines AI analysis, technical indicators, and market momentum
    to return the TOP N coins with the greatest upside potential.

    Evaluation factors:
    - AI prediction score (ai_prediction)
    - Trend strength (trend)
    - RSI oversold condition
    - Bollinger Band position (near lower band = upside potential)
    - Current PnL (heavily dropped coin = rebound potential)
    """
    system = request.app.state.system

    # 1. Fetch currently held coins
    holdings = []

    # First check trade_client
    if not hasattr(system, "trade_client") or system.trade_client is None:
        return {"ok": False, "error": "trade_client not available (DRY mode?)"}

    try:
        accounts = system.trade_client.accounts(skip_currencies=["USDT"])
        if accounts is None:
            return {"ok": False, "error": "accounts() returned None"}

        for a in accounts:
            cur = str(a.get("currency") or "").upper()
            if not cur:
                continue
            qty = float(a.get("balance") or 0) + float(a.get("locked") or 0)
            if qty <= 0:
                continue
            market = f"{cur}/USDT"
            symbol = f"{cur}USDT"
            avg_buy = float(a.get("avg_buy_price") or 0)
            holdings.append({
                "market": market,
                "symbol": symbol,
                "currency": cur,
                "qty": qty,
                "avg_buy_price": avg_buy,
            })
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("strategy_ranking_router.holdings_upside_ranking L104: %s", e)
        import traceback
        return {"ok": False, "error": f"Failed to fetch holdings: {e}", "traceback": traceback.format_exc()}

    if not holdings:
        return {"ok": True, "message": "No held coins (balance 0)", "rankings": []}

    # 2. Fetch current prices (exchange ticker)
    symbols = [h["symbol"] for h in holdings]
    try:
        # Convert symbols to exchange market format
        exchange_markets = []
        for s in symbols:
            if s.startswith(Q.config.market_prefix):
                exchange_markets.append(s)
            else:
                # Extract base currency from symbol like BTCUSDT
                base = s[:-4] if s.endswith("USDT") else s
                exchange_markets.append(Q.market(base))
        market_set = set(m.upper() for m in exchange_markets)
        resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        raw_data = parse_bybit_list(resp.json())
        tickers = {}
        for t in raw_data:
            if not isinstance(t, dict):
                continue
            t = normalize_bybit_ticker(t)
            if t.get("market", "").upper() in market_set:
                tickers[t["market"]] = t
    except Exception:
        logger.error("strategy_ranking_router.holdings_upside_ranking L133 except", exc_info=True)
        tickers = {}

    # 3. Compute upside potential score for each coin
    rankings = []

    for h in holdings:
        market = h["market"]
        symbol = h["symbol"]
        # Look up by market code (market format)
        ticker = tickers.get(market, {})
        current_price = float(ticker.get("trade_price") or 0)

        if current_price <= 0:
            continue

        avg_buy = h["avg_buy_price"]
        qty = h["qty"]

        # Current PnL
        pnl_pct = ((current_price - avg_buy) / avg_buy * 100) if avg_buy > 0 else 0

        # Valuation
        eval_usdt = current_price * qty

        # AI analysis
        ai_score = 0.5
        trend = 0.0
        volatility = 0.0
        rsi = 50.0
        bb_position = 0.5  # 0=lower, 0.5=middle, 1=upper

        ctx = system.coordinator.get_context(market)
        if ctx:
            brain = getattr(ctx, "current_ai", {}).get("brain", {})
            ai_score = float(brain.get("ai_prediction", 0.5))
            trend = float(brain.get("trend", 0.0))
            volatility = float(brain.get("volatility", 0.0))
            rsi = float(brain.get("rsi", 50.0))

            # Compute Bollinger Band position
            bb_upper = float(brain.get("bb_upper", current_price))
            bb_lower = float(brain.get("bb_lower", current_price))
            if bb_upper > bb_lower:
                bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)

        # Compute upside potential score (0~100)
        upside_score = 0.0

        # (1) AI prediction score (weight: 30%)
        # >= 0.5 is a bullish signal
        ai_factor = (ai_score - 0.5) * 2.0  # -1 ~ +1
        upside_score += max(0, ai_factor * 30)

        # (2) Trend strength (weight: 20%)
        # trend > 0 means uptrend
        trend_factor = min(1.0, max(-1.0, trend))
        upside_score += max(0, trend_factor * 20)

        # (3) RSI oversold bonus (weight: 20%)
        # RSI < 30 = oversold = rebound potential
        if rsi < 30:
            upside_score += 20
        elif rsi < 40:
            upside_score += 10
        elif rsi > 70:
            upside_score -= 10  # overbought = downside risk

        # (4) Bollinger Band position (weight: 15%)
        # near lower band = upside potential
        bb_factor = 1.0 - bb_position  # 0=upper, 1=lower
        upside_score += bb_factor * 15

        # (5) Drop rebound expectation (weight: 15%)
        # heavily dropped coin = rebound potential (but excessive drop = fundamental issue)
        if -30 <= pnl_pct < -10:
            upside_score += 15  # moderate drop = rebound expected
        elif -10 <= pnl_pct < 0:
            upside_score += 10
        elif pnl_pct < -30:
            upside_score += 5   # excessive drop = risk

        # Normalize score (0~100)
        upside_score = max(0, min(100, upside_score))

        # Expected upside (heuristic)
        # volatility-based + AI confidence
        daily_range = float(ticker.get("high_price") or current_price) - float(ticker.get("low_price") or current_price)
        daily_vol_pct = daily_range / current_price if current_price > 0 else 0.05
        expected_upside_pct = daily_vol_pct * 100 * max(0.2, ai_score)

        rankings.append({
            "rank": 0,
            "market": market,
            "currency": h["currency"],
            "current_price": current_price,
            "avg_buy_price": avg_buy,
            "qty": qty,
            "eval_usdt": eval_usdt,
            "pnl_pct": round(pnl_pct, 2),
            "upside_score": round(upside_score, 1),
            "expected_upside_pct": round(expected_upside_pct, 2),
            "analysis": {
                "ai_score": round(ai_score, 3),
                "trend": round(trend, 3),
                "rsi": round(rsi, 1),
                "bb_position": round(bb_position, 2),
                "volatility": round(volatility, 4),
            },
            "reason": _get_upside_reason(ai_score, trend, rsi, bb_position, pnl_pct),
        })

    # Sort by upside potential
    rankings.sort(key=lambda x: x["upside_score"], reverse=True)

    # Assign ranks
    for i, r in enumerate(rankings):
        r["rank"] = i + 1

    return {
        "ok": True,
        "total_holdings": len(rankings),
        "top_n": top_n,
        "rankings": rankings[:top_n],
        "all_rankings": rankings,
        "summary": {
            "best_pick": rankings[0] if rankings else None,
            "avg_upside_score": round(sum(r["upside_score"] for r in rankings) / len(rankings), 1) if rankings else 0,
            "total_eval_usdt": sum(r["eval_usdt"] for r in rankings),
        }
    }


def _get_upside_reason(ai_score: float, trend: float, rsi: float, bb_position: float, pnl_pct: float) -> str:
    """Generate the reason for the upside potential assessment."""
    reasons = []

    if ai_score >= 0.65:
        reasons.append("Strong AI buy signal")
    elif ai_score >= 0.55:
        reasons.append("AI buy bias")

    if trend >= 0.3:
        reasons.append("Uptrend")

    if rsi < 30:
        reasons.append("RSI oversold")
    elif rsi < 40:
        reasons.append("RSI low")

    if bb_position < 0.2:
        reasons.append("BB lower band touch")
    elif bb_position < 0.4:
        reasons.append("Near BB lower band")

    if -30 <= pnl_pct < -10:
        reasons.append("Drop rebound expected")

    return " · ".join(reasons) if reasons else "Analyzing"


# ============================================================
# Market Upside Ranking - upside potential ranking of all markets
# ============================================================

@router.get(
    "/market/upside",
    summary="Get upside potential ranking of all exchange markets",
    responses={
        200: {"description": "All markets ranked by upside potential"},
    },
)
def market_upside_ranking(
    request: Request,
    top_n: int = Query(10, ge=1, le=50, description="Number of top coins to return"),
    min_volume_usdt: float = Query(1_000_000, ge=0, description="Minimum 24h volume in USDT"),
    min_price: float = Query(100, ge=0, description="Minimum price in USDT (filters penny coins)"),
    max_spread_bps: float = Query(50, ge=0, description="Maximum bid-ask spread in basis points (0=no filter)"),
):
    """
    Upside potential ranking of all markets.

    Fast analysis based on ticker data + engine context.
    (Responds immediately without fetching candles)

    Evaluation factors:
    - AI prediction score (when context is available)
    - Daily change rate and volatility
    - Trading volume
    - Current price position (vs high/low)

    Filtering:
    - min_price: minimum price (default 0.01 USDT) - excludes penny coins
    - max_spread_bps: maximum spread (default 50 bps = 0.5%) - excludes illiquid coins
    """
    import time

    effective_min_vol = min_volume_usdt if min_volume_usdt else 1_000_000

    # Cache check
    cache_key = _build_cache_key(
        "market/upside",
        top_n=top_n, min_volume_usdt=effective_min_vol,
        min_price=min_price, max_spread_bps=max_spread_bps
    )
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    # 1. Fetch all exchange market tickers
    try:
        # First get all markets
        markets_resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        all_markets = [Q.normalize(str(m.get("symbol") or "")) for m in parse_bybit_list(markets_resp.json()) if isinstance(m, dict) and str(m.get("symbol") or "")]
        if not all_markets:
            return {"ok": True, "message": "No markets", "rankings": []}

        # Then get tickers for all markets
        resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        _raw_tickers = parse_bybit_list(resp.json())
        _market_set = set(m.upper() for m in all_markets[:100]) if all_markets else set()
        tickers = [normalize_bybit_ticker(t) for t in _raw_tickers if isinstance(t, dict) and Q.normalize(str(t.get("symbol") or "")).upper() in _market_set]
    except Exception as e:
        logger.warning("strategy_ranking_router.market_upside_ranking L355: %s", e)
        return {"ok": False, "error": f"Failed to fetch tickers: {e}"}

    if not tickers:
        return {"ok": True, "message": "No markets", "rankings": []}

    # 2. Volume + price filtering
    filtered_tickers = []
    filtered_out = {"low_volume": 0, "low_price": 0, "wide_spread": 0}

    for t in tickers:
        vol24 = float(t.get("acc_trade_price_24h") or 0)
        last_price = float(t.get("trade_price") or 0)

        if vol24 < effective_min_vol:
            filtered_out["low_volume"] += 1
            continue
        if min_price > 0 and last_price < min_price:
            filtered_out["low_price"] += 1
            continue
        # Note: Ticker doesn't include bid/ask prices, skip spread filter

        filtered_tickers.append(t)

    if not filtered_tickers:
        return {"ok": True, "message": "No markets meet filter criteria", "rankings": [], "filtered_out": filtered_out}

    # 3. Compute upside potential score for each coin (ticker only, no candles)
    system = request.app.state.system
    rankings = []

    for ticker in filtered_tickers:
        market = ticker.get("market", "")
        currency = market.replace(Q.config.market_prefix, "") if market.startswith(Q.config.market_prefix) else market
        current_price = float(ticker.get("trade_price") or 0)

        if current_price <= 0:
            continue

        vol24 = float(ticker.get("acc_trade_price_24h") or 0)
        high = float(ticker.get("high_price") or current_price)
        low = float(ticker.get("low_price") or current_price)
        prev_close = float(ticker.get("prev_closing_price") or current_price)

        # Daily range (%)
        daily_vol_pct = (high - low) / low if low > 0 else 0.05

        # Daily change rate (%) - signed_change_rate is a ratio so * 100
        change_pct = float(ticker.get("signed_change_rate") or 0) * 100

        # Current price position (vs high/low) - closer to low = more upside
        price_position = (current_price - low) / (high - low) if high > low else 0.5

        # AI analysis (used when context is available)
        ai_score = 0.5
        trend = 0.0
        volatility = daily_vol_pct
        rsi = 50.0
        bb_position = price_position  # ticker-based approximation

        ctx = system.coordinator.get_context(market)
        if ctx:
            brain = getattr(ctx, "current_ai", {}).get("brain", {})
            ai_score = float(brain.get("ai_prediction", 0.5))
            trend = float(brain.get("trend", 0.0))
            volatility = float(brain.get("volatility", daily_vol_pct))
            rsi = float(brain.get("rsi", 50.0))
            bb_upper = float(brain.get("bb_upper", high))
            bb_lower = float(brain.get("bb_lower", low))
            if bb_upper > bb_lower:
                bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)
        else:
            # No context: ticker-based heuristic
            # If declining, expect rebound (but exclude excessive drops)
            if -5 <= change_pct < -1:
                trend = 0.1  # slight rebound expected
            elif change_pct >= 2:
                trend = 0.2  # upward momentum

        # Compute upside potential score (0~100)
        upside_score = 0.0

        # (1) AI prediction score (weight: 25%)
        ai_factor = (ai_score - 0.5) * 2.0
        upside_score += max(0, ai_factor * 25)

        # (2) Trend strength (weight: 20%)
        trend_factor = min(1.0, max(-1.0, trend * 10))  # scale adjustment
        upside_score += max(0, trend_factor * 20)

        # (3) RSI oversold bonus (weight: 20%)
        if rsi < 30:
            upside_score += 20
        elif rsi < 40:
            upside_score += 12
        elif rsi < 50:
            upside_score += 5
        elif rsi > 70:
            upside_score -= 10

        # (4) Bollinger Band position (weight: 15%)
        bb_factor = 1.0 - bb_position
        upside_score += bb_factor * 15

        # (5) High-volume bonus (weight: 10%)
        # higher volume = better liquidity (USDT basis)
        if vol24 >= 500_000_000:  # 500M USDT or more
            upside_score += 10
        elif vol24 >= 100_000_000:  # 100M USDT or more
            upside_score += 7
        elif vol24 >= 50_000_000:  # 50M USDT or more
            upside_score += 4

        # (6) Volatility bonus (weight: 10%)
        # moderate volatility = opportunity
        if 0.03 <= daily_vol_pct <= 0.10:
            upside_score += 10
        elif 0.02 <= daily_vol_pct < 0.03:
            upside_score += 5
        elif daily_vol_pct > 0.15:
            upside_score -= 5  # excessive volatility = risk

        # Normalize score (0~100)
        upside_score = max(0, min(100, upside_score))

        # Expected upside
        confidence = max(0.2, (ai_score - 0.5) * 2.0)
        expected_upside_pct = daily_vol_pct * 100 * confidence

        rankings.append({
            "rank": 0,
            "market": market,
            "currency": currency,
            "current_price": current_price,
            "change_pct": round(change_pct, 2),
            "volume_24h_usdt": vol24,
            "upside_score": round(upside_score, 1),
            "expected_upside_pct": round(expected_upside_pct, 2),
            "analysis": {
                "ai_score": round(ai_score, 3),
                "trend": round(trend, 4),
                "rsi": round(rsi, 1),
                "bb_position": round(bb_position, 2),
                "volatility": round(volatility, 4),
                "daily_range_pct": round(daily_vol_pct * 100, 2),
            },
            "reason": _get_market_upside_reason(ai_score, trend, rsi, bb_position, vol24, daily_vol_pct),
        })

    # Sort by upside potential
    rankings.sort(key=lambda x: x["upside_score"], reverse=True)

    # Assign ranks
    for i, r in enumerate(rankings):
        r["rank"] = i + 1

    result = {
        "ok": True,
        "total_markets": len(rankings),
        "analyzed_at": time.time(),
        "top_n": top_n,
        "rankings": rankings[:top_n],
        "summary": {
            "best_pick": rankings[0] if rankings else None,
            "avg_upside_score": round(sum(r["upside_score"] for r in rankings) / len(rankings), 1) if rankings else 0,
            "high_potential_count": len([r for r in rankings if r["upside_score"] >= 50]),
        }
    }
    _set_cached(cache_key, result)
    return result


def _get_market_upside_reason(ai_score: float, trend: float, rsi: float, bb_position: float, vol24: float, daily_vol_pct: float) -> str:
    """Generate the reason for the market-wide upside potential assessment."""
    reasons = []

    if ai_score >= 0.65:
        reasons.append("Strong AI buy")
    elif ai_score >= 0.55:
        reasons.append("AI buy bias")

    if trend >= 0.03:
        reasons.append("Uptrend")
    elif trend >= 0.01:
        reasons.append("Trend reversing")

    if rsi < 30:
        reasons.append("RSI oversold")
    elif rsi < 40:
        reasons.append("RSI low")

    if bb_position < 0.2:
        reasons.append("BB lower band")
    elif bb_position < 0.35:
        reasons.append("Near BB lower band")

    if vol24 >= 500_000_000:  # 500M USDT or more
        reasons.append("Large cap")
    elif vol24 >= 100_000_000:  # 100M USDT or more
        reasons.append("Mid cap")

    if 0.05 <= daily_vol_pct <= 0.10:
        reasons.append("Moderate volatility")

    return " · ".join(reasons) if reasons else "Analyzing"


# ============================================================
# Rebound Opportunity - find rebound opportunities after a sharp drop
# ============================================================

@router.get(
    "/market/rebound",
    summary="Get rebound opportunity ranking from all exchange markets",
    responses={
        200: {"description": "All markets ranked by rebound potential after decline"},
    },
)
def market_rebound_ranking(
    request: Request,
    top_n: int = Query(5, ge=1, le=30, description="Number of top coins to return"),
    min_volume_usdt: float = Query(1_000_000, ge=0, description="Minimum 24h volume in USDT"),
    max_decline_pct: float = Query(-3, le=0, description="Maximum change rate to consider (e.g., -3 means only coins down 3%+)"),
    timeframe: str = Query("24h", description="Timeframe for decline: 1h, 4h, or 24h"),
    min_price: float = Query(100, ge=0, description="Minimum price in USDT (filters penny coins)"),
    max_spread_bps: float = Query(50, ge=0, description="Maximum bid-ask spread in basis points (0=no filter)"),
):
    """
    Find rebound opportunities after a sharp drop across all markets.

    Analyzes the cause of the drop and finds coins likely to stop falling and rebound.

    Evaluation factors:
    - Drop over the selected timeframe (required: at or below max_decline_pct)
    - Current price position vs high (near the bottom)
    - Volume spike (confirming a bottom after panic selling)
    - Intraday volatility and rebound signals
    - AI analysis (when context is available)

    Filtering:
    - min_price: minimum price (default 0.01 USDT) - excludes penny coins
    - max_spread_bps: maximum spread (default 50 bps = 0.5%) - excludes illiquid coins
    """
    import time

    # Cache check
    cache_key = _build_cache_key(
        "market/rebound",
        top_n=top_n, min_volume_usdt=min_volume_usdt, max_decline_pct=max_decline_pct,
        timeframe=timeframe, min_price=min_price, max_spread_bps=max_spread_bps
    )
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    # 1. Fetch all exchange market tickers
    try:
        # First get all markets
        markets_resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        all_markets = [Q.normalize(str(m.get("symbol") or "")) for m in parse_bybit_list(markets_resp.json()) if isinstance(m, dict) and str(m.get("symbol") or "")]
        if not all_markets:
            return {"ok": True, "message": "No markets", "rankings": []}

        # Then get tickers for all markets
        resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        _raw_tickers = parse_bybit_list(resp.json())
        _market_set = set(m.upper() for m in all_markets[:100]) if all_markets else set()
        tickers = [normalize_bybit_ticker(t) for t in _raw_tickers if isinstance(t, dict) and Q.normalize(str(t.get("symbol") or "")).upper() in _market_set]
    except Exception as e:
        logger.warning("strategy_ranking_router.market_rebound_ranking L623: %s", e)
        return {"ok": False, "error": f"Failed to fetch tickers: {e}"}

    if not tickers:
        return {"ok": True, "message": "No markets", "rankings": []}

    # 2. Build ticker map after volume + price filtering
    tickers = {}
    filtered_out = {"low_volume": 0, "low_price": 0, "wide_spread": 0}

    for t in tickers:
        market = t.get("market", "")
        vol_24h = float(t.get("acc_trade_price_24h") or 0)
        last_price = float(t.get("trade_price") or 0)

        if vol_24h < min_volume_usdt:
            filtered_out["low_volume"] += 1
            continue
        if min_price > 0 and last_price < min_price:
            filtered_out["low_price"] += 1
            continue
        # Note: Ticker doesn't include bid/ask prices, skip spread filter

        tickers[market] = t

    if not tickers:
        return {"ok": True, "message": "No markets meet filter criteria", "rankings": [], "filtered_out": filtered_out}

    # 2.5. Fetch 1h/4h candles (if needed)
    candle_changes = {}  # market -> change_rate
    timeframe_label = "24h"

    if timeframe in ("1h", "4h"):
        minutes = 60 if timeframe == "1h" else 240
        timeframe_label = "1h" if timeframe == "1h" else "4h"

        for market in list(tickers.keys())[:100]:  # up to 100 only
            try:
                candle_resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": str(minutes), "limit": 2}, timeout=3.0)
                _raw_c = parse_bybit_list(candle_resp.json())
                candles = [{"trade_price": float(k[4]), "timestamp": int(k[0])} for k in _raw_c if isinstance(k, (list, tuple)) and len(k) >= 5]

                if candles and len(candles) >= 2:
                    current = float(candles[0].get("trade_price") or 0)  # most recent
                    past = float(candles[1].get("trade_price") or 0)  # earlier candle
                    if past > 0:
                        candle_changes[market] = ((current - past) / past) * 100

                time.sleep(0.02)  # OS scheduling yield
            except Exception as exc:
                logger.warning("[RANKING_API] 2.5. fetch 1h/4h candles: %s", exc, exc_info=True)

    # 3. Filter dropping coins and compute scores
    system = request.app.state.system
    rankings = []

    for market, ticker in tickers.items():
        current_price = float(ticker.get("trade_price") or 0)
        if current_price <= 0:
            continue

        vol_24h = float(ticker.get("acc_trade_price_24h") or 0)

        # Change rate (depends on timeframe)
        if timeframe in ("1h", "4h") and market in candle_changes:
            change_rate = candle_changes[market]
        else:
            change_rate = float(ticker.get("signed_change_rate") or 0) * 100

        # Drop filter (only at or below max_decline_pct)
        if change_rate > max_decline_pct:
            continue

        currency = market.replace(Q.config.market_prefix, "") if market.startswith(Q.config.market_prefix) else market

        # Price position analysis
        high_price = float(ticker.get("high_price") or current_price)
        low_price = float(ticker.get("low_price") or current_price)
        prev_close = float(ticker.get("prev_closing_price") or current_price)

        # Position within intraday range (0=low, 1=high)
        if high_price > low_price:
            intraday_position = (current_price - low_price) / (high_price - low_price)
        else:
            intraday_position = 0.5

        # Drop vs previous close
        drop_from_prev = ((current_price - prev_close) / prev_close * 100) if prev_close > 0 else 0

        # AI analysis (when context is available)
        rsi = 50.0
        trend = 0.0
        bb_position = 0.5
        ai_score = 0.5
        active_strategy = None

        ctx = system.coordinator.contexts.get(market)

        # Check if market is actively used in OMA
        if system.oma_registry.has_market(market):
            oma_state = system.oma_registry.get_state(market).value
            if oma_state in ("ACTIVE", "RECOVERY"):
                if ctx:
                    ctrls = getattr(ctx, "controls", {}) or {}
                    strat = ctrls.get("strategy", {}) or {}
                    if strat.get("enabled"):
                        active_strategy = str(strat.get("mode") or "CUSTOM").upper()
                    else:
                        active_strategy = "AI"
                else:
                    active_strategy = oma_state

        if ctx:
            brain = getattr(ctx, "current_ai", {}).get("brain", {})
            rsi = float(brain.get("rsi", 50.0))
            trend = float(brain.get("trend", 0.0))
            ai_score = float(brain.get("ai_prediction", 0.5))

            bb_upper = float(brain.get("bb_upper", current_price))
            bb_lower = float(brain.get("bb_lower", current_price))
            if bb_upper > bb_lower:
                bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)

        # ========== Compute rebound opportunity score ==========
        rebound_score = 0.0
        rebound_signals = []

        # (1) Drop analysis (weight: 25%)
        # NOTE: decline_severity values are kept in Korean — they are lookup keys
        # in the dashboard JS severity-icon map (dashboard.js).
        if change_rate <= -15:
            decline_severity = "폭락"
            rebound_score += 20  # extreme drop is risky
            rebound_signals.append(f"Crash ({change_rate:.1f}%)")
        elif change_rate <= -10:
            decline_severity = "급락"
            rebound_score += 25
            rebound_signals.append(f"Sharp drop ({change_rate:.1f}%)")
        elif change_rate <= -5:
            decline_severity = "하락"
            rebound_score += 20
            rebound_signals.append(f"Drop ({change_rate:.1f}%)")
        else:
            decline_severity = "조정"
            rebound_score += 15
            rebound_signals.append(f"Pullback ({change_rate:.1f}%)")

        # (2) Bottom rebound signal (weight: 25%)
        # Rebounding from the intraday low?
        if intraday_position < 0.2:
            rebound_score += 10
            rebound_signals.append("Near bottom")
        elif 0.2 <= intraday_position <= 0.5:
            rebound_score += 25  # rebounding from the bottom
            rebound_signals.append("Rebounding from bottom")
        elif 0.5 < intraday_position <= 0.7:
            rebound_score += 20
            rebound_signals.append("Recovery in progress")
        else:
            rebound_score += 5  # already recovered a lot

        # (3) RSI oversold (weight: 20%)
        if rsi < 25:
            rebound_score += 20
            rebound_signals.append(f"RSI deeply oversold ({rsi:.0f})")
        elif rsi < 30:
            rebound_score += 15
            rebound_signals.append(f"RSI oversold ({rsi:.0f})")
        elif rsi < 40:
            rebound_score += 10
            rebound_signals.append(f"RSI low ({rsi:.0f})")

        # (4) BB lower band (weight: 15%)
        if bb_position < 0.15:
            rebound_score += 15
            rebound_signals.append("Broke below BB lower band")
        elif bb_position < 0.3:
            rebound_score += 10
            rebound_signals.append("Near BB lower band")

        # (5) Volume analysis (weight: 15%) - USDT basis
        # high volume = focused attention
        if vol_24h >= 500_000_000:  # 500M USDT or more
            rebound_score += 15
            rebound_signals.append("Large volume")
        elif vol_24h >= 100_000_000:  # 100M USDT or more
            rebound_score += 10
            rebound_signals.append("Active trading")
        elif vol_24h >= 30_000_000:  # 30M USDT or more
            rebound_score += 5

        # Normalize score (0~100)
        rebound_score = max(0, min(100, rebound_score))

        rankings.append({
            "rank": 0,
            "market": market,
            "currency": currency,
            "current_price": current_price,
            "change_rate": round(change_rate, 2),
            "decline_severity": decline_severity,
            "rebound_score": round(rebound_score, 1),
            "intraday_position": round(intraday_position, 2),
            "volume_24h_usdt": vol_24h,
            "active_strategy": active_strategy,
            "analysis": {
                "rsi": round(rsi, 1),
                "bb_position": round(bb_position, 2),
                "trend": round(trend, 3),
                "ai_score": round(ai_score, 3),
                "high_price": high_price,
                "low_price": low_price,
            },
            "signals": rebound_signals,
            "reason": " · ".join(rebound_signals[:3]) if rebound_signals else "Analyzing",
        })

    # Sort by rebound opportunity score
    rankings.sort(key=lambda x: x["rebound_score"], reverse=True)

    # Assign ranks
    for i, r in enumerate(rankings):
        r["rank"] = i + 1

    result = {
        "ok": True,
        "timeframe": timeframe,
        "timeframe_label": timeframe_label,
        "total_declining": len(rankings),
        "top_n": top_n,
        "rankings": rankings[:top_n],
        "all_rankings": rankings[:30],  # up to 30 only
        "summary": {
            "best_rebound": rankings[0] if rankings else None,
            "avg_rebound_score": round(sum(r["rebound_score"] for r in rankings) / len(rankings), 1) if rankings else 0,
            "total_declining_count": len(rankings),
        }
    }
    _set_cached(cache_key, result)
    return result


# ============================================================
# RSI Ranking - RSI ranking of all USDT markets (find oversold coins)
# ============================================================

@router.get(
    "/market/rsi",
    summary="Get RSI ranking of all exchange markets",
    responses={
        200: {"description": "All markets ranked by RSI (lowest first = most oversold)"},
    },
)
def market_rsi_ranking(
    request: Request,
    top_n: int = Query(10, ge=1, le=50, description="Number of coins to return"),
    min_volume_usdt: float = Query(1_000_000, ge=0, description="Minimum 24h volume in USDT"),
    rsi_max: float = Query(40, ge=0, le=100, description="Maximum RSI to filter (only show oversold)"),
    min_price: float = Query(100, ge=0, description="Minimum price in USDT (filters penny coins)"),
    max_spread_bps: float = Query(50, ge=0, description="Maximum bid-ask spread in basis points (0=no filter)"),
):
    """
    RSI ranking of all markets.

    Lower RSI = oversold = rebound potential

    - RSI < 30: deeply oversold (strong rebound expected)
    - RSI 30~40: oversold (rebound possible)
    - RSI 40~60: neutral
    - RSI > 70: overbought (downside possible)

    Filtering:
    - min_price: minimum price (default 0.01 USDT) - excludes penny coins
    - max_spread_bps: maximum spread (default 50 bps = 0.5%) - excludes illiquid coins
    """
    import time as time_module

    # Cache check
    cache_key = _build_cache_key(
        "market/rsi",
        top_n=top_n, min_volume_usdt=min_volume_usdt, rsi_max=rsi_max,
        min_price=min_price, max_spread_bps=max_spread_bps
    )
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    # 1. Fetch all exchange market tickers
    try:
        # First get all markets
        markets_resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        all_markets = [Q.normalize(str(m.get("symbol") or "")) for m in parse_bybit_list(markets_resp.json()) if isinstance(m, dict) and str(m.get("symbol") or "")]
        if not all_markets:
            return {"ok": True, "message": "No markets", "rankings": []}

        # Then get tickers for all markets
        resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        _raw_tickers = parse_bybit_list(resp.json())
        _market_set = set(m.upper() for m in all_markets[:100]) if all_markets else set()
        tickers = [normalize_bybit_ticker(t) for t in _raw_tickers if isinstance(t, dict) and Q.normalize(str(t.get("symbol") or "")).upper() in _market_set]
    except Exception as e:
        logger.warning("strategy_ranking_router.market_rsi_ranking L921: %s", e)
        return {"ok": False, "error": f"Failed to fetch tickers: {e}"}

    if not tickers:
        return {"ok": True, "message": "No markets", "rankings": []}

    # 2. Build ticker map after volume + price filtering
    tickers = {}
    vol_filtered_markets = []
    filtered_out = {"low_volume": 0, "low_price": 0, "wide_spread": 0}

    for t in tickers:
        market = t.get("market", "")
        vol_24h = float(t.get("acc_trade_price_24h") or 0)
        last_price = float(t.get("trade_price") or 0)

        if vol_24h < min_volume_usdt:
            filtered_out["low_volume"] += 1
            continue
        if min_price > 0 and last_price < min_price:
            filtered_out["low_price"] += 1
            continue
        # Note: Ticker doesn't include bid/ask prices, skip spread filter

        tickers[market] = t
        vol_filtered_markets.append(market)

    if not vol_filtered_markets:
        return {"ok": True, "message": "No markets meet filter criteria", "rankings": [], "filtered_out": filtered_out}

    # 3. Fetch candles for RSI/MACD/Ichimoku calculation (60 x 15min candles)
    rankings = []

    def calc_ema(data, period):
        """Compute EMA"""
        if len(data) < period:
            return None
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period
        for price in data[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    for market in vol_filtered_markets[:80]:  # up to 80 only (API limit)
        try:
            candle_resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": "15", "limit": 60}, timeout=3.0)
            _raw_candles = parse_bybit_list(candle_resp.json())
            candles = [{"trade_price": float(k[4]), "high_price": float(k[2]), "low_price": float(k[3]), "opening_price": float(k[1]), "candle_acc_trade_volume": float(k[5]), "timestamp": int(k[0])} for k in _raw_candles if isinstance(k, (list, tuple)) and len(k) >= 6]

            if not candles or len(candles) < 26:
                continue

            # Bybit returns newest-first, reverse for calculations
            candles = list(reversed(candles))
            closes = [float(c.get("trade_price") or 0) for c in candles]
            highs = [float(c.get("high_price") or 0) for c in candles]
            lows = [float(c.get("low_price") or 0) for c in candles]
            volumes = [float(c.get("candle_acc_trade_volume") or 0) for c in candles]

            # RSI calculation (14 period)
            gains = []
            losses = []
            for i in range(1, len(closes)):
                diff = closes[i] - closes[i-1]
                if diff > 0:
                    gains.append(diff)
                    losses.append(0)
                else:
                    gains.append(0)
                    losses.append(abs(diff))

            if len(gains) < 14:
                continue

            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14

            if avg_loss == 0:
                rsi = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))

            # RSI filter
            if rsi > rsi_max:
                continue

            # MACD calculation (12, 26, 9)
            ema12 = calc_ema(closes, 12)
            ema26 = calc_ema(closes, 26)
            macd_line = (ema12 - ema26) if ema12 and ema26 else 0
            macd_score = 40 if macd_line > 0 else 20  # 40 if rising, 20 if falling

            # Simplified Ichimoku (tenkan 9, kijun 26)
            tenkan = (max(highs[-9:]) + min(lows[-9:])) / 2 if len(highs) >= 9 else closes[-1]
            kijun = (max(highs[-26:]) + min(lows[-26:])) / 2 if len(highs) >= 26 else closes[-1]
            current = closes[-1]

            # Ichimoku score: price above tenkan/kijun = rising
            ichimoku_score = 0
            if current > tenkan:
                ichimoku_score += 50
            if current > kijun:
                ichimoku_score += 50

            # Volume score: recent volume vs average volume
            avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 1
            current_vol = volumes[-1]
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1
            volume_score = min(100, int(vol_ratio * 40))  # max 100 points

            ticker = tickers.get(market, {})
            currency = market.replace(Q.config.market_prefix, "") if market.startswith(Q.config.market_prefix) else market
            current_price = float(ticker.get("trade_price") or closes[-1])
            change_rate = float(ticker.get("signed_change_rate") or 0) * 100
            vol_24h = float(ticker.get("acc_trade_price_24h") or 0)

            # RSI status
            if rsi < 20:
                rsi_status = "Deeply oversold"
                rsi_emoji = "🔴"
            elif rsi < 30:
                rsi_status = "Oversold"
                rsi_emoji = "🟠"
            elif rsi < 40:
                rsi_status = "Low"
                rsi_emoji = "🟡"
            else:
                rsi_status = "Neutral"
                rsi_emoji = "⚪"

            # RSI score (lower = higher score - oversold = rebound opportunity)
            rsi_score = max(0, 100 - int(rsi * 2.5))

            rankings.append({
                "rank": 0,
                "market": market,
                "currency": currency,
                "current_price": current_price,
                "change_rate": round(change_rate, 2),
                "rsi": round(rsi, 1),
                "rsi_status": rsi_status,
                "rsi_emoji": rsi_emoji,
                "volume_24h_usdt": vol_24h,
                "details": {
                    "volume_score": volume_score,
                    "rsi_score": rsi_score,
                    "ichimoku_score": ichimoku_score,
                    "macd_score": macd_score,
                },
            })

            time_module.sleep(0.02)  # Rate limit

        except (ConnectionError, OSError):
            logger.warning("[RSI ranking] %s connection failed, skipping", market)
            continue
        except Exception as exc:
            logger.warning("[RANKING_API] RSI score (lower = higher, oversold = rebound) except-> continue: %s", exc, exc_info=True)
            continue

    # Sort by lowest RSI
    rankings.sort(key=lambda x: x["rsi"])

    # Assign ranks
    for i, r in enumerate(rankings):
        r["rank"] = i + 1

    result = {
        "ok": True,
        "rsi_max": rsi_max,
        "total_oversold": len(rankings),
        "top_n": top_n,
        "rankings": rankings[:top_n],
        "all_rankings": rankings[:30],
        "summary": {
            "most_oversold": rankings[0] if rankings else None,
            "avg_rsi": round(sum(r["rsi"] for r in rankings) / len(rankings), 1) if rankings else 0,
        }
    }
    _set_cached(cache_key, result)
    return result


# ============================================================
# Technical Aggregate Score
# Combines volume + RSI + Ichimoku + MACD
# ============================================================

@router.get(
    "/market/tech-score",
    summary="Get comprehensive technical analysis score",
    responses={
        200: {"description": "All markets ranked by technical aggregate score"},
    },
)
def market_tech_score(
    request: Request,
    top_n: int = Query(10, ge=1, le=50, description="Number of coins to return"),
    min_volume_usdt: float = Query(10_000_000, ge=0, description="Minimum 24h volume in USDT"),
    min_score: float = Query(50, ge=0, le=100, description="Minimum score to show"),
    min_price: float = Query(100, ge=0, description="Minimum price in USDT (filters penny coins)"),
    max_spread_bps: float = Query(50, ge=0, description="Maximum bid-ask spread in basis points (0=no filter)"),
):
    """
    Comprehensive technical analysis score.

    Combines 4 indicators into a 0~100 buy-suitability score.

    - Volume spike (20%): volume increase vs average
    - RSI (25%): higher score the more oversold
    - Ichimoku (25%): cloud breakout/position
    - MACD (30%): golden cross, histogram direction

    Filtering:
    - min_volume_usdt: minimum 24h volume (default 100M USDT)
    - min_price: minimum price (default 0.01 USDT) - excludes penny coins
    - max_spread_bps: maximum spread (default 50 bps = 0.5%) - excludes illiquid coins
    """
    import time as time_module

    # Cache check
    cache_key = _build_cache_key(
        "market/tech-score",
        top_n=top_n, min_volume_usdt=min_volume_usdt, min_score=min_score,
        min_price=min_price, max_spread_bps=max_spread_bps
    )
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    # 1. Fetch all exchange market tickers
    try:
        # First get all markets
        markets_resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        all_markets = [Q.normalize(str(m.get("symbol") or "")) for m in parse_bybit_list(markets_resp.json()) if isinstance(m, dict) and str(m.get("symbol") or "")]
        if not all_markets:
            return {"ok": True, "message": "No markets", "rankings": []}

        # Then get tickers for all markets
        resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        _raw_tickers = parse_bybit_list(resp.json())
        _market_set = set(m.upper() for m in all_markets[:100]) if all_markets else set()
        tickers = [normalize_bybit_ticker(t) for t in _raw_tickers if isinstance(t, dict) and Q.normalize(str(t.get("symbol") or "")).upper() in _market_set]
    except Exception as e:
        logger.warning("strategy_ranking_router.market_tech_score L1162: %s", e)
        return {"ok": False, "error": f"Failed to fetch tickers: {e}"}

    if not tickers:
        return {"ok": True, "message": "No markets", "rankings": []}

    # 2. Build ticker map after volume + price filtering
    tickers = {}
    vol_filtered_markets = []
    filtered_out = {"low_volume": 0, "low_price": 0, "wide_spread": 0}

    for t in tickers:
        market = t.get("market", "")
        vol_24h = float(t.get("acc_trade_price_24h") or 0)
        last_price = float(t.get("trade_price") or 0)

        # Volume filter
        if vol_24h < min_volume_usdt:
            filtered_out["low_volume"] += 1
            continue

        # Price filter (excludes penny coins)
        if min_price > 0 and last_price < min_price:
            filtered_out["low_price"] += 1
            continue

        # Note: Ticker doesn't include bid/ask prices, skip spread filter

        tickers[market] = t
        vol_filtered_markets.append(market)

    if not vol_filtered_markets:
        return {"ok": True, "message": "No markets meet filter criteria", "rankings": [], "filtered_out": filtered_out}

    # 3. Compute technical score for each coin
    rankings = []

    for market in vol_filtered_markets[:60]:  # up to 60 (API limit)
        try:
            # Fetch 30 x 1h candles (for Ichimoku, MACD calculation)
            candle_resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": "60", "limit": 30}, timeout=3.0)
            _raw_candles = parse_bybit_list(candle_resp.json())
            candles = [{"trade_price": float(k[4]), "high_price": float(k[2]), "low_price": float(k[3]), "opening_price": float(k[1]), "candle_acc_trade_volume": float(k[5]), "timestamp": int(k[0])} for k in _raw_candles if isinstance(k, (list, tuple)) and len(k) >= 6]

            if not candles or len(candles) < 26:
                continue

            # Reversed for chronological order
            candles = list(reversed(candles))
            closes = [float(c.get("trade_price") or 0) for c in candles]
            volumes = [float(c.get("candle_acc_trade_volume") or 0) for c in candles]
            highs = [float(c.get("high_price") or 0) for c in candles]
            lows = [float(c.get("low_price") or 0) for c in candles]

            current_price = closes[-1]

            # ========== 1. RSI calculation (14 period) ==========
            gains, losses = [], []
            for i in range(1, min(15, len(closes))):
                diff = closes[i] - closes[i-1]
                gains.append(max(0, diff))
                losses.append(max(0, -diff))

            avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else 0
            avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else 1

            if avg_loss == 0:
                rsi = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))

            # RSI score (higher the more oversold)
            if rsi < 20:
                rsi_score = 100
            elif rsi < 30:
                rsi_score = 80
            elif rsi < 40:
                rsi_score = 60
            elif rsi < 50:
                rsi_score = 40
            elif rsi < 60:
                rsi_score = 30
            elif rsi < 70:
                rsi_score = 20
            else:
                rsi_score = 0

            # ========== 2. MACD calculation ==========
            def ema(data, period):
                if len(data) < period:
                    return data[-1] if data else 0
                multiplier = 2 / (period + 1)
                ema_val = sum(data[:period]) / period
                for price in data[period:]:
                    ema_val = (price - ema_val) * multiplier + ema_val
                return ema_val

            ema12 = ema(closes, 12)
            ema26 = ema(closes, 26)
            macd_line = ema12 - ema26

            # Simple signal line (9-period EMA of MACD)
            # Simplified here: good if MACD > 0 and rising
            prev_ema12 = ema(closes[:-1], 12) if len(closes) > 1 else ema12
            prev_ema26 = ema(closes[:-1], 26) if len(closes) > 1 else ema26
            prev_macd = prev_ema12 - prev_ema26

            macd_momentum = macd_line - prev_macd  # histogram direction

            # MACD score
            macd_score = 50  # default
            if macd_line > 0 and macd_momentum > 0:
                macd_score = 90  # golden cross + rising
            elif macd_line < 0 and macd_momentum > 0:
                macd_score = 70  # still negative but turning up
            elif macd_line > 0 and macd_momentum < 0:
                macd_score = 40  # positive but declining
            elif macd_line < 0 and macd_momentum < 0:
                macd_score = 10  # dead cross + falling

            # ========== 3. Ichimoku (simplified calculation) ==========
            # Tenkan: 9-period (high+low)/2
            # Kijun: 26-period (high+low)/2
            if len(highs) >= 26 and len(lows) >= 26:
                tenkan = (max(highs[-9:]) + min(lows[-9:])) / 2  # tenkan
                kijun = (max(highs[-26:]) + min(lows[-26:])) / 2  # kijun

                # Cloud (senkou span A/B) - simplified
                senkou_a = (tenkan + kijun) / 2
                senkou_b = (max(highs[-52:]) + min(lows[-52:])) / 2 if len(highs) >= 52 else kijun
                cloud_top = max(senkou_a, senkou_b)
                cloud_bottom = min(senkou_a, senkou_b)

                # Ichimoku score
                ichimoku_score = 50
                if current_price > cloud_top:
                    ichimoku_score = 90  # above the cloud
                    if current_price > tenkan > kijun:
                        ichimoku_score = 100  # perfect bullish alignment
                elif current_price > cloud_bottom:
                    ichimoku_score = 60  # inside the cloud
                else:
                    ichimoku_score = 20  # below the cloud
                    if tenkan < kijun:
                        ichimoku_score = 10  # bearish alignment
            else:
                ichimoku_score = 50

            # ========== 4. Volume score ==========
            avg_vol = sum(volumes[:-1]) / (len(volumes) - 1) if len(volumes) > 1 else 1
            current_vol = volumes[-1] if volumes else 0
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1

            if vol_ratio >= 3.0:
                volume_score = 100  # volume explosion
            elif vol_ratio >= 2.0:
                volume_score = 80
            elif vol_ratio >= 1.5:
                volume_score = 60
            elif vol_ratio >= 1.0:
                volume_score = 40
            else:
                volume_score = 20  # declining volume

            # ========== Aggregate score calculation ==========
            # Volume(20%) + RSI(25%) + Ichimoku(25%) + MACD(30%)
            total_score = (
                volume_score * 0.20 +
                rsi_score * 0.25 +
                ichimoku_score * 0.25 +
                macd_score * 0.30
            )

            if total_score < min_score:
                continue

            ticker = tickers.get(market, {})
            currency = market.replace(Q.config.market_prefix, "") if market.startswith(Q.config.market_prefix) else market
            change_rate = float(ticker.get("signed_change_rate") or 0) * 100
            vol_24h = float(ticker.get("acc_trade_price_24h") or 0)

            # Signal determination
            # NOTE: signal values are kept in Korean — the dashboard JS compares them
            # with strict equality (r.signal === "강력 매수" / "매수") in dashboard.js.
            if total_score >= 80:
                signal = "강력 매수"
                signal_emoji = "🟢"
            elif total_score >= 65:
                signal = "매수"
                signal_emoji = "🔵"
            elif total_score >= 50:
                signal = "중립"
                signal_emoji = "⚪"
            elif total_score >= 35:
                signal = "매도"
                signal_emoji = "🟡"
            else:
                signal = "강력 매도"
                signal_emoji = "🔴"

            rankings.append({
                "rank": 0,
                "market": market,
                "currency": currency,
                "current_price": current_price,
                "change_rate": round(change_rate, 2),
                "total_score": round(total_score, 1),
                "signal": signal,
                "signal_emoji": signal_emoji,
                "details": {
                    "volume_score": round(volume_score, 1),
                    "rsi_score": round(rsi_score, 1),
                    "ichimoku_score": round(ichimoku_score, 1),
                    "macd_score": round(macd_score, 1),
                    "rsi": round(rsi, 1),
                    "vol_ratio": round(vol_ratio, 2),
                },
                "volume_24h_usdt": vol_24h,
            })

            time_module.sleep(0.02)  # Rate limit

        except (ConnectionError, OSError):
            logger.warning("[Tech score] %s connection failed, skipping", market)
            continue
        except Exception as exc:
            logger.warning("[RANKING_API] signal determination except-> continue: %s", exc, exc_info=True)
            continue

    # Sort by highest score
    rankings.sort(key=lambda x: x["total_score"], reverse=True)

    # Assign ranks
    for i, r in enumerate(rankings):
        r["rank"] = i + 1

    result = {
        "ok": True,
        "min_score": min_score,
        "min_price": min_price,
        "max_spread_bps": max_spread_bps,
        "total_qualified": len(rankings),
        "top_n": top_n,
        "rankings": rankings[:top_n],
        "all_rankings": rankings[:30],
        "filtered_out": filtered_out,
        "summary": {
            "best_pick": rankings[0] if rankings else None,
            "avg_score": round(sum(r["total_score"] for r in rankings) / len(rankings), 1) if rankings else 0,
        }
    }
    _set_cached(cache_key, result)
    return result


# ============================================================
# TOP 5 Rankings Unified API
# ============================================================

@router.get(
    "/market/rankings",
    summary="Get unified TOP 5 rankings from all market analysis APIs",
    responses={
        200: {"description": "Unified rankings from rebound, RSI, tech-score, and upside APIs"},
    },
)
def market_rankings_unified(
    request: Request,
    top_n: int = Query(5, ge=1, le=10, description="Number of top coins per category"),
    min_volume_usdt: float = Query(1_000_000, ge=0, description="Minimum 24h volume in USDT"),
    min_price: float = Query(100, ge=0, description="Minimum price in USDT"),
    max_spread_bps: float = Query(50, ge=0, description="Maximum bid-ask spread in basis points"),
):
    """
    Unified TOP 5 rankings API.

    Fetches the results of 4 analysis APIs at once:
    - rebound: TOP 5 sharp-drop rebound opportunities
    - rsi_oversold: TOP 5 RSI oversold
    - tech_score: TOP 5 aggregate technical score
    - upside: TOP 5 market-wide upside potential

    Cache TTL: 30s
    """
    import time as time_module

    # Cache check
    cache_key = _build_cache_key(
        "market/rankings",
        top_n=top_n, min_volume_usdt=min_volume_usdt,
        min_price=min_price, max_spread_bps=max_spread_bps
    )
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    # 1. Fetch all exchange market tickers (once)
    try:
        # First get all markets
        markets_resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        all_markets = [Q.normalize(str(m.get("symbol") or "")) for m in parse_bybit_list(markets_resp.json()) if isinstance(m, dict) and str(m.get("symbol") or "")]
        if not all_markets:
            return {"ok": True, "message": "No markets", "rankings": {}}

        # Then get tickers for all markets
        resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        _raw_tickers = parse_bybit_list(resp.json())
        _market_set = set(m.upper() for m in all_markets[:100]) if all_markets else set()
        tickers = [normalize_bybit_ticker(t) for t in _raw_tickers if isinstance(t, dict) and Q.normalize(str(t.get("symbol") or "")).upper() in _market_set]
    except Exception as e:
        logger.warning("strategy_ranking_router.market_rankings_unified L1467: %s", e)
        return {"ok": False, "error": f"Failed to fetch tickers: {e}"}

    if not tickers:
        return {"ok": True, "message": "No markets", "rankings": {}}

    # 2. Common filtering
    tickers = {}
    for t in tickers:
        market = t.get("market", "")
        vol_24h = float(t.get("acc_trade_price_24h") or 0)
        last_price = float(t.get("trade_price") or 0)

        if vol_24h < min_volume_usdt:
            continue
        if min_price > 0 and last_price < min_price:
            continue
        # Note: Ticker doesn't include bid/ask prices, skip spread filter

        tickers[market] = t

    if not tickers:
        return {"ok": True, "message": "No markets meet filter criteria", "rankings": {}}

    system = request.app.state.system
    markets_list = list(tickers.keys())[:80]  # up to 80

    # 3. Fetch candle data (60 x 15min candles)
    candle_data = {}
    for market in markets_list:
        try:
            candle_resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": "15", "limit": 60}, timeout=3.0)
            _raw_candles = parse_bybit_list(candle_resp.json())
            candles = [{"trade_price": float(k[4]), "high_price": float(k[2]), "low_price": float(k[3]), "opening_price": float(k[1]), "candle_acc_trade_volume": float(k[5]), "timestamp": int(k[0])} for k in _raw_candles if isinstance(k, (list, tuple)) and len(k) >= 6]
            if candles and len(candles) >= 26:
                # Reverse for chronological order
                candle_data[market] = list(reversed(candles))
            time_module.sleep(0.02)
        except Exception as exc:
            logger.warning("[RANKING_API] Reverse for chronological order: %s", exc, exc_info=True)

    # 4. Run each analysis
    rebound_list = []
    rsi_list = []
    tech_list = []
    upside_list = []

    def calc_ema(data, period):
        if len(data) < period:
            return data[-1] if data else 0
        multiplier = 2 / (period + 1)
        ema_val = sum(data[:period]) / period
        for price in data[period:]:
            ema_val = (price - ema_val) * multiplier + ema_val
        return ema_val

    for market, ticker in tickers.items():
        currency = market.replace(Q.config.market_prefix, "") if market.startswith(Q.config.market_prefix) else market
        current_price = float(ticker.get("trade_price") or 0)
        if current_price <= 0:
            continue

        vol_24h = float(ticker.get("acc_trade_price_24h") or 0)
        high = float(ticker.get("high_price") or current_price)
        low = float(ticker.get("low_price") or current_price)
        change_pct = float(ticker.get("signed_change_rate") or 0) * 100

        # Price position
        price_position = (current_price - low) / (high - low) if high > low else 0.5
        daily_vol_pct = (high - low) / low if low > 0 else 0.05

        # Fetch AI context
        ai_score = 0.5
        trend = 0.0
        rsi = 50.0
        bb_position = price_position

        ctx = system.coordinator.contexts.get(market)
        if ctx:
            brain = getattr(ctx, "current_ai", {}).get("brain", {})
            ai_score = float(brain.get("ai_prediction", 0.5))
            trend = float(brain.get("trend", 0.0))
            rsi = float(brain.get("rsi", 50.0))
            bb_upper = float(brain.get("bb_upper", high))
            bb_lower = float(brain.get("bb_lower", low))
            if bb_upper > bb_lower:
                bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)

        # Candle-based RSI/MACD calculation
        candles = candle_data.get(market)
        rsi_calc = rsi
        macd_score = 50
        ichimoku_score = 50
        volume_score = 40

        if candles:
            closes = [float(c.get("trade_price") or 0) for c in candles]
            highs_c = [float(c.get("high_price") or 0) for c in candles]
            lows_c = [float(c.get("low_price") or 0) for c in candles]
            volumes = [float(c.get("candle_acc_trade_volume") or 0) for c in candles]

            # RSI calculation
            gains, losses = [], []
            for i in range(1, len(closes)):
                diff = closes[i] - closes[i-1]
                gains.append(max(0, diff))
                losses.append(max(0, -diff))

            if len(gains) >= 14:
                avg_gain = sum(gains[-14:]) / 14
                avg_loss = sum(losses[-14:]) / 14
                if avg_loss > 0:
                    rs = avg_gain / avg_loss
                    rsi_calc = 100 - (100 / (1 + rs))
                else:
                    rsi_calc = 100.0

            # MACD
            if len(closes) >= 26:
                ema12 = calc_ema(closes, 12)
                ema26 = calc_ema(closes, 26)
                macd_line = ema12 - ema26
                prev_ema12 = calc_ema(closes[:-1], 12)
                prev_ema26 = calc_ema(closes[:-1], 26)
                prev_macd = prev_ema12 - prev_ema26
                macd_momentum = macd_line - prev_macd

                if macd_line > 0 and macd_momentum > 0:
                    macd_score = 90
                elif macd_line < 0 and macd_momentum > 0:
                    macd_score = 70
                elif macd_line > 0 and macd_momentum < 0:
                    macd_score = 40
                else:
                    macd_score = 10

            # Ichimoku
            if len(highs_c) >= 26:
                tenkan = (max(highs_c[-9:]) + min(lows_c[-9:])) / 2
                kijun = (max(highs_c[-26:]) + min(lows_c[-26:])) / 2
                senkou_a = (tenkan + kijun) / 2
                senkou_b = (max(highs_c) + min(lows_c)) / 2
                cloud_top = max(senkou_a, senkou_b)
                cloud_bottom = min(senkou_a, senkou_b)

                if current_price > cloud_top:
                    ichimoku_score = 90
                elif current_price > cloud_bottom:
                    ichimoku_score = 60
                else:
                    ichimoku_score = 20

            # Volume score
            if len(volumes) > 1:
                avg_vol = sum(volumes[:-1]) / (len(volumes) - 1)
                vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1
                if vol_ratio >= 3.0:
                    volume_score = 100
                elif vol_ratio >= 2.0:
                    volume_score = 80
                elif vol_ratio >= 1.5:
                    volume_score = 60
                else:
                    volume_score = 40

        # RSI score calculation
        if rsi_calc < 20:
            rsi_score = 100
        elif rsi_calc < 30:
            rsi_score = 80
        elif rsi_calc < 40:
            rsi_score = 60
        elif rsi_calc < 50:
            rsi_score = 40
        else:
            rsi_score = 20

        # ===== (A) Rebound Score =====
        if change_pct <= -3:  # dropping coins only
            rebound_score = 0.0
            if change_pct <= -15:
                rebound_score += 20
            elif change_pct <= -10:
                rebound_score += 25
            elif change_pct <= -5:
                rebound_score += 20
            else:
                rebound_score += 15

            if price_position < 0.2:
                rebound_score += 10
            elif price_position <= 0.5:
                rebound_score += 25
            else:
                rebound_score += 5

            if rsi_calc < 25:
                rebound_score += 20
            elif rsi_calc < 30:
                rebound_score += 15
            elif rsi_calc < 40:
                rebound_score += 10

            if bb_position < 0.15:
                rebound_score += 15
            elif bb_position < 0.3:
                rebound_score += 10

            if vol_24h >= 500_000_000:  # 500M USDT
                rebound_score += 15
            elif vol_24h >= 100_000_000:  # 100M USDT
                rebound_score += 10

            rebound_score = max(0, min(100, rebound_score))
            rebound_list.append({
                "market": market,
                "price": current_price,
                "change_pct": round(change_pct, 2),
                "score": round(rebound_score, 1),
                "rsi": round(rsi_calc, 1),
            })

        # ===== (B) RSI Oversold =====
        if rsi_calc <= 40:
            rsi_status = "Deeply oversold" if rsi_calc < 20 else ("Oversold" if rsi_calc < 30 else "Low")
            rsi_list.append({
                "market": market,
                "price": current_price,
                "change_pct": round(change_pct, 2),
                "rsi": round(rsi_calc, 1),
                "rsi_status": rsi_status,
            })

        # ===== (C) Tech Score =====
        tech_total = (
            volume_score * 0.20 +
            rsi_score * 0.25 +
            ichimoku_score * 0.25 +
            macd_score * 0.30
        )
        if tech_total >= 50:
            # NOTE: signal values kept in Korean — dashboard JS compares them with
            # strict equality (r.signal === "강력 매수" / "매수") in dashboard.js.
            signal = "강력 매수" if tech_total >= 80 else ("매수" if tech_total >= 65 else "중립")
            tech_list.append({
                "market": market,
                "price": current_price,
                "change_pct": round(change_pct, 2),
                "score": round(tech_total, 1),
                "signal": signal,
            })

        # ===== (D) Upside Score =====
        upside_score = 0.0
        ai_factor = (ai_score - 0.5) * 2.0
        upside_score += max(0, ai_factor * 25)
        trend_factor = min(1.0, max(-1.0, trend * 10))
        upside_score += max(0, trend_factor * 20)

        if rsi_calc < 30:
            upside_score += 20
        elif rsi_calc < 40:
            upside_score += 12
        elif rsi_calc < 50:
            upside_score += 5

        bb_factor = 1.0 - bb_position
        upside_score += bb_factor * 15

        if vol_24h >= 500_000_000:  # 500M USDT
            upside_score += 10
        elif vol_24h >= 100_000_000:  # 100M USDT
            upside_score += 7

        if 0.03 <= daily_vol_pct <= 0.10:
            upside_score += 10

        upside_score = max(0, min(100, upside_score))
        upside_list.append({
            "market": market,
            "price": current_price,
            "change_pct": round(change_pct, 2),
            "score": round(upside_score, 1),
            "ai_score": round(ai_score, 3),
        })

    # 5. Sort each list and extract TOP N
    rebound_list.sort(key=lambda x: x["score"], reverse=True)
    rsi_list.sort(key=lambda x: x["rsi"])  # lowest RSI first
    tech_list.sort(key=lambda x: x["score"], reverse=True)
    upside_list.sort(key=lambda x: x["score"], reverse=True)

    result = {
        "ok": True,
        "timestamp": int(time_module.time()),
        "rankings": {
            "rebound": {
                "title": "Sharp-drop rebound opportunities",
                "recommended_strategy": "GAZUA",
                "items": rebound_list[:top_n],
            },
            "rsi_oversold": {
                "title": "RSI oversold",
                "recommended_strategy": "LIGHTNING",
                "items": rsi_list[:top_n],
            },
            "tech_score": {
                "title": "Aggregate technical score",
                "recommended_strategy": "LADDER",
                "items": tech_list[:top_n],
            },
            "upside": {
                "title": "All markets",
                "recommended_strategy": "AUTOLOOP",
                "items": upside_list[:top_n],
            },
        },
    }
    _set_cached(cache_key, result)
    return result


# ============================================================
# Manual Parameter Calculator
# ------------------------------------------------------------
# Purpose:
# - Compute per-strategy recommended parameters for manually entered coins
#   that are not in the recommendation lists
# - Return optimal values for each strategy (LADDER/LIGHTNING/GAZUA)
# ============================================================
@router.get(
    "/calc_params",
    summary="Manual coin parameter calculation",
    description="Analyzes the AI score/indicators of the given market and returns per-strategy recommended parameters",
)
def calc_params(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    strategy: str = Query(..., description="Strategy (LADDER, LIGHTNING, GAZUA)"),
    budget: float = Query(100, description="Budget (USDT)"),
):
    """Manual coin parameter calculation."""
    import time as time_module

    market = market.strip().upper()
    strategy = strategy.strip().upper()

    # Normalize market format: BTC, BTC/USDT, etc. -> BTCUSDT
    market = market.replace("_", "").replace("/", "-")
    prefix = Q.config.market_prefix
    quote = Q.config.symbol  # USDT
    if market.startswith(quote):
        if not market.startswith(prefix):
            market = Q.market(market[len(quote):])
    elif not market.startswith(prefix):
        market = Q.market(market)

    if not market or market == prefix:
        return {"ok": False, "error": "market required (e.g., BTC, BTCUSDT)"}
    if strategy not in ("LADDER", "LIGHTNING", "GAZUA"):
        return {"ok": False, "error": "strategy must be LADDER, LIGHTNING, or GAZUA"}

    try:
        # Bybit V5 ticker
        resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5)
        if resp.status_code != 200:
            return {"ok": False, "error": f"Failed to fetch ticker for {market}"}
        _raw_tickers = parse_bybit_list(resp.json())
        ticker_data = None
        for _t in _raw_tickers:
            if isinstance(_t, dict):
                _tc = normalize_bybit_ticker(_t)
                if _tc.get("market", "").upper() == market.upper():
                    ticker_data = _tc
                    break
        if not ticker_data:
            return {"ok": False, "error": f"No ticker data for {market}"}
        price = float(ticker_data.get("trade_price", 0))
        change_rate = float(ticker_data.get("signed_change_rate", 0)) * 100
        volume_24h = float(ticker_data.get("acc_trade_price_24h", 0))
    except Exception as e:
        logger.warning("strategy_ranking_router.calc_params L1844: %s", e)
        return {"ok": False, "error": f"Ticker fetch error: {e}"}

    if price <= 0:
        return {"ok": False, "error": f"Invalid price for {market}"}

    try:
        # Bybit V5 kline (1h candles)
        candle_resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": "60", "limit": 48}, timeout=5)
        _raw_c = parse_bybit_list(candle_resp.json()) if candle_resp.status_code == 200 else []
        candles = [{"trade_price": float(k[4]), "high_price": float(k[2]), "low_price": float(k[3]), "opening_price": float(k[1]), "candle_acc_trade_volume": float(k[5]), "timestamp": int(k[0])} for k in _raw_c if isinstance(k, (list, tuple)) and len(k) >= 6]
    except Exception:
        logger.error("strategy_ranking_router.calc_params L1855 except", exc_info=True)
        candles = []

    volatility = 0.0
    momentum = 0.0
    trend = 0.0
    rsi = 50.0

    if candles and len(candles) >= 14:
        # Candle format (reversed for chronological order)
        closes = [float(c.get("trade_price") or 0) for c in reversed(candles)]
        if len(closes) >= 2:
            volatility = indicators.volatility(closes, 14) or 0.0
            momentum = indicators.momentum(closes, 10) or 0.0
            trend = indicators.trend(closes, 20) or 0.0
            rsi = indicators.rsi(closes, 14) or 50.0

    ai_score = 0.5
    try:
        system = getattr(request.app.state, "system", None)
        if system and hasattr(system, "ai_trainer") and system.ai_trainer:
            score = system.ai_trainer.predict_score(market)
            if score is not None:
                ai_score = float(score)
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[RANKING_API] AI score prediction failed: %s", exc, exc_info=True)

    params = {}

    if strategy == "LADDER":
        use_atr = volatility > 3.0
        step_pct = round(max(1.0, min(3.0, volatility * 0.5)), 1)
        tp_pct = round(max(2.0, min(5.0, volatility * 0.8)), 1)
        steps = max(5, min(15, int(budget / 20)))
        params = {
            "step_pct": step_pct,
            "use_atr": use_atr,
            "atr_mult": 1.5 if use_atr else 1.0,
            "tp_pct": tp_pct,
            "steps": steps,
            "martingale": 1.05,
        }
    elif strategy == "LIGHTNING":
        tp_pct = round(max(3.0, min(8.0, abs(momentum) * 1.5 + 3.0)), 1)
        sl_pct = round(-abs(tp_pct * 0.6), 1)
        params = {
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "manual_exit": False,
        }
    elif strategy == "GAZUA":
        tp_pct = round(max(5.0, min(15.0, abs(trend) * 10 + 5.0)), 1)
        sl_pct = round(-abs(tp_pct * 0.5), 1)
        params = {
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "manual_exit": True,
            "buy_now": False,
        }

    return {
        "ok": True,
        "market": market,
        "strategy": strategy,
        "budget": budget,
        "price": price,
        "change_rate": round(change_rate, 2),
        "ai_score": round(ai_score, 3),
        "volatility": round(volatility, 2),
        "momentum": round(momentum, 2),
        "trend": round(trend, 2),
        "rsi": round(rsi, 1),
        "recommended_params": params,
        "timestamp": int(time_module.time()),
    }


# ============================================================
# Daily PnL API (strategy prefix compatibility)
# ============================================================

@router.get(
    "/daily-pnl",
    summary="Get daily PnL summary",
    responses={
        200: {"description": "Daily PnL summary for the last N days"},
    },
)
def get_daily_pnl(
    request: Request,
    days: int = Query(7, ge=1, le=30, description="Number of days"),
):
    """
    Get daily PnL summary for the last N days.
    """
    from pathlib import Path
    import json
    from datetime import datetime, timedelta

    pnl_dir = Path("runtime/daily_pnl")
    result_days = []

    try:
        if pnl_dir.exists():
            today = datetime.now()
            for i in range(days):
                date = today - timedelta(days=i)
                date_str = date.strftime("%Y-%m-%d")
                file_path = pnl_dir / f"{date_str}.json"

                if file_path.exists():
                    try:
                        data = json.loads(file_path.read_text(encoding="utf-8"))
                        result_days.append({
                            "date": date_str,
                            "realized_pnl": data.get("realized_pnl", 0),
                            "unrealized_pnl": data.get("unrealized_pnl", 0),
                            "total_pnl": data.get("total_pnl", 0),
                            "trade_count": data.get("trade_count", 0),
                            "win_count": data.get("win_count", 0),
                            "loss_count": data.get("loss_count", 0),
                        })
                    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[RANKING_API] strategy_ranking_router.get_daily_pnl fallback: %s", exc, exc_info=True)
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
        logger.warning("[RANKING_API] strategy_ranking_router.get_daily_pnl fallback: %s", exc, exc_info=True)

    return {
        "ok": True,
        "days": result_days,
        "count": len(result_days),
    }



# (CONTRARIAN Scanner/Setup/Stop → strategy_contrarian_router.py)

# (SNIPER STRATEGY ENDPOINTS → strategy_sniper_router.py)


# ============================================================
# Multi-Timeframe AI Analysis
# ============================================================

@router.get(
    "/multi-timeframe/{market}",
    summary="Multi-timeframe AI analysis",
    description="Computes AI scores across 5min, 15min, 1h timeframes and selects the best timeframe.",
)
def get_multi_timeframe_analysis(
    market: str,
    force_refresh: bool = Query(False, description="Ignore cache and recompute"),
) -> Dict[str, Any]:
    """Fetch multi-timeframe AI analysis results."""
    try:
        from app.core.multi_timeframe_ai import analyze_multi_timeframe

        result = analyze_multi_timeframe(market, force_refresh=force_refresh)

        if not result:
            return {
                "ok": False,
                "error": "Analysis failed (insufficient data)",
                "market": market.upper(),
            }

        return {
            "ok": True,
            "market": result.market,
            "best": result.best_timeframe.to_dict(),
            "all_timeframes": [tf.to_dict() for tf in result.all_timeframes],
            "selection_reason": result.selection_reason,
            "computed_at": result.computed_at,
        }
    except (AttributeError, TypeError, ValueError) as e:
        logger.exception(f"multi-timeframe analysis error: {e}")
        return {
            "ok": False,
            "error": str(e),
            "market": market.upper(),
        }


@router.get(
    "/multi-timeframe/batch",
    summary="Multi-timeframe AI batch analysis",
    description="Runs multi-timeframe AI analysis for multiple markets.",
)
def get_multi_timeframe_batch(
    markets: str = Query(..., description="Comma-separated market list (e.g., BTCUSDT,ETHUSDT)"),
    force_refresh: bool = Query(False, description="Ignore cache"),
) -> Dict[str, Any]:
    """Multi-market timeframe analysis."""
    try:
        from app.core.multi_timeframe_ai import analyze_multi_timeframe

        market_list = [m.strip().upper() for m in markets.split(",") if m.strip()]

        if not market_list:
            return {"ok": False, "error": "Market list is empty"}

        if len(market_list) > 20:
            return {"ok": False, "error": "Up to 20 markets are supported"}

        results = {}
        for m in market_list:
            result = analyze_multi_timeframe(m, force_refresh=force_refresh)
            if result:
                results[m] = {
                    "best_tf": result.best_timeframe.label,
                    "ai_score": result.best_timeframe.ai_score,
                    "rsi": result.best_timeframe.rsi,
                    "signal": result.best_timeframe.signal,
                    "confidence": result.best_timeframe.confidence,
                    "reason": result.selection_reason,
                }
            else:
                results[m] = {"error": "Analysis failed"}

        return {
            "ok": True,
            "count": len(results),
            "results": results,
        }
    except (AttributeError, TypeError, ValueError) as e:
        logger.exception(f"multi-timeframe batch error: {e}")
        return {"ok": False, "error": str(e)}


# ============================================================
# Surge Scanner - real-time surge/contrarian coin scanner
# [2026-01-31] Contrarian + AI analysis integration
# ============================================================

def _get_market_benchmark(tickers: list, timeframe: str = "24h") -> dict:
    """Compute market benchmark (BTC + market average).

    Returns:
        {
            "btc_change": BTC change rate (%),
            "market_avg": market average change rate (%),
            "market_median": market median change rate (%),
            "rising_count": number of rising coins,
            "falling_count": number of falling coins,
        }
    """
    btc_change = 0.0
    changes = []

    for t in tickers:
        market = t.get("market", "")
        change = float(t.get("signed_change_rate") or 0) * 100

        if market == f"{Q.config.market_prefix}BTC":
            btc_change = change

        changes.append(change)

    if not changes:
        return {"btc_change": 0, "market_avg": 0, "market_median": 0, "rising_count": 0, "falling_count": 0}

    changes.sort()
    median_idx = len(changes) // 2

    return {
        "btc_change": round(btc_change, 2),
        "market_avg": round(sum(changes) / len(changes), 2),
        "market_median": round(changes[median_idx], 2),
        "rising_count": sum(1 for c in changes if c > 0),
        "falling_count": sum(1 for c in changes if c < 0),
    }


@router.get(
    "/market/surge",
    summary="Real-time surge/contrarian coin scanner",
    responses={
        200: {"description": "List of surging and contrarian coins"},
    },
)
def market_surge_scanner(
    request: Request,
    top_n: int = Query(10, ge=1, le=50, description="Top N"),
    min_surge_pct: float = Query(3.0, ge=0.5, description="Minimum surge rate (%)"),
    min_volume_usdt: float = Query(500_000, ge=0, description="Minimum 24h volume (USDT)"),
    timeframe: str = Query("1h", description="Timeframe: 5m, 15m, 1h, 4h, 24h"),
    exclude_active: bool = Query(False, description="Exclude already active markets"),
    mode: str = Query("both", description="Mode: absolute (absolute surge), relative (contrarian), both"),
):
    """
    Scan all markets for surging/contrarian coins.

    **Mode description:**
    - `absolute`: by absolute surge rate (simply >= +5%)
    - `relative`: by contrarian strength (relative strength vs market)
      - e.g.: market avg -3%, coin +5% -> contrarian strength = +8%
    - `both`: computes both into an aggregate score (★ default)

    **Timeframe:**
    - 5m: 5 minutes (ultra-scalp) | 15m: 15 minutes (scalping)
    - 1h: 1 hour (intraday) ★ | 4h: 4 hours (swing)
    - 24h: 24 hours (daily)

    **SNIPER target:**
    - coins with high contrarian strength (rising alone while market falls)
    - AI score + volume spike + RSI headroom
    """
    import time

    # Cache check (5s TTL)
    cache_key = _build_cache_key(
        "market/surge",
        top_n=top_n, min_surge_pct=min_surge_pct, min_volume_usdt=min_volume_usdt,
        timeframe=timeframe, exclude_active=exclude_active, mode=mode
    )
    cached = _get_cached(cache_key, ttl=5.0)
    if cached is not None:
        return cached

    # Timeframe settings
    tf_minutes = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "24h": 1440}.get(timeframe, 60)
    tf_candles = {"5m": 3, "15m": 3, "1h": 2, "4h": 2, "24h": 1}.get(timeframe, 2)

    # 1. Fetch all market tickers
    try:
        markets_resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        all_markets = [Q.normalize(str(m.get("symbol") or "")) for m in parse_bybit_list(markets_resp.json()) if isinstance(m, dict) and str(m.get("symbol") or "")]

        if not all_markets:
            return {"ok": True, "message": "No markets", "surging": []}
        resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        _raw_tickers = parse_bybit_list(resp.json())
        _market_set = set(m.upper() for m in all_markets[:100]) if all_markets else set()
        tickers = [normalize_bybit_ticker(t) for t in _raw_tickers if isinstance(t, dict) and Q.normalize(str(t.get("symbol") or "")).upper() in _market_set]
    except Exception as e:
        logger.warning("strategy_ranking_router.market_surge_scanner L2189: %s", e)
        return {"ok": False, "error": f"Ticker fetch failed: {e}"}

    if not tickers:
        return {"ok": True, "message": "No tickers", "surging": []}

    # 2. Compute market benchmark (for contrarian analysis)
    benchmark = _get_market_benchmark(tickers, timeframe)
    btc_change = benchmark["btc_change"]
    market_avg = benchmark["market_avg"]

    # System reference
    system = request.app.state.system
    active_markets = set()
    if exclude_active and system:
        try:
            for m in system.oma_registry.get_all_markets():
                state = system.oma_registry.get_state(m)
                if state in (MarketState.ACTIVE, MarketState.RECOVERY):
                    active_markets.add(m)
        except (AttributeError, TypeError) as exc:
            logger.warning("[RANKING_API] system reference: %s", exc, exc_info=True)

    # 3. Compute per-timeframe BTC change rate from BTC candles
    btc_tf_change = btc_change  # default is 24h
    if timeframe != "24h":
        try:
            btc_market = Q.market("BTC")
            btc_resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": btc_market, "interval": str(tf_minutes), "limit": tf_candles}, timeout=3.0)
            _raw_btc = parse_bybit_list(btc_resp.json())
            btc_candles = [{"trade_price": float(k[4]), "opening_price": float(k[1]), "timestamp": int(k[0])} for k in _raw_btc if isinstance(k, (list, tuple)) and len(k) >= 5]
            if btc_candles and len(btc_candles) >= 2:
                btc_curr = float(btc_candles[0].get("trade_price") or 0)
                btc_past = float(btc_candles[-1].get("opening_price") or 0)
                if btc_past > 0:
                    btc_tf_change = ((btc_curr - btc_past) / btc_past) * 100
        except Exception as exc:
            logger.warning("[RANKING_API] 3. compute per-timeframe BTC change from BTC candles: %s", exc, exc_info=True)

    # 4. Volume filtering + candidate selection
    candidates = []

    for t in tickers:
        market = t.get("market", "")

        if market in active_markets:
            continue
        if market == f"{Q.config.market_prefix}BTC":  # BTC is the benchmark, so exclude
            continue

        vol_24h = float(t.get("acc_trade_price_24h") or 0)
        if vol_24h < min_volume_usdt:
            continue

        last_price = float(t.get("trade_price") or 0)
        if last_price <= 0:
            continue

        change_24h = float(t.get("signed_change_rate") or 0) * 100

        candidates.append({
            "market": market,
            "ticker": t,
            "change_24h": change_24h,
        })

    # 5. Compute per-timeframe change rate + contrarian strength
    surging = []
    all_tf_changes = []  # for computing market average

    if timeframe != "24h" and candidates:
        for batch_start in range(0, min(len(candidates), 100), 10):
            batch = candidates[batch_start:batch_start + 10]

            for c in batch:
                market = c["market"]
                ticker = c["ticker"]

                try:
                    candle_resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": str(tf_minutes), "limit": tf_candles}, timeout=3.0)
                    _raw_c = parse_bybit_list(candle_resp.json())
                    candles = [{"trade_price": float(k[4]), "opening_price": float(k[1])} for k in _raw_c if isinstance(k, (list, tuple)) and len(k) >= 5]

                    if candles and len(candles) >= 2:
                        current_price = float(candles[0].get("trade_price") or 0)
                        past_price = float(candles[-1].get("opening_price") or 0)

                        if past_price > 0:
                            surge_pct = ((current_price - past_price) / past_price) * 100
                            all_tf_changes.append(surge_pct)

                            # contrarian strength = coin change rate - BTC change rate
                            relative_strength = surge_pct - btc_tf_change

                            # Filtering: depends on mode
                            passes_filter = False
                            if mode == "absolute" and surge_pct >= min_surge_pct:
                                passes_filter = True
                            elif mode == "relative" and relative_strength >= min_surge_pct:
                                passes_filter = True
                            elif mode == "both" and (surge_pct >= min_surge_pct or relative_strength >= min_surge_pct):
                                passes_filter = True

                            if passes_filter:
                                current_vol = float(candles[0].get("candle_acc_trade_volume") or 0)
                                past_vol = float(candles[-1].get("candle_acc_trade_volume") or 0)
                                vol_surge = (current_vol / past_vol) if past_vol > 0 else 1.0

                                surging.append({
                                    "market": market,
                                    "price": current_price,
                                    "surge_pct": round(surge_pct, 2),
                                    "relative_strength": round(relative_strength, 2),
                                    "vs_btc": round(relative_strength, 2),
                                    "change_24h": round(c["change_24h"], 2),
                                    "volume_24h": float(ticker.get("acc_trade_price_24h") or 0),
                                    "vol_surge_ratio": round(vol_surge, 2),
                                    "high_price": float(ticker.get("high_price") or 0),
                                    "low_price": float(ticker.get("low_price") or 0),
                                })

                    time.sleep(0.02)
                except Exception as exc:
                    logger.warning("[RANKING_API] filtering by mode: %s", exc, exc_info=True)
    else:
        # 24h timeframe
        for c in candidates:
            ticker = c["ticker"]
            change = c["change_24h"]
            relative_strength = change - btc_change

            passes_filter = False
            if mode == "absolute" and change >= min_surge_pct:
                passes_filter = True
            elif mode == "relative" and relative_strength >= min_surge_pct:
                passes_filter = True
            elif mode == "both" and (change >= min_surge_pct or relative_strength >= min_surge_pct):
                passes_filter = True

            if passes_filter:
                surging.append({
                    "market": c["market"],
                    "price": float(ticker.get("trade_price") or 0),
                    "surge_pct": round(change, 2),
                    "relative_strength": round(relative_strength, 2),
                    "vs_btc": round(relative_strength, 2),
                    "change_24h": round(change, 2),
                    "volume_24h": float(ticker.get("acc_trade_price_24h") or 0),
                    "vol_surge_ratio": 1.0,
                    "high_price": float(ticker.get("high_price") or 0),
                    "low_price": float(ticker.get("low_price") or 0),
                })

    # 6. Compute timeframe market average
    market_tf_avg = sum(all_tf_changes) / len(all_tf_changes) if all_tf_changes else market_avg

    # 7. AI/RSI analysis + surge score calculation
    for item in surging:
        market = item["market"]
        ctx = system.coordinator.contexts.get(market) if system else None

        item["rsi"] = 50.0
        item["ai_score"] = 0.5
        item["trend"] = 0.0
        item["momentum"] = 0.0

        if ctx:
            brain = getattr(ctx, "current_ai", {}).get("brain", {})
            item["rsi"] = float(brain.get("rsi", 50.0))
            item["ai_score"] = float(brain.get("ai_score", 0.5))
            item["trend"] = float(brain.get("trend", 0.0))
            item["momentum"] = float(brain.get("momentum", 0.0))

        # Aggregate score calculation (AI-based)
        surge = item["surge_pct"]
        rel_str = item["relative_strength"]
        rsi = item["rsi"]
        ai = item["ai_score"]
        vol_surge = item.get("vol_surge_ratio", 1.0)

        # score = contrarian strength(40%) + absolute surge(20%) + AI(25%) + volume spike(15%)
        # RSI overbought penalty
        rsi_penalty = max(0, (rsi - 70) * 0.1) if rsi > 70 else 0

        score = (
            rel_str * 0.4 +           # contrarian strength (key)
            surge * 0.2 +              # absolute surge rate
            (ai - 0.5) * 50 * 0.25 +   # AI score (0.5 baseline)
            min(vol_surge, 3.0) * 0.15 * 10 -  # volume spike
            rsi_penalty * 5             # overbought penalty
        )
        item["sniper_score"] = round(score, 2)

        # Determine contrarian status
        item["is_contrarian"] = rel_str > 0 and btc_tf_change < 0

        # Strategy recommendation
        if item["is_contrarian"] and rel_str >= 5:
            item["recommended_strategy"] = "SNIPER"
            item["reason"] = f"Contrarian surge (+{rel_str:.1f}% vs BTC {btc_tf_change:+.1f}%)"
        elif surge >= 10 and rsi < 80:
            item["recommended_strategy"] = "SNIPER"
            item["reason"] = "Strong surge + RSI headroom"
        elif surge >= 5:
            item["recommended_strategy"] = "LIGHTNING"
            item["reason"] = "Momentum surge"
        else:
            item["recommended_strategy"] = "GAZUA"
            item["reason"] = "Trend rising"

        # Warnings
        if rsi >= 75:
            item["warning"] = "Caution: RSI overbought"
        elif surge >= 20:
            item["warning"] = "Caution: surge fatigue"

    # 8. Sort: highest sniper_score first
    surging.sort(key=lambda x: -x.get("sniper_score", 0))
    surging = surging[:top_n]

    result = {
        "ok": True,
        "mode": mode,
        "timeframe": timeframe,
        "min_surge_pct": min_surge_pct,
        "benchmark": {
            "btc_change": round(btc_tf_change, 2),
            "market_avg": round(market_tf_avg, 2),
            **benchmark,
        },
        "scanned_count": len(tickers),
        "surging_count": len(surging),
        "surging": surging,
        "timestamp": time.time(),
    }

    _set_cached(cache_key, result)
    return result
