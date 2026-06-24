# ============================================================
# File: app/manager/performance_budget.py
# Autocoin OS v3-H — Performance-Based Budget Rebalancer
# ------------------------------------------------------------
# Purpose:
# - Performance-based automatic budget adjustment
# - High-return coins -> increase budget
# - Consecutive-loss coins -> reduce budget or remove
# ============================================================

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.manager.ledger_pnl import MarketFillAgg


@dataclass
class PerformanceMetrics:
    """Per-market performance metrics."""
    market: str
    strategy: str = ""

    # PnL data
    net_cash_usdt: float = 0.0
    trade_count: int = 0
    sell_count: int = 0
    fees_usdt: float = 0.0

    # Budget info
    current_budget_usdt: float = 0.0

    # Performance indicators
    roi_pct: float = 0.0          # Return on Investment %
    net_per_trade: float = 0.0    # net profit per trade
    win_rate: float = 0.0         # win rate (estimated)

    # Time info
    active_age_sec: float = 0.0
    first_ts: float = 0.0
    last_ts: float = 0.0


@dataclass
class BudgetAdjustment:
    """Budget adjustment result."""
    market: str
    strategy: str
    old_budget_usdt: float
    new_budget_usdt: float
    change_pct: float
    action: str  # "increase", "decrease", "remove", "hold"
    reason: str
    metrics: Dict[str, Any] = field(default_factory=dict)


