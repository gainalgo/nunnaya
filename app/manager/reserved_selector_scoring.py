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
    """과거 성과 기반 스코어 조정값 반환.

    Returns:
        -0.5 ~ +0.5 범위의 조정값
        - 양수: 과거 수익 → 보너스
        - 음수: 과거 손실 → 감점 (but 배제 아님)
        - 0: 데이터 없음
    """
    if not pnl_cache:
        return 0.0

    market_data = pnl_cache.get(market)
    if not market_data:
        return 0.0

    # 해당 전략으로 거래한 이력만 확인
    strategy_pnl = market_data.get(strategy.upper(), {})
    if not strategy_pnl:
        # 전략별 데이터 없으면 전체 데이터 사용
        strategy_pnl = market_data.get("_total", {})

    if not strategy_pnl:
        return 0.0

    net_pnl = float(strategy_pnl.get("net_pnl_usdt", 0.0))
    trade_count = int(strategy_pnl.get("trade_count", 0))
    win_rate = float(strategy_pnl.get("win_rate", 0.5))

    if trade_count == 0:
        return 0.0

    # 스코어 계산
    # 1. 승률 기반 (0.5 기준, ±0.2)
    win_bonus = (win_rate - 0.5) * 0.4  # 0.3 → -0.08, 0.7 → +0.08

    # 2. 순수익 기반 (로그 스케일, ±0.3)
    if net_pnl > 0:
        pnl_bonus = min(0.3, math.log1p(net_pnl / 10000) * 0.05)
    elif net_pnl < 0:
        pnl_bonus = max(-0.3, -math.log1p(abs(net_pnl) / 10000) * 0.05)
    else:
        pnl_bonus = 0.0

    # 3. 거래 횟수 가중 (경험치)
    # 거래가 많을수록 데이터 신뢰도 높음
    exp_weight = min(1.0, trade_count / 10.0)  # 10회 이상이면 100% 반영

    total_adjustment = (win_bonus + pnl_bonus) * exp_weight

    # 범위 제한
    return max(-0.5, min(0.5, total_adjustment))

# ============================================================
# PROTECTED — Market PnL Cache Loader
# ============================================================

