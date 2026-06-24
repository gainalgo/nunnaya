# ============================================================
# File: app/manager/risk_classifier.py
# Autocoin OS v3-H — Capital Permission Risk Classifier (cap_ratio)
# ============================================================

from __future__ import annotations
import logging

import time
from typing import Dict, Any, Optional

from app.engine.hyper_engine_context import HyperEngineContext

class RiskBand:
    """Capital Permission Bands (LEGACY)."""
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"

from app.core.constants import env_float as _env_float
logger = logging.getLogger(__name__)

# ============================================================
# Suspicion v1 — Internal Risk Mapping
# ============================================================
# NOTE:
# - Risk is not 'trust management' but 'suspicion management'.
# - L0~L5 are internal judgment stages
# - In the UI it is shown as a 3-color traffic light (RED/YELLOW/GREEN) + intensity.

SUSPICION_LEVEL_TABLE = [
    (80.0, "L0"),
    (65.0, "L1"),
    (50.0, "L2"),
    (35.0, "L3"),
    (20.0, "L4"),
    (0.0,  "L5"),
]

LEVEL_TO_GROUP = {
    "L0": "RED",
    "L1": "RED",
    "L2": "YELLOW",
    "L3": "YELLOW",
    "L4": "GREEN",
    "L5": "GREEN",
}

LEVEL_TO_INTENSITY = {
    "L0": 1.0,
    "L1": 0.6,
    "L2": 0.4,
    "L3": 0.8,
    "L4": 0.5,
    "L5": 1.0,   # strongest trust (pulse allowed)
}

