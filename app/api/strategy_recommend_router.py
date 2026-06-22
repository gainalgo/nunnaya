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

# [2026-02-01] [PROTECTED] 전략 → topn_selector 프로필 매핑
# DO NOT MODIFY - 각 전략은 고유한 특성에 맞는 프로필로 스코어링해야 함
# 이 매핑을 변경하면 전략별 코인 선별이 망가집니다
STRATEGY_TO_PROFILE: Dict[str, str] = {
    "PINGPONG": "pingpong",    # 박스권, 횡보, 변동성
    "AUTOLOOP": "autorope",    # 유동성 + 적당한 변동성
    "LADDER": "ladder",        # 트렌드 추종, 분할매수
    "LIGHTNING": "lightning",  # 브레이크아웃, 모멘텀
    "GAZUA": "gazua",          # 강한 상승 모멘텀
    "CONTRARIAN": "pingpong",  # 역행 = 변동성 + 박스권 유사
    "SNIPER": "lightning",     # 급등 저격 = 모멘텀 유사
}

logger = logging.getLogger(__name__)

router = APIRouter()

# ============================================================
# Endpoint-specific lock (not shared — stays with the endpoints)
# ============================================================
_recommend_semaphore = threading.Semaphore(1)  # 추천 계산 동시 1개 제한 (tick loop 스레드풀 보호)


# 전략별 타임프레임 설정 (멀티타임프레임 분석)
STRATEGY_TIMEFRAMES = {
    # 전략: (캔들 단위(분), 캔들 개수, 설명)
    "LIGHTNING": (1, 60, "1분×60개=1시간 - 단타/급등락"),
    "PINGPONG": (5, 60, "5분×60개=5시간 - 박스권 스윙"),
    "AUTOLOOP": (15, 60, "15분×60개=15시간 - 분할매수"),
    "LADDER": (60, 48, "1시간×48개=2일 - DCA 하락추세"),
    "GAZUA": (240, 42, "4시간×42개=7일 - 추세추종"),
    "SNIPER": (60, 24, "1시간×24개=1일 - 저격 타이밍"),
    "CONTRARIAN": (15, 60, "15분×60개=15시간 - 역행 매매"),
}

def _get_strategy_timeframe(strategy: str) -> tuple:
    """전략별 캔들 타임프레임 반환 (unit_min, count)."""
    s = str(strategy).upper()
    if s in STRATEGY_TIMEFRAMES:
        return STRATEGY_TIMEFRAMES[s][0], STRATEGY_TIMEFRAMES[s][1]
    # 기본값: 5분 30개
    return 5, 30

# ---- 추천 API 캔들 캐시 (429 방지) ----
_ai_candle_cache: Dict[str, Tuple[float, List[float]]] = {}
_AI_CANDLE_CACHE_TTL = 300.0  # 5분 캐시


