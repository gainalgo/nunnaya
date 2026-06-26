"""Strategy Prioritization based on Market Regime.

File: app/manager/regime_strategy.py

Maps market regimes to optimal strategy priorities:
- BULL: Aggressive strategies (SNIPER, LIGHTNING, GAZUA)
- BEAR: Defensive strategies (LADDER, CONTRARIAN)
- SIDEWAYS: Range-bound strategies (AUTOLOOP, PINGPONG)
- VOLATILE: Volatility strategies (LIGHTNING, CONTRARIAN)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from app.core.market_regime import MarketRegime, RegimeDetector


class StrategyPriority(Enum):
    """Strategy priority levels."""
    VERY_HIGH = 5
    HIGH = 4
    MEDIUM = 3
    LOW = 2
    VERY_LOW = 1


@dataclass
class StrategyWeight:
    """Strategy weighting for a specific regime."""
    priority: StrategyPriority
    score_multiplier: float = 1.0
    budget_multiplier: float = 1.0
    description: str = ""


@dataclass
class RegimeStrategyMapping:
    """Complete mapping of regime to strategy priorities."""
    regime: MarketRegime
    confidence: float = 0.0
    strategies: Dict[str, StrategyWeight] = field(default_factory=dict)
    timestamp: float = 0.0

    def get_score_multiplier(self, strategy: str) -> float:
        """Get score multiplier for a strategy."""
        weight = self.strategies.get(strategy.upper())
        if not weight:
            return 1.0
        return weight.score_multiplier

    def get_budget_multiplier(self, strategy: str) -> float:
        """Get budget multiplier for a strategy."""
        weight = self.strategies.get(strategy.upper())
        if not weight:
            return 1.0
        return weight.budget_multiplier

    def get_priority_rank(self, strategy: str) -> int:
        """Get priority rank (5=highest, 1=lowest)."""
        weight = self.strategies.get(strategy.upper())
        if not weight:
            return 3  # MEDIUM
        return weight.priority.value


# Default strategy priorities by regime
DEFAULT_REGIME_STRATEGY_MAP: Dict[MarketRegime, Dict[str, StrategyWeight]] = {
    MarketRegime.BULL: {
        "SNIPER": StrategyWeight(
            priority=StrategyPriority.VERY_HIGH,
            score_multiplier=1.25,
            budget_multiplier=1.20,
            description="Momentum capture in uptrend"
        ),
        "LIGHTNING": StrategyWeight(
            priority=StrategyPriority.VERY_HIGH,
            score_multiplier=1.20,
            budget_multiplier=1.15,
            description="Quick profits in strong trends"
        ),
        "GAZUA": StrategyWeight(
            priority=StrategyPriority.HIGH,
            score_multiplier=1.15,
            budget_multiplier=1.10,
            description="Long-term hold winners"
        ),
        "AUTOLOOP": StrategyWeight(
            priority=StrategyPriority.MEDIUM,
            score_multiplier=1.00,
            budget_multiplier=1.00,
            description="Neutral in bull market"
        ),
        "PINGPONG": StrategyWeight(
            priority=StrategyPriority.LOW,
            score_multiplier=0.90,
            budget_multiplier=0.90,
            description="Less effective in trending market"
        ),
        "LADDER": StrategyWeight(
            priority=StrategyPriority.LOW,
            score_multiplier=0.85,
            budget_multiplier=0.85,
            description="DCA not optimal in uptrend"
        ),
        "CONTRARIAN": StrategyWeight(
            priority=StrategyPriority.VERY_LOW,
            score_multiplier=0.75,
            budget_multiplier=0.75,
            description="Counter-trend not suitable for bull"
        ),
    },
    MarketRegime.BEAR: {
        "LADDER": StrategyWeight(
            priority=StrategyPriority.VERY_HIGH,
            score_multiplier=1.25,
            budget_multiplier=1.20,
            description="DCA shines in downtrend"
        ),
        "CONTRARIAN": StrategyWeight(
            priority=StrategyPriority.VERY_HIGH,
            score_multiplier=1.20,
            budget_multiplier=1.15,
            description="Bottom fishing opportunities"
        ),
        "PINGPONG": StrategyWeight(
            priority=StrategyPriority.MEDIUM,
            score_multiplier=1.00,
            budget_multiplier=1.00,
            description="Neutral in bear market"
        ),
        "AUTOLOOP": StrategyWeight(
            priority=StrategyPriority.MEDIUM,
            score_multiplier=0.95,
            budget_multiplier=0.95,
            description="Slightly less effective"
        ),
        "GAZUA": StrategyWeight(
            priority=StrategyPriority.LOW,
            score_multiplier=0.80,
            budget_multiplier=0.80,
            description="Long holds risky in downtrend"
        ),
        "SNIPER": StrategyWeight(
            priority=StrategyPriority.VERY_LOW,
            score_multiplier=0.75,
            budget_multiplier=0.75,
            description="Momentum trading difficult"
        ),
        "LIGHTNING": StrategyWeight(
            priority=StrategyPriority.VERY_LOW,
            score_multiplier=0.70,
            budget_multiplier=0.70,
            description="Quick profits hard to find"
        ),
    },
    MarketRegime.SIDEWAYS: {
        "AUTOLOOP": StrategyWeight(
            priority=StrategyPriority.VERY_HIGH,
            score_multiplier=1.25,
            budget_multiplier=1.20,
            description="Perfect for range-bound markets"
        ),
        "PINGPONG": StrategyWeight(
            priority=StrategyPriority.VERY_HIGH,
            score_multiplier=1.20,
            budget_multiplier=1.15,
            description="Exploit price oscillations"
        ),
        "LADDER": StrategyWeight(
            priority=StrategyPriority.MEDIUM,
            score_multiplier=1.00,
            budget_multiplier=1.00,
            description="Moderate effectiveness"
        ),
        "CONTRARIAN": StrategyWeight(
            priority=StrategyPriority.MEDIUM,
            score_multiplier=1.00,
            budget_multiplier=1.00,
            description="Some reversal opportunities"
        ),
        "GAZUA": StrategyWeight(
            priority=StrategyPriority.LOW,
            score_multiplier=0.90,
            budget_multiplier=0.90,
            description="No clear trend to ride"
        ),
        "SNIPER": StrategyWeight(
            priority=StrategyPriority.LOW,
            score_multiplier=0.85,
            budget_multiplier=0.85,
            description="Momentum hard to capture"
        ),
        "LIGHTNING": StrategyWeight(
            priority=StrategyPriority.LOW,
            score_multiplier=0.85,
            budget_multiplier=0.85,
            description="Weak directional signals"
        ),
    },
    MarketRegime.VOLATILE: {
        "LIGHTNING": StrategyWeight(
            priority=StrategyPriority.VERY_HIGH,
            score_multiplier=1.30,
            budget_multiplier=1.25,
            description="Capture large swings quickly"
        ),
        "CONTRARIAN": StrategyWeight(
            priority=StrategyPriority.VERY_HIGH,
            score_multiplier=1.25,
            budget_multiplier=1.20,
            description="Profit from overreactions"
        ),
        "SNIPER": StrategyWeight(
            priority=StrategyPriority.HIGH,
            score_multiplier=1.15,
            budget_multiplier=1.10,
            description="Momentum in volatile moves"
        ),
        "AUTOLOOP": StrategyWeight(
            priority=StrategyPriority.MEDIUM,
            score_multiplier=1.00,
            budget_multiplier=1.00,
            description="Moderate effectiveness"
        ),
        "PINGPONG": StrategyWeight(
            priority=StrategyPriority.LOW,
            score_multiplier=0.90,
            budget_multiplier=0.90,
            description="Stop-loss triggers frequent"
        ),
        "LADDER": StrategyWeight(
            priority=StrategyPriority.LOW,
            score_multiplier=0.85,
            budget_multiplier=0.85,
            description="Hard to average down safely"
        ),
        "GAZUA": StrategyWeight(
            priority=StrategyPriority.VERY_LOW,
            score_multiplier=0.75,
            budget_multiplier=0.75,
            description="Long holds risky in chaos"
        ),
    },
}


class RegimeStrategyManager:
    """Manages strategy prioritization based on market regime."""

    def __init__(self, detector: Optional[RegimeDetector] = None):
        self.detector = detector or RegimeDetector()
        self._cache: Dict[str, RegimeStrategyMapping] = {}

    def get_strategy_mapping(
        self,
        market: str = "BTCUSDT",
        force_refresh: bool = False,
    ) -> RegimeStrategyMapping:
        """Get strategy mapping for current market regime.

        Args:
            market: Market to detect regime for
            force_refresh: Force regime detection (bypass cache)

        Returns:
            RegimeStrategyMapping with priorities and multipliers
        """
        import time

        # Check cache (30-second TTL)
        if not force_refresh:
            cached = self._cache.get(market)
            if cached and (time.time() - cached.timestamp) < 30.0:
                return cached

        # Detect regime
        result = self.detector.detect(market)

        # Get strategy weights
        strategies = DEFAULT_REGIME_STRATEGY_MAP.get(
            result.regime,
            {}  # Fallback to empty if unknown regime
        )

        # Apply confidence weighting to multipliers
        adjusted_strategies = {}
        for strat_name, weight in strategies.items():
            # Blend multiplier based on confidence
            # confidence=0.0 → 1.0 (neutral), confidence=1.0 → full multiplier
            score_mult = 1.0 + (weight.score_multiplier - 1.0) * result.confidence
            budget_mult = 1.0 + (weight.budget_multiplier - 1.0) * result.confidence

            adjusted_strategies[strat_name] = StrategyWeight(
                priority=weight.priority,
                score_multiplier=score_mult,
                budget_multiplier=budget_mult,
                description=weight.description,
            )

        mapping = RegimeStrategyMapping(
            regime=result.regime,
            confidence=result.confidence,
            strategies=adjusted_strategies,
            timestamp=time.time(),
        )

        self._cache[market] = mapping
        return mapping

    def get_top_strategies(
        self,
        market: str = "BTCUSDT",
        limit: int = 3,
    ) -> List[str]:
        """Get top N strategies for current regime.

        Args:
            market: Market to detect regime for
            limit: Number of top strategies to return

        Returns:
            List of strategy names (e.g., ["SNIPER", "LIGHTNING", "GAZUA"])
        """
        mapping = self.get_strategy_mapping(market)

        # Sort by priority (descending)
        ranked = sorted(
            mapping.strategies.items(),
            key=lambda x: x[1].priority.value,
            reverse=True,
        )

        return [name for name, _ in ranked[:limit]]

    def should_enter(
        self,
        strategy: str,
        market: str = "BTCUSDT",
        base_score: float = 0.0,
    ) -> bool:
        """Check if strategy should enter based on regime.

        Args:
            strategy: Strategy name
            market: Market to check
            base_score: Base candidate score (0-100)

        Returns:
            True if strategy is suitable for current regime
        """
        mapping = self.get_strategy_mapping(market)
        weight = mapping.strategies.get(strategy.upper())

        if not weight:
            return True  # Unknown strategy, allow entry

        # Require at least MEDIUM priority
        if weight.priority.value < 3:
            return False

        # Apply score multiplier threshold
        adjusted_score = base_score * weight.score_multiplier
        return adjusted_score >= 50.0  # Minimum adjusted score
