"""
Budget Policy Module
- 모든 주문의 예산 계산/검증을 단일 지점에서 처리
- GAZUA 예산 보호, 전략별 제한, 전역 제한 등 통합

[MIGRATED 2026-01-23] CoinStock → Autocoin
- USDT 기반 주문
- min_order: 10 USDT → 5 USDT
- max_order: 1,000 USDT → 1,000 USDT
"""

from __future__ import annotations
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass

from app.core.constants import env_float, env_bool


@dataclass
class BudgetDecision:
    """예산 결정 결과"""
    allowed: bool
    final_usdt: float
    reason: str
    original_usdt: float
    adjustments: Dict[str, float]  # 각 단계별 조정 내역


class BudgetPolicy:
    """예산 정책 엔진"""
    
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
        """주문 허용 여부 및 최종 금액 계산.
        
        결정 트리:
        1. emergency_stop 체크
        2. 최소 주문 금액 체크
        3. 전략별 상한 체크
        4. GAZUA 예산 보호 (할당된 예산 초과 금지)
        5. 잔여 자본 체크
        6. 최종 금액 결정
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
        
        # 2. 최소 주문 금액
        if final < self.min_order_usdt:
            return BudgetDecision(
                allowed=False,
                final_usdt=0.0,
                reason=f"below_min_order:{final:.0f}<{self.min_order_usdt:.0f}",
                original_usdt=requested_usdt,
                adjustments={}
            )
        
        # 3. 최대 주문 금액
        if final > self.max_order_usdt:
            adjustments["max_order_cap"] = self.max_order_usdt - final
            final = self.max_order_usdt
        
        # 4. GAZUA 예산 보호
        if self.gazua_budget_protect and strategy.upper() == "GAZUA":
            allocated = float(getattr(ctx, "allocated_capital", 0.0) or 0.0)
            if allocated > 0 and final > allocated:
                adjustments["gazua_budget_cap"] = allocated - final
                final = allocated
        
        # 5. 잔여 자본 체크
        usable = float(getattr(ctx, "usable_capital", 0.0) or 0.0)
        if usable > 0 and final > usable:
            adjustments["usable_cap"] = usable - final
            final = usable
        
        # 6. 최종 체크
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
        """전략별 예산 배수 반환"""
        multipliers = {
            "PINGPONG": 1.0,
            "AUTOLOOP": 1.1,
            "LADDER": 1.4,
            "LIGHTNING": 0.7,
            "GAZUA": 1.2,
        }
        return multipliers.get(strategy.upper(), 1.0)


# 싱글톤
_budget_policy: Optional[BudgetPolicy] = None

def get_budget_policy() -> BudgetPolicy:
    global _budget_policy
    if _budget_policy is None:
        _budget_policy = BudgetPolicy()
    return _budget_policy