def _load_market_pnl_cache(system: Any) -> Dict[str, Dict[str, Any]]:
    """시스템의 거래 원장에서 마켓별 성과 데이터 로드.

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

        # 최근 30일 데이터
        ledger = getattr(system, "ledger", None)
        if not ledger:
            return cache

        records = list(ledger.tail(5000))  # 최근 5000개 레코드
        if not records:
            return cache

        now = time.time()
        since_ts = now - (30 * 24 * 3600)  # 30일 전

        aggs = aggregate_fill_pnl(records, since_ts=since_ts, until_ts=now, markets=None)

        # market → strategy 매핑 (OMA_ENTRY / FILL_BUY / FILL_SELL 이벤트 기반)
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
                logger.warning("[reserved_selector_scoring] %s: %s", 'market → strategy 매핑 (OMA_ENTRY / FILL_BUY / FILL_SELL 이벤트 기반) except-> continue', exc, exc_info=True)
                continue

        for market, agg in aggs.items():
            if market not in cache:
                cache[market] = {}

            net_pnl = agg.net_cash_usdt
            trade_count = agg.trade_n

            # 승률 계산 (net_pnl 기반 단순화)
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

            # 전략별 attribution: 동일 마켓은 하나의 전략에만 속함
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
    # [2026-03-09] decide() 정렬: BB 하단 근접도가 핵심 (현재가 ≤ BB하단 → buy)
    # 유동성/스프레드는 체결 가능성, BB 근접도는 진입 타이밍
    _LIQ_CAP = math.log1p(50_000_000_000.0)  # ≈ 24.7
    liq = min(math.log1p(max(0.0, s.vol24_usdt)), _LIQ_CAP)
    spread_pen = math.log1p(max(0.0, s.spread_bps))
    depth = math.log1p(max(0.0, min(s.depth_ask_usdt, s.depth_bid_usdt)))
    trades = math.log1p(float(s.recent_trades or 0))
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # ── BB 하단 근접도 (decide가 price <= bb_lower 를 요구) ──
    bb_entry_score = 0.0
    if s.bb_lower > 0 and s.bb_upper > s.bb_lower and s.price > 0:
        bb_range = s.bb_upper - s.bb_lower
        dist_from_lower = (s.price - s.bb_lower) / bb_range  # 0=하단, 1=상단
        if dist_from_lower <= 0.05:
            bb_entry_score = 15.0   # BB 하단 도달/이탈 → decide()가 즉시 buy
        elif dist_from_lower <= 0.15:
            bb_entry_score = 10.0   # BB 하단 근접 → 곧 buy 가능
        elif dist_from_lower <= 0.30:
            bb_entry_score = 5.0    # BB 하단 접근 중
        elif dist_from_lower >= 0.80:
            bb_entry_score = -8.0   # BB 상단 → decide()가 절대 buy 안 함

    return (
        1.5 * liq
        + 0.7 * depth
        + 0.4 * trades
        - 0.8 * spread_pen
        + 3.0 * bb_entry_score     # BB 근접도가 가장 큰 가중치
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

    # [2026-03-03] 저가/저거래량 실행품질 패널티
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)
    return (1.2 * liq) + (2.5 * vol_score) + (0.6 * depth) - (0.8 * spread_pen) + bb_bonus + eq_pen

def _score_lightning(s: MarketSnapshot, ai_features: Optional[Dict[str, float]] = None) -> float:
    """LIGHTNING v2: 돌파 적합성 + 변동성/BB 인식 스코어링.

    [2026-02-23] ATR sweet spot, BB 위치, 모멘텀 가속도 반영.
    [2026-03-09] decide() 정렬: 모멘텀 급등 + 거래량 서지 추가
    """
    # ── 1. 유동성 기본 ──
    liq = math.log1p(max(0.0, s.vol24_usdt))

    # ── 2. ATR sweet spot (1.0~5% 적정, 돌파 기회 충분 + 과열 아님) ──
    volatility = s.atr_pct if s.atr_pct > 0 else 0.0
    if volatility < 0.8:
        vol_score = -1.0
    elif volatility <= 5.0:
        vol_score = min(4.0, volatility * 0.9)
    else:
        vol_score = max(0.0, 4.0 - (volatility - 5.0) * 0.6)

    # ── 3. BB 위치 (중간~상단 선호 = 돌파 준비 구간) ──
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

    # ── 4. 체결 가능성 ──
    spread_pen = math.log1p(max(0.0, s.spread_bps)) * 0.8
    rr = math.log1p(max(0.0, s.range_ratio_24h))
    trades = math.log1p(float(s.recent_trades or 0))

    # [2026-03-03] 저가/저거래량 실행품질 패널티
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # ── [2026-03-09] 모멘텀/거래량 서지 (decide가 모멘텀 급등 요구) ──
    momentum_score = 0.0
    if ai_features:
        trend = float(ai_features.get("trend", 0.0))
        vol_surge = float(ai_features.get("volume_surge", 0.0))
        # 양의 추세 + 거래량 급증 = 돌파 신호
        if trend > 2.0 and vol_surge > 1.5:
            momentum_score = 10.0     # 강한 돌파 징후
        elif trend > 1.0 and vol_surge > 1.0:
            momentum_score = 5.0      # 돌파 준비
        elif trend > 0.5:
            momentum_score = 2.0      # 약한 상승
        elif trend < -2.0:
            momentum_score = -5.0     # 하락 중 → 돌파 부적합

    # ── [2026-03-18] BB squeeze detection (좁은 밴드 = 돌파 임박) ──
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
        + 2.5 * momentum_score        # 모멘텀이 핵심
        + eq_pen
        + 1.5 * squeeze_score
        + 1.5 * depth_score
    )

def _score_sniper(s: MarketSnapshot, ai_features: Dict[str, float], rsi: float) -> float:
    """SNIPER v2 후보 점수: 반등 확률 기반 스코어링.

    [2026-02-23] ATR/BB 실데이터 기반 전면 개편
    - BB 하단 근접도 → 구조적 바닥 신호 (가중치 0.25)
    - RSI 과매도 → 확률적 반등 구간 (0.15)
    - 거래량 반전 → 매수세 유입 신호 (0.20)
    - 추세 초기 전환 → EMA cross 초기 (0.15)
    - cross_exchange 연동은 별도 모듈 (0.15 외부)
    - 체결 가능성(depth/spread) → 실행 feasibility (0.10)
    """
    # ── 1. 유동성 기본 (최소 기준만) ──
    liq = math.log1p(max(0.0, s.vol24_usdt))
    if s.vol24_usdt > 1_000_000_000:
        liq *= 0.3
    elif s.vol24_usdt > 100_000_000:
        liq *= 0.6

    # ── 2. 변동성: 실제 ATR% 우선, fallback ai_features ──
    volatility = s.atr_pct if s.atr_pct > 0 else float(ai_features.get("volatility", 0.0))

    # 변동성 sweet spot (1.5~6%): 저격 기회 충분 + 과열 아님
    if volatility < 0.5:
        vol_score = -2.0                                    # 너무 조용
    elif volatility <= 6.0:
        vol_score = min(4.0, volatility * 0.8)              # sweet spot
    else:
        vol_score = max(0.0, 4.0 - (volatility - 6.0) * 0.5)  # 과열 감점

    # ── 3. BB 하단 근접도 (구조적 바닥 신호) ──
    bb_proximity = 0.0
    if s.bb_lower > 0 and s.bb_upper > s.bb_lower and s.price > 0:
        bb_range = s.bb_upper - s.bb_lower
        dist_from_lower = (s.price - s.bb_lower) / bb_range  # 0=하단, 1=상단
        if dist_from_lower <= 0.15:
            bb_proximity = 5.0          # BB 하단 15% 이내 → 최고점
        elif dist_from_lower <= 0.30:
            bb_proximity = 3.0          # BB 하단 30% 이내 → 양호
        elif dist_from_lower >= 0.85:
            bb_proximity = -3.0         # BB 상단 근접 → 고점 매수 위험

    # BB width 보너스: 적정 변동성 밴드
    bb_width_bonus = 0.0
    if s.bb_width_pct > 0:
        if 2.0 <= s.bb_width_pct <= 8.0:
            bb_width_bonus = min(2.0, s.bb_width_pct * 0.3)
        elif s.bb_width_pct > 12.0:
            bb_width_bonus = -1.0       # 과도한 확장 → 위험

    # ── 4. RSI 과매도 (반등 확률 구간) ──
    rsi_bonus = 0.0
    if rsi < 25:
        rsi_bonus = 4.0                 # 극단 과매도
    elif rsi < 30:
        rsi_bonus = 3.0
    elif rsi < 40:
        rsi_bonus = max(0.0, (40.0 - rsi) / 10.0) * 2.0

    # ── 5. 추세 방향 (초기 반전 우대) ──
    trend = float(ai_features.get("trend", 0.0))

    uptrend_bonus = 0.0
    if trend > 0:
        uptrend_bonus = min(3.0, (trend / 5.0) * 2.5)

    # 과매도 + 추세 반전 초기 = 최적 저격 타이밍
    reversal_bonus = 0.0
    if rsi < 35 and trend > -1.0 and trend < 2.0:
        reversal_bonus = 2.5            # 바닥 반등 초기

    # 강한 하락 추세 패널티 (떨어지는 칼날, 단계적)
    # [FIX #11] RSI 극과매도(< 25)일 때 패널티 50% 감경 — 캡철레이션 바닥 기회 보존
    falling_knife_pen = 0.0
    if trend < -5.0:
        falling_knife_pen = 10.0
    elif trend < -3.0:
        falling_knife_pen = 5.0
    elif trend < -2.0:
        falling_knife_pen = 2.0
    if rsi < 25 and falling_knife_pen > 0:
        falling_knife_pen *= 0.5

    # ── 6. 거래량 반전 신호 ──
    volume_surge = float(ai_features.get("volume_surge", 0.0))
    vol_reversal = min(2.0, volume_surge * 0.5) if volume_surge > 1.0 else 0.0

    # ── 7. 체결 가능성 (실행 feasibility) ──
    spread_pen = math.log1p(max(0.0, s.spread_bps)) * 0.8
    depth_score = 0.0
    min_depth = min(s.depth_ask_usdt, s.depth_bid_usdt)
    if min_depth > 50_000_000:          # 5천만 이상
        depth_score = 1.0
    elif min_depth > 10_000_000:
        depth_score = 0.5

    # ── 8. 24h range (변동폭 보너스) ──
    range_bonus = math.log1p(max(0.0, s.range_ratio_24h * 100))

    # [2026-03-03] 저가/저거래량 실행품질 패널티
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # 2026-03-10: 고래 활동 가감점 (SNIPER = 고래 매도 → 급락 → 저격 기회)
    whale_bonus = 0.0
    try:
        _wd = get_whale_detector()
        _vs = volume_surge + 1.0  # volume_surge를 spike_ratio로 변환
        _pc = trend * 1.5
        _wi = _wd.detect(_vs, 1.0, _pc, market=s.market)
        whale_bonus = _wd.get_strategy_score_bonus(_wi, "SNIPER")
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[reserved_selector_scoring] %s: %s", '2026-03-10: 고래 활동 가감점 (SNIPER = 고래 매도 → 급락 → 저격 기회)', exc, exc_info=True)

    # ── 최종 반등 확률 스코어 ──
    return (
        0.4 * liq
        + 2.5 * bb_proximity           # 구조적 바닥 (0.25)
        + 1.5 * bb_width_bonus
        + 2.0 * rsi_bonus              # 과매도 (0.15)
        + 2.0 * uptrend_bonus           # 추세 초기 (0.15)
        + 2.5 * reversal_bonus          # 반전 초기
        + 2.0 * vol_reversal            # 거래량 반전 (0.20)
        + 1.5 * vol_score               # 변동성 적정
        + 1.0 * depth_score             # 체결 가능성 (0.10)
        + 1.2 * range_bonus
        - spread_pen
        - falling_knife_pen
        + eq_pen                        # 실행품질 (저가/저거래량)
        + whale_bonus                   # 고래 활동
    )

def _calc_sniper_params(
    s: MarketSnapshot,
    ai_features: Dict[str, float],
    rsi: float,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """SNIPER v2 파라미터 자동 계산.

    [2026-02-23] ATR/BB 기반 정밀 파라미터 + 2-Phase 진입 지원.
    - ATR% 기반 lookback/threshold (변동성 적응)
    - BB upper → TP 타겟 참조
    - Probe/Confirm 2단계 진입 파라미터
    - Time-stop 파라미터 (횡보 탈출)
    """
    # 실제 ATR% 우선, fallback ai_features
    volatility = s.atr_pct if s.atr_pct > 0 else float(ai_features.get("volatility", 2.0))
    range_24h = s.range_ratio_24h * 100

    # ATR 기반 Lookback (변동성 높을수록 짧게)
    if volatility > 5.0:
        lookback_min = 240      # 4시간
    elif volatility > 3.0:
        lookback_min = 360      # 6시간
    elif volatility > 1.5:
        lookback_min = 720      # 12시간
    else:
        lookback_min = 1440     # 24시간

    # 실제 고가/저가 조회
    highlow_data: Dict[str, float] = {}
    if session is not None:
        highlow_data = fetch_highlow_for_lookback(session, s.market, lookback_min)

    actual_high = highlow_data.get("high", 0.0)
    actual_low = highlow_data.get("low", 0.0)
    actual_range_pct = highlow_data.get("range_pct", 0.0)
    distance_from_low = highlow_data.get("distance_from_low_pct", 0.0)

    # ── Threshold: ATR 기반 우선, highlow fallback ──
    if volatility > 0.5:
        # ATR의 30~40%를 threshold로 (변동성 적응)
        threshold_pct = max(0.3, min(2.5, volatility * 0.35))
    elif actual_range_pct > 0:
        threshold_pct = max(0.3, min(2.5, actual_range_pct * 0.20))
    else:
        threshold_pct = max(0.3, min(2.0, range_24h * 0.15))

    # ── TP: BB upper 거리 우선, highlow fallback ──
    trend = float(ai_features.get("trend", 0.0))

    # BB upper까지 거리를 TP 참조값으로
    bb_tp = 0.0
    if s.bb_upper > 0 and s.price > 0:
        bb_tp = (s.bb_upper - s.price) / s.price * 100.0

    # [2026-03-18] TP/SL: 기본 하한은 낮게, 실제 조절은 UI Guards에서
    # TP: 0.8% ~ 15%, SL: 1.5% ~ 6% — trailing이 이윤을 키우는 구조
    if bb_tp > 1.0:
        base_tp = max(0.8, min(15.0, bb_tp * 0.80))
    elif actual_range_pct > 0:
        base_tp = max(0.8, min(15.0, actual_range_pct * 0.45))
    else:
        base_tp = max(0.8, min(8.0, range_24h * 0.45))

    # 추세 기반 TP 조정
    if trend > 3.0:
        base_tp = min(base_tp * 1.5, 15.0)
    elif trend < -3.0:
        base_tp = max(base_tp * 0.7, 0.8)

    # 저점 근처 + 상승 추세 보너스
    if distance_from_low < 15 and trend > 0:
        base_tp = min(base_tp * 1.4, 15.0)

    # RSI 과매도 보너스 (강한 반등 기대)
    if rsi < 30 and trend > -2.0:
        base_tp = min(base_tp + 2.5, 15.0)
    elif rsi < 40 and trend > 0:
        base_tp = min(base_tp + 1.5, 12.0)

    # ── SL: ATR 기반 동적 (변동성 높으면 넓게) ──
    if volatility > 0.5:
        base_sl = max(1.5, min(6.0, volatility * 1.0))
    else:
        base_sl = max(1.5, min(5.0, base_tp * 0.6))

    # Trail: TP의 30% (더 넓게 잡아 추세 유지)
    trail_dist = max(0.8, base_tp * 0.30)

    # ── Time-stop: ATR 기반 (변동성 높으면 짧은 대기) ──
    if volatility > 4.0:
        time_stop_min = 30          # 고변동: 30분
    elif volatility > 2.0:
        time_stop_min = 60          # 중변동: 1시간
    else:
        time_stop_min = 120         # 저변동: 2시간

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
        # [2026-03-07] rsi_entry_max: 셀렉터↔플러그인 RSI 임계값 동기화
        # 셀렉터 RSI<55 허용 + 플러그인 grace zone(+15%) → 42*1.15≈48.3 실질 허용
        "rsi_entry_max": 42.0,
        "rsi_entry_enabled": True,
        "rsi_exit_enabled": True,
        "use_limit": True,
        "fallback_to_market": True,
        "expiry_min": max(30, lookback_min // 2),
        "trend_protect_enabled": True,
        "ema_cross_enabled": False,
        # 실제 고가/저가 정보
        "actual_high": actual_high,
        "actual_low": actual_low,
        "actual_range_pct": round(actual_range_pct, 2),
        "distance_from_low_pct": round(distance_from_low, 2),
        # [2026-02-23] SNIPER v2 파라미터
        "sniper_schema_ver": 2,
        "probe_ratio": 0.3,             # Probe 진입 비율 (30%)
        "confirm_ratio": 0.7,           # Confirm 진입 비율 (70%)
        "watch_sec": 180,               # Phase 0 관측 시간 (초)
        "confirm_window_sec": 300,      # Probe→Confirm 확인 윈도우 (5분)
        "time_stop_min": time_stop_min, # 횡보 타임아웃 (분)
        "atr_pct": round(volatility, 2),
        "bb_upper": round(s.bb_upper, 2) if s.bb_upper > 0 else 0.0,
        "bb_lower": round(s.bb_lower, 2) if s.bb_lower > 0 else 0.0,
        "bb_middle": round(s.bb_middle, 2) if s.bb_middle > 0 else 0.0,
        # [2026-03-02] DCA 물타기 설정 (UI 조정 가능)
        "dca_step_pct": float(os.getenv("SNIPER_DCA_STEP_PCT", 0.2)),
        "dca_add_ratio": float(os.getenv("SNIPER_DCA_ADD_RATIO", 0.5)),
        "dca_max_depth_pct": float(os.getenv("SNIPER_DCA_MAX_DEPTH_PCT", 1.0)),
    }

def _score_gazua(s: MarketSnapshot, ai_features: Optional[Dict[str, float]] = None) -> float:
    # [2026-03-09] decide() 정렬: AI ≥ 0.65 + 추세 상승이 핵심 진입 조건
    liq = math.log1p(max(0.0, s.vol24_usdt))
    rr = math.log1p(max(0.0, s.range_ratio_24h))
    spread_pen = math.log1p(max(0.0, s.spread_bps))
    depth = math.log1p(max(0.0, min(s.depth_ask_usdt, s.depth_bid_usdt)))
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # ── AI 점수 + 추세 (decide가 ai >= 0.65 + RS 상대강도 요구) ──
    ai_entry_score = 0.0
    trend_score = 0.0
    if ai_features:
        trend = float(ai_features.get("trend", 0.0))
        vol_surge = float(ai_features.get("volume_surge", 0.0))
        # 강한 상승 추세 = GAZUA 적합
        if trend > 3.0:
            trend_score = 10.0
        elif trend > 1.5:
            trend_score = 5.0
        elif trend > 0:
            trend_score = 2.0
        elif trend < -2.0:
            trend_score = -8.0     # 하락장 → GAZUA 부적합
        # 거래량 급증 보너스
        if vol_surge > 2.0:
            trend_score += 3.0

    return (
        1.8 * liq
        + 0.5 * rr
        + 0.5 * depth
        - 0.7 * spread_pen
        + 2.5 * trend_score            # 추세/AI가 핵심
        + eq_pen
    )

def _score_autoloop(s: MarketSnapshot, rsi_macd: Optional[Dict[str, Any]] = None) -> float:
    # [2026-03-09] decide() 정렬: RSI ≤ 28 + MACD 반전 상승이 핵심 진입 조건
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

    # ── RSI/MACD 진입 적합도 (decide가 rsi<=28 + macd_turning_up 요구) ──
    rsi_entry_score = 0.0
    macd_entry_score = 0.0
    if rsi_macd:
        rsi = float(rsi_macd.get("rsi") or 50.0)
        macd_hist = float(rsi_macd.get("macd_hist") or 0.0)
        macd_hist_prev = float(rsi_macd.get("macd_hist_prev") or 0.0)
        # RSI 과매도 구간 (decide: rsi <= rsi_buy=28)
        if rsi <= 28:
            rsi_entry_score = 12.0    # 즉시 buy 가능 구간
        elif rsi <= 35:
            rsi_entry_score = 6.0     # 근접 — 곧 진입 가능
        elif rsi <= 42:
            rsi_entry_score = 2.0     # 접근 중
        elif rsi >= 65:
            rsi_entry_score = -5.0    # 과매수 → decide 절대 buy 안 함
        # MACD 반전 상승 (decide: macd_turning_up = hist > hist_prev)
        if macd_hist > macd_hist_prev:
            macd_entry_score = 5.0    # 반전 상승 확인
        elif macd_hist < macd_hist_prev and macd_hist < 0:
            macd_entry_score = -3.0   # 하락 가속 → buy 불가

    return (
        1.8 * liq
        + 0.5 * rr
        - 0.25 * spread_pen
        + range_bonus
        + 2.5 * rsi_entry_score       # RSI 과매도가 핵심
        + 2.0 * macd_entry_score       # MACD 반전 확인
        + eq_pen
    )

def _score_contrarian(s: MarketSnapshot, rsi_macd: Optional[Dict[str, Any]] = None) -> float:
    """CONTRARIAN 전략 적합성 점수.

    [2026-03-09] decide() 정렬: RSI 과매도 반전 + 역행 신호가 핵심
    """
    liq = math.log1p(max(0.0, s.vol24_usdt))
    rr = math.log1p(max(0.0, s.range_ratio_24h))
    spread_pen = math.log1p(max(0.0, s.spread_bps))
    depth = math.log1p(max(0.0, min(s.depth_ask_usdt, s.depth_bid_usdt)))
    eq_pen = _execution_quality_penalty(s.price, s.vol24_usdt, s.spread_bps)

    # ── RSI 과매도 (decide가 RSI < 50 + 반전 요구) ──
    rsi_score = 0.0
    if rsi_macd:
        rsi = float(rsi_macd.get("rsi") or 50.0)
        if rsi < 30:
            rsi_score = 10.0      # 극단 과매도 → 역발상 최적
        elif rsi < 40:
            rsi_score = 6.0       # 과매도 구간
        elif rsi < 50:
            rsi_score = 2.0       # CONTRARIAN 진입 가능
        elif rsi >= 60:
            rsi_score = -5.0      # 과매수 → 역발상 부적합

    return (
        1.5 * liq
        + 0.8 * rr
        + 0.6 * depth
        - 0.5 * spread_pen
        + 2.5 * rsi_score              # RSI 과매도가 핵심
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
    """CONTRARIAN 전략 적합성 점수 (실시간 역행 스캐너 연동).

    Args:
        s: 마켓 스냅샷
        contrarian_score: 역행 스코어 (0-3)
        contrarian_data: 역행 스캐너 데이터 (volume_spike, tf_score 등)
        coin_ret_24h: 코인 24h 수익률 (%)
        btc_ret_24h: BTC 24h 수익률 (%) — 내부 폴백 RS 계산용
        rsi_macd: RSI/MACD 캐시 데이터

    Returns:
        최종 점수 (높을수록 좋음)
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

    # 내부 폴백 RS: BTC 대비 코인 역행성 (음수 = BTC보다 약세 = CONTRARIAN 선호)
    rs_actual = coin_ret_24h - btc_ret_24h
    if rs_actual < -5.0:
        bonus += 15.0   # 강한 역행 → 최적
    elif rs_actual < -2.0:
        bonus += 8.0    # 중간 역행
    elif rs_actual < 0:
        bonus += 3.0    # 약한 역행
    elif rs_actual > 5.0:
        bonus -= 10.0   # BTC 대비 강세 → CONTRARIAN 부적합

    # ── [2026-03-18] contrarian_data 세부 필드 스코어링 ──
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
# Multi-Stage Confidence Scoring (전략별 다단계 신뢰도)
# [2026-03-08] SNIPER compute_scope_score 6-stage 패턴을 전 전략 확대
# 각 조건이 독립 점수 → 합산 → 0~100 스케일 confidence
# ============================================================

