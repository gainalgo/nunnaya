# ============================================================
# File: app/manager/strategy_graduator.py
# Autocoin OS v3-H — Strategy Graduation System
# ------------------------------------------------------------
# 목적:
# - 코인 상태 변화 감지 → 자동 전략 전환
# - LIGHTNING (발굴) → 수익 확정 → PINGPONG (안정화)
# - LADDER (저점 매집) → 반등 시 → GAZUA (홀드)
# ============================================================

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class GraduationPath(Enum):
    """전략 졸업 경로."""
    LIGHTNING_TO_PINGPONG = "LIGHTNING→PINGPONG"
    LADDER_TO_GAZUA = "LADDER→GAZUA"
    GAZUA_TO_PINGPONG = "GAZUA→PINGPONG"
    PINGPONG_TO_AUTOLOOP = "PINGPONG→AUTOLOOP"
    DEMOTION = "DEMOTION"  # 하향 전환


@dataclass
class GraduationCondition:
    """졸업 조건."""
    min_roi_pct: float = 0.0
    min_trades: int = 0
    min_sells: int = 0
    min_age_hours: float = 0.0
    
    # AI 피처 조건
    min_momentum: Optional[float] = None
    max_volatility: Optional[float] = None
    min_trend: Optional[float] = None
    
    # 가격 조건
    price_above_avg_pct: Optional[float] = None  # 평균 매수가 대비
    
    # 추가 조건
    requires_profit_lock: bool = False  # 수익 확정 필요


@dataclass
class MarketContext:
    """마켓 컨텍스트 (졸업 판단용)."""
    market: str
    current_strategy: str
    
    # 성과 데이터
    roi_pct: float = 0.0
    trade_count: int = 0
    sell_count: int = 0
    net_cash_usdt: float = 0.0
    
    # 시간 데이터
    active_age_hours: float = 0.0
    
    # 가격 데이터
    current_price: float = 0.0
    avg_buy_price: float = 0.0
    
    # AI 피처
    momentum: float = 0.0
    volatility: float = 0.0
    trend: float = 0.0
    ai_prediction: float = 0.5
    rsi: float = 50.0
    
    # 포지션
    position_qty: float = 0.0
    position_value_usdt: float = 0.0


@dataclass
class GraduationDecision:
    """졸업 판단 결과."""
    market: str
    from_strategy: str
    to_strategy: Optional[str]
    path: Optional[GraduationPath]
    should_graduate: bool
    reason: str
    confidence: float  # 0.0 ~ 1.0
    conditions_met: List[str] = field(default_factory=list)
    conditions_failed: List[str] = field(default_factory=list)
    recommended_actions: List[str] = field(default_factory=list)


