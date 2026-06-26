# ============================================================
# File: app/manager/strategy_weight_adjuster.py
# Autocoin OS v3-H — Strategy Weight Adjuster
# ------------------------------------------------------------
# Auto-adjusts strategy budget weights based on recent performance
# - Tracks the last 7 days of performance
# - Computes performance-based budget multipliers
# - Automatically scales down strategies on losing streaks
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
# Data classes
# ============================================================

@dataclass
class StrategyWeight:
    """Strategy weight"""
    strategy: str
    base_weight: float = 1.0  # base multiplier
    performance_weight: float = 1.0  # performance-based multiplier
    final_weight: float = 1.0  # final multiplier (base * performance)
    reason: str = ""

    # performance metrics
    win_rate: float = 0.0
    roi_pct: float = 0.0
    total_trades: int = 0
    consecutive_losses: int = 0


@dataclass
class WeightAdjustmentConfig:
    """Weight adjustment settings"""
    # performance-metric weights
    win_rate_weight: float = 0.4
    roi_weight: float = 0.4
    trade_count_weight: float = 0.2

    # multiplier range
    min_weight: float = 0.3  # min 30% (prevent full block)
    max_weight: float = 2.0  # max 200% (prevent over-concentration)

    # losing-streak penalty
    loss_streak_threshold: int = 3  # penalty starts at 3 losses in a row
    loss_streak_penalty: float = 0.2  # -20% per additional loss

    # minimum sample size
    min_trades_for_adjustment: int = 5  # at least 5 trades

    # performance window
    lookback_days: int = 7


# ============================================================
# StrategyWeightAdjuster
# ============================================================

