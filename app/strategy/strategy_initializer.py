# ============================================================
# File: app/strategy/strategy_initializer.py
# StrategyPipeline v3-H Final Edition (AI-enhanced version)
# ============================================================

from __future__ import annotations
import logging
from typing import Dict, Any
import math

logger = logging.getLogger(__name__)

from app.strategy.strategy_brain import StrategyBrain
from app.strategy.strategy_judge import StrategyJudge
from app.strategy.strategy_risk_auto_regulator import StrategyRiskAutoRegulator
from app.strategy.strategy_self_optimizer import StrategySelfOptimizer

class StrategyPipeline:
    """
    v3-H Final Edition:
    An enhanced AI strategy pipeline that flows through
    Brain → Judge → Risk → Optimizer → Final Fusion.
    """

    def __init__(self):
        self.brain = StrategyBrain()
        self.judge = StrategyJudge()
        self.risk = StrategyRiskAutoRegulator()
        self.optimizer = StrategySelfOptimizer()

    # --------------------------------------------------------
    # Enhanced run()
    # --------------------------------------------------------
    def run(self, market: str, price: float, context=None) -> Dict[str, Any]:
        """
        Market analysis → judgment → risk adjustment → optimization → final signal decision.
        When context is provided, Brain can use enhanced information
        based on a longer market history.
        """
        import time as _time  # [PERF-TELEMETRY]

        price_history = None
        if context is not None:
            # NOTE:
            # - Zeros, negatives, or NaN/inf may slip in due to context restore/feed defects.
            # - Brain may compute things like (last-first)/first which can raise division-by-zero,
            #   so we sanitize the data here as a first pass.
            raw = (getattr(context, "_tick_prices", None) or list(getattr(context, "price_history", [])))[-20:]
            history = []
            for p in raw:
                try:
                    fp = float(p)
                except (TypeError, ValueError) as exc:
                    logger.warning("[strategy_initializer] %s: %s", 'first-pass sanitize except-> continue', exc, exc_info=True)
                    continue
                if (not math.isfinite(fp)) or fp <= 0.0:
                    continue
                history.append(fp)
            price_history = history if len(history) >= 5 else None

        # 1) Market analysis
        _t_brain = _time.perf_counter()  # [PERF-TELEMETRY]
        brain_out = self.brain.analyze(
            market=market,
            price=price,
            price_history=price_history,
            policy=context.policy if context else None,
            context=context,
        )
        _t_brain_ms = (_time.perf_counter() - _t_brain) * 1000  # [PERF-TELEMETRY]

        # 2) Initial signal
        _t_judge = _time.perf_counter()  # [PERF-TELEMETRY]
        decision = self.judge.decide(
            market=market,
            price=price,
            policy=context.policy if context else None,
            brain=brain_out
        )
        _t_judge_ms = (_time.perf_counter() - _t_judge) * 1000  # [PERF-TELEMETRY]

        # 3) Risk-based adjustment
        _t_risk = _time.perf_counter()  # [PERF-TELEMETRY]
        adjusted = self.risk.adjust(
            market=market,
            price=price,
            policy=context.policy if context else None,
            brain=brain_out,
            signal=decision
        )
        _t_risk_ms = (_time.perf_counter() - _t_risk) * 1000  # [PERF-TELEMETRY]

        # 4) Optimization layer (learning-based policy improvement)
        _t_opt = _time.perf_counter()  # [PERF-TELEMETRY]
        optimized_signal = self.optimizer.refine(
            market=market,
            price=price,
            policy=context.policy if context else None,
            brain=brain_out,
            signal=adjusted,
            context=context
        )
        _t_opt_ms = (_time.perf_counter() - _t_opt) * 1000  # [PERF-TELEMETRY]

        # 5) Final Fusion signal decision
        final_signal = optimized_signal.signal

        return {
            "signal": final_signal,
            "brain": brain_out.to_dict(),
            "judge": decision.to_dict(),
            "risk": adjusted.to_dict(),
            "optimized": optimized_signal.to_dict(),
            # [PERF-TELEMETRY] internal pipeline timing
            "_perf": {
                "brain_ms": round(_t_brain_ms, 2),
                "judge_ms": round(_t_judge_ms, 2),
                "risk_ms": round(_t_risk_ms, 2),
                "optimizer_ms": round(_t_opt_ms, 2),
            },
        }