class RiskClassifier:
    """Manager-level Risk Classifier.

    LEGACY output fields (kept):
    - band: L0/L1/L2
    - unlock: True only at L2
    - cap_ratio
    - cap_usdt
    - reason

    Suspicion v1 (added):
    - suspicion_score (0~100)
    - suspicion_level (L0~L5)
    - suspicion_group (RED/YELLOW/GREEN)
    - suspicion_intensity (0~1)
    """

    def __init__(
        self,
        *,
        fee_rate: float = 0.001,
        l1_conf_min: float = 8.0,
        l2_conf_min: float = 18.0,
        l2_gap_min: float = 10.0,
    ):
        self.fee_rate = float(fee_rate)
        self.l1_conf_min = float(l1_conf_min)
        self.l2_conf_min = float(l2_conf_min)
        self.l2_gap_min = float(l2_gap_min)

        # env-based cap ratio (LEGACY)
        self.cap_l1 = _env_float("RISK_CAP_RATIO_L1", 0.2)
        self.cap_l2 = _env_float("RISK_CAP_RATIO_L2", 1.0)

    def classify(self, ctx: HyperEngineContext) -> Dict[str, Any]:
        # ====================================================
        # Suspicion v1 — suspicion score calculation (PRIMARY)
        # ====================================================
        # NOTE:
        # - Context does not compute. We compute here and only write-back.
        # - Does not replace the existing L0/L1/L2 capital gate.
        # - Continuously re-evaluates 'is there still no reason to be suspicious?'.

        suspicion = float(getattr(ctx, "suspicion_score", 50.0))
        confidence = ctx.confidence

        prev_conf = None
        if hasattr(ctx, "confidence_history") and ctx.confidence_history:
            prev_conf = ctx.confidence_history[-1]

        # 1) confidence absolute value
        if confidence is not None:
            if confidence < self.l1_conf_min:
                suspicion += 10.0
            elif confidence > self.l2_conf_min:
                suspicion -= 5.0

        # 2) confidence rate of change (a drop is immediately suspicious)
        if confidence is not None and prev_conf is not None:
            delta = confidence - prev_conf
            if delta < -2.0:
                suspicion += min(10.0, abs(delta) * 2.0)

        # 3) volatility-based suspicion (refer to strategy_reason)
        # SUSPICION: strategy_reason structure may change → approach defensively
        try:
            features = ctx.strategy_reason.get("features", {})
            vol = features.get("vol")
            if vol is not None and vol > 3.0:
                suspicion += min(10.0, float(vol))
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[risk_classifier] %s: %s", 'SUSPICION: strategy_reason structure may change → approach defensively', exc, exc_info=True)

        # 4) base suspicion weight when holding a position
        if ctx.position is not None:
            suspicion += 3.0

        # clamp
        suspicion = max(0.0, min(100.0, suspicion))

        # score → level
        level = "L3"
        for th, lv in SUSPICION_LEVEL_TABLE:
            if suspicion >= th:
                level = lv
                break

        group = LEVEL_TO_GROUP.get(level, "YELLOW")
        intensity = LEVEL_TO_INTENSITY.get(level, 0.5)

        # Context write-back (memory)
        ctx.suspicion_score = suspicion
        ctx.suspicion_level = level
        ctx.suspicion_group = group
        ctx.suspicion_intensity = intensity
        ctx.suspicion_ts = time.time()

        if hasattr(ctx, "confidence_history") and confidence is not None:
            ctx.confidence_history.append(confidence)
        if hasattr(ctx, "suspicion_history"):
            ctx.suspicion_history.append(suspicion)

        # ====================================================
        # LEGACY — Confidence-based Capital Gate (L0/L1/L2)
        # ====================================================
        # NOTE:
        # Kept for existing stable operation.
        # May still be valid in EMA-stabilized markets and long-term operation.
        # Run in parallel until Suspicion v1 is sufficiently validated.

        bias: Optional[str] = ctx.bias
        confidence: Optional[float] = ctx.confidence
        ema_scores: Dict[str, float] = ctx.ema_scores or {}

        # decision not formed
        if bias is None or confidence is None:
            return self._result(
                band=RiskBand.L0,
                unlock=False,
                cap_ratio=0.0,
                cap_usdt=0.0,
                reason={"cause": "no_decision"},
            )

        vals = sorted(list(ema_scores.values()), reverse=True)
        ema_gap = (vals[0] - vals[1]) if len(vals) >= 2 else 0.0

        # L0
        if confidence < self.l1_conf_min:
            return self._result(
                band=RiskBand.L0,
                unlock=False,
                cap_ratio=0.0,
                cap_usdt=0.0,
                reason={
                    "bias": bias,
                    "confidence": confidence,
                    "ema_gap": ema_gap,
                    "rule": f"confidence < {self.l1_conf_min}",
                },
            )

        # L1
        if confidence < self.l2_conf_min:
            cap_ratio = float(self.cap_l1)
            cap_usdt = float(ctx.allocated_capital or 0.0) * cap_ratio
            return self._result(
                band=RiskBand.L1,
                unlock=False,
                cap_ratio=cap_ratio,
                cap_usdt=cap_usdt,
                reason={
                    "bias": bias,
                    "confidence": confidence,
                    "ema_gap": ema_gap,
                    "rule": f"{self.l1_conf_min} <= confidence < {self.l2_conf_min}",
                },
            )

        # L2 unlock condition
        if ema_gap >= self.l2_gap_min:
            cap_ratio = float(self.cap_l2)
            cap_usdt = float(ctx.allocated_capital or 0.0) * cap_ratio
            return self._result(
                band=RiskBand.L2,
                unlock=True,
                cap_ratio=cap_ratio,
                cap_usdt=cap_usdt,
                reason={
                    "bias": bias,
                    "confidence": confidence,
                    "ema_gap": ema_gap,
                    "rule": f"confidence >= {self.l2_conf_min} and ema_gap >= {self.l2_gap_min}",
                },
            )

        # insufficient gap → stay at L1
        cap_ratio = float(self.cap_l1)
        cap_usdt = float(ctx.allocated_capital or 0.0) * cap_ratio
        return self._result(
            band=RiskBand.L1,
            unlock=False,
            cap_ratio=cap_ratio,
            cap_usdt=cap_usdt,
            reason={
                "bias": bias,
                "confidence": confidence,
                "ema_gap": ema_gap,
                "rule": f"ema_gap < {self.l2_gap_min}",
            },
        )

    def _result(
        self,
        *,
        band: str,
        unlock: bool,
        cap_ratio: float,
        cap_usdt: float,
        reason: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "band": band,
            "unlock": bool(unlock),
            "cap_ratio": float(cap_ratio),
            "cap_usdt": float(cap_usdt),
            "reason": reason,
            "fee_rate": self.fee_rate,
        }