def _fetch_candles_for_ai(market: str, strategy: str = "AUTOLOOP") -> List[float]:
    """Fetch recent candles for on-the-fly AI analysis.

    전략별 멀티타임프레임 지원:
    - LIGHTNING: 1분 캔들 (단타)
    - PINGPONG: 5분 캔들 (박스권)
    - AUTOLOOP: 15분 캔들 (분할매수)
    - LADDER: 1시간 캔들 (DCA)
    - GAZUA: 4시간 캔들 (추세추종)
    - SNIPER: 1시간 캔들 (저격)
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

        # 전략별 타임프레임 가져오기
        unit_min, count = _get_strategy_timeframe(strategy)

        # 캐시 확인
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
    # SNIPER(s)는 실행 로직은 SNIPER를 공용 사용하되, 추천/라벨만 별도 분리한다.
    st = "SNIPER" if snipers_mode else st_raw
    strategy_label = "SNIPERS" if snipers_mode else st
    min_price_eff = max(0.0, float(min_price or 0.0))
    max_price_eff = max(0.0, float(max_price or 0.0))
    if min_price_eff > 0 and max_price_eff > 0 and max_price_eff < min_price_eff:
        min_price_eff, max_price_eff = max_price_eff, min_price_eff

    # --- CACHE CHECK (900초 read-TTL — prewarm 사이클(n=20 으로 늘려 컴퓨트↑, ~540-610s)보다 길게 ─
    #     사이클보다 짧으면 매 사이클 cold 구간 생겨 200마켓 full fetch로 느려짐.
    #     n 은 prewarm 기본값(20)과 같아야 cache_key 적중 → 대시보드는 n=20 로 호출할 것) ---
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

    # [2026-03-03] 직렬화: rank_topn_by_public_candles는 스레드풀에서 동시 1개만 실행
    # 동시 요청이 들어오면 stale 캐시 반환 (tick loop 스레드풀 보호)
    _recommend_acquired = _recommend_semaphore.acquire(blocking=False)
    if not _recommend_acquired:
        stale = _get_cached(cache_key, ttl=86400)  # 오래된 캐시라도 반환
        if stale:
            return stale
        profile = STRATEGY_TO_PROFILE.get(st, "ladder")
        return {"ok": True, "items": [], "profile": profile, "strategy": strategy_label, "computing": True}

    # [2026-02-01] 전략별 프로필 기반 랭킹 사용
    profile = STRATEGY_TO_PROFILE.get(st, "ladder")  # fallback to ladder

    try:
        # topn_selector의 프로필 기반 랭킹 호출
        # 각 전략에 맞는 특성(변동성, 모멘텀, 유동성 등)으로 스코어링
        ranked_n = int(n * 2)
        if min_price_eff > 0 or max_price_eff > 0:
            ranked_n = int(max(n * 4, 50))
        ranked = rank_topn_by_public_candles(
            n=ranked_n,  # 여유있게 가져와서 필터링
            profile=profile,
            candle_unit_minutes=5,  # 5분 캔들 (속도 vs 정확도 균형)
            candle_count=60,        # 5시간 데이터
            max_markets=200,
            request_sleep=0.05,     # API 속도 조절
        )

        # MarketFeatures → 마켓 리스트 + 스코어
        ranked_markets = {f.market: (score, f) for score, f in ranked}

    except (TypeError, ValueError) as e:
        logger.warning(f"[recommendations] topn_selector failed for {st}/{profile}: {e}")
        ranked_markets = {}

    # 티커로 현재가 정보 보강
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

    # 프로필 랭킹 순서대로 후보 생성 (스코어 높은 순)
    if ranked_markets:
        top_candidates = []
        for market in ranked_markets.keys():
            t = ticker_map.get(market)
            if t:
                top_candidates.append(t)
    else:
        # fallback: 거래대금 순
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

    # ★ 자본(equity) 연동 base — 옛 KRW 잔재(base_budget=100_000원)가 USDT 로 라벨만 바뀌어
    #   $0.13 코인에 $200K 추천하던 버그 fix. 실제 자본(USDT) 기준, 없으면 보수적 200.
    _acct_eq = float(getattr(system, "_last_equity_usdt", 0) or 0)
    if _acct_eq <= 0:
        _acct_eq = float(getattr(system, "equity_usdt", 0) or 0)
    if _acct_eq <= 0:
        _acct_eq = 200.0
    _budget_cap = max(10.0, _acct_eq * 0.5)   # 한 코인에 자본 절반 초과 추천 금지

    # Get AI Model Info (Training Data Size)
    model_info = ai_trainer.get_info()
    model_rows = model_info.get("rows", 0)

    # --- OPTIMIZATION: Pre-fetch candle data in parallel ---
    # 기존에는 후보 코인마다 순차적으로 캔들 데이터를 요청하여 느렸습니다.
    # 이제 한 번에 병렬로 요청하여 응답 속도를 대폭 개선합니다.
    markets_to_fetch = []
    for t in top_candidates:
        market = t.get("market")
        if market and not system.coordinator.contexts.get(market):
            markets_to_fetch.append(market)

    candle_histories = {}
    if markets_to_fetch:
        # 과도한 병렬은 429/실패로 ai_score=0.5 평탄화를 유발하므로 워커를 제한한다.
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
            # 컨텍스트 확인
            ctx = system.coordinator.contexts.get(market)
            if ctx:
                # 전략 모드 확인
                ctrls = getattr(ctx, "controls", {}) or {}
                strat = ctrls.get("strategy", {}) or {}
                if strat.get("enabled"):
                    active_strategy = str(strat.get("mode") or "CUSTOM").upper()
                else:
                    active_strategy = "AI (AUTOCOIN)" # 기본 엔진
            else:
                active_strategy = f"{oma_state} (NO_CTX)"

        # AI Score (if available)
        ai_score = 0.5
        volatility = 0.0
        trend = 0.0

        # RSI, momentum 초기값 (이전에 하드코딩 50이던 것을 실제 계산으로 수정)
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
            # RSI 계산을 위해 최소 15개 데이터 필요 (length 14 + 1)
            if hist and len(hist) >= 15:
                try:
                    # hist는 이미 가격 리스트 (float list) - _fetch_candles_for_ai가 반환
                    prices = hist  # 이미 리스트
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
                        # Brain 없으면 indicators로 직접 계산
                        volatility = indicators.volatility(prices, 20) or 0.0
                        trend = indicators.trend(prices, 20) or 0.0
                        momentum = indicators.trend(prices, 3) or 0.0
                        rsi_val = indicators.rsi(prices, 14)
                        rsi = float(rsi_val) if rsi_val is not None else 50.0
                        ai_from_live = True
                except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
                    logger.warning(f"[recommendations] {market} indicator calc failed: {e}")

        # AI/지표가 평탄화(0.5/0.0)될 때는 프로필 피처를 보조 입력으로 사용해 전략 성격을 보존한다.
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
        # 1) 거래대금 기반 팩터
        vol24 = float(t.get("acc_trade_price_24h") or 0)
        vol_factor = (vol24 / median_vol) ** 0.5 if median_vol > 0 else 1.0
        vol_factor = max(0.5, min(2.0, vol_factor))

        # 2) 코인 가격 기반 최소 예산 (최소 0.001개 이상 거래 가능하도록)
        coin_price = float(t.get("trade_price") or 0)
        # 가격대별 최소 예산: 고가 코인은 더 많은 자본 필요 (USDT 기준)
        if coin_price >= 50_000:    # BTC급 ($50K 이상)
            min_budget = 500
        elif coin_price >= 1_000:   # ETH급 ($1K 이상)
            min_budget = 200
        elif coin_price >= 100:     # $100 이상
            min_budget = 100
        elif coin_price >= 10:      # $10 이상
            min_budget = 50
        else:                       # 저가 코인
            min_budget = 30

        base_budget = max(10.0, _acct_eq * 0.15)   # per-deploy ≈ 자본의 15% (다중 슬롯 가정, USDT)

        # 3) RSI 기반 예산 조정
        # RSI < 30 (과매도): 매수 기회 → 예산 증가
        # RSI > 70 (과매수): 위험 구간 → 예산 감소
        rsi_factor = 1.0
        if rsi < 30:
            rsi_factor = 1.3  # 과매도 시 30% 증가
        elif rsi < 40:
            rsi_factor = 1.15
        elif rsi > 70:
            rsi_factor = 0.7  # 과매수 시 30% 감소
        elif rsi > 60:
            rsi_factor = 0.85

        suggested_budget = int((base_budget * vol_factor * rsi_factor) * 100) / 100  # USDT 0.01 단위
        # 최소 예산 보장 (단, 자본 초과 금지)
        suggested_budget = max(suggested_budget, min(min_budget, _budget_cap))
        suggested_budget = min(suggested_budget, _budget_cap)  # ★ 자본 절반 초과 X (옛 KRW 잔재 재발 방지)

        # Price Prediction (Heuristic based on Volatility & AI Score)
        # AI Score가 0.5 이상일 때 상승 방향으로 예측
        # 변동성(Daily Range)을 시간 단위로 나누어 목표가 산출
        pred = {}
        rec = {}
        ladder_params = {}
        gazua_params = {}
        lightning_params = {}

        # 변동성 계산 (try 블록 밖에서 정의)
        curr = float(t.get("trade_price") or 0)
        high = float(t.get("high_price") or curr)
        low = float(t.get("low_price") or curr)
        daily_vol_pct = (high - low) / low if low > 0 else 0.05
        change_rate_pct = float(t.get("signed_change_rate") or 0) * 100.0

        # SNIPER(s) 전용 후보 필터:
        # - 변동폭은 충분해야 하고
        # - 급락 추세 코인은 제외
        if snipers_mode:
            if daily_vol_pct < 0.025:  # 2.5% 미만: 스윙 부족
                continue
            if change_rate_pct <= -4.5:  # 과도한 하락: 칼날낙하 제외
                continue

        try:

            # AI 확신도 (0.5~1.0 -> 0.0~1.0 scaling)
            # 최소 0.2 정도의 가중치는 두어 목표가가 현재가보다 높게 나오도록 보정
            confidence = max(0.2, (ai_score - 0.5) * 2.0)

            # 시간별 예상 상승폭 (단순화된 제곱근 시간 법칙 적용)
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

                # Lightning specific params: 단기 고변동성 스캘핑
                # TP/SL은 변동성에 비례, 홀딩 시간 짧게
                rec_tp = 1.5  # 기본 1.5%
                rec_sl = -1.0  # 기본 -1%

                # 변동성 기반 조정
                if daily_vol_pct > 0.08:
                    rec_tp = 3.0
                    rec_sl = -2.0
                elif daily_vol_pct > 0.05:
                    rec_tp = 2.5
                    rec_sl = -1.5
                elif daily_vol_pct > 0.03:
                    rec_tp = 2.0
                    rec_sl = -1.2

                # AI 확신도 조정
                if ai_score >= 0.7:
                    rec_tp += 0.5  # 고확신 시 목표가 상향
                elif ai_score < 0.5:
                    rec_tp -= 0.3  # 저확신 시 보수적
                    rec_sl = min(rec_sl, -SNIPER_MIN_SL_PCT)  # SL 하한선 유지

                # RSI 기반 조정 (LIGHTNING)
                if rsi < 30:
                    rec_tp += 0.5  # 과매도 → 반등 기대 → TP 상향
                    rec_sl -= 0.3  # SL 여유 확보
                elif rsi > 70:
                    rec_tp -= 0.5  # 과매수 → 보수적
                    rec_sl = min(rec_sl + 0.3, -SNIPER_MIN_SL_PCT)  # SL 하한선 유지

                # 최대 보유 시간 (분) - 변동성 클수록 짧게
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
                # 기본값
                base_step = 1.0
                sl_recommend = -5.0 # default fallback

                # ATR 계산 시도 (hist가 있을 경우에만)
                atr_14h = None
                try:
                    hist = candle_histories.get(market) if 'candle_histories' in dir() or 'candle_histories' in locals() else None
                    if hist and len(hist) >= 100:
                        atr_14h = indicators.atr_simplified(hist, min(840, len(hist)))
                except (TypeError, ValueError) as exc:
                    logger.warning("[RECOMMEND_API] ATR calc: %s", exc, exc_info=True)

                if atr_14h and curr > 0:
                    atr_pct = (atr_14h / curr) * 100.0
                    # 손절 = ATR(14h) * 1.5
                    sl_recommend = -(atr_pct * 1.5)

                    # Step Gap도 ATR 비율에 맞춰 조정 (예: ATR의 0.5배)
                    base_step = max(0.5, min(5.0, atr_pct * 0.5))
                else:
                    # Fallback to daily vol
                    if daily_vol_pct > 0.05:
                        base_step = 1.5
                    elif daily_vol_pct < 0.02:
                        base_step = 0.5

                # AI & Volatility based tuning
                # 1. Steps: 변동성이 크면 더 많은 단계로 분할하여 리스크 분산
                rec_steps = 10
                if daily_vol_pct > 0.10: rec_steps = 20
                elif daily_vol_pct > 0.05: rec_steps = 15

                # 2. ATR Mode: 변동성이 매우 크거나 AI 확신이 낮을 때 켜기
                rec_atr_enabled = (daily_vol_pct > 0.08) or (ai_score < 0.4)

                # 3. Martingale: AI가 좋으면 공격적
                rec_martingale = 1.05 if daily_vol_pct > 0.03 else 1.0
                if ai_score > 0.7: rec_martingale = 1.15
                elif ai_score > 0.6: rec_martingale = 1.10
                elif ai_score < 0.4: rec_martingale = 1.0

                # 4. TP: 변동성 + AI Score
                rec_tp = 2.0
                if daily_vol_pct > 0.05: rec_tp = 3.0
                if ai_score > 0.7: rec_tp += 1.0

                # 5. RSI 기반 조정 (LADDER)
                if rsi < 30:
                    rec_steps = min(rec_steps + 5, 25)  # 과매도 → 더 많은 분할매수
                    rec_martingale = min(rec_martingale + 0.05, 1.25)  # 마틴 강화
                elif rsi > 70:
                    rec_steps = max(rec_steps - 3, 5)  # 과매수 → 진입 자제
                    rec_tp = max(rec_tp - 0.5, 1.0)  # 보수적 TP

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

                # RSI 기반 조정 (GAZUA)
                if rsi < 30:
                    rec_tp += 3.0  # 과매도 → 큰 반등 기대
                    rec_sl -= 1.0  # SL 여유
                elif rsi < 40:
                    rec_tp += 1.5
                elif rsi > 70:
                    rec_tp = max(rec_tp - 3.0, 5.0)  # 과매수 → 조기 익절
                    rec_sl = max(rec_sl + 1.5, -3.0)  # SL 타이트

                gazua_params = {"tp": rec_tp, "sl": rec_sl}

        except Exception as e:
            import traceback
            logger.warning(f"[recommendations] {market} params 계산 실패: {e}\n{traceback.format_exc()}")

        # recommended_params 통합 (프론트엔드 호환)
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
            # CONTRARIAN 파라미터 (운영 기본값: TP 15 / SL -50)
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
            # SNIPER: 최저가/최고가 저격 매수/매도
            # [2026-02-02] lookback 기본값 상향 조정 (소강기/하락기 대응)
            #
            # 변동성 범위 (상향 조정):
            # - 초고변동 (>10%): 1~2시간 (단타)
            # - 고변동 (5~10%): 2~4시간 (스윙)
            # - 중변동 (2~5%): 4~8시간 (중기)
            # - 저변동 (<2%): 12~24시간 (하루 최저/최고)

            sn_lookback = 240  # 기본 4시간 (기존 1시간에서 상향)
            sn_threshold = 0.3
            sn_expiry = 360  # 기본 6시간 (기존 1시간에서 상향)
            sn_tp = max(2.0, SNIPER_MIN_TP_PCT)
            sn_sl = SNIPER_MIN_SL_PCT

            # 변동성 기반 lookback/expiry 결정 (전체 상향)
            if daily_vol_pct > 0.10:
                # 초고변동: 1~2시간 (기존 5~15분)
                sn_lookback = 60
                sn_expiry = 120
                sn_threshold = 0.5
                sn_tp = 3.0
                sn_sl = max(SNIPER_MIN_SL_PCT, 2.0)
            elif daily_vol_pct > 0.08:
                # 고변동: 2~3시간 (기존 15~30분)
                sn_lookback = 120
                sn_expiry = 180
                sn_threshold = 0.4
                sn_tp = 2.5
                sn_sl = max(SNIPER_MIN_SL_PCT, 1.8)
            elif daily_vol_pct > 0.05:
                # 중고변동: 3~4시간 (기존 30분~1시간)
                sn_lookback = 180
                sn_expiry = 240
                sn_threshold = 0.35
                sn_tp = 2.2
                sn_sl = max(SNIPER_MIN_SL_PCT, 1.6)
            elif daily_vol_pct > 0.03:
                # 중변동: 4~6시간 (기존 1~3시간)
                sn_lookback = 240
                sn_expiry = 360
                sn_threshold = 0.3
                sn_tp = 2.0
                sn_sl = max(SNIPER_MIN_SL_PCT, 1.5)
            elif daily_vol_pct > 0.02:
                # 저변동: 6~12시간 (기존 3~6시간)
                sn_lookback = 360
                sn_expiry = 720
                sn_threshold = 0.25
                sn_tp = 1.8
                sn_sl = max(SNIPER_MIN_SL_PCT, 1.2)
            else:
                # 초저변동: 12~24시간 (하루 최저/최고 탐색)
                sn_lookback = 720  # 12시간
                sn_expiry = 1440  # 24시간
                sn_threshold = 0.2
                sn_tp = 1.5
                sn_sl = max(SNIPER_MIN_SL_PCT, 1.0)

            # AI 확신도 기반 조정
            if ai_score >= 0.7:
                sn_tp += 0.5
            elif ai_score < 0.4:
                sn_tp = max(sn_tp - 0.5, SNIPER_MIN_TP_PCT)
                sn_sl = max(sn_sl - 0.3, SNIPER_MIN_SL_PCT)

            # RSI 기반 미세 조정
            if rsi < 30:
                sn_threshold = max(sn_threshold - 0.05, 0.1)
                sn_tp += 0.3
            elif rsi > 70:
                sn_threshold += 0.05
                sn_expiry = max(sn_expiry // 2, 15)

            # SNIPER(s): 스윙/롱홀드형으로 늘어지는 값 방지 (단기 순환에 맞게 상한 제한)
            if snipers_mode:
                sn_lookback = min(sn_lookback, 180)   # 최대 3시간
                sn_expiry = min(sn_expiry, 120)       # 최대 2시간
                sn_threshold = max(sn_threshold, 0.25)

            sn_tp, sn_sl = _clamp_sniper_tp_sl(sn_tp, sn_sl)

            recommended_params = {
                "expiry_min": sn_expiry,
                "tp_pct": round(sn_tp, 1),
                "sl_pct": round(sn_sl, 1),
                # Entry (저격 매수)
                "entry_enabled": True,
                "entry_lookback_min": sn_lookback,
                "entry_threshold_pct": round(max(sn_threshold, 0.1), 2),
                # Exit (저격 매도)
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
            # PINGPONG: 박스권 매매 - 변동성 기반
            pp_tp = max(2.0, min(6.0, daily_vol_pct * 80 + 2.0)) if daily_vol_pct else 3.0
            pp_sl = -(pp_tp * 0.7)
            # RSI 기반 조정
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
            # AUTOLOOP: 분할매수 + 익절
            al_tp = max(1.5, min(4.0, daily_vol_pct * 60 + 1.5)) if daily_vol_pct else 2.5
            # AI 확신도 기반 배율
            conf_tier = "high" if ai_score >= 0.8 else ("medium" if ai_score >= 0.6 else "low")
            budget_mult = 1.3 if conf_tier == "high" else (1.0 if conf_tier == "medium" else 0.8)
            # RSI 기반 조정
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

        # [2026-01-30] 전략별 AI 조정 + Regime 적합도 적용
        # Regime 추정: trend + volatility 기반
        est_regime = "NEUTRAL"
        if trend > 1.0 and volatility < 3.0:
            est_regime = "BULL"
        elif trend < -1.0 and volatility > 1.5:
            est_regime = "BEAR"

        ai_adjustment = adjust_ai_score_for_strategy(ai_score, strategy=st, regime=est_regime)
        regime_fit = get_regime_fit(est_regime, strategy=st)

        # AI 조정 점수 (전략-국면 적합도 반영)
        adjusted_score = ai_adjustment.get("adjusted_score", ai_score)
        ai_should_buy = ai_adjustment.get("should_buy", True)

        # 전역 TP/SL 하한 보정
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
            "strategy": strategy_label,  # 요청된 전략 라벨 (SNIPERS 포함)
            "profile": strategy_label,
            "profile_score": round(float(profile_score), 4),
            "profile_features": profile_features,
            "price": float(t.get("trade_price") or 0),
            "change_rate": change_rate_pct,
            "high_price": float(t.get("high_price") or 0),
            "low_price": float(t.get("low_price") or 0),
            "acc_trade_price_24h": float(t.get("acc_trade_price_24h") or 0),
            "active_strategy": active_strategy, # None이면 미사용
            "oma_state": oma_state,
            "ai_score": ai_score,
            "ai_adjusted_score": adjusted_score,  # 전략-국면 조정 점수
            "ai_should_buy": ai_should_buy,       # AI 매수 허용 여부
            "regime": est_regime,                 # 추정 국면
            "regime_fit": regime_fit,             # 전략-국면 적합도
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
    # Strategy-Specific Sorting & Filtering (Strategy Advisor 로직 통합)
    # --------------------------------------------------------
    from app.manager.strategy_graduator import suggest_strategy_for_ai_features

    # st는 이미 위에서 정의됨 (strategy.strip().upper())

    # 각 코인에 대해 추천 전략 계산
    for item in items:
        try:
            rec_strategy, confidence, reason = suggest_strategy_for_ai_features(
                momentum=float(item.get("momentum") or 0),
                volatility=float(item.get("volatility") or 0) / 100.0,  # 퍼센트 -> 비율
                trend=float(item.get("trend") or 0),
                ai_prediction=float(item.get("ai_score") or 0.5),
                rsi=float(item.get("rsi") or 50.0),
            )
            item["recommended_strategy"] = rec_strategy
            item["strategy_confidence"] = round(confidence, 3)
            # suggest_strategy는 5개 코어 전략만 분류한다.
            # (SNIPER/SNIPERS/CONTRARIAN)는 추천 정렬의 1차 기준으로 사용하지 않는다.
            if st in ("LADDER", "LIGHTNING", "GAZUA", "PINGPONG", "AUTOLOOP"):
                item["strategy_match"] = (rec_strategy == st)
            else:
                item["strategy_match"] = False
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("strategy_recommend_router.get_rich_recommendations L918 except", exc_info=True)
            item["recommended_strategy"] = "AUTOLOOP"
            item["strategy_confidence"] = 0.5
            item["strategy_match"] = False

    # 1) 전략 프로필 점수 우선 (핵심)
    # 2) AI 매수 허용 여부
    # 3) 조정 AI 점수 / 국면 적합도
    # 4) 거래대금
    # strategy_match는 보조 정보로만 남기고 정렬 1순위에서 제외한다.
    items.sort(key=lambda x: (
        -float(x.get("profile_score") or 0),            # 프로필 적합도
        -1 if x.get("ai_should_buy") else 0,            # AI 매수 허용 우선
        -float(x.get("ai_adjusted_score") or 0),        # 조정 점수 높은 순
        -float(x.get("regime_fit") or 0),               # 국면 적합도 높은 순
        -float(x.get("acc_trade_price_24h") or 0),      # 거래대금 높은 순
    ))

    # AI should_buy=True인 것만 우선, False도 표시는 함 (경고용)
    # 단, 완전히 제외하지는 않고 should_buy=False인 것은 뒤로
    items = [x for x in items if x.get("ai_score", 0) >= 0.3]  # 최소 임계값 낮춤

    # 전략 일치 코인 통계는 유지하되, 반환은 프로필 점수 상위 N으로 단순화한다.
    matched = [x for x in items if x.get("strategy_match")]
    final_items = items[:n]

    # [2026-02-01] 프로필 기반 스코어 보강(누락 시에만)
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
# G-pre. Background pre-warm helper (hyper_system._strategy_recommend_loop 에서 호출)
# ============================================================
def prewarm_recommendation(system: Any, strategy: str, n: int = 20) -> None:
    """전략 추천 캐시를 백그라운드에서 직렬로 갱신.

    hyper_system._strategy_recommend_loop 에서 전략마다 45초 간격으로 호출된다.
    - 120초 이내 캐시가 살아있으면 건너뜀 (사이클 ~540-610s > 120s 라 매 사이클 갱신됨)
    - 세마포어는 get_rich_recommendations 내부에서 관리하므로 여기서 별도 처리 없음
    - 실행 실패(세마포어 경합)는 다음 사이클에서 재시도
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
        return  # 아직 신선함 - 건너뜀

    # fake request: get_rich_recommendations는 request.app.state.system만 필요
    state = types.SimpleNamespace(system=system)
    app_ns = types.SimpleNamespace(state=state)
    req = types.SimpleNamespace(app=app_ns)
    try:
        get_rich_recommendations(req, strategy=strategy, n=n, min_price=0.0, max_price=0.0)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[RECOMMEND_API] prewarm get_rich_recommendations: %s", exc, exc_info=True)