class PerformanceBudgetRebalancer:
    """Performance-based budget rebalancer.

    Algorithm:
    1. Collect per-market performance metrics
    2. Compute ROI and profit per trade
    3. Classify performance grades:
       - STAR: top 20% -> budget +20%
       - GOOD: top 40% -> budget +10%
       - NORMAL: middle -> hold
       - POOR: bottom 30% -> budget -20%
       - FAIL: bottom 10% or consecutive losses -> budget -50% or remove
    4. Adjust within the total budget constraint
    """

    def __init__(
        self,
        # Performance thresholds
        min_trades_for_eval: int = 3,
        min_age_minutes: int = 60,

        # Budget adjustment ratios
        star_increase_pct: float = 20.0,
        good_increase_pct: float = 10.0,
        poor_decrease_pct: float = 20.0,
        fail_decrease_pct: float = 50.0,

        # Constraints
        min_budget_usdt: float = 50.0,
        max_budget_usdt: float = 5000.0,
        max_single_increase_pct: float = 30.0,

        # Removal conditions
        remove_threshold_roi_pct: float = -30.0,
        remove_min_trades: int = 5,
    ):
        self.min_trades_for_eval = min_trades_for_eval
        self.min_age_minutes = min_age_minutes
        
        self.star_increase_pct = star_increase_pct
        self.good_increase_pct = good_increase_pct
        self.poor_decrease_pct = poor_decrease_pct
        self.fail_decrease_pct = fail_decrease_pct
        
        self.min_budget_usdt = min_budget_usdt
        self.max_budget_usdt = max_budget_usdt
        self.max_single_increase_pct = max_single_increase_pct
        
        self.remove_threshold_roi_pct = remove_threshold_roi_pct
        self.remove_min_trades = remove_min_trades

    def calculate_metrics(
        self,
        pnl_agg: MarketFillAgg,
        current_budget_usdt: float,
        strategy: str = "",
        active_since_ts: float = 0.0,
    ) -> PerformanceMetrics:
        """Compute performance metrics from PnL data."""
        now = time.time()
        age_sec = now - active_since_ts if active_since_ts > 0 else 0.0
        
        m = PerformanceMetrics(
            market=pnl_agg.market,
            strategy=strategy,
            net_cash_usdt=pnl_agg.net_cash_usdt,
            trade_count=pnl_agg.trade_n,
            sell_count=pnl_agg.sell_n,
            fees_usdt=pnl_agg.fees_usdt,
            current_budget_usdt=current_budget_usdt,
            active_age_sec=age_sec,
            first_ts=pnl_agg.first_ts,
            last_ts=pnl_agg.last_ts,
        )
        
        # ROI calculation
        if current_budget_usdt > 0:
            m.roi_pct = (pnl_agg.net_cash_usdt / current_budget_usdt) * 100

        # Profit per trade
        if pnl_agg.trade_n > 0:
            m.net_per_trade = pnl_agg.net_cash_usdt / pnl_agg.trade_n

        # Win rate estimate (based on sell proceeds)
        if pnl_agg.sell_n > 0:
            avg_sell = pnl_agg.sell_funds_usdt / pnl_agg.sell_n
            avg_buy = pnl_agg.buy_funds_usdt / pnl_agg.buy_n if pnl_agg.buy_n > 0 else 0
            if avg_buy > 0:
                m.win_rate = min(1.0, max(0.0, avg_sell / avg_buy))
        
        return m

    def classify_performance(
        self,
        metrics: List[PerformanceMetrics],
    ) -> Dict[str, str]:
        """Classify performance grades.

        Returns:
            {market: grade} where grade in ["STAR", "GOOD", "NORMAL", "POOR", "FAIL", "SKIP"]
        """
        result: Dict[str, str] = {}
        
        # Filter to only evaluable markets
        evaluable = []
        for m in metrics:
            # Minimum conditions not met -> SKIP
            if m.trade_count < self.min_trades_for_eval:
                result[m.market] = "SKIP"
                continue
            if m.active_age_sec < self.min_age_minutes * 60:
                result[m.market] = "SKIP"
                continue
            evaluable.append(m)
        
        if not evaluable:
            return result
        
        # Sort by ROI
        evaluable.sort(key=lambda x: x.roi_pct, reverse=True)
        n = len(evaluable)

        for i, m in enumerate(evaluable):
            percentile = i / n  # 0.0 = top, 1.0 = bottom

            # Check removal condition
            if m.roi_pct <= self.remove_threshold_roi_pct and m.trade_count >= self.remove_min_trades:
                result[m.market] = "FAIL"
            elif percentile < 0.20:
                result[m.market] = "STAR"
            elif percentile < 0.40:
                result[m.market] = "GOOD"
            elif percentile < 0.70:
                result[m.market] = "NORMAL"
            elif percentile < 0.90:
                result[m.market] = "POOR"
            else:
                result[m.market] = "FAIL"
        
        return result

    def calculate_adjustments(
        self,
        metrics: List[PerformanceMetrics],
        total_capital_usdt: float,
        grades: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[BudgetAdjustment], Dict[str, Any]]:
        """Compute budget adjustments.

        Args:
            metrics: per-market performance metrics
            total_capital_usdt: total available capital
            grades: pre-computed grades (auto-computed if omitted)

        Returns:
            (list of adjustments, summary info)
        """
        if grades is None:
            grades = self.classify_performance(metrics)
        
        adjustments: List[BudgetAdjustment] = []
        metrics_map = {m.market: m for m in metrics}
        
        for market, grade in grades.items():
            m = metrics_map.get(market)
            if not m:
                continue
            
            old_budget_usdt = m.current_budget_usdt
            new_budget_usdt = old_budget_usdt
            action = "hold"
            reason = f"grade:{grade}"
            
            if grade == "SKIP":
                action = "skip"
                reason = "insufficient_data"
            elif grade == "STAR":
                increase = min(self.star_increase_pct, self.max_single_increase_pct)
                new_budget_usdt = old_budget_usdt * (1 + increase / 100)
                action = "increase"
                reason = f"star_performer:+{increase:.0f}%"
            elif grade == "GOOD":
                increase = min(self.good_increase_pct, self.max_single_increase_pct)
                new_budget_usdt = old_budget_usdt * (1 + increase / 100)
                action = "increase"
                reason = f"good_performer:+{increase:.0f}%"
            elif grade == "NORMAL":
                action = "hold"
                reason = "normal_performer"
            elif grade == "POOR":
                new_budget_usdt = old_budget_usdt * (1 - self.poor_decrease_pct / 100)
                action = "decrease"
                reason = f"poor_performer:-{self.poor_decrease_pct:.0f}%"
            elif grade == "FAIL":
                if m.roi_pct <= self.remove_threshold_roi_pct:
                    new_budget_usdt = 0
                    action = "remove"
                    reason = f"fail_remove:roi={m.roi_pct:.1f}%"
                else:
                    new_budget_usdt = old_budget_usdt * (1 - self.fail_decrease_pct / 100)
                    action = "decrease"
                    reason = f"fail_performer:-{self.fail_decrease_pct:.0f}%"
            
            # Apply constraints
            if action != "remove" and new_budget_usdt > 0:
                new_budget_usdt = max(self.min_budget_usdt, min(self.max_budget_usdt, new_budget_usdt))
                if new_budget_usdt >= 1000:
                    new_budget_usdt = round(new_budget_usdt / 1000) * 1000
            
            change_pct = 0.0
            if old_budget_usdt > 0:
                change_pct = ((new_budget_usdt - old_budget_usdt) / old_budget_usdt) * 100
            
            adjustments.append(BudgetAdjustment(
                market=market,
                strategy=m.strategy,
                old_budget_usdt=old_budget_usdt,
                new_budget_usdt=new_budget_usdt,
                change_pct=round(change_pct, 2),
                action=action,
                reason=reason,
                metrics={
                    "roi_pct": round(m.roi_pct, 2),
                    "net_cash_usdt": round(m.net_cash_usdt, 2),
                    "trade_count": m.trade_count,
                    "net_per_trade": round(m.net_per_trade, 2),
                    "active_age_min": round(m.active_age_sec / 60, 1),
                },
            ))
        
        # Check total budget constraint
        total_new = sum(a.new_budget_usdt for a in adjustments if a.action != "skip")
        scale = 1.0
        if total_new > total_capital_usdt and total_new > 0:
            scale = total_capital_usdt / total_new
            for a in adjustments:
                if a.action not in ("skip", "remove"):
                    a.new_budget_usdt = round((a.new_budget_usdt * scale) / 1000) * 1000
                    a.change_pct = ((a.new_budget_usdt - a.old_budget_usdt) / a.old_budget_usdt * 100) if a.old_budget_usdt > 0 else 0
        
        summary = {
            "ts": time.time(),
            "total_capital_usdt": total_capital_usdt,
            "total_old_budget_usdt": sum(m.current_budget_usdt for m in metrics),
            "total_new_budget_usdt": sum(a.new_budget_usdt for a in adjustments),
            "scale_applied": round(scale, 3),
            "grades": dict(grades),
            "star_count": sum(1 for g in grades.values() if g == "STAR"),
            "good_count": sum(1 for g in grades.values() if g == "GOOD"),
            "poor_count": sum(1 for g in grades.values() if g == "POOR"),
            "fail_count": sum(1 for g in grades.values() if g == "FAIL"),
            "remove_count": sum(1 for a in adjustments if a.action == "remove"),
        }
        
        return adjustments, summary


def get_budget_adjustment_recommendation(
    net_cash_usdt: float,
    trade_count: int,
    current_budget: float,
    roi_pct: float,
    min_trades: int = 3,
) -> Tuple[str, float, str]:
    """Simple budget adjustment recommendation.

    Returns:
        (action, multiplier, reason)
    """
    if trade_count < min_trades:
        return ("hold", 1.0, "insufficient_trades")
    
    if roi_pct >= 10:
        return ("increase", 1.20, f"high_roi:{roi_pct:.1f}%")
    elif roi_pct >= 5:
        return ("increase", 1.10, f"good_roi:{roi_pct:.1f}%")
    elif roi_pct >= 0:
        return ("hold", 1.0, f"neutral_roi:{roi_pct:.1f}%")
    elif roi_pct >= -10:
        return ("decrease", 0.90, f"low_roi:{roi_pct:.1f}%")
    elif roi_pct >= -20:
        return ("decrease", 0.70, f"poor_roi:{roi_pct:.1f}%")
    else:
        return ("remove", 0.0, f"fail_roi:{roi_pct:.1f}%")
