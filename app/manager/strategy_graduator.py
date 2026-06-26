# ============================================================
# File: app/manager/strategy_graduator.py
# Autocoin OS v3-H — Strategy Graduation System
# ------------------------------------------------------------
# Purpose:
# - Detect coin state changes -> auto strategy transition
# - LIGHTNING (discovery) -> profit locked -> PINGPONG (stabilize)
# - LADDER (bottom accumulation) -> on bounce -> GAZUA (hold)
# ============================================================

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class GraduationPath(Enum):
    """Strategy graduation path."""
    LIGHTNING_TO_PINGPONG = "LIGHTNING→PINGPONG"
    LADDER_TO_GAZUA = "LADDER→GAZUA"
    GAZUA_TO_PINGPONG = "GAZUA→PINGPONG"
    PINGPONG_TO_AUTOLOOP = "PINGPONG→AUTOLOOP"
    DEMOTION = "DEMOTION"  # downward transition


@dataclass
class GraduationCondition:
    """Graduation condition."""
    min_roi_pct: float = 0.0
    min_trades: int = 0
    min_sells: int = 0
    min_age_hours: float = 0.0

    # AI feature conditions
    min_momentum: Optional[float] = None
    max_volatility: Optional[float] = None
    min_trend: Optional[float] = None

    # Price condition
    price_above_avg_pct: Optional[float] = None  # vs average buy price

    # Extra condition
    requires_profit_lock: bool = False  # profit lock required


@dataclass
class MarketContext:
    """Market context (for graduation decisions)."""
    market: str
    current_strategy: str

    # Performance data
    roi_pct: float = 0.0
    trade_count: int = 0
    sell_count: int = 0
    net_cash_usdt: float = 0.0

    # Time data
    active_age_hours: float = 0.0

    # Price data
    current_price: float = 0.0
    avg_buy_price: float = 0.0

    # AI features
    momentum: float = 0.0
    volatility: float = 0.0
    trend: float = 0.0
    ai_prediction: float = 0.5
    rsi: float = 50.0

    # Position
    position_qty: float = 0.0
    position_value_usdt: float = 0.0


