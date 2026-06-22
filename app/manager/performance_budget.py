# ============================================================
# File: app/manager/performance_budget.py
# Autocoin OS v3-H — Performance-Based Budget Rebalancer
# ------------------------------------------------------------
# 목적:
# - 성과 기반 예산 자동 조정
# - 수익률 좋은 코인 → 예산 증액
# - 연속 손실 코인 → 예산 감액 또는 퇴출
# ============================================================

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.manager.ledger_pnl import MarketFillAgg


@dataclass
class PerformanceMetrics:
    """마켓별 성과 메트릭."""
    market: str
    strategy: str = ""
    
    # PnL 데이터
    net_cash_usdt: float = 0.0
    trade_count: int = 0
    sell_count: int = 0
    fees_usdt: float = 0.0
    
    # 예산 정보
    current_budget_usdt: float = 0.0
    
    # 성과 지표
    roi_pct: float = 0.0          # Return on Investment %
    net_per_trade: float = 0.0    # 거래당 순수익
    win_rate: float = 0.0         # 승률 (추정)
    
    # 시간 정보
    active_age_sec: float = 0.0
    first_ts: float = 0.0
    last_ts: float = 0.0


@dataclass
class BudgetAdjustment:
    """예산 조정 결과."""
    market: str
    strategy: str
    old_budget_usdt: float
    new_budget_usdt: float
    change_pct: float
    action: str  # "increase", "decrease", "remove", "hold"
    reason: str
    metrics: Dict[str, Any] = field(default_factory=dict)


class PerformanceBudgetRebalancer:
    """성과 기반 예산 리밸런서.
    
    알고리즘:
    1. 마켓별 성과 메트릭 수집
    2. ROI 및 거래당 수익 계산
    3. 성과 등급 분류:
       - STAR: 상위 20% → 예산 +20%
       - GOOD: 상위 40% → 예산 +10%
       - NORMAL: 중간 → 유지
       - POOR: 하위 30% → 예산 -20%
       - FAIL: 하위 10% 또는 연속 손실 → 예산 -50% 또는 퇴출
    4. 총 예산 제약 내에서 조정
    """

    def __init__(
        self,
        # 성과 기준
        min_trades_for_eval: int = 3,
        min_age_minutes: int = 60,
        
        # 예산 조정 비율
        star_increase_pct: float = 20.0,
        good_increase_pct: float = 10.0,
        poor_decrease_pct: float = 20.0,
        fail_decrease_pct: float = 50.0,
        
        # 제약
        min_budget_usdt: float = 50.0,
        max_budget_usdt: float = 5000.0,
        max_single_increase_pct: float = 30.0,
        
        # 퇴출 조건
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
        """PnL 데이터에서 성과 메트릭 계산."""
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
        
        # ROI 계산
        if current_budget_usdt > 0:
            m.roi_pct = (pnl_agg.net_cash_usdt / current_budget_usdt) * 100
        
        # 거래당 수익
        if pnl_agg.trade_n > 0:
            m.net_per_trade = pnl_agg.net_cash_usdt / pnl_agg.trade_n
        
        # 승률 추정 (sell 수익 기반)
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
        """성과 등급 분류.
        
        Returns:
            {market: grade} where grade in ["STAR", "GOOD", "NORMAL", "POOR", "FAIL", "SKIP"]
        """
        result: Dict[str, str] = {}
        
        # 평가 가능한 마켓만 필터
        evaluable = []
        for m in metrics:
            # 최소 조건 미충족 → SKIP
            if m.trade_count < self.min_trades_for_eval:
                result[m.market] = "SKIP"
                continue
            if m.active_age_sec < self.min_age_minutes * 60:
                result[m.market] = "SKIP"
                continue
            evaluable.append(m)
        
        if not evaluable:
            return result
        
        # ROI 기준 정렬
        evaluable.sort(key=lambda x: x.roi_pct, reverse=True)
        n = len(evaluable)
        
        for i, m in enumerate(evaluable):
            percentile = i / n  # 0.0 = 최상위, 1.0 = 최하위
            
            # 퇴출 조건 체크
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
        """예산 조정 계산.
        
        Args:
            metrics: 마켓별 성과 메트릭
            total_capital_usdt: 총 가용 자본
            grades: 미리 계산된 등급 (없으면 자동 계산)
            
        Returns:
            (조정 리스트, 요약 정보)
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
            
            # 제약 적용
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
        
        # 총 예산 제약 체크
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
    """단순 예산 조정 추천.
    
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
