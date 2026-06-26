"""
Budget Policy Module
- Handle budget computation/validation for all orders at a single point
- Unifies GAZUA budget protection, per-strategy limits, global limits, etc.

[MIGRATED 2026-01-23] CoinStock → Autocoin
- USDT-based orders
- min_order: 10 USDT → 5 USDT
- max_order: 1,000 USDT → 1,000 USDT
"""

from __future__ import annotations
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass

from app.core.constants import env_float, env_bool


@dataclass
class BudgetDecision:
    """Budget decision result"""
    allowed: bool
    final_usdt: float
    reason: str
    original_usdt: float
    adjustments: Dict[str, float]  # Adjustment details per stage


class BudgetPolicy:
    """Budget policy engine"""
    
    def __init__(self):
        self.min_order_usdt = env_float("OMA_MIN_ORDER_USDT", default=5.0)
        self.max_order_usdt = env_float("OMA_MAX_ORDER_USDT", default=10000.0)
        self.gazua_budget_protect = env_bool("OMA_GAZUA_BUDGET_PROTECT", default=True)
    
    def compute_allowed_order(
        self,
        *,
        ctx: Any,
        requested_usdt: float,
        strategy: str,
        market_state: str,
        is_entry: bool = True,
    ) -> BudgetDecision:
        """Compute whether an order is allowed and its final amount.

        Decision tree:
        1. emergency_stop check
        2. Minimum order amount check
        3. Per-strategy cap check
        4. GAZUA budget protection (do not exceed allocated budget)
        5. Remaining capital check
        6. Final amount decision
        """
        adjustments: Dict[str, float] = {}
        final = requested_usdt
        
        # 1. Emergency stop
        if getattr(ctx, "emergency_stop", False):
            return BudgetDecision(
                allowed=False,
                final_usdt=0.0,
                reason="emergency_stop",
                original_usdt=requested_usdt,
                adjustments={}
            )
        
        # 2. Minimum order amount
        if final < self.min_order_usdt:
            return BudgetDecision(
                allowed=False,
                final_usdt=0.0,
                reason=f"below_min_order:{final:.0f}<{self.min_order_usdt:.0f}",
                original_usdt=requested_usdt,
                adjustments={}
            )
        
        # 3. Maximum order amount
        if final > self.max_order_usdt:
            adjustments["max_order_cap"] = self.max_order_usdt - final
            final = self.max_order_usdt
        
        # 4. GAZUA budget protection
        if self.gazua_budget_protect and strategy.upper() == "GAZUA":
            allocated = float(getattr(ctx, "allocated_capital", 0.0) or 0.0)
            if allocated > 0 and final > allocated:
                adjustments["gazua_budget_cap"] = allocated - final
                final = allocated
        
        # 5. Remaining capital check
        usable = float(getattr(ctx, "usable_capital", 0.0) or 0.0)
        if usable > 0 and final > usable:
            adjustments["usable_cap"] = usable - final
            final = usable
        
        # 6. Final check
        if final < self.min_order_usdt:
            return BudgetDecision(
                allowed=False,
                final_usdt=0.0,
                reason="adjusted_below_min",
                original_usdt=requested_usdt,
                adjustments=adjustments
            )
        
        return BudgetDecision(
            allowed=True,
            final_usdt=final,
            reason="approved",
            original_usdt=requested_usdt,
            adjustments=adjustments
        )
    
    def get_strategy_multiplier(self, strategy: str) -> float:
        """Return the per-strategy budget multiplier"""
        multipliers = {
            "PINGPONG": 1.0,
            "AUTOLOOP": 1.1,
            "LADDER": 1.4,
            "LIGHTNING": 0.7,
            "GAZUA": 1.2,
        }
        return multipliers.get(strategy.upper(), 1.0)


# Singleton
_budget_policy: Optional[BudgetPolicy] = None

def get_budget_policy() -> BudgetPolicy:
    global _budget_policy
    if _budget_policy is None:
        _budget_policy = BudgetPolicy()
    return _budget_policy
