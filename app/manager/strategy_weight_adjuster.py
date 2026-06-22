# ============================================================
# File: app/manager/strategy_weight_adjuster.py
# Autocoin OS v3-H — Strategy Weight Adjuster
# ------------------------------------------------------------
# 최근 성과 기반 전략 예산 가중치 자동 조정
# - 최근 7일 성과 추적
# - 성과 기반 예산 배율 계산
# - 연패 전략 자동 축소
# ============================================================

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

from app.manager.performance_analyzer import PerformanceAnalyzer, StrategyPerformance

logger = logging.getLogger(__name__)


# ============================================================
# 데이터 클래스
# ============================================================

@dataclass
class StrategyWeight:
    """전략 가중치"""
    strategy: str
    base_weight: float = 1.0  # 기본 배율
    performance_weight: float = 1.0  # 성과 기반 배율
    final_weight: float = 1.0  # 최종 배율 (base * performance)
    reason: str = ""
    
    # 성과 지표
    win_rate: float = 0.0
    roi_pct: float = 0.0
    total_trades: int = 0
    consecutive_losses: int = 0


@dataclass
class WeightAdjustmentConfig:
    """가중치 조정 설정"""
    # 성과 지표 가중치
    win_rate_weight: float = 0.4
    roi_weight: float = 0.4
    trade_count_weight: float = 0.2
    
    # 배율 범위
    min_weight: float = 0.3  # 최소 30% (완전 차단 방지)
    max_weight: float = 2.0  # 최대 200% (과도한 집중 방지)
    
    # 연패 페널티
    loss_streak_threshold: int = 3  # 3연패부터 페널티
    loss_streak_penalty: float = 0.2  # 연패마다 -20%
    
    # 최소 샘플 수
    min_trades_for_adjustment: int = 5  # 최소 5거래 이상
    
    # 성과 기간
    lookback_days: int = 7


# ============================================================
# StrategyWeightAdjuster
# ============================================================