class StrategyGraduator:
    """전략 졸업 시스템.
    
    졸업 경로:
    1. LIGHTNING → PINGPONG
       - 조건: 수익 확정 (ROI > 5%), 거래 3회 이상
       - 근거: 발굴 성공 → 안정적 수익 추구
       
    2. LADDER → GAZUA
       - 조건: 반등 감지 (momentum > 0.3, trend > 0), 포지션 확보
       - 근거: 저점 매집 완료 → 상승 대기
       
    3. GAZUA → PINGPONG
       - 조건: 목표가 도달 (ROI > 30%) 또는 모멘텀 약화
       - 근거: 장기 홀드 완료 → 수익 실현
       
    4. PINGPONG → AUTOLOOP
       - 조건: 안정적 수익 (ROI > 10%, 거래 10회 이상)
       - 근거: 검증 완료 → 자동 루프
    """

    def __init__(self):
        self.graduation_paths = self._init_graduation_paths()

    def _init_graduation_paths(self) -> Dict[str, Dict[str, GraduationCondition]]:
        """졸업 경로별 조건 초기화."""
        return {
            "LIGHTNING": {
                "PINGPONG": GraduationCondition(
                    min_roi_pct=5.0,
                    min_trades=3,
                    min_sells=1,
                    min_age_hours=2.0,
                    requires_profit_lock=True,
                ),
            },
            "LADDER": {
                "GAZUA": GraduationCondition(
                    min_roi_pct=-5.0,  # 손실 허용 (매집 중)
                    min_trades=2,
                    min_age_hours=4.0,
                    min_momentum=0.3,
                    min_trend=0.0,
                    price_above_avg_pct=5.0,  # 평균 매수가 +5% 이상
                ),
            },
            "GAZUA": {
                "PINGPONG": GraduationCondition(
                    min_roi_pct=20.0,
                    min_trades=1,
                    min_sells=1,
                    min_age_hours=12.0,
                    max_volatility=0.05,  # 안정화 필요
                ),
            },
            "PINGPONG": {
                "AUTOLOOP": GraduationCondition(
                    min_roi_pct=10.0,
                    min_trades=10,
                    min_sells=5,
                    min_age_hours=24.0,
                ),
            },
        }

    def evaluate_graduation(
        self,
        ctx: MarketContext,
    ) -> GraduationDecision:
        """졸업 여부 평가."""
        current = ctx.current_strategy.upper()
        paths = self.graduation_paths.get(current, {})
        
        if not paths:
            return GraduationDecision(
                market=ctx.market,
                from_strategy=current,
                to_strategy=None,
                path=None,
                should_graduate=False,
                reason="no_graduation_path",
                confidence=0.0,
            )
        
        best_decision: Optional[GraduationDecision] = None
        best_confidence = 0.0
        
        for to_strategy, condition in paths.items():
            decision = self._evaluate_path(ctx, to_strategy, condition)
            if decision.confidence > best_confidence:
                best_decision = decision
                best_confidence = decision.confidence
        
        if best_decision and best_decision.should_graduate:
            return best_decision
        
        return best_decision or GraduationDecision(
            market=ctx.market,
            from_strategy=current,
            to_strategy=None,
            path=None,
            should_graduate=False,
            reason="conditions_not_met",
            confidence=0.0,
        )

    def _evaluate_path(
        self,
        ctx: MarketContext,
        to_strategy: str,
        cond: GraduationCondition,
    ) -> GraduationDecision:
        """특정 경로 평가."""
        met: List[str] = []
        failed: List[str] = []
        
        # ROI 체크
        if ctx.roi_pct >= cond.min_roi_pct:
            met.append(f"roi:{ctx.roi_pct:.1f}%>={cond.min_roi_pct}%")
        else:
            failed.append(f"roi:{ctx.roi_pct:.1f}%<{cond.min_roi_pct}%")
        
        # 거래 수 체크
        if ctx.trade_count >= cond.min_trades:
            met.append(f"trades:{ctx.trade_count}>={cond.min_trades}")
        else:
            failed.append(f"trades:{ctx.trade_count}<{cond.min_trades}")
        
        # 매도 수 체크
        if ctx.sell_count >= cond.min_sells:
            met.append(f"sells:{ctx.sell_count}>={cond.min_sells}")
        else:
            failed.append(f"sells:{ctx.sell_count}<{cond.min_sells}")
        
        # 활성 기간 체크
        if ctx.active_age_hours >= cond.min_age_hours:
            met.append(f"age:{ctx.active_age_hours:.1f}h>={cond.min_age_hours}h")
        else:
            failed.append(f"age:{ctx.active_age_hours:.1f}h<{cond.min_age_hours}h")
        
        # AI 피처 체크
        if cond.min_momentum is not None:
            if ctx.momentum >= cond.min_momentum:
                met.append(f"momentum:{ctx.momentum:.2f}>={cond.min_momentum}")
            else:
                failed.append(f"momentum:{ctx.momentum:.2f}<{cond.min_momentum}")
        
        if cond.max_volatility is not None:
            if ctx.volatility <= cond.max_volatility:
                met.append(f"vol:{ctx.volatility:.3f}<={cond.max_volatility}")
            else:
                failed.append(f"vol:{ctx.volatility:.3f}>{cond.max_volatility}")
        
        if cond.min_trend is not None:
            if ctx.trend >= cond.min_trend:
                met.append(f"trend:{ctx.trend:.2f}>={cond.min_trend}")
            else:
                failed.append(f"trend:{ctx.trend:.2f}<{cond.min_trend}")
        
        # 가격 조건 체크
        if cond.price_above_avg_pct is not None and ctx.avg_buy_price > 0:
            price_vs_avg = ((ctx.current_price - ctx.avg_buy_price) / ctx.avg_buy_price) * 100
            if price_vs_avg >= cond.price_above_avg_pct:
                met.append(f"price_vs_avg:{price_vs_avg:.1f}%>={cond.price_above_avg_pct}%")
            else:
                failed.append(f"price_vs_avg:{price_vs_avg:.1f}%<{cond.price_above_avg_pct}%")
        
        # 졸업 판단
        total = len(met) + len(failed)
        confidence = len(met) / total if total > 0 else 0.0
        should_graduate = len(failed) == 0 and len(met) > 0
        
        # 경로 결정
        path = self._get_path(ctx.current_strategy.upper(), to_strategy)
        
        # 추천 액션
        actions: List[str] = []
        if should_graduate:
            actions.append(f"switch_to:{to_strategy}")
            if cond.requires_profit_lock:
                actions.append("lock_profit_first")
        else:
            # 가장 가까운 조건 안내
            if failed:
                actions.append(f"wait_for:{failed[0]}")
        
        return GraduationDecision(
            market=ctx.market,
            from_strategy=ctx.current_strategy.upper(),
            to_strategy=to_strategy if should_graduate else None,
            path=path if should_graduate else None,
            should_graduate=should_graduate,
            reason="all_conditions_met" if should_graduate else "partial_conditions",
            confidence=confidence,
            conditions_met=met,
            conditions_failed=failed,
            recommended_actions=actions,
        )

    def _get_path(self, from_s: str, to_s: str) -> Optional[GraduationPath]:
        """경로 enum 반환."""
        key = f"{from_s}→{to_s}"
        for p in GraduationPath:
            if p.value == key:
                return p
        return None

    def batch_evaluate(
        self,
        contexts: List[MarketContext],
    ) -> Tuple[List[GraduationDecision], Dict[str, Any]]:
        """복수 마켓 일괄 평가."""
        decisions = [self.evaluate_graduation(ctx) for ctx in contexts]
        
        graduates = [d for d in decisions if d.should_graduate]
        
        summary = {
            "ts": time.time(),
            "total_evaluated": len(contexts),
            "total_graduates": len(graduates),
            "graduates_by_path": {},
        }
        
        for d in graduates:
            if d.path:
                key = d.path.value
                summary["graduates_by_path"][key] = summary["graduates_by_path"].get(key, 0) + 1
        
        return decisions, summary


