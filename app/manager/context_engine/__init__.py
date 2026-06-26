"""Context Engine — auxiliary layer for FOCUS entry decisions (2026-04-19)

The existing `_compute_conviction_score()` only looks at a single coin's H4
indicators. This engine additionally looks at the "market context":

    1. BTC Regime        — BTC 4H trend → penalty for counter-trend entry
    2. Direction Memory  — recent results for same coin+direction over N tries → block/penalty on losing streak
    3. Market Bias       — aggregated recent direction across multiple coins → penalty for fighting the crowd
    4. Session Profile   — KST time-of-day (dawn/morning) → conviction bonus

All default OFF. Toggled individually via FocusConfig.

# Usage
    engine = ContextEngine(config=focus_mgr.config)
    verdict = engine.evaluate(
        market="ETHUSDT",
        direction="SHORT",
        now_ts=time.time(),
        btc_candles=price_store.get_prices("BTCUSDT", count=60),
    )
    # verdict.delta, verdict.block, verdict.reason, verdict.details

Philosophy:
- Minimize absolute hard blocks (only direction_memory losing streak)
- Most effects are conviction adjustments (-2 ~ +2)
- Modules are independent — turning one OFF leaves the rest working
- No side effects (recording/learning is handled by FocusManager)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .btc_regime import BtcRegimeModule
from .direction_memory import DirectionMemoryModule
from .market_bias import MarketBiasModule
from .session_profile import SessionProfileModule

logger = logging.getLogger(__name__)


@dataclass
class Verdict:
    """Context evaluation result."""
    delta: int = 0                     # conviction adjustment (negative=penalty)
    block: bool = False                # True = entry fully blocked
    reason: str = ""                   # block reason when block=True
    details: Dict[str, Any] = field(default_factory=dict)  # per-module contribution

    def summary(self) -> str:
        parts = []
        for mod, info in self.details.items():
            d = info.get("delta", 0)
            if d != 0 or info.get("block"):
                # ★ [2026-05-17] Phase 5 formula float-conversion side effect — force int()
                parts.append(f"{mod}{int(d):+d}" + ("*" if info.get("block") else ""))
        return "/".join(parts) if parts else "neutral"


class ContextEngine:
    """Integrated evaluation of the 4 context modules."""

    def __init__(self, config: Any):
        self.config = config
        self.session = SessionProfileModule(config)
        self.direction = DirectionMemoryModule(config)
        self.btc = BtcRegimeModule(config)
        self.bias = MarketBiasModule(config)

    def evaluate(
        self,
        market: str,
        direction: str,
        now_ts: float,
        btc_candles: Optional[List[Any]] = None,
    ) -> Verdict:
        """Integrated evaluation. Order: blocks first → sum penalties/bonuses.

        Args:
            market: "ETHUSDT" format
            direction: "LONG" / "SHORT"
            now_ts: current unix timestamp (injectable for tests)
            btc_candles: BTC H4 candle list (skip BTC module if absent)

        Returns:
            Verdict (delta, block, reason, details)
        """
        v = Verdict()
        if not direction:
            return v  # skip when direction undecided (scanner pre-stage)

        mkt = market.upper()
        dir_u = direction.upper()

        # 1) Direction Memory — losing-streak block takes top priority
        try:
            dm = self.direction.evaluate(mkt, dir_u, now_ts)
            v.details["direction_memory"] = dm
            if dm.get("block"):
                v.block = True
                v.reason = dm.get("reason", "direction_memory_block")
                return v  # hard block — no need to evaluate later modules
            v.delta += int(dm.get("delta", 0))
        except Exception as exc:
            logger.warning("[ctx] direction_memory error: %s", exc)
            v.details["direction_memory"] = {"error": str(exc)}

        # 2) BTC Regime — penalty for direction misalignment
        try:
            bt = self.btc.evaluate(dir_u, btc_candles, now_ts)
            v.details["btc_regime"] = bt
            v.delta += int(bt.get("delta", 0))
        except Exception as exc:
            logger.warning("[ctx] btc_regime error: %s", exc)
            v.details["btc_regime"] = {"error": str(exc)}

        # 3) Market Bias — penalty for fighting the crowd
        try:
            mb = self.bias.evaluate(mkt, dir_u, now_ts)
            v.details["market_bias"] = mb
            v.delta += int(mb.get("delta", 0))
        except Exception as exc:
            logger.warning("[ctx] market_bias error: %s", exc)
            v.details["market_bias"] = {"error": str(exc)}

        # 4) Session Profile — time-of-day adjustment
        try:
            sp = self.session.evaluate(dir_u, now_ts)
            v.details["session_profile"] = sp
            v.delta += int(sp.get("delta", 0))
        except Exception as exc:
            logger.warning("[ctx] session_profile error: %s", exc)
            v.details["session_profile"] = {"error": str(exc)}

        return v


__all__ = ["ContextEngine", "Verdict"]
