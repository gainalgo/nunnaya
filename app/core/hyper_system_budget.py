"""
HyperSystem Budget Module
- Budget allocation logic
- Smart Allocation
- F&G / Regime multiplier composition

[MIGRATED 2026-03-31] Bybit USDT
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional, TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from app.engine.hyper_engine_context import HyperEngineContext

logger = logging.getLogger(__name__)

def compute_fg_multiplier(
    budget_strategy: str,
    fear_greed_module: Optional[Any] = None,
) -> float:
    """Compute budget multiplier based on Fear & Greed.

    Args:
        budget_strategy: "regime" | "fg" | "extreme" | "hybrid"
        fear_greed_module: return value of get_fear_greed()

    Returns:
        Multiplier (1.0 = no change)
    """
    if budget_strategy not in ("fg", "extreme", "hybrid"):
        return 1.0
    
    if fear_greed_module is None:
        return 1.0
    
    try:
        fg_result = fear_greed_module.fetch()
        
        if budget_strategy == "fg":
            return fg_result.budget_multiplier
        elif budget_strategy == "extreme":
            # Apply only at extremes (0-25, 75-100)
            if fg_result.value <= 25 or fg_result.value >= 75:
                return fg_result.budget_multiplier
            return 1.0
        elif budget_strategy == "hybrid":
            return fg_result.budget_multiplier
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning(f"F&G multiplier error: {e}")
    
    return 1.0

def compute_regime_multiplier(
    regime_detector: Optional[Any] = None,
    market: str = "BTCUSDT",
) -> float:
    """Compute budget multiplier based on Market Regime.

    Returns:
        Multiplier (1.0 = no change)
    """
    if regime_detector is None:
        return 1.0
    
    try:
        result = regime_detector.detect(market)
        return regime_detector.get_budget_multiplier(result.regime)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning(f"Regime multiplier error: {e}")
    
    return 1.0

def compute_smart_allocation_scores(
    markets: List[str],
    contexts: Dict[str, "HyperEngineContext"],
    *,
    w_profit: float = 0.5,
    w_ai: float = 0.3,
    w_risk: float = 0.2,
    vol_th: float = 0.05,
    loss_penalty: float = 0.3,
    min_mult: float = 0.5,
    max_mult: float = 2.0,
) -> Dict[str, float]:
    """Compute Smart Allocation scores.

    Returns:
        {market: multiplier} dictionary
    """
    scores: Dict[str, float] = {}
    
    for market in markets:
        ctx = contexts.get(market)
        if not ctx:
            scores[market] = 1.0
            continue
        
        # 1. Profit Score
        profit_score = 0.0
        try:
            recent_pnl = getattr(ctx, "recent_pnl_pct", None)
            if recent_pnl is not None:
                if recent_pnl > 0:
                    profit_score = min(1.0, recent_pnl / 10.0)
                else:
                    profit_score = max(-1.0, recent_pnl / 10.0) * loss_penalty
        except (TypeError, ValueError) as exc:
            logger.warning("[hyper_system_budget] %s: %s", '1. Profit Score', exc, exc_info=True)
        
        # 2. AI Score
        ai_score = 0.0
        try:
            current_ai = getattr(ctx, "current_ai", None)
            if isinstance(current_ai, dict):
                brain = current_ai.get("brain", {})
                ai_pred = float(brain.get("ai_prediction", 0.5))
                ai_score = (ai_pred - 0.5) * 2.0
        except (TypeError, ValueError) as exc:
            logger.warning("[hyper_system_budget] %s: %s", '2. AI Score', exc, exc_info=True)
        
        # 3. Risk Penalty
        risk_penalty = 0.0
        try:
            volatility = getattr(ctx, "volatility_24h", None)
            if volatility is not None and volatility > vol_th:
                risk_penalty = min(0.5, (volatility - vol_th) * 5.0)
        except (TypeError, ValueError) as exc:
            logger.warning("[hyper_system_budget] %s: %s", '3. Risk Penalty', exc, exc_info=True)
        
        # 4. Final Score
        final_score = w_profit * profit_score + w_ai * ai_score - w_risk * risk_penalty
        mult = 1.0 + final_score
        mult = max(min_mult, min(max_mult, mult))
        
        scores[market] = mult
    
    return scores

def distribute_budget_by_scores(
    scores: Dict[str, float],
    total_budget: float,
    min_per_market: float = 5.0,
) -> Dict[str, float]:
    """Distribute budget based on scores.

    min_per_market: 5 USDT

    Returns:
        {market: allocated_usdt} dictionary
    """
    if not scores or total_budget <= 0:
        return {}
    
    total_weight = sum(scores.values())
    if total_weight <= 0:
        total_weight = len(scores)
    
    allocations: Dict[str, float] = {}
    for market, weight in scores.items():
        allocated = (weight / total_weight) * total_budget
        allocated = max(min_per_market, allocated)
        allocations[market] = allocated
    
    # Scale down proportionally if total is exceeded
    total_allocated = sum(allocations.values())
    if total_allocated > total_budget:
        scale = total_budget / total_allocated
        allocations = {m: v * scale for m, v in allocations.items()}
    
    return allocations

def equal_distribution(
    markets: List[str],
    total_budget: float,
    min_per_market: float = 5.0,
) -> Dict[str, float]:
    """Equal distribution.

    min_per_market: 5 USDT
    """
    if not markets or total_budget <= 0:
        return {}
    
    per_market = total_budget / len(markets)
    per_market = max(min_per_market, per_market)
    
    return {m: per_market for m in markets}
