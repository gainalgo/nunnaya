# ============================================================
# File: app/strategy/strategy_judge.py
# ------------------------------------------------------------
# StrategyJudge
# - Brain 분석결과 + 정책 기반으로 buy/sell/hold 의사결정을 수행한다.
# ============================================================

from __future__ import annotations
from typing import Dict, Any

from .strategy_types import (
    StrategySignal,
    StrategyPolicy,
    StrategyBrainOutput,
)


class StrategyJudge:
    """
    BrainOutput + Policy 조합으로 매수/매도/홀드 신호를 판단하는 모듈.
    """

    # --------------------------------------------------------
    # 메인 판단 함수
    # --------------------------------------------------------
    def decide(
        self,
        market: str,
        price: float,
        policy: StrategyPolicy,
        brain: StrategyBrainOutput
    ) -> StrategySignal:
        """
        매수/매도/홀드 결정을 내리는 핵심 로직.
        """

        rsi_val = brain.rsi
        macd_hist = brain.macd_histogram
        trend_val = brain.trend

        # --------------------------
        # 간단한 RSI 기반 조건
        # --------------------------
        if rsi_val is not None:
            if rsi_val < policy.get("rsi_low", 30):
                return StrategySignal("buy")
            if rsi_val > policy.get("rsi_high", 70):
                return StrategySignal("sell")

        # --------------------------
        # MACD Histogram 기반 모멘텀 판단
        # --------------------------
        if macd_hist is not None:
            if macd_hist > 0:
                if trend_val is not None and trend_val > 0:
                    return StrategySignal("buy")
            elif macd_hist < 0:
                if trend_val is not None and trend_val < 0:
                    return StrategySignal("sell")

        # --------------------------
        # 기본: Hold
        # --------------------------
        return StrategySignal("hold")
