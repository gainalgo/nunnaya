# ============================================================
# File: app/manager/ai_position_sizing.py
# Autocoin OS v3-H — AI Confidence-Based Position Sizing
# ------------------------------------------------------------
# 목적:
# - AI 예측 신뢰도에 따른 포지션 크기 조정
# - 신뢰도 높으면 예산 증액
# - 신뢰도 낮으면 예산 감액
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class AISignal:
    """AI 신호 데이터."""
    market: str
    
    # 핵심 예측
    prediction: float = 0.5      # 0.0 = 강한 하락, 1.0 = 강한 상승
    confidence: float = 0.5      # 0.0 = 불확실, 1.0 = 확실
    
    # 보조 지표
    momentum: float = 0.0
    volatility: float = 0.0
    trend: float = 0.0
    rsi: float = 50.0
    
    # 메타
    model_version: str = ""
    timestamp: float = 0.0


@dataclass
class SizingDecision:
    """포지션 사이징 결정."""
    market: str
    base_budget: float
    adjusted_budget: float
    multiplier: float
    confidence_tier: str  # "high", "medium", "low", "very_low"
    reason: str
    details: Dict[str, Any]


class AIPositionSizer:
    """AI 신뢰도 기반 포지션 사이저.
    
    신뢰도 티어:
    - HIGH (>0.8): +30% 예산
    - MEDIUM (0.6-0.8): 기본 예산
    - LOW (0.4-0.6): -20% 예산
    - VERY_LOW (<0.4): -40% 예산 또는 진입 거부
    """

    def __init__(
        self,
        high_confidence_threshold: float = 0.8,
        medium_confidence_threshold: float = 0.6,
        low_confidence_threshold: float = 0.4,
        
        high_multiplier: float = 1.3,
        medium_multiplier: float = 1.0,
        low_multiplier: float = 0.8,
        very_low_multiplier: float = 0.6,
        
        min_confidence_for_entry: float = 0.35,
        
        # 추가 보정 요소
        volatility_penalty_threshold: float = 0.05,
        momentum_boost_threshold: float = 0.5,
    ):
        self.high_conf = high_confidence_threshold
        self.medium_conf = medium_confidence_threshold
        self.low_conf = low_confidence_threshold
        
        self.high_mult = high_multiplier
        self.medium_mult = medium_multiplier
        self.low_mult = low_multiplier
        self.very_low_mult = very_low_multiplier
        
        self.min_confidence = min_confidence_for_entry
        self.volatility_penalty = volatility_penalty_threshold
        self.momentum_boost = momentum_boost_threshold

    def calculate_sizing(
        self,
        signal: AISignal,
        base_budget: float,
    ) -> SizingDecision:
        """포지션 사이징 계산."""
        conf = signal.confidence
        
        # 티어 결정
        if conf >= self.high_conf:
            tier = "high"
            base_mult = self.high_mult
        elif conf >= self.medium_conf:
            tier = "medium"
            base_mult = self.medium_mult
        elif conf >= self.low_conf:
            tier = "low"
            base_mult = self.low_mult
        else:
            tier = "very_low"
            base_mult = self.very_low_mult
        
        # 보정 요소 적용
        adjustments = []
        final_mult = base_mult
        
        # 1. 변동성 페널티
        if signal.volatility > self.volatility_penalty:
            vol_penalty = min(0.2, (signal.volatility - self.volatility_penalty) * 2)
            final_mult -= vol_penalty
            adjustments.append(f"vol_penalty:-{vol_penalty:.2f}")
        
        # 2. 모멘텀 부스트 (같은 방향이면)
        if signal.prediction > 0.5 and signal.momentum > self.momentum_boost:
            mom_boost = min(0.15, (signal.momentum - self.momentum_boost) * 0.3)
            final_mult += mom_boost
            adjustments.append(f"momentum_boost:+{mom_boost:.2f}")
        elif signal.prediction < 0.5 and signal.momentum < -self.momentum_boost:
            mom_boost = min(0.15, (abs(signal.momentum) - self.momentum_boost) * 0.3)
            final_mult += mom_boost
            adjustments.append(f"momentum_boost:+{mom_boost:.2f}")
        
        # 3. RSI 극단값 조정
        if signal.rsi < 25 or signal.rsi > 75:
            # 과매수/과매도 → 약간 보수적
            final_mult *= 0.9
            adjustments.append(f"rsi_extreme:{signal.rsi:.0f}")
        
        # 최소/최대 제한
        final_mult = max(0.3, min(1.5, final_mult))
        
        adjusted_budget = base_budget * final_mult
        # [FIX 2026-01-23] round() → floor() + 최소주문 보장
        # round()는 소수점 이하를 0으로 만들 수 있음
        MIN_ORDER_USDT = 5.0  # Bybit 최소 주문 금액
        adjusted_budget = int(adjusted_budget * 100) / 100  # USDT 0.01 단위
        adjusted_budget = max(MIN_ORDER_USDT, adjusted_budget)  # 최소주문 보장
        
        reason_parts = [f"conf:{conf:.2f}", f"tier:{tier}", f"mult:{final_mult:.2f}"]
        if adjustments:
            reason_parts.extend(adjustments)
        
        return SizingDecision(
            market=signal.market,
            base_budget=base_budget,
            adjusted_budget=adjusted_budget,
            multiplier=final_mult,
            confidence_tier=tier,
            reason=",".join(reason_parts),
            details={
                "confidence": conf,
                "prediction": signal.prediction,
                "volatility": signal.volatility,
                "momentum": signal.momentum,
                "rsi": signal.rsi,
                "base_multiplier": base_mult,
                "adjustments": adjustments,
            },
        )

    def should_enter(self, signal: AISignal) -> Tuple[bool, str]:
        """진입 여부 결정."""
        if signal.confidence < self.min_confidence:
            return (False, f"low_confidence:{signal.confidence:.2f}<{self.min_confidence}")
        
        # 예측 방향과 현재 상태 일치 여부
        if signal.prediction > 0.5:
            # 상승 예측
            if signal.momentum < -0.5:
                return (False, "prediction_momentum_conflict:up_pred_but_strong_down_mom")
        else:
            # 하락 예측
            if signal.momentum > 0.5:
                return (False, "prediction_momentum_conflict:down_pred_but_strong_up_mom")
        
        return (True, f"entry_ok:conf={signal.confidence:.2f},pred={signal.prediction:.2f}")

    def get_recommended_strategy(
        self,
        signal: AISignal,
    ) -> Tuple[str, float, str]:
        """신호 기반 전략 추천.
        
        Returns:
            (strategy, confidence, reason)
        """
        pred = signal.prediction
        conf = signal.confidence
        vol = signal.volatility
        mom = signal.momentum
        
        # 높은 신뢰도 + 강한 상승 예측 → LIGHTNING
        if conf > 0.75 and pred > 0.7 and mom > 0.3:
            return ("LIGHTNING", conf, "high_conf_bullish")
        
        # 높은 신뢰도 + 강한 하락 예측 + 높은 변동성 → LADDER
        if conf > 0.7 and pred < 0.4 and vol > 0.03:
            return ("LADDER", conf, "high_conf_bearish_volatile")
        
        # 중간 신뢰도 + 상승 예측 → GAZUA
        if conf > 0.6 and pred > 0.6:
            return ("GAZUA", conf * 0.9, "medium_conf_bullish")
        
        # 기본 → PINGPONG
        return ("PINGPONG", 0.7, "default_neutral")


def calculate_dynamic_budget(
    base_budget: float,
    ai_confidence: float,
    ai_prediction: float,
    volatility: float = 0.0,
    momentum: float = 0.0,
) -> Tuple[float, str]:
    """간단한 동적 예산 계산 함수.
    
    Returns:
        (adjusted_budget, reason)
    """
    sizer = AIPositionSizer()
    
    signal = AISignal(
        market="",
        prediction=ai_prediction,
        confidence=ai_confidence,
        volatility=volatility,
        momentum=momentum,
    )
    
    decision = sizer.calculate_sizing(signal, base_budget)
    return (decision.adjusted_budget, decision.reason)
