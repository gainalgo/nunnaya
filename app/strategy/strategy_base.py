from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal

Signal = Literal["buy", "sell", "hold", "reserve"]


@dataclass
class Decision:
    """Common result structure for strategy decisions."""

    signal: Signal = "hold"
    reason: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class StrategyPlugin:
    """Base class for strategy plugins."""

    name: str = "base"

    def decide(self, ctx: Any, price: float) -> Decision:
        return Decision(signal="hold", reason="base_hold")