class StrategyWeightAdjuster:
    """Strategy weight adjuster"""
    
    def __init__(
        self,
        *,
        config: WeightAdjustmentConfig = None,
        performance_analyzer: PerformanceAnalyzer = None
    ):
        self.config = config or WeightAdjustmentConfig()
        self.performance_analyzer = performance_analyzer or PerformanceAnalyzer()
        
        # weight per strategy
        self.strategy_weights: Dict[str, StrategyWeight] = {}

        # consecutive-loss tracking
        self.consecutive_losses: Dict[str, int] = defaultdict(int)
        
        logger.info(
            f"StrategyWeightAdjuster initialized: "
            f"lookback={self.config.lookback_days}d, "
            f"min_trades={self.config.min_trades_for_adjustment}"
        )
    
    # ============================================================
    # Weight calculation
    # ============================================================

    def calculate_weights(
        self,
        ledger_records: List[Dict],
        strategies: List[str] = None
    ) -> Dict[str, StrategyWeight]:
        """Calculate weight per strategy"""

        # performance analysis
        since_ts = time.time() - (self.config.lookback_days * 86400)
        perf_by_strategy = self.performance_analyzer.analyze_strategy_performance(
            ledger_records,
            since_ts=since_ts
        )
        
        # strategy list
        if strategies is None:
            strategies = list(perf_by_strategy.keys())

        weights = {}

        for strategy in strategies:
            perf = perf_by_strategy.get(strategy)

            if perf is None or perf.total_trades < self.config.min_trades_for_adjustment:
                # insufficient samples - default weight
                weights[strategy] = StrategyWeight(
                    strategy=strategy,
                    base_weight=1.0,
                    performance_weight=1.0,
                    final_weight=1.0,
                    reason="Insufficient data",
                    total_trades=perf.total_trades if perf else 0
                )
                continue
            
            # performance-based weight
            performance_weight = self._calculate_performance_weight(perf)

            # apply losing-streak penalty
            loss_streak_penalty = self._calculate_loss_streak_penalty(
                strategy,
                perf
            )

            # final weight
            final_weight = performance_weight * loss_streak_penalty
            final_weight = max(self.config.min_weight, min(self.config.max_weight, final_weight))

            # reason text
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
        """Compute the performance-based weight"""

        # win-rate score (0~1)
        win_rate_score = perf.win_rate / 100.0

        # ROI score (-1~1, normalized)
        # ROI 50% = 1.0, 0% = 0.5, -50% = 0.0
        roi_score = max(0.0, min(1.0, (perf.roi_pct + 50) / 100.0))

        # trade-count score (more trades = higher confidence)
        # 10 trades = 0.5, 30+ trades = 1.0
        trade_score = min(1.0, perf.total_trades / 30.0)

        # weighted average
        score = (
            win_rate_score * self.config.win_rate_weight +
            roi_score * self.config.roi_weight +
            trade_score * self.config.trade_count_weight
        )

        # convert to multiplier (0.5~1.5)
        weight = 0.5 + score * 1.0
        
        return weight
    
    def _calculate_loss_streak_penalty(
        self, 
        strategy: str, 
        perf: StrategyPerformance
    ) -> float:
        """Compute the losing-streak penalty"""

        streak = self.consecutive_losses.get(strategy, 0)

        if streak < self.config.loss_streak_threshold:
            return 1.0

        # accumulate penalty per loss in the streak
        excess_losses = streak - self.config.loss_streak_threshold + 1
        penalty = 1.0 - (excess_losses * self.config.loss_streak_penalty)

        return max(0.3, penalty)  # min 30%
    
    def _generate_reason(
        self,
        perf: StrategyPerformance,
        performance_weight: float,
        loss_streak_penalty: float
    ) -> str:
        """Describe why the weight was adjusted"""

        parts = []

        # performance
        if performance_weight > 1.2:
            parts.append(f"strong performance (win rate {perf.win_rate:.0f}%, ROI {perf.roi_pct:+.1f}%)")
        elif performance_weight < 0.8:
            parts.append(f"weak performance (win rate {perf.win_rate:.0f}%, ROI {perf.roi_pct:+.1f}%)")
        else:
            parts.append(f"average performance (win rate {perf.win_rate:.0f}%)")

        # losing streak
        if loss_streak_penalty < 1.0:
            streak = self.consecutive_losses.get(perf.strategy, 0)
            parts.append(f"{streak}-loss streak penalty")

        return " | ".join(parts)
    
    # ============================================================
    # Consecutive-loss tracking
    # ============================================================

    def track_trade_result(
        self,
        strategy: str,
        pnl_usdt: float
    ):
        """Track a trade result (losing-streak count)"""

        if pnl_usdt < 0:
            # loss - increment streak count
            self.consecutive_losses[strategy] += 1
            logger.info(
                f"Strategy {strategy} loss streak: "
                f"{self.consecutive_losses[strategy]}"
            )
        elif pnl_usdt > 0:
            # win - reset streak
            if self.consecutive_losses[strategy] > 0:
                logger.info(
                    f"Strategy {strategy} streak broken: "
                    f"{self.consecutive_losses[strategy]} losses → WIN"
                )
            self.consecutive_losses[strategy] = 0
    
    # ============================================================
    # Budget multiplier application
    # ============================================================

    def get_budget_multiplier(self, strategy: str) -> float:
        """Get the budget multiplier for a strategy"""

        weight = self.strategy_weights.get(strategy)

        if weight is None:
            return 1.0  # default

        return weight.final_weight

    def apply_to_budget(
        self,
        base_budget_usdt: float,
        strategy: str
    ) -> float:
        """Apply the weight to a budget"""
        
        multiplier = self.get_budget_multiplier(strategy)
        adjusted = base_budget_usdt * multiplier
        
        logger.debug(
            f"Budget adjustment for {strategy}: "
            f"{base_budget_usdt:,.0f} → {adjusted:,.0f} "
            f"(x{multiplier:.2f})"
        )
        
        return adjusted
    
    # ============================================================
    # Status query
    # ============================================================

    def get_status(self) -> Dict:
        """Get the weight adjuster's status"""
        
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
        """Strategy adjustment recommendations"""
        
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
# Singleton instance
# ============================================================
_strategy_weight_adjuster: Optional[StrategyWeightAdjuster] = None


def get_strategy_weight_adjuster() -> StrategyWeightAdjuster:
    """Strategy weight adjuster singleton"""
    global _strategy_weight_adjuster
    if _strategy_weight_adjuster is None:
        _strategy_weight_adjuster = StrategyWeightAdjuster()
    return _strategy_weight_adjuster
