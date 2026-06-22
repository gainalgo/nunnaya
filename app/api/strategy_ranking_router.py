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
# Holdings Upside Ranking - 보유 코인 상승 여력 순위
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
    현재 보유 중인 코인들의 상승 여력 순위.

    AI 분석, 기술적 지표, 시장 모멘텀을 종합하여
    가장 상승 여력이 큰 코인 TOP N을 반환합니다.

    평가 요소:
    - AI 예측 점수 (ai_prediction)
    - 추세 강도 (trend)
    - RSI 과매도 여부
    - 볼린저 밴드 위치 (하단 근처 = 상승 여력)
    - 현재 손익률 (낙폭 큰 코인 = 반등 여력)
    """
    system = request.app.state.system

    # 1. 현재 보유 코인 조회
    holdings = []

    # 먼저 trade_client 확인
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
        return {"ok": True, "message": "보유 코인 없음 (잔고 0)", "rankings": []}

    # 2. 현재가 조회 (exchange ticker)
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

    # 3. 각 코인의 상승 여력 점수 계산
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

        # 현재 손익률
        pnl_pct = ((current_price - avg_buy) / avg_buy * 100) if avg_buy > 0 else 0

        # 평가 금액
        eval_usdt = current_price * qty

        # AI 분석
        ai_score = 0.5
        trend = 0.0
        volatility = 0.0
        rsi = 50.0
        bb_position = 0.5  # 0=하단, 0.5=중간, 1=상단

        ctx = system.coordinator.get_context(market)
        if ctx:
            brain = getattr(ctx, "current_ai", {}).get("brain", {})
            ai_score = float(brain.get("ai_prediction", 0.5))
            trend = float(brain.get("trend", 0.0))
            volatility = float(brain.get("volatility", 0.0))
            rsi = float(brain.get("rsi", 50.0))

            # 볼린저 밴드 위치 계산
            bb_upper = float(brain.get("bb_upper", current_price))
            bb_lower = float(brain.get("bb_lower", current_price))
            if bb_upper > bb_lower:
                bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)

        # 상승 여력 점수 계산 (0~100)
        upside_score = 0.0

        # (1) AI 예측 점수 (가중치: 30%)
        # 0.5 이상이면 상승 신호
        ai_factor = (ai_score - 0.5) * 2.0  # -1 ~ +1
        upside_score += max(0, ai_factor * 30)

        # (2) 추세 강도 (가중치: 20%)
        # trend > 0 이면 상승 추세
        trend_factor = min(1.0, max(-1.0, trend))
        upside_score += max(0, trend_factor * 20)

        # (3) RSI 과매도 보너스 (가중치: 20%)
        # RSI < 30 = 과매도 = 반등 여력
        if rsi < 30:
            upside_score += 20
        elif rsi < 40:
            upside_score += 10
        elif rsi > 70:
            upside_score -= 10  # 과매수 = 하락 가능성

        # (4) 볼린저 밴드 위치 (가중치: 15%)
        # 하단 근처 = 상승 여력
        bb_factor = 1.0 - bb_position  # 0=상단, 1=하단
        upside_score += bb_factor * 15

        # (5) 낙폭 반등 기대 (가중치: 15%)
        # 크게 하락한 코인 = 반등 여력 (단, 과도한 하락은 펀더멘탈 문제)
        if -30 <= pnl_pct < -10:
            upside_score += 15  # 적당한 낙폭 = 반등 기대
        elif -10 <= pnl_pct < 0:
            upside_score += 10
        elif pnl_pct < -30:
            upside_score += 5   # 과도한 낙폭 = 리스크

        # 점수 정규화 (0~100)
        upside_score = max(0, min(100, upside_score))

        # 예상 상승률 (휴리스틱)
        # 변동성 기반 + AI 신뢰도
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

    # 상승 여력 순으로 정렬
    rankings.sort(key=lambda x: x["upside_score"], reverse=True)

    # 순위 부여
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
    """상승 여력 판단 이유 생성."""
    reasons = []

    if ai_score >= 0.65:
        reasons.append("AI 강력 매수 신호")
    elif ai_score >= 0.55:
        reasons.append("AI 매수 우세")

    if trend >= 0.3:
        reasons.append("상승 추세")

    if rsi < 30:
        reasons.append("RSI 과매도")
    elif rsi < 40:
        reasons.append("RSI 저점")

    if bb_position < 0.2:
        reasons.append("BB 하단 터치")
    elif bb_position < 0.4:
        reasons.append("BB 하단 근처")

    if -30 <= pnl_pct < -10:
        reasons.append("낙폭 반등 기대")

    return " · ".join(reasons) if reasons else "분석 중"


# ============================================================
# Market Upside Ranking - 전체 마켓 상승 여력 순위
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
    전체 마켓의 상승 여력 순위.

    티커 데이터 + 엔진 context 기반 빠른 분석.
    (캔들 조회 없이 즉시 응답)

    평가 요소:
    - AI 예측 점수 (context 있는 경우)
    - 일일 등락률 및 변동성
    - 거래량
    - 현재가 위치 (고가/저가 대비)

    필터링:
    - min_price: 최소 가격 (기본 0.01 USDT) - 페니코인 제외
    - max_spread_bps: 최대 스프레드 (기본 50 bps = 0.5%) - 유동성 낮은 코인 제외
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

    # 1. Exchange 마켓 티커 전체 조회
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

    # 2. 거래량 + 가격 필터링
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

    # 3. 각 코인의 상승 여력 점수 계산 (캔들 조회 없이 티커만 사용)
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

        # 일일 변동폭 (%)
        daily_vol_pct = (high - low) / low if low > 0 else 0.05

        # 일일 등락률 (%) - signed_change_rate는 비율이므로 * 100
        change_pct = float(ticker.get("signed_change_rate") or 0) * 100

        # 현재가 위치 (고가/저가 대비) - 저가에 가까울수록 상승 여력
        price_position = (current_price - low) / (high - low) if high > low else 0.5

        # AI 분석 (context가 있으면 사용)
        ai_score = 0.5
        trend = 0.0
        volatility = daily_vol_pct
        rsi = 50.0
        bb_position = price_position  # 티커 기반 근사

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
            # context 없으면 티커 기반 휴리스틱
            # 하락 중이면 반등 기대 (단, 과도한 하락은 제외)
            if -5 <= change_pct < -1:
                trend = 0.1  # 약간의 반등 기대
            elif change_pct >= 2:
                trend = 0.2  # 상승 모멘텀

        # 상승 여력 점수 계산 (0~100)
        upside_score = 0.0

        # (1) AI 예측 점수 (가중치: 25%)
        ai_factor = (ai_score - 0.5) * 2.0
        upside_score += max(0, ai_factor * 25)

        # (2) 추세 강도 (가중치: 20%)
        trend_factor = min(1.0, max(-1.0, trend * 10))  # 스케일 조정
        upside_score += max(0, trend_factor * 20)

        # (3) RSI 과매도 보너스 (가중치: 20%)
        if rsi < 30:
            upside_score += 20
        elif rsi < 40:
            upside_score += 12
        elif rsi < 50:
            upside_score += 5
        elif rsi > 70:
            upside_score -= 10

        # (4) 볼린저 밴드 위치 (가중치: 15%)
        bb_factor = 1.0 - bb_position
        upside_score += bb_factor * 15

        # (5) 거래량 상위 보너스 (가중치: 10%)
        # 거래량 많을수록 유동성 좋음 (USDT 기준)
        if vol24 >= 500_000_000:  # 500M USDT 이상
            upside_score += 10
        elif vol24 >= 100_000_000:  # 100M USDT 이상
            upside_score += 7
        elif vol24 >= 50_000_000:  # 50M USDT 이상
            upside_score += 4

        # (6) 변동성 보너스 (가중치: 10%)
        # 적당한 변동성 = 기회
        if 0.03 <= daily_vol_pct <= 0.10:
            upside_score += 10
        elif 0.02 <= daily_vol_pct < 0.03:
            upside_score += 5
        elif daily_vol_pct > 0.15:
            upside_score -= 5  # 과도한 변동성 = 리스크

        # 점수 정규화 (0~100)
        upside_score = max(0, min(100, upside_score))

        # 예상 상승률
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

    # 상승 여력 순으로 정렬
    rankings.sort(key=lambda x: x["upside_score"], reverse=True)

    # 순위 부여
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
    """전체 마켓 상승 여력 판단 이유 생성."""
    reasons = []

    if ai_score >= 0.65:
        reasons.append("AI 강력 매수")
    elif ai_score >= 0.55:
        reasons.append("AI 매수 우세")

    if trend >= 0.03:
        reasons.append("상승 추세")
    elif trend >= 0.01:
        reasons.append("추세 전환 중")

    if rsi < 30:
        reasons.append("RSI 과매도")
    elif rsi < 40:
        reasons.append("RSI 저점")

    if bb_position < 0.2:
        reasons.append("BB 하단")
    elif bb_position < 0.35:
        reasons.append("BB 하단 근처")

    if vol24 >= 500_000_000:  # 500M USDT 이상
        reasons.append("대형주")
    elif vol24 >= 100_000_000:  # 100M USDT 이상
        reasons.append("중형주")

    if 0.05 <= daily_vol_pct <= 0.10:
        reasons.append("변동성 적정")

    return " · ".join(reasons) if reasons else "분석 중"


# ============================================================
# Rebound Opportunity - 전체 마켓에서 급락 후 반등 기회 포착
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
    전체 마켓에서 급락 후 반등 기회를 찾습니다.

    급락의 원인을 분석하고, 하락이 멈추고 반등할 가능성이 높은 코인을 찾습니다.

    평가 요소:
    - 선택한 시간대 낙폭 (필수: max_decline_pct 이하)
    - 고가 대비 현재가 위치 (저점 근처)
    - 거래량 급증 (패닉 셀링 후 바닥 확인)
    - 일중 변동성 및 반등 신호
    - AI 분석 (context 있는 경우)

    필터링:
    - min_price: 최소 가격 (기본 0.01 USDT) - 페니코인 제외
    - max_spread_bps: 최대 스프레드 (기본 50 bps = 0.5%) - 유동성 낮은 코인 제외
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

    # 1. Exchange 마켓 티커 전체 조회
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

    # 2. 거래량 + 가격 필터링 후 티커 맵 생성
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

    # 2.5. 1h/4h 캔들 조회 (필요한 경우)
    candle_changes = {}  # market -> change_rate
    timeframe_label = "24시간"

    if timeframe in ("1h", "4h"):
        minutes = 60 if timeframe == "1h" else 240
        timeframe_label = "1시간" if timeframe == "1h" else "4시간"

        for market in list(tickers.keys())[:100]:  # 최대 100개만
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
                logger.warning("[RANKING_API] 2.5. 1h/4h 캔들 조회 (필요한 경우): %s", exc, exc_info=True)

    # 3. 급락 코인 필터링 및 점수 계산
    system = request.app.state.system
    rankings = []

    for market, ticker in tickers.items():
        current_price = float(ticker.get("trade_price") or 0)
        if current_price <= 0:
            continue

        vol_24h = float(ticker.get("acc_trade_price_24h") or 0)

        # 변화율 (timeframe에 따라)
        if timeframe in ("1h", "4h") and market in candle_changes:
            change_rate = candle_changes[market]
        else:
            change_rate = float(ticker.get("signed_change_rate") or 0) * 100

        # 급락 필터 (max_decline_pct 이하만)
        if change_rate > max_decline_pct:
            continue

        currency = market.replace(Q.config.market_prefix, "") if market.startswith(Q.config.market_prefix) else market

        # 가격 위치 분석
        high_price = float(ticker.get("high_price") or current_price)
        low_price = float(ticker.get("low_price") or current_price)
        prev_close = float(ticker.get("prev_closing_price") or current_price)

        # 일중 범위 내 위치 (0=저점, 1=고점)
        if high_price > low_price:
            intraday_position = (current_price - low_price) / (high_price - low_price)
        else:
            intraday_position = 0.5

        # 전일 대비 낙폭
        drop_from_prev = ((current_price - prev_close) / prev_close * 100) if prev_close > 0 else 0

        # AI 분석 (context 있는 경우)
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

        # ========== 반등 기회 점수 계산 ==========
        rebound_score = 0.0
        rebound_signals = []

        # (1) 낙폭 분석 (가중치: 25%)
        if change_rate <= -15:
            decline_severity = "폭락"
            rebound_score += 20  # 극심한 낙폭은 리스크
            rebound_signals.append(f"폭락 ({change_rate:.1f}%)")
        elif change_rate <= -10:
            decline_severity = "급락"
            rebound_score += 25
            rebound_signals.append(f"급락 ({change_rate:.1f}%)")
        elif change_rate <= -5:
            decline_severity = "하락"
            rebound_score += 20
            rebound_signals.append(f"하락 ({change_rate:.1f}%)")
        else:
            decline_severity = "조정"
            rebound_score += 15
            rebound_signals.append(f"조정 ({change_rate:.1f}%)")

        # (2) 저점 반등 신호 (가중치: 25%)
        # 일중 저점에서 반등 중인가?
        if intraday_position < 0.2:
            rebound_score += 10
            rebound_signals.append("저점 근처")
        elif 0.2 <= intraday_position <= 0.5:
            rebound_score += 25  # 저점에서 반등 중
            rebound_signals.append("저점 반등 중")
        elif 0.5 < intraday_position <= 0.7:
            rebound_score += 20
            rebound_signals.append("회복 진행 중")
        else:
            rebound_score += 5  # 이미 많이 회복

        # (3) RSI 과매도 (가중치: 20%)
        if rsi < 25:
            rebound_score += 20
            rebound_signals.append(f"RSI 극과매도 ({rsi:.0f})")
        elif rsi < 30:
            rebound_score += 15
            rebound_signals.append(f"RSI 과매도 ({rsi:.0f})")
        elif rsi < 40:
            rebound_score += 10
            rebound_signals.append(f"RSI 저점 ({rsi:.0f})")

        # (4) BB 하단 (가중치: 15%)
        if bb_position < 0.15:
            rebound_score += 15
            rebound_signals.append("BB 하단 이탈")
        elif bb_position < 0.3:
            rebound_score += 10
            rebound_signals.append("BB 하단 근처")

        # (5) 거래량 분석 (가중치: 15%) - USDT 기준
        # 거래량이 크면 관심 집중
        if vol_24h >= 500_000_000:  # 500M USDT 이상
            rebound_score += 15
            rebound_signals.append("대형 거래량")
        elif vol_24h >= 100_000_000:  # 100M USDT 이상
            rebound_score += 10
            rebound_signals.append("활발한 거래")
        elif vol_24h >= 30_000_000:  # 30M USDT 이상
            rebound_score += 5

        # 점수 정규화 (0~100)
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
            "reason": " · ".join(rebound_signals[:3]) if rebound_signals else "분석 중",
        })

    # 반등 기회 점수 순으로 정렬
    rankings.sort(key=lambda x: x["rebound_score"], reverse=True)

    # 순위 부여
    for i, r in enumerate(rankings):
        r["rank"] = i + 1

    result = {
        "ok": True,
        "timeframe": timeframe,
        "timeframe_label": timeframe_label,
        "total_declining": len(rankings),
        "top_n": top_n,
        "rankings": rankings[:top_n],
        "all_rankings": rankings[:30],  # 최대 30개만
        "summary": {
            "best_rebound": rankings[0] if rankings else None,
            "avg_rebound_score": round(sum(r["rebound_score"] for r in rankings) / len(rankings), 1) if rankings else 0,
            "total_declining_count": len(rankings),
        }
    }
    _set_cached(cache_key, result)
    return result


# ============================================================
# RSI Ranking - 전체 USDT 마켓 RSI 순위 (과매도 코인 찾기)
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
    전체 마켓의 RSI 순위.

    RSI가 낮은 코인 = 과매도 상태 = 반등 가능성

    - RSI < 30: 극과매도 (강력 반등 기대)
    - RSI 30~40: 과매도 (반등 가능)
    - RSI 40~60: 중립
    - RSI > 70: 과매수 (하락 가능)

    필터링:
    - min_price: 최소 가격 (기본 0.01 USDT) - 페니코인 제외
    - max_spread_bps: 최대 스프레드 (기본 50 bps = 0.5%) - 유동성 낮은 코인 제외
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

    # 1. Exchange 마켓 티커 전체 조회
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

    # 2. 거래량 + 가격 필터링 후 티커 맵 생성
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

    # 3. RSI/MACD/일목 계산을 위한 캔들 조회 (15분 캔들 60개)
    rankings = []

    def calc_ema(data, period):
        """EMA 계산"""
        if len(data) < period:
            return None
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period
        for price in data[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    for market in vol_filtered_markets[:80]:  # 최대 80개만 (API 제한)
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

            # RSI 계산 (14 period)
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

            # RSI 필터
            if rsi > rsi_max:
                continue

            # MACD 계산 (12, 26, 9)
            ema12 = calc_ema(closes, 12)
            ema26 = calc_ema(closes, 26)
            macd_line = (ema12 - ema26) if ema12 and ema26 else 0
            macd_score = 40 if macd_line > 0 else 20  # 상승세면 40점, 하락세면 20점

            # 일목균형표 간이 계산 (전환선 9, 기준선 26)
            tenkan = (max(highs[-9:]) + min(lows[-9:])) / 2 if len(highs) >= 9 else closes[-1]
            kijun = (max(highs[-26:]) + min(lows[-26:])) / 2 if len(highs) >= 26 else closes[-1]
            current = closes[-1]

            # 일목 점수: 가격이 전환선/기준선 위면 상승세
            ichimoku_score = 0
            if current > tenkan:
                ichimoku_score += 50
            if current > kijun:
                ichimoku_score += 50

            # 거래량 점수: 최근 거래량 vs 평균 거래량
            avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 1
            current_vol = volumes[-1]
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1
            volume_score = min(100, int(vol_ratio * 40))  # 최대 100점

            ticker = tickers.get(market, {})
            currency = market.replace(Q.config.market_prefix, "") if market.startswith(Q.config.market_prefix) else market
            current_price = float(ticker.get("trade_price") or closes[-1])
            change_rate = float(ticker.get("signed_change_rate") or 0) * 100
            vol_24h = float(ticker.get("acc_trade_price_24h") or 0)

            # RSI 상태 판단
            if rsi < 20:
                rsi_status = "극과매도"
                rsi_emoji = "🔴"
            elif rsi < 30:
                rsi_status = "과매도"
                rsi_emoji = "🟠"
            elif rsi < 40:
                rsi_status = "저점"
                rsi_emoji = "🟡"
            else:
                rsi_status = "중립"
                rsi_emoji = "⚪"

            # RSI 점수 (낮을수록 높은 점수 - 과매도 = 반등 기회)
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
            logger.warning("[RANKING_API] RSI 점수 (낮을수록 높은 점수 - 과매도 = 반등 기회) except-> continue: %s", exc, exc_info=True)
            continue

    # RSI 낮은 순으로 정렬
    rankings.sort(key=lambda x: x["rsi"])

    # 순위 부여
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
# Technical Aggregate Score - 종합 기술 점수
# 거래량 + RSI + 일목균형표 + MACD 종합
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
    종합 기술 분석 점수.

    4가지 지표를 종합하여 매수 적합도를 0~100 점수로 계산합니다.

    - 거래량 급등 (20%): 평균 대비 거래량 증가
    - RSI (25%): 과매도 상태일수록 높은 점수
    - 일목균형표 (25%): 구름 돌파/위치
    - MACD (30%): 골든크로스, 히스토그램 방향

    필터링:
    - min_volume_usdt: 24시간 거래량 최소값 (기본 100M USDT)
    - min_price: 최소 가격 (기본 0.01 USDT) - 페니코인 제외
    - max_spread_bps: 최대 스프레드 (기본 50 bps = 0.5%) - 유동성 낮은 코인 제외
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

    # 1. Exchange 마켓 티커 전체 조회
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

    # 2. 거래량 + 가격 필터링 후 티커 맵 생성
    tickers = {}
    vol_filtered_markets = []
    filtered_out = {"low_volume": 0, "low_price": 0, "wide_spread": 0}

    for t in tickers:
        market = t.get("market", "")
        vol_24h = float(t.get("acc_trade_price_24h") or 0)
        last_price = float(t.get("trade_price") or 0)

        # 거래량 필터
        if vol_24h < min_volume_usdt:
            filtered_out["low_volume"] += 1
            continue

        # 가격 필터 (페니코인 제외)
        if min_price > 0 and last_price < min_price:
            filtered_out["low_price"] += 1
            continue

        # Note: Ticker doesn't include bid/ask prices, skip spread filter

        tickers[market] = t
        vol_filtered_markets.append(market)

    if not vol_filtered_markets:
        return {"ok": True, "message": "No markets meet filter criteria", "rankings": [], "filtered_out": filtered_out}

    # 3. 각 코인의 기술 점수 계산
    rankings = []

    for market in vol_filtered_markets[:60]:  # 최대 60개 (API 제한)
        try:
            # 1시간 캔들 30개 조회 (일목균형표, MACD 계산용)
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

            # ========== 1. RSI 계산 (14 period) ==========
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

            # RSI 점수 (과매도일수록 높음)
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

            # ========== 2. MACD 계산 ==========
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

            # 간단한 시그널 라인 (9일 EMA of MACD)
            # 여기서는 단순화: MACD > 0 이고 상승 중이면 좋음
            prev_ema12 = ema(closes[:-1], 12) if len(closes) > 1 else ema12
            prev_ema26 = ema(closes[:-1], 26) if len(closes) > 1 else ema26
            prev_macd = prev_ema12 - prev_ema26

            macd_momentum = macd_line - prev_macd  # 히스토그램 방향

            # MACD 점수
            macd_score = 50  # 기본
            if macd_line > 0 and macd_momentum > 0:
                macd_score = 90  # 골든크로스 + 상승
            elif macd_line < 0 and macd_momentum > 0:
                macd_score = 70  # 아직 음수지만 상승 전환
            elif macd_line > 0 and macd_momentum < 0:
                macd_score = 40  # 양수지만 하락 중
            elif macd_line < 0 and macd_momentum < 0:
                macd_score = 10  # 데드크로스 + 하락

            # ========== 3. 일목균형표 (간략 계산) ==========
            # 전환선: 9일 (고가+저가)/2
            # 기준선: 26일 (고가+저가)/2
            if len(highs) >= 26 and len(lows) >= 26:
                tenkan = (max(highs[-9:]) + min(lows[-9:])) / 2  # 전환선
                kijun = (max(highs[-26:]) + min(lows[-26:])) / 2  # 기준선

                # 구름 (선행스팬 A/B) - 단순화
                senkou_a = (tenkan + kijun) / 2
                senkou_b = (max(highs[-52:]) + min(lows[-52:])) / 2 if len(highs) >= 52 else kijun
                cloud_top = max(senkou_a, senkou_b)
                cloud_bottom = min(senkou_a, senkou_b)

                # 일목 점수
                ichimoku_score = 50
                if current_price > cloud_top:
                    ichimoku_score = 90  # 구름 위
                    if current_price > tenkan > kijun:
                        ichimoku_score = 100  # 완벽한 상승 배열
                elif current_price > cloud_bottom:
                    ichimoku_score = 60  # 구름 안
                else:
                    ichimoku_score = 20  # 구름 아래
                    if tenkan < kijun:
                        ichimoku_score = 10  # 하락 배열
            else:
                ichimoku_score = 50

            # ========== 4. 거래량 점수 ==========
            avg_vol = sum(volumes[:-1]) / (len(volumes) - 1) if len(volumes) > 1 else 1
            current_vol = volumes[-1] if volumes else 0
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1

            if vol_ratio >= 3.0:
                volume_score = 100  # 거래량 폭발
            elif vol_ratio >= 2.0:
                volume_score = 80
            elif vol_ratio >= 1.5:
                volume_score = 60
            elif vol_ratio >= 1.0:
                volume_score = 40
            else:
                volume_score = 20  # 거래량 감소

            # ========== 종합 점수 계산 ==========
            # 거래량(20%) + RSI(25%) + 일목(25%) + MACD(30%)
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

            # 신호 판단
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
            logger.warning("[RANKING_API] 신호 판단 except-> continue: %s", exc, exc_info=True)
            continue

    # 점수 높은 순으로 정렬
    rankings.sort(key=lambda x: x["total_score"], reverse=True)

    # 순위 부여
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
# TOP 5 Rankings Unified API - 통합 랭킹 API
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
    통합 TOP 5 랭킹 API.

    4개 분석 API의 결과를 한번에 조회합니다:
    - rebound: 급락 반등 기회 TOP 5
    - rsi_oversold: RSI 과매도 TOP 5
    - tech_score: 종합 기술 점수 TOP 5
    - upside: 전체 마켓 상승 여력 TOP 5

    캐시 TTL: 30초
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

    # 1. Exchange 마켓 티커 전체 조회 (한 번만)
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

    # 2. 공통 필터링
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
    markets_list = list(tickers.keys())[:80]  # 최대 80개

    # 3. 캔들 데이터 조회 (15분 캔들 60개)
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

    # 4. 각 분석 수행
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

        # 가격 위치
        price_position = (current_price - low) / (high - low) if high > low else 0.5
        daily_vol_pct = (high - low) / low if low > 0 else 0.05

        # AI context 조회
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

        # 캔들 기반 RSI/MACD 계산
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

            # RSI 계산
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

            # 일목균형표
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

            # 거래량 점수
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

        # RSI 점수 계산
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
        if change_pct <= -3:  # 급락 코인만
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
            rsi_status = "극과매도" if rsi_calc < 20 else ("과매도" if rsi_calc < 30 else "저점")
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

    # 5. 각 리스트 정렬 및 TOP N 추출
    rebound_list.sort(key=lambda x: x["score"], reverse=True)
    rsi_list.sort(key=lambda x: x["rsi"])  # RSI 낮은 순
    tech_list.sort(key=lambda x: x["score"], reverse=True)
    upside_list.sort(key=lambda x: x["score"], reverse=True)

    result = {
        "ok": True,
        "timestamp": int(time_module.time()),
        "rankings": {
            "rebound": {
                "title": "급락 반등 기회",
                "recommended_strategy": "GAZUA",
                "items": rebound_list[:top_n],
            },
            "rsi_oversold": {
                "title": "RSI 과매도",
                "recommended_strategy": "LIGHTNING",
                "items": rsi_list[:top_n],
            },
            "tech_score": {
                "title": "종합 기술 점수",
                "recommended_strategy": "LADDER",
                "items": tech_list[:top_n],
            },
            "upside": {
                "title": "전체 마켓",
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
# 목적:
# - 추천 목록에 없는 코인도 수동 입력 시 전략별 권장 파라미터 계산
# - 각 전략(LADDER/LIGHTNING/GAZUA)에 맞는 최적 값 반환
# ============================================================
@router.get(
    "/calc_params",
    summary="수동 코인 파라미터 계산",
    description="입력된 마켓의 AI 점수/지표를 분석하여 전략별 권장 파라미터 반환",
)
def calc_params(
    request: Request,
    market: str = Query(..., description="마켓 코드 (예: BTCUSDT)"),
    strategy: str = Query(..., description="전략 (LADDER, LIGHTNING, GAZUA)"),
    budget: float = Query(100, description="Budget (USDT)"),
):
    """수동 코인 파라미터 계산."""
    import time as time_module

    market = market.strip().upper()
    strategy = strategy.strip().upper()

    # 마켓 형식 정규화: BTC, BTC/USDT 등 → BTCUSDT
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
        logger.warning("[RANKING_API] Candle format (reversed for chronological order): %s", exc, exc_info=True)

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
    summary="다중 타임프레임 AI 분석",
    description="5분, 15분, 1시간 타임프레임에서 AI 점수를 계산하고 최적 타임프레임을 선택합니다.",
)
def get_multi_timeframe_analysis(
    market: str,
    force_refresh: bool = Query(False, description="캐시 무시하고 새로 계산"),
) -> Dict[str, Any]:
    """다중 타임프레임 AI 분석 결과 조회."""
    try:
        from app.core.multi_timeframe_ai import analyze_multi_timeframe

        result = analyze_multi_timeframe(market, force_refresh=force_refresh)

        if not result:
            return {
                "ok": False,
                "error": "분석 실패 (데이터 부족)",
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
    summary="다중 타임프레임 AI 배치 분석",
    description="여러 마켓의 다중 타임프레임 AI 분석을 수행합니다.",
)
def get_multi_timeframe_batch(
    markets: str = Query(..., description="쉼표로 구분된 마켓 목록 (예: BTCUSDT,ETHUSDT)"),
    force_refresh: bool = Query(False, description="캐시 무시"),
) -> Dict[str, Any]:
    """다중 마켓 타임프레임 분석."""
    try:
        from app.core.multi_timeframe_ai import analyze_multi_timeframe

        market_list = [m.strip().upper() for m in markets.split(",") if m.strip()]

        if not market_list:
            return {"ok": False, "error": "마켓 목록이 비어있습니다"}

        if len(market_list) > 20:
            return {"ok": False, "error": "최대 20개 마켓까지 지원됩니다"}

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
                results[m] = {"error": "분석 실패"}

        return {
            "ok": True,
            "count": len(results),
            "results": results,
        }
    except (AttributeError, TypeError, ValueError) as e:
        logger.exception(f"multi-timeframe batch error: {e}")
        return {"ok": False, "error": str(e)}


# ============================================================
# Surge Scanner - 실시간 급등/역행 코인 스캐너
# [2026-01-31] 역행(Contrarian) + AI 분석 통합
# ============================================================

def _get_market_benchmark(tickers: list, timeframe: str = "24h") -> dict:
    """시장 기준점 계산 (BTC + 시장 평균).

    Returns:
        {
            "btc_change": BTC 변화율 (%),
            "market_avg": 시장 평균 변화율 (%),
            "market_median": 시장 중앙값 변화율 (%),
            "rising_count": 상승 코인 수,
            "falling_count": 하락 코인 수,
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
    summary="실시간 급등/역행 코인 스캐너",
    responses={
        200: {"description": "급등 및 역행 코인 목록"},
    },
)
def market_surge_scanner(
    request: Request,
    top_n: int = Query(10, ge=1, le=50, description="상위 N개"),
    min_surge_pct: float = Query(3.0, ge=0.5, description="최소 급등률 (%)"),
    min_volume_usdt: float = Query(500_000, ge=0, description="최소 24시간 거래량 (USDT)"),
    timeframe: str = Query("1h", description="타임프레임: 5m, 15m, 1h, 4h, 24h"),
    exclude_active: bool = Query(False, description="이미 활성화된 마켓 제외"),
    mode: str = Query("both", description="모드: absolute(절대급등), relative(역행), both(둘다)"),
):
    """
    전체 마켓에서 급등/역행 코인을 스캔합니다.

    **모드 설명:**
    - `absolute`: 절대 급등률 기준 (단순히 +5% 이상)
    - `relative`: 역행 강도 기준 (시장 대비 상대 강도)
      - 예: 시장평균 -3%, 코인 +5% → 역행강도 = +8%
    - `both`: 둘 다 계산하여 종합 점수 (★ 기본값)

    **타임프레임:**
    - 5m: 5분 (초단타) | 15m: 15분 (스캘핑)
    - 1h: 1시간 (단타) ★ | 4h: 4시간 (스윙)
    - 24h: 24시간 (일일)

    **SNIPER 타겟:**
    - 역행 강도 높은 코인 (시장 하락 시 혼자 상승)
    - AI 점수 + 거래량 급증 + RSI 여유
    """
    import time

    # Cache check (5초 TTL)
    cache_key = _build_cache_key(
        "market/surge",
        top_n=top_n, min_surge_pct=min_surge_pct, min_volume_usdt=min_volume_usdt,
        timeframe=timeframe, exclude_active=exclude_active, mode=mode
    )
    cached = _get_cached(cache_key, ttl=5.0)
    if cached is not None:
        return cached

    # 타임프레임 설정
    tf_minutes = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "24h": 1440}.get(timeframe, 60)
    tf_candles = {"5m": 3, "15m": 3, "1h": 2, "4h": 2, "24h": 1}.get(timeframe, 2)

    # 1. 전체 마켓 티커 조회
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
        return {"ok": False, "error": f"티커 조회 실패: {e}"}

    if not tickers:
        return {"ok": True, "message": "티커 없음", "surging": []}

    # 2. 시장 기준점 계산 (역행 분석용)
    benchmark = _get_market_benchmark(tickers, timeframe)
    btc_change = benchmark["btc_change"]
    market_avg = benchmark["market_avg"]

    # 시스템 참조
    system = request.app.state.system
    active_markets = set()
    if exclude_active and system:
        try:
            for m in system.oma_registry.get_all_markets():
                state = system.oma_registry.get_state(m)
                if state in (MarketState.ACTIVE, MarketState.RECOVERY):
                    active_markets.add(m)
        except (AttributeError, TypeError) as exc:
            logger.warning("[RANKING_API] 시스템 참조: %s", exc, exc_info=True)

    # 3. BTC 캔들로 타임프레임별 BTC 변화율 계산
    btc_tf_change = btc_change  # 기본값은 24h
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
            logger.warning("[RANKING_API] 3. BTC 캔들로 타임프레임별 BTC 변화율 계산: %s", exc, exc_info=True)

    # 4. 거래량 필터링 + 후보 선별
    candidates = []

    for t in tickers:
        market = t.get("market", "")

        if market in active_markets:
            continue
        if market == f"{Q.config.market_prefix}BTC":  # BTC는 기준이므로 제외
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

    # 5. 타임프레임별 변화율 + 역행 강도 계산
    surging = []
    all_tf_changes = []  # 시장 평균 계산용

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

                            # 역행 강도 = 코인 변화율 - BTC 변화율
                            relative_strength = surge_pct - btc_tf_change

                            # 필터링: 모드에 따라
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
                    logger.warning("[RANKING_API] 필터링: 모드에 따라: %s", exc, exc_info=True)
    else:
        # 24h 타임프레임
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

    # 6. 타임프레임 시장 평균 계산
    market_tf_avg = sum(all_tf_changes) / len(all_tf_changes) if all_tf_changes else market_avg

    # 7. AI/RSI 분석 + 급등 점수 계산
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

        # 종합 점수 계산 (AI 기반)
        surge = item["surge_pct"]
        rel_str = item["relative_strength"]
        rsi = item["rsi"]
        ai = item["ai_score"]
        vol_surge = item.get("vol_surge_ratio", 1.0)

        # 점수 = 역행강도(40%) + 절대급등(20%) + AI(25%) + 거래량급증(15%)
        # RSI 과매수 페널티
        rsi_penalty = max(0, (rsi - 70) * 0.1) if rsi > 70 else 0

        score = (
            rel_str * 0.4 +           # 역행 강도 (핵심)
            surge * 0.2 +              # 절대 급등률
            (ai - 0.5) * 50 * 0.25 +   # AI 점수 (0.5 기준)
            min(vol_surge, 3.0) * 0.15 * 10 -  # 거래량 급증
            rsi_penalty * 5             # 과매수 페널티
        )
        item["sniper_score"] = round(score, 2)

        # 역행 여부 판정
        item["is_contrarian"] = rel_str > 0 and btc_tf_change < 0

        # 전략 추천
        if item["is_contrarian"] and rel_str >= 5:
            item["recommended_strategy"] = "SNIPER"
            item["reason"] = f"역행 급등 (BTC {btc_tf_change:+.1f}% 대비 +{rel_str:.1f}%)"
        elif surge >= 10 and rsi < 80:
            item["recommended_strategy"] = "SNIPER"
            item["reason"] = "강한 급등 + RSI 여유"
        elif surge >= 5:
            item["recommended_strategy"] = "LIGHTNING"
            item["reason"] = "모멘텀 급등"
        else:
            item["recommended_strategy"] = "GAZUA"
            item["reason"] = "추세 상승"

        # 경고
        if rsi >= 75:
            item["warning"] = "RSI 과매수 주의"
        elif surge >= 20:
            item["warning"] = "급등 피로 주의"

    # 8. 정렬: sniper_score 높은 순
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