def _confidence_pingpong(
    s: MarketSnapshot,
    ai_features: Dict[str, float],
    rsi_macd: Dict[str, Any],
) -> Dict[str, Any]:
    """PINGPONG 다단계 신뢰도: 빠른 회전 적합성 (max 90).

    핵심: 높은 유동성 + 좁은 스프레드 + 적정 변동성 + 활발한 거래.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: 유동성 (거래대금 50억 이상 = 만점, max 20)
    vol_b = s.vol24_usdt / 1e9  # 십억 단위
    s1 = min(20.0, max(0.0, vol_b * 4.0))  # 5B → 20
    stages["liquidity"] = round(s1, 1)
    conf += s1

    # Stage 2: 스프레드 (5bps 이하 = 만점, max 20)
    sp = max(0.0, s.spread_bps)
    s2 = max(0.0, 20.0 - sp * 0.8)  # 0bps→20, 25bps→0
    stages["spread"] = round(s2, 1)
    conf += s2

    # Stage 3: 호가 깊이 (양쪽 $50K 이상 = 만점, max 15)
    min_depth = min(s.depth_ask_usdt, s.depth_bid_usdt)
    s3 = min(15.0, max(0.0, min_depth / 1e7 * 3.0))  # 50M → 15
    stages["depth"] = round(s3, 1)
    conf += s3

    # Stage 4: 변동성 범위 TP 달성 가능성 (range 3~7% = 최적, max 15)
    range_pct = s.range_ratio_24h * 100.0
    if 3.0 <= range_pct <= 7.0:
        s4 = 15.0
    elif 1.5 <= range_pct < 3.0:
        s4 = range_pct * 5.0  # 1.5%→7.5, 3%→15
    elif range_pct > 7.0:
        s4 = max(5.0, 15.0 - (range_pct - 7.0) * 2.0)
    else:
        s4 = max(0.0, range_pct * 3.0)
    stages["volatility_range"] = round(s4, 1)
    conf += s4

    # Stage 5: RSI 중립대 (40~60 = 최적 진입, max 10)
    rsi = float(rsi_macd.get("rsi", 50.0))
    if 40.0 <= rsi <= 60.0:
        s5 = 10.0
    elif 30.0 <= rsi < 40.0 or 60.0 < rsi <= 70.0:
        s5 = 5.0
    else:
        s5 = 0.0
    stages["rsi_neutral"] = round(s5, 1)
    conf += s5

    # Stage 6: 거래 활동성 (최근 체결 20건 이상, max 10)
    trades = float(s.recent_trades or 0)
    s6 = min(10.0, max(0.0, trades * 0.5))  # 20 → 10
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
    """AUTOLOOP 다단계 신뢰도: 중속 회전 적합성 (max 90).

    핵심: 유동성 + 적정 변동 범위(1~5%) + 추세 중립 + 스프레드 허용범위 넓음.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: 유동성 (거래대금, max 20)
    vol_b = s.vol24_usdt / 1e9
    s1 = min(20.0, max(0.0, vol_b * 4.0))
    stages["liquidity"] = round(s1, 1)
    conf += s1

    # Stage 2: 변동 범위 (1~5% 최적, max 20)
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

    # Stage 3: 스프레드 (AUTOLOOP은 30bps까지 허용, max 15)
    sp = max(0.0, s.spread_bps)
    s3 = max(0.0, 15.0 - sp * 0.5)  # 0→15, 30→0
    stages["spread"] = round(s3, 1)
    conf += s3

    # Stage 4: 추세 중립 (|trend| < 0.3 = 최적, max 15)
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

    # Stage 5: RSI 사이클 여지 (35~65 = 사이클 가능, max 10)
    rsi = float(rsi_macd.get("rsi", 50.0))
    if 35.0 <= rsi <= 65.0:
        s5 = 10.0
    elif 25.0 <= rsi < 35.0 or 65.0 < rsi <= 75.0:
        s5 = 5.0
    else:
        s5 = 0.0
    stages["rsi_cycle_room"] = round(s5, 1)
    conf += s5

    # Stage 6: 호가 깊이 (max 10)
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
    """LADDER 다단계 신뢰도: 그리드/DCA 적합성 (max 90).

    핵심: 횡보 + 적정 변동성(ATR 2~6%) + 고점 미근접 + 안정적 거래량.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: 횡보 (|trend| < 0.15 = 최적, max 22)
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

    # Stage 2: 적정 변동성 ATR 2~6% (max 20)
    vol_pct = s.atr_pct if s.atr_pct > 0 else float(ai_features.get("volatility", 0.0))
    if 2.0 <= vol_pct <= 6.0:
        s2 = 20.0
    elif 1.0 <= vol_pct < 2.0:
        s2 = vol_pct * 10.0  # 1%→10, 2%→20
    elif vol_pct > 6.0:
        s2 = max(5.0, 20.0 - (vol_pct - 6.0) * 3.0)
    else:
        s2 = max(0.0, vol_pct * 5.0)
    stages["volatility_grid"] = round(s2, 1)
    conf += s2

    # Stage 3: 유동성 (max 15)
    vol_b = s.vol24_usdt / 1e9
    s3 = min(15.0, max(0.0, vol_b * 5.0))
    stages["liquidity"] = round(s3, 1)
    conf += s3

    # Stage 4: BB 밴드폭 (2~10% = 그리드 최적, max 13)
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

    # Stage 5: 스프레드 (max 10)
    sp = max(0.0, s.spread_bps)
    s5 = max(0.0, 10.0 - sp * 0.4)
    stages["spread"] = round(s5, 1)
    conf += s5

    # Stage 6: 모멘텀 안정 (|momentum| < 0.5 = 안정, max 10)
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
    """LIGHTNING 다단계 신뢰도: 변동성 돌파 적합성 (max 90).

    핵심: 강한 모멘텀 + 거래량 급증 + 상승 추세 + ATR 적정 + BB 돌파 준비.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: 모멘텀 가속 (momentum > 1.0 = 강한 상승, max 22)
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
        s1 = max(0.0, 5.0 + mom * 5.0)  # 음수 모멘텀 감점
    stages["momentum_acceleration"] = round(min(22.0, s1), 1)
    conf += stages["momentum_acceleration"]

    # Stage 2: 거래량 급증 (volume_surge > 1.0 = 돌파 진위, max 20)
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

    # Stage 3: 상승 추세 (trend > 0 = 방향성, max 18)
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

    # Stage 4: ATR 적정 (1.5~5%, max 12)
    vol_pct = s.atr_pct if s.atr_pct > 0 else float(ai_features.get("volatility", 0.0))
    if 1.5 <= vol_pct <= 5.0:
        s4 = 12.0
    elif vol_pct > 5.0:
        s4 = max(3.0, 12.0 - (vol_pct - 5.0) * 2.0)
    else:
        s4 = max(0.0, vol_pct * 6.0)
    stages["atr_sweetspot"] = round(s4, 1)
    conf += s4

    # Stage 5: BB 돌파 준비 (중간~상단, max 10)
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

    # Stage 6: MACD 상승 전환 (max 8)
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
    """GAZUA 다단계 신뢰도: 장기 상승 보유 적합성 (max 90).

    핵심: 강한 상승 모멘텀 + BTC 대비 상대강도 + 추세 방향 + 유동성.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: 상승 추세 (trend > 0.2 = 확실한 상승, max 22)
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

    # Stage 2: BTC 상대강도 RS (코인수익-BTC수익, max 20)
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

    # Stage 3: 모멘텀 (momentum > 0.5 = 매수세 확인, max 18)
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

    # Stage 4: 유동성 (장기 보유 시 청산 가능성, max 12)
    vol_b = s.vol24_usdt / 1e9
    s4 = min(12.0, max(0.0, vol_b * 3.0))
    stages["liquidity"] = round(s4, 1)
    conf += s4

    # Stage 5: MACD 상승 (bullish = 추세 확인, max 10)
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

    # Stage 6: 거래량 급증 (매수세 동반 확인, max 8)
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
    """CONTRARIAN 다단계 신뢰도: 역발상 적합성 (max 90).

    핵심: 시장 하락 중 역행 + RSI 과매도 + 유동성 충분 + 추세 반전 신호.
    """
    conf = 0.0
    stages: Dict[str, float] = {}

    # Stage 1: Contrarian Score (외부 스캐너 기반, max 25)
    s1 = min(25.0, max(0.0, float(contrarian_score) * 8.0))
    stages["contrarian_signal"] = round(s1, 1)
    conf += s1

    # Stage 2: RSI 과매도 (< 40 = 반전 기회, max 20)
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

    # Stage 3: 유동성 (역행 매매 시 슬리피지 방지, max 15)
    vol_b = s.vol24_usdt / 1e9
    s3 = min(15.0, max(0.0, vol_b * 5.0))
    stages["liquidity"] = round(s3, 1)
    conf += s3

    # Stage 4: 하락 추세에서 반전 (trend < 0 + momentum > 0 = 반등, max 15)
    trend = float(ai_features.get("trend", 0.0))
    mom = float(ai_features.get("momentum", 0.0))
    if trend < -0.1 and mom > 0.3:
        s4 = 15.0  # 하락 중 반등 모멘텀
    elif trend < -0.1 and mom > 0:
        s4 = 8.0
    elif trend < 0:
        s4 = 4.0
    else:
        s4 = 0.0
    stages["reversal_signal"] = round(s4, 1)
    conf += s4

    # Stage 5: 스프레드 (max 8)
    sp = max(0.0, s.spread_bps)
    s5 = max(0.0, 8.0 - sp * 0.32)
    stages["spread"] = round(s5, 1)
    conf += s5

    # Stage 6: MACD 반전 (bearish→neutral or histogram 상승, max 7)
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
    """LADDER ICAG v3 적합성 점수 (AI 피처 + ATR/BB 기반).

    선호 조건 (ICAG 그리드 트레이딩):
    - 적정 변동성 (ATR 2~6% 또는 AI volatility 1.5~5%) → 그리드 간격에 최적
    - 횡보 또는 약한 하락 (|trend| < 0.3) → 평균회귀 기회
    - 강한 상승/하락 → 감점 (그리드 트레이딩 부적합)
    """
    base_score = _score_ladder(s)

    trend = float(ai_features.get("trend", 0.0))
    # Use real ATR% if enriched, otherwise fallback to AI volatility feature
    volatility = s.atr_pct if s.atr_pct > 0 else float(ai_features.get("volatility", 0.0))

    ai_bonus = 0.0

    # 적정 변동성 선호 (bell curve: 1.5~5% sweet spot)
    if volatility < 0.5:
        ai_bonus -= 5.0                     # 너무 조용 → 기회 없음
    elif volatility <= 5.0:
        ai_bonus += min(15.0, volatility * 4.0)  # sweet spot
    else:
        ai_bonus += max(0.0, 15.0 - (volatility - 5.0) * 2.0)  # 과열 → 감소

    # 횡보/약한 하락 선호 (ICAG 평균회귀 핵심)
    abs_trend = abs(trend)
    # [2026-03-08] Guard: trend=0 AND volatility=0은 데이터 미확인 → 보너스 차단
    if abs_trend == 0.0 and volatility == 0.0:
        pass                                # 데이터 없음 → 보너스 없음
    elif abs_trend < 0.15:
        ai_bonus += 10.0                    # 횡보 → 최고
    elif abs_trend < 0.3:
        ai_bonus += 5.0                     # 약한 추세 → 양호
    elif abs_trend < 0.5:
        pass                                # 중간 추세 → 보통
    else:
        ai_bonus -= 8.0 * abs_trend         # 강한 추세 → 감점

    # 약한 하락은 약간의 보너스 (DCA 진입 기회)
    if -0.4 < trend < -0.1:
        ai_bonus += 3.0

    return base_score + ai_bonus

def _score_lightning_ai(s: MarketSnapshot, ai_features: Dict[str, float], *, price_change_pct: float = 0.0) -> float:
    """LIGHTNING v2 AI 피처 기반 스코어링.

    [2026-02-23] 모멘텀 가속도 + 거래량 서지 + 추세 방향 반영.
    """
    base_score = _score_lightning(s, ai_features=ai_features)

    momentum = float(ai_features.get("momentum", 0.0))
    volume_surge = float(ai_features.get("volume_surge", 0.0))
    volatility = float(ai_features.get("volatility", 0.0))
    trend = float(ai_features.get("trend", 0.0))

    ai_bonus = 0.0

    # 모멘텀 가속도: 상승 가속 시 강한 보너스
    if momentum > 1.0 and trend > 0:
        ai_bonus += min(25.0, momentum * 8.0)
    elif momentum > 1.0:
        ai_bonus += min(15.0, momentum * 5.0)

    # 거래량 서지: 돌파 진위 판별 핵심
    if volume_surge > 1.0:
        ai_bonus += min(20.0, volume_surge * 12.0)
    elif volume_surge > 0.5:
        ai_bonus += min(10.0, volume_surge * 8.0)

    # 적정 변동성 보너스 (과열 감점)
    if 1.5 <= volatility <= 5.0:
        ai_bonus += min(10.0, volatility * 2.5)
    elif volatility > 6.0:
        ai_bonus -= min(8.0, (volatility - 6.0) * 2.0)

    # 하락 모멘텀 감점
    if momentum < -1.0:
        ai_bonus -= min(15.0, abs(momentum) * 5.0)
    # 강한 하락 추세 감점
    if trend < -3.0:
        ai_bonus -= min(10.0, abs(trend) * 2.0)

    # 2026-03-10: 고래 활동 가감점 (LIGHTNING = 변동성 전략, 고래 활동 자체가 기회)
    try:
        _wd = get_whale_detector()
        # volume_surge를 spike_ratio로 활용 (2배 이상이면 고래 의심)
        _vs = volume_surge + 1.0  # volume_surge=1.0 → spike_ratio=2.0
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
    """GAZUA V2 전략 적합성 점수 (모멘텀 + BTC상대강도 + AI).

    V2 핵심: 강한 상승 모멘텀 + 독자 매수세(RS) + AI 신뢰도 기반 선별.
    RS는 코인 24h 수익률 − BTC 24h 수익률로 실계산 (양수=BTC 대비 강세).
    """
    base_score = _score_gazua(s, ai_features=ai_features)

    momentum = float(ai_features.get("momentum", 0.0))
    volume_surge = float(ai_features.get("volume_surge", 0.0))

    # 1. 모멘텀 배율
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

    # 2. 거래량 보너스
    vol_bonus = min(max(0.0, volume_surge * 0.10), 0.15)

    # 3. BTC 실제 상대강도 (코인 24h 수익률 − BTC 24h 수익률)
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

    # 2026-03-10: 고래 활동 가감점 (GAZUA = 고래 매수 시 장기 호재)
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