@dataclass
class GraduationDecision:
    """Graduation decision result."""
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
    """Strategy graduation system.

    Graduation paths:
    1. LIGHTNING -> PINGPONG
       - Condition: profit locked (ROI > 5%), 3+ trades
       - Rationale: discovery succeeded -> pursue stable profit

    2. LADDER -> GAZUA
       - Condition: bounce detected (momentum > 0.3, trend > 0), position secured
       - Rationale: bottom accumulation done -> wait for rally

    3. GAZUA -> PINGPONG
       - Condition: target reached (ROI > 30%) or momentum weakening
       - Rationale: long hold done -> realize profit

    4. PINGPONG -> AUTOLOOP
       - Condition: stable profit (ROI > 10%, 10+ trades)
       - Rationale: validated -> auto loop
    """

    def __init__(self):
        self.graduation_paths = self._init_graduation_paths()

    def _init_graduation_paths(self) -> Dict[str, Dict[str, GraduationCondition]]:
        """Initialize conditions per graduation path."""
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
                    min_roi_pct=-5.0,  # loss allowed (accumulating)
                    min_trades=2,
                    min_age_hours=4.0,
                    min_momentum=0.3,
                    min_trend=0.0,
                    price_above_avg_pct=5.0,  # average buy price +5% or more
                ),
            },
            "GAZUA": {
                "PINGPONG": GraduationCondition(
                    min_roi_pct=20.0,
                    min_trades=1,
                    min_sells=1,
                    min_age_hours=12.0,
                    max_volatility=0.05,  # stabilization required
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
        """Evaluate whether to graduate."""
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
        """Evaluate a specific path."""
        met: List[str] = []
        failed: List[str] = []

        # ROI check
        if ctx.roi_pct >= cond.min_roi_pct:
            met.append(f"roi:{ctx.roi_pct:.1f}%>={cond.min_roi_pct}%")
        else:
            failed.append(f"roi:{ctx.roi_pct:.1f}%<{cond.min_roi_pct}%")
        
        # Trade count check
        if ctx.trade_count >= cond.min_trades:
            met.append(f"trades:{ctx.trade_count}>={cond.min_trades}")
        else:
            failed.append(f"trades:{ctx.trade_count}<{cond.min_trades}")
        
        # Sell count check
        if ctx.sell_count >= cond.min_sells:
            met.append(f"sells:{ctx.sell_count}>={cond.min_sells}")
        else:
            failed.append(f"sells:{ctx.sell_count}<{cond.min_sells}")
        
        # Active age check
        if ctx.active_age_hours >= cond.min_age_hours:
            met.append(f"age:{ctx.active_age_hours:.1f}h>={cond.min_age_hours}h")
        else:
            failed.append(f"age:{ctx.active_age_hours:.1f}h<{cond.min_age_hours}h")
        
        # AI feature check
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
        
        # Price condition check
        if cond.price_above_avg_pct is not None and ctx.avg_buy_price > 0:
            price_vs_avg = ((ctx.current_price - ctx.avg_buy_price) / ctx.avg_buy_price) * 100
            if price_vs_avg >= cond.price_above_avg_pct:
                met.append(f"price_vs_avg:{price_vs_avg:.1f}%>={cond.price_above_avg_pct}%")
            else:
                failed.append(f"price_vs_avg:{price_vs_avg:.1f}%<{cond.price_above_avg_pct}%")
        
        # Graduation decision
        total = len(met) + len(failed)
        confidence = len(met) / total if total > 0 else 0.0
        should_graduate = len(failed) == 0 and len(met) > 0

        # Determine path
        path = self._get_path(ctx.current_strategy.upper(), to_strategy)

        # Recommended actions
        actions: List[str] = []
        if should_graduate:
            actions.append(f"switch_to:{to_strategy}")
            if cond.requires_profit_lock:
                actions.append("lock_profit_first")
        else:
            # Hint the closest condition
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
        """Return the path enum."""
        key = f"{from_s}→{to_s}"
        for p in GraduationPath:
            if p.value == key:
                return p
        return None

    def batch_evaluate(
        self,
        contexts: List[MarketContext],
    ) -> Tuple[List[GraduationDecision], Dict[str, Any]]:
        """Batch-evaluate multiple markets."""
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
    """Recommend a strategy based on AI features.

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
    
    # LADDER: high volatility + downtrend (scaled buying)
    if volatility > 0.03 and momentum < 0 and trend < 0:
        scores["LADDER"] = 0.8 + volatility * 2
        if rsi < 30:
            scores["LADDER"] += 0.2
    
    # LIGHTNING: strong momentum + upside breakout (scalp)
    if momentum > 0.5 and volatility > 0.02:
        scores["LIGHTNING"] = 0.7 + momentum * 0.5
        if trend > 0:
            scores["LIGHTNING"] += 0.2
    
    # GAZUA: AI bullish prediction + undervalued (trend following)
    if ai_prediction > 0.6 and trend >= 0:
        scores["GAZUA"] = 0.6 + ai_prediction * 0.4
        if 30 <= rsi <= 60:
            scores["GAZUA"] += 0.2
    
    # AUTOLOOP: mid volatility + ranging (auto range trading)
    if 0.015 < volatility < 0.05 and abs(trend) < 1.0:
        scores["AUTOLOOP"] = 0.65 + (0.05 - abs(trend) * 0.01)
        if 35 <= rsi <= 65:
            scores["AUTOLOOP"] += 0.15
    
    # PINGPONG: stable low volatility (range trading)
    if volatility < 0.02 and abs(momentum) < 0.3:
        scores["PINGPONG"] = 0.7
        if 40 <= rsi <= 60:
            scores["PINGPONG"] += 0.2
    
    # Pick the highest-scoring strategy
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
