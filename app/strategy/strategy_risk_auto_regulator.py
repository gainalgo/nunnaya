# ============================================================
# File: app/strategy/strategy_risk_auto_regulator.py
# ------------------------------------------------------------
# StrategyRiskAutoRegulator
# - Judge가 만든 신호를 시장 리스크 상황에 따라 조정한다.
# ============================================================

from __future__ import annotations
from typing import Dict, Any

from .strategy_types import (
    StrategyPolicy,
    StrategySignal,
    StrategyBrainOutput,
)


class StrategyRiskAutoRegulator:
    """
    Brain/Policy 데이터를 기반으로 Judge의 신호를 조절하는 레이어.
    """

    # --------------------------------------------------------
    # 메인 리스크 조정 함수
    # --------------------------------------------------------
    def adjust(
        self,
        market: str,
        price: float,
        policy: StrategyPolicy,
        brain: StrategyBrainOutput,
        signal: StrategySignal
    ) -> StrategySignal:
        """
        Judge 신호를 그대로 사용할지, 위험하면 hold로 변경할지 판단한다.
        """

        volatility = brain.volatility
        trend_val = brain.trend

        # --------------------------
        # 시장 변동성이 너무 높으면 거래 중단
        # --------------------------
        if volatility is not None and volatility > policy.get("max_volatility", 5.0):
            return StrategySignal("hold")

        # --------------------------
        # 하락 추세가 매우 강한데 buy 신호가 나오면 무시
        # --------------------------
        if signal.signal == "buy":
            if trend_val is not None and trend_val < policy.get("min_uptrend", -2.0):
                return StrategySignal("hold")

        # --------------------------
        # 상승 추세가 강한데 sell 신호는 약간 보류
        # --------------------------
        if signal.signal == "sell":
            if trend_val is not None and trend_val > policy.get("max_downtrend", 2.0):
                return StrategySignal("hold")

        # --------------------------
        # 기본: 신호 유지
        # --------------------------
        return signal