# ============================================================
# F. Strategy Recommendation (어떤 코인이 어떤 전략에 적합한가)
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
    market: Optional[str] = Query(None, description="특정 마켓 (없으면 전체)"),
    top_n: int = Query(20, description="상위 N개만"),
):
    """
    각 코인에 대해 어떤 전략이 가장 적합한지 추천합니다.

    - LADDER: 고변동성 + 하락 추세 (분할매수)
    - LIGHTNING: 강한 상승 모멘텀 (단타)
    - GAZUA: AI 상승 예측 + 횡보/상승 (추세추종)
    - PINGPONG: 안정적 박스권 (구간매매)
    """
    from app.manager.strategy_graduator import suggest_strategy_for_ai_features

    system = request.app.state.system

    # 0) 거래지원 종료 예정 마켓 조회
    # Note: 바이낸스는 delisting API를 제공하지 않으므로 빈 dict 사용
    delisting_markets = {}

    # 1) 마켓 목록
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

    # 2) 캔들 데이터 가져오기 (병렬)
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
        results = list(ex.map(fetch_candles, markets[:50]))  # 최대 50개
    for m, data in results:
        if data:
            candle_map[m] = data

    # 3) Brain 분석 + 전략 추천
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
        # RSI 계산을 위해 최소 15개 데이터 필요 (length 14 + 1)
        if not candles or len(candles) < 15:
            logger.warning(f"[recommend] {m}: skipped (candles={len(candles) if candles else 0})")
            continue

        try:
            # 가격 히스토리 (최신순 → 과거순으로 변환)
            # 바이낸스 klines가 리스트 형태일 수 있으므로 처리
            if candles and isinstance(candles[0], list):
                # Raw candle format: [timestamp, open, high, low, close, volume, ...]
                prices = [float(c[4]) for c in reversed(candles) if len(c) >= 5]
            else:
                # Dict format (our internal format)
                prices = [float(c.get("trade_price") or 0) for c in reversed(candles)]
            current_price = prices[-1] if prices else 0

            # Brain 분석
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

            # 전략 추천
            strategy, confidence, reason = suggest_strategy_for_ai_features(
                momentum=momentum,
                volatility=volatility,
                trend=trend,
                ai_prediction=ai_score,
                rsi=rsi,
            )

            # 거래지원 종료 경고 체크
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
                "warning": "⚠️ 거래지원 종료 예정" if is_delisting else None,
            })
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
            logger.warning("[RECOMMEND_API] %s: %s", m, e, exc_info=True)
            continue

    # 4) Confidence 순 정렬
    recommendations.sort(key=lambda x: x["confidence"], reverse=True)

    # 5) 전략별 그룹화
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
        "by_strategy": {k: v[:5] for k, v in by_strategy.items()},  # 전략별 상위 5개
        "summary": {
            "LADDER": len(by_strategy.get("LADDER", [])),
            "LIGHTNING": len(by_strategy.get("LIGHTNING", [])),
            "GAZUA": len(by_strategy.get("GAZUA", [])),
            "AUTOLOOP": len(by_strategy.get("AUTOLOOP", [])),
            "PINGPONG": len(by_strategy.get("PINGPONG", [])),
        }
    }