def suggest_strategy_for_ai_features(
    momentum: float,
    volatility: float,
    trend: float,
    ai_prediction: float,
    rsi: float,
) -> Tuple[str, float, str]:
    """AI 피처 기반 전략 추천.
    
    Returns:
        (strategy, confidence, reason)
    """
    scores = {
        "LADDER": 0.0,
        "LIGHTNING": 0.0,
        "GAZUA": 0.0,
        "PINGPONG": 0.0,
        "AUTOLOOP": 0.0,
    }
    
    # LADDER: 고변동성 + 하락 추세 (분할매수)
    if volatility > 0.03 and momentum < 0 and trend < 0:
        scores["LADDER"] = 0.8 + volatility * 2
        if rsi < 30:
            scores["LADDER"] += 0.2
    
    # LIGHTNING: 강한 모멘텀 + 상승 돌파 (단타)
    if momentum > 0.5 and volatility > 0.02:
        scores["LIGHTNING"] = 0.7 + momentum * 0.5
        if trend > 0:
            scores["LIGHTNING"] += 0.2
    
    # GAZUA: AI 상승 예측 + 저평가 (추세추종)
    if ai_prediction > 0.6 and trend >= 0:
        scores["GAZUA"] = 0.6 + ai_prediction * 0.4
        if 30 <= rsi <= 60:
            scores["GAZUA"] += 0.2
    
    # AUTOLOOP: 중간 변동성 + 횡보 (자동 구간매매)
    if 0.015 < volatility < 0.05 and abs(trend) < 1.0:
        scores["AUTOLOOP"] = 0.65 + (0.05 - abs(trend) * 0.01)
        if 35 <= rsi <= 65:
            scores["AUTOLOOP"] += 0.15
    
    # PINGPONG: 안정적 저변동 (구간매매)
    if volatility < 0.02 and abs(momentum) < 0.3:
        scores["PINGPONG"] = 0.7
        if 40 <= rsi <= 60:
            scores["PINGPONG"] += 0.2
    
    # 최고 점수 전략 선택
    best = max(scores.items(), key=lambda x: x[1])
    strategy, score = best
    
    if score < 0.5:
        return ("AUTOLOOP", 0.5, "default_low_confidence")
    
    reasons = []
    if strategy == "LADDER":
        reasons.append(f"high_vol:{volatility:.2f},down_momentum:{momentum:.2f}")
    elif strategy == "LIGHTNING":
        reasons.append(f"strong_momentum:{momentum:.2f},vol:{volatility:.2f}")
    elif strategy == "GAZUA":
        reasons.append(f"ai_bullish:{ai_prediction:.2f},trend:{trend:.2f}")
    elif strategy == "AUTOLOOP":
        reasons.append(f"ranging:vol={volatility:.2f},trend={trend:.2f}")
    elif strategy == "PINGPONG":
        reasons.append(f"stable:vol={volatility:.2f},rsi={rsi:.0f}")
    
    return (strategy, min(1.0, score), ",".join(reasons))