class StrategyWeightAdjuster:
    """전략 가중치 조정기"""
    
    def __init__(
        self,
        *,
        config: WeightAdjustmentConfig = None,
        performance_analyzer: PerformanceAnalyzer = None
    ):
        self.config = config or WeightAdjustmentConfig()
        self.performance_analyzer = performance_analyzer or PerformanceAnalyzer()
        
        # 전략별 가중치
        self.strategy_weights: Dict[str, StrategyWeight] = {}
        
        # 연속 손실 추적
        self.consecutive_losses: Dict[str, int] = defaultdict(int)
        
        logger.info(
            f"StrategyWeightAdjuster initialized: "
            f"lookback={self.config.lookback_days}d, "
            f"min_trades={self.config.min_trades_for_adjustment}"
        )
    
    # ============================================================
    # 가중치 계산
    # ============================================================
    
    def calculate_weights(
        self,
        ledger_records: List[Dict],
        strategies: List[str] = None
    ) -> Dict[str, StrategyWeight]:
        """전략별 가중치 계산"""
        
        # 성과 분석
        since_ts = time.time() - (self.config.lookback_days * 86400)
        perf_by_strategy = self.performance_analyzer.analyze_strategy_performance(
            ledger_records,
            since_ts=since_ts
        )
        
        # 전략 목록
        if strategies is None:
            strategies = list(perf_by_strategy.keys())
        
        weights = {}
        
        for strategy in strategies:
            perf = perf_by_strategy.get(strategy)
            
            if perf is None or perf.total_trades < self.config.min_trades_for_adjustment:
                # 샘플 부족 - 기본 가중치
                weights[strategy] = StrategyWeight(
                    strategy=strategy,
                    base_weight=1.0,
                    performance_weight=1.0,
                    final_weight=1.0,
                    reason="Insufficient data",
                    total_trades=perf.total_trades if perf else 0
                )
                continue
            
            # 성과 기반 가중치 계산
            performance_weight = self._calculate_performance_weight(perf)
            
            # 연패 페널티 적용
            loss_streak_penalty = self._calculate_loss_streak_penalty(
                strategy, 
                perf
            )
            
            # 최종 가중치
            final_weight = performance_weight * loss_streak_penalty
            final_weight = max(self.config.min_weight, min(self.config.max_weight, final_weight))
            
            # 이유 설명
            reason = self._generate_reason(perf, performance_weight, loss_streak_penalty)
            
            weights[strategy] = StrategyWeight(
                strategy=strategy,
                base_weight=1.0,
                performance_weight=final_weight,
                final_weight=final_weight,
                reason=reason,
                win_rate=perf.win_rate,
                roi_pct=perf.roi_pct,
                total_trades=perf.total_trades,
                consecutive_losses=self.consecutive_losses.get(strategy, 0)
            )
        
        self.strategy_weights = weights
        return weights
    
    def _calculate_performance_weight(self, perf: StrategyPerformance) -> float:
        """성과 기반 가중치 계산"""
        
        # 승률 점수 (0~1)
        win_rate_score = perf.win_rate / 100.0
        
        # ROI 점수 (-1~1, 정규화)
        # ROI 50% = 1.0, 0% = 0.5, -50% = 0.0
        roi_score = max(0.0, min(1.0, (perf.roi_pct + 50) / 100.0))
        
        # 거래 수 점수 (많을수록 신뢰도 높음)
        # 10거래 = 0.5, 30거래 이상 = 1.0
        trade_score = min(1.0, perf.total_trades / 30.0)
        
        # 가중 평균
        score = (
            win_rate_score * self.config.win_rate_weight +
            roi_score * self.config.roi_weight +
            trade_score * self.config.trade_count_weight
        )
        
        # 배율로 변환 (0.5~1.5)
        weight = 0.5 + score * 1.0
        
        return weight
    
    def _calculate_loss_streak_penalty(
        self, 
        strategy: str, 
        perf: StrategyPerformance
    ) -> float:
        """연패 페널티 계산"""
        
        streak = self.consecutive_losses.get(strategy, 0)
        
        if streak < self.config.loss_streak_threshold:
            return 1.0
        
        # 연패마다 페널티 누적
        excess_losses = streak - self.config.loss_streak_threshold + 1
        penalty = 1.0 - (excess_losses * self.config.loss_streak_penalty)
        
        return max(0.3, penalty)  # 최소 30%
    
    def _generate_reason(
        self,
        perf: StrategyPerformance,
        performance_weight: float,
        loss_streak_penalty: float
    ) -> str:
        """가중치 조정 이유 설명"""
        
        parts = []
        
        # 성과
        if performance_weight > 1.2:
            parts.append(f"우수 성과 (승률 {perf.win_rate:.0f}%, ROI {perf.roi_pct:+.1f}%)")
        elif performance_weight < 0.8:
            parts.append(f"부진 성과 (승률 {perf.win_rate:.0f}%, ROI {perf.roi_pct:+.1f}%)")
        else:
            parts.append(f"보통 성과 (승률 {perf.win_rate:.0f}%)")
        
        # 연패
        if loss_streak_penalty < 1.0:
            streak = self.consecutive_losses.get(perf.strategy, 0)
            parts.append(f"{streak}연패 페널티")
        
        return " | ".join(parts)
    
    # ============================================================
    # 연속 손실 추적
    # ============================================================
    
    def track_trade_result(
        self,
        strategy: str,
        pnl_usdt: float
    ):
        """거래 결과 추적 (연패 카운트)"""
        
        if pnl_usdt < 0:
            # 손실 - 연패 카운트 증가
            self.consecutive_losses[strategy] += 1
            logger.info(
                f"Strategy {strategy} loss streak: "
                f"{self.consecutive_losses[strategy]}"
            )
        elif pnl_usdt > 0:
            # 승리 - 연패 리셋
            if self.consecutive_losses[strategy] > 0:
                logger.info(
                    f"Strategy {strategy} streak broken: "
                    f"{self.consecutive_losses[strategy]} losses → WIN"
                )
            self.consecutive_losses[strategy] = 0
    
    # ============================================================
    # 예산 배율 적용
    # ============================================================
    
    def get_budget_multiplier(self, strategy: str) -> float:
        """전략별 예산 배율 조회"""
        
        weight = self.strategy_weights.get(strategy)
        
        if weight is None:
            return 1.0  # 기본값
        
        return weight.final_weight
    
    def apply_to_budget(
        self,
        base_budget_usdt: float,
        strategy: str
    ) -> float:
        """예산에 가중치 적용"""
        
        multiplier = self.get_budget_multiplier(strategy)
        adjusted = base_budget_usdt * multiplier
        
        logger.debug(
            f"Budget adjustment for {strategy}: "
            f"{base_budget_usdt:,.0f} → {adjusted:,.0f} "
            f"(x{multiplier:.2f})"
        )
        
        return adjusted
    
    # ============================================================
    # 상태 조회
    # ============================================================
    
    def get_status(self) -> Dict:
        """가중치 조정기 상태 조회"""
        
        weights_dict = {}
        for strategy, weight in self.strategy_weights.items():
            weights_dict[strategy] = {
                "final_weight": weight.final_weight,
                "performance_weight": weight.performance_weight,
                "reason": weight.reason,
                "win_rate": weight.win_rate,
                "roi_pct": weight.roi_pct,
                "total_trades": weight.total_trades,
                "consecutive_losses": weight.consecutive_losses
            }
        
        return {
            "strategy_weights": weights_dict,
            "consecutive_losses": dict(self.consecutive_losses),
            "config": {
                "lookback_days": self.config.lookback_days,
                "min_trades": self.config.min_trades_for_adjustment,
                "min_weight": self.config.min_weight,
                "max_weight": self.config.max_weight,
                "loss_streak_threshold": self.config.loss_streak_threshold
            }
        }
    
    def get_recommendations(self) -> List[Dict]:
        """전략 조정 권장사항"""
        
        recommendations = []
        
        for strategy, weight in self.strategy_weights.items():
            if weight.final_weight < 0.7:
                recommendations.append({
                    "strategy": strategy,
                    "action": "REDUCE",
                    "weight": weight.final_weight,
                    "reason": weight.reason,
                    "severity": "HIGH" if weight.final_weight < 0.5 else "MEDIUM"
                })
            elif weight.final_weight > 1.5:
                recommendations.append({
                    "strategy": strategy,
                    "action": "INCREASE",
                    "weight": weight.final_weight,
                    "reason": weight.reason,
                    "severity": "OPPORTUNITY"
                })
        
        return recommendations


# ============================================================
# 싱글톤 인스턴스
# ============================================================
_strategy_weight_adjuster: Optional[StrategyWeightAdjuster] = None


def get_strategy_weight_adjuster() -> StrategyWeightAdjuster:
    """전략 가중치 조정기 싱글톤"""
    global _strategy_weight_adjuster
    if _strategy_weight_adjuster is None:
        _strategy_weight_adjuster = StrategyWeightAdjuster()
    return _strategy_weight_adjuster
