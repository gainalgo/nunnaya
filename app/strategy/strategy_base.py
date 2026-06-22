from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal

Signal = Literal["buy", "sell", "hold", "reserve"]


@dataclass
class Decision:
    """전략 판단의 공통 결과 구조."""

    signal: Signal = "hold"
    reason: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class StrategyPlugin:
    """전략 플러그인 기본 클래스."""

    name: str = "base"

    def decide(self, ctx: Any, price: float) -> Decision:
        return Decision(signal="hold", reason="base_hold")
