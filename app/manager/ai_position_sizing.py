# ============================================================
# File: app/manager/ai_position_sizing.py
# Autocoin OS v3-H — AI Confidence-Based Position Sizing
# ------------------------------------------------------------
# Purpose:
# - Adjust position size based on AI prediction confidence
# - Higher confidence -> larger budget
# - Lower confidence -> smaller budget
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class AISignal:
    """AI signal data."""
    market: str

    # Core prediction
    prediction: float = 0.5      # 0.0 = strong down, 1.0 = strong up
    confidence: float = 0.5      # 0.0 = uncertain, 1.0 = certain

    # Auxiliary indicators
    momentum: float = 0.0
    volatility: float = 0.0
    trend: float = 0.0
    rsi: float = 50.0

    # Meta
    model_version: str = ""
    timestamp: float = 0.0


@dataclass
class SizingDecision:
    """Position sizing decision."""
    market: str
    base_budget: float
    adjusted_budget: float
    multiplier: float
    confidence_tier: str  # "high", "medium", "low", "very_low"
    reason: str
    details: Dict[str, Any]


class AIPositionSizer:
    """AI confidence-based position sizer.

    Confidence tiers:
    - HIGH (>0.8): +30% budget
    - MEDIUM (0.6-0.8): base budget
    - LOW (0.4-0.6): -20% budget
    - VERY_LOW (<0.4): -40% budget or entry rejection
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
        
        # Additional correction factors
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
        """Calculate position sizing."""
        conf = signal.confidence

        # Determine tier
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
        
        # Apply correction factors
        adjustments = []
        final_mult = base_mult

        # 1. Volatility penalty
        if signal.volatility > self.volatility_penalty:
            vol_penalty = min(0.2, (signal.volatility - self.volatility_penalty) * 2)
            final_mult -= vol_penalty
            adjustments.append(f"vol_penalty:-{vol_penalty:.2f}")
        
        # 2. Momentum boost (when in the same direction)
        if signal.prediction > 0.5 and signal.momentum > self.momentum_boost:
            mom_boost = min(0.15, (signal.momentum - self.momentum_boost) * 0.3)
            final_mult += mom_boost
            adjustments.append(f"momentum_boost:+{mom_boost:.2f}")
        elif signal.prediction < 0.5 and signal.momentum < -self.momentum_boost:
            mom_boost = min(0.15, (abs(signal.momentum) - self.momentum_boost) * 0.3)
            final_mult += mom_boost
            adjustments.append(f"momentum_boost:+{mom_boost:.2f}")
        
        # 3. RSI extreme adjustment
        if signal.rsi < 25 or signal.rsi > 75:
            # Overbought/oversold -> slightly conservative
            final_mult *= 0.9
            adjustments.append(f"rsi_extreme:{signal.rsi:.0f}")

        # Min/max clamp
        final_mult = max(0.3, min(1.5, final_mult))

        adjusted_budget = base_budget * final_mult
        # [FIX 2026-01-23] round() -> floor() + guarantee minimum order
        # round() can drop the value to zero below the decimal point
        MIN_ORDER_USDT = 5.0  # Bybit minimum order amount
        adjusted_budget = int(adjusted_budget * 100) / 100  # USDT 0.01 unit
        adjusted_budget = max(MIN_ORDER_USDT, adjusted_budget)  # guarantee minimum order
        
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
        """Decide whether to enter."""
        if signal.confidence < self.min_confidence:
            return (False, f"low_confidence:{signal.confidence:.2f}<{self.min_confidence}")

        # Whether prediction direction matches current state
        if signal.prediction > 0.5:
            # Up prediction
            if signal.momentum < -0.5:
                return (False, "prediction_momentum_conflict:up_pred_but_strong_down_mom")
        else:
            # Down prediction
            if signal.momentum > 0.5:
                return (False, "prediction_momentum_conflict:down_pred_but_strong_up_mom")
        
        return (True, f"entry_ok:conf={signal.confidence:.2f},pred={signal.prediction:.2f}")

    def get_recommended_strategy(
        self,
        signal: AISignal,
    ) -> Tuple[str, float, str]:
        """Recommend a strategy based on the signal.

        Returns:
            (strategy, confidence, reason)
        """
        pred = signal.prediction
        conf = signal.confidence
        vol = signal.volatility
        mom = signal.momentum
        
        # High confidence + strong up prediction -> LIGHTNING
        if conf > 0.75 and pred > 0.7 and mom > 0.3:
            return ("LIGHTNING", conf, "high_conf_bullish")

        # High confidence + strong down prediction + high volatility -> LADDER
        if conf > 0.7 and pred < 0.4 and vol > 0.03:
            return ("LADDER", conf, "high_conf_bearish_volatile")

        # Medium confidence + up prediction -> GAZUA
        if conf > 0.6 and pred > 0.6:
            return ("GAZUA", conf * 0.9, "medium_conf_bullish")

        # Default -> PINGPONG
        return ("PINGPONG", 0.7, "default_neutral")


def calculate_dynamic_budget(
    base_budget: float,
    ai_confidence: float,
    ai_prediction: float,
    volatility: float = 0.0,
    momentum: float = 0.0,
) -> Tuple[float, str]:
    """Simple dynamic budget calculation function.

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
