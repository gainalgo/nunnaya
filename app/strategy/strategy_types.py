# ============================================================
# File: app/strategy/strategy_types.py
# ------------------------------------------------------------
# 전략 계층 전체가 공유하는 데이터 구조 정의
# StrategySignal / StrategyPolicy / StrategyBrainOutput
# ============================================================

from __future__ import annotations
from typing import Dict, Any


# ------------------------------------------------------------
# 매매 신호 구조
# ------------------------------------------------------------
class StrategySignal:
    """
    최종 매매 신호 객체.
    signal: "buy", "sell", "hold"
    """

    def __init__(self, signal: str):
        self.signal = signal

    def to_dict(self) -> Dict[str, Any]:
        return {"signal": self.signal}


# ------------------------------------------------------------
# 정책 구조 (단순 Dict 래퍼)
# ------------------------------------------------------------
class StrategyPolicy(dict):
    """
    전략 정책을 나타내는 구조.
    사실상 Dict 기반으로 동작하지만
    타입 안전성과 미묘한 구조 확장을 위해 클래스 래핑.
    """

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


# ------------------------------------------------------------
# 두뇌 출력 구조 (기술적 분석 결과)
# ------------------------------------------------------------
class StrategyBrainOutput:
    """
    Brain 단계에서 계산되는 모든 기술적 지표 값 저장.
    Judge / Risk / Optimizer에서 모두 참조한다.
    """

    def __init__(
        self,
        rsi: float | None,
        macd: float | None,
        macd_signal: float | None,
        macd_histogram: float | None,
        sma: float | None,
        ema: float | None,
        volatility: float | None,
        trend: float | None,
        momentum: float | None,
    ):
        self.rsi = rsi
        self.macd = macd
        self.macd_signal = macd_signal
        self.macd_histogram = macd_histogram
        self.sma = sma
        self.ema = ema
        self.volatility = volatility
        self.trend = trend
        self.momentum = momentum

    # --------------------------------------------------------
    # dict 변환
    # --------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "rsi": self.rsi,
            "macd": self.macd,
            "macd_signal": self.macd_signal,
            "macd_histogram": self.macd_histogram,
            "sma": self.sma,
            "ema": self.ema,
            "volatility": self.volatility,
            "trend": self.trend,
            "momentum": self.momentum,
        }
