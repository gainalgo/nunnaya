# ============================================================
# File: app/strategy/strategy_initializer.py
# StrategyPipeline v3-H Final Edition (AI 강화 버전)
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
    Brain → Judge → Risk → Optimizer → Final Fusion 로 이어지는
    강화형 AI 전략 파이프라인.
    """

    def __init__(self):
        self.brain = StrategyBrain()
        self.judge = StrategyJudge()
        self.risk = StrategyRiskAutoRegulator()
        self.optimizer = StrategySelfOptimizer()

    # --------------------------------------------------------
    # 강화된 run()
    # --------------------------------------------------------
    def run(self, market: str, price: float, context=None) -> Dict[str, Any]:
        """
        시장 분석 → 판단 → 위험 조정 → 최적화 → 최종 시그널 결정.
        context가 제공되면 더 많은 시장 히스토리를 기반으로
        Brain이 강화된 정보를 사용할 수 있다.
        """
        import time as _time  # [PERF-TELEMETRY]

        price_history = None
        if context is not None:
            # NOTE:
            # - context 복원/피드 결함 등으로 0, 음수, NaN/inf가 섞일 수 있다.
            # - Brain 내부에서 (last-first)/first 같은 계산을 할 경우 division-by-zero가 날 수 있으므로
            #   여기서 1차 정화한다.
            raw = (getattr(context, "_tick_prices", None) or list(getattr(context, "price_history", [])))[-20:]
            history = []
            for p in raw:
                try:
                    fp = float(p)
                except (TypeError, ValueError) as exc:
                    logger.warning("[strategy_initializer] %s: %s", '여기서 1차 정화한다. except-> continue', exc, exc_info=True)
                    continue
                if (not math.isfinite(fp)) or fp <= 0.0:
                    continue
                history.append(fp)
            price_history = history if len(history) >= 5 else None

        # 1) 시장 분석
        _t_brain = _time.perf_counter()  # [PERF-TELEMETRY]
        brain_out = self.brain.analyze(
            market=market,
            price=price,
            price_history=price_history,
            policy=context.policy if context else None,
            context=context,
        )
        _t_brain_ms = (_time.perf_counter() - _t_brain) * 1000  # [PERF-TELEMETRY]

        # 2) 초기 시그널
        _t_judge = _time.perf_counter()  # [PERF-TELEMETRY]
        decision = self.judge.decide(
            market=market,
            price=price,
            policy=context.policy if context else None,
            brain=brain_out
        )
        _t_judge_ms = (_time.perf_counter() - _t_judge) * 1000  # [PERF-TELEMETRY]

        # 3) 위험 기반 조정
        _t_risk = _time.perf_counter()  # [PERF-TELEMETRY]
        adjusted = self.risk.adjust(
            market=market,
            price=price,
            policy=context.policy if context else None,
            brain=brain_out,
            signal=decision
        )
        _t_risk_ms = (_time.perf_counter() - _t_risk) * 1000  # [PERF-TELEMETRY]

        # 4) 최적화 레이어 (학습 기반 policy 개선)
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

        # 5) 최종 Fusion 시그널 결정
        final_signal = optimized_signal.signal

        return {
            "signal": final_signal,
            "brain": brain_out.to_dict(),
            "judge": decision.to_dict(),
            "risk": adjusted.to_dict(),
            "optimized": optimized_signal.to_dict(),
            # [PERF-TELEMETRY] pipeline 내부 타이밍
            "_perf": {
                "brain_ms": round(_t_brain_ms, 2),
                "judge_ms": round(_t_judge_ms, 2),
                "risk_ms": round(_t_risk_ms, 2),
                "optimizer_ms": round(_t_opt_ms, 2),
            },
        }
