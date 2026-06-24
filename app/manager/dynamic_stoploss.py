# ============================================================
# File: app/manager/dynamic_stoploss.py
# Autocoin OS v3-H — Dynamic Stop-Loss System
# ------------------------------------------------------------
# Purpose:
# - Dynamically adjust the stop-loss line as time passes
# - Tighter stop-loss the longer a position is held
# - Trailing stop support
# ============================================================

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from app.core.constants import env_bool

# [MIGRATED 2026-01-23] Market Regime integration
try:
    from app.core.market_regime import get_regime_detector, MarketRegime
except ImportError:
    logger.warning("[DynamicStopLoss] market_regime import failed", exc_info=True)
    get_regime_detector = None
    MarketRegime = None

REGIME_TP_SL_ENABLED = env_bool("OMA_REGIME_TP_SL_ENABLED", default=False)

if TYPE_CHECKING:
    from typing import Protocol

    class RegimeInfo(Protocol):
        """Market regime info protocol (for type hints)."""
        @property
        def regime(self) -> Any: ...


# Volatility-based scaling constants
ATR_REF = 1.5  # Reference volatility (%)
VOL_SCALE_MIN = 0.7
VOL_SCALE_MAX = 1.8

# SL width multiplier per regime
REGIME_SL_MULT: Dict[str, float] = {
    "BULL": 1.20,      # Wider SL
    "BEAR": 0.75,      # Tighter SL
    "SIDEWAYS": 0.90,
    "VOLATILE": 1.30,
}

# TP recommendation constants
TP_RATIO = 1.5  # TP ratio relative to SL
TP_MIN = 1.5    # Minimum TP %

REGIME_TP_MULT: Dict[str, float] = {
    "BULL": 1.25,
    "BEAR": 0.70,
    "SIDEWAYS": 0.85,
    "VOLATILE": 1.10,
}


class StopLossMode(Enum):
    """Stop-loss mode."""
    FIXED = "fixed"              # Fixed stop-loss
    TIME_DECAY = "time_decay"    # Tightens as time passes
    TRAILING = "trailing"        # Trailing stop
    HYBRID = "hybrid"            # Hybrid (time + trailing)


@dataclass
class PositionInfo:
    """Position info."""
    market: str
    entry_price: float
    entry_ts: float
    current_price: float
    quantity: float
    highest_price: float = 0.0  # Highest price while held
    lowest_price: float = 0.0   # Lowest price while held
    strategy: str = ""


@dataclass
class StopLossLevel:
    """Stop-loss level."""
    market: str
    stop_price: float
    stop_pct: float          # Stop-loss % relative to entry price
    trigger_price: float     # Trigger price (for trailing)
    mode: StopLossMode
    reason: str
    urgency: str             # "none", "watch", "close", "immediate"
    time_held_min: float
    details: Dict[str, Any]


class DynamicStopLossManager:
    """Dynamic stop-loss manager.

    Time-based stop-loss logic:
    - 0-30 min: -7% stop (wide room)
    - 30 min-1 hr: -5% stop
    - 1-2 hr: -4% stop
    - 2-4 hr: -3% stop
    - 4 hr+: -2% stop (tight)

    Trailing stop:
    - Stop out on a set % drop from the highest price
    """

    def __init__(
        self,
        # Time-based stop-loss (minutes, stop%)
        time_decay_schedule: Optional[List[Tuple[int, float]]] = None,

        # Trailing stop
        trailing_activation_pct: float = 3.0,  # Activate when profit >= 3%
        trailing_distance_pct: float = 2.0,    # Stop out on 2% drop from highest price

        # Min/max stop-loss
        min_stop_pct: float = 1.5,
        max_stop_pct: float = 10.0,

        # Per-strategy adjustment
        strategy_adjustments: Optional[Dict[str, float]] = None,
    ):
        self.time_decay_schedule = time_decay_schedule or [
            (0, -7.0),      # 0-30 min: -7%
            (30, -5.0),     # 30-60 min: -5%
            (60, -4.0),     # 1-2 hr: -4%
            (120, -3.0),    # 2-4 hr: -3%
            (240, -2.5),    # 4-8 hr: -2.5%
            (480, -2.0),    # 8 hr+: -2%
        ]

        self.trailing_activation = trailing_activation_pct
        self.trailing_distance = trailing_distance_pct
        self.min_stop = min_stop_pct
        self.max_stop = max_stop_pct

        self.strategy_adjustments = strategy_adjustments or {
            "PINGPONG": 1.0,     # Default
            "AUTOLOOP": 1.0,
            "LADDER": 1.5,       # Wider stop (DCA nature)
            "LIGHTNING": 0.7,    # Tighter (fast stop-loss)
            "GAZUA": 1.3,        # Wider stop (long-term hold)
        }

    def calculate_stop_level(
        self,
        position: PositionInfo,
        mode: StopLossMode = StopLossMode.HYBRID,
        regime: Optional["RegimeInfo"] = None,
        atr_pct: Optional[float] = None,
    ) -> StopLossLevel:
        """Calculate the stop-loss level."""
        now = time.time()
        held_sec = now - position.entry_ts
        held_min = held_sec / 60

        entry = position.entry_price
        current = position.current_price
        highest = position.highest_price if position.highest_price > 0 else current

        # Current P&L %
        current_pnl_pct = ((current - entry) / entry) * 100 if entry > 0 else 0
        highest_pnl_pct = ((highest - entry) / entry) * 100 if entry > 0 else 0

        # Volatility-based scaling
        if atr_pct is not None and atr_pct > 0:
            vol_scale = max(VOL_SCALE_MIN, min(VOL_SCALE_MAX, atr_pct / ATR_REF))
        else:
            vol_scale = 1.0

        # 1. Time-based stop-loss %
        time_stop_pct = self._get_time_based_stop(held_min)

        # 2. Strategy adjustment
        strat_adj = self.strategy_adjustments.get(position.strategy.upper(), 1.0)
        time_stop_pct *= strat_adj

        # Adjust trailing params per regime
        if regime and regime.regime.value == "VOLATILE":
            effective_trailing_activation = max(1.0, self.trailing_activation * 0.7)
            effective_trailing_distance = max(0.8, self.trailing_distance * 0.6)
        else:
            effective_trailing_activation = self.trailing_activation
            effective_trailing_distance = self.trailing_distance

        # 3. Trailing stop check
        trailing_stop_pct = None
        if highest_pnl_pct >= effective_trailing_activation:
            trailing_stop_pct = highest_pnl_pct - effective_trailing_distance

        # 4. Determine final stop-loss per mode
        if mode == StopLossMode.FIXED:
            final_stop_pct = time_stop_pct
            reason = f"fixed:{time_stop_pct:.1f}%"
        elif mode == StopLossMode.TIME_DECAY:
            final_stop_pct = time_stop_pct
            reason = f"time_decay:{held_min:.0f}min→{time_stop_pct:.1f}%"
        elif mode == StopLossMode.TRAILING:
            if trailing_stop_pct is not None:
                final_stop_pct = trailing_stop_pct
                reason = f"trailing:high={highest_pnl_pct:.1f}%→stop={final_stop_pct:.1f}%"
            else:
                final_stop_pct = time_stop_pct
                reason = f"trailing_inactive:using_time={time_stop_pct:.1f}%"
        else:  # HYBRID
            if trailing_stop_pct is not None and trailing_stop_pct > time_stop_pct:
                final_stop_pct = trailing_stop_pct
                reason = f"hybrid_trailing:stop={final_stop_pct:.1f}%"
            else:
                final_stop_pct = time_stop_pct
                reason = f"hybrid_time:{held_min:.0f}min→{time_stop_pct:.1f}%"
        
        # Adjust SL width per regime
        if regime is not None:
            sl_mult = REGIME_SL_MULT.get(regime.regime.value, 1.0)
            final_stop_pct *= sl_mult

        # 5. Min/max clamp
        final_stop_pct = max(-self.max_stop, min(-self.min_stop, final_stop_pct))

        # 6. Compute stop-loss price
        stop_price = entry * (1 + final_stop_pct / 100)

        # 7. Determine urgency
        urgency = self._determine_urgency(current_pnl_pct, final_stop_pct)

        # Compute recommended TP value
        regime_value = regime.regime.value if regime else None
        tp_mult = REGIME_TP_MULT.get(regime_value, 1.0) if regime_value else 1.0
        tp_pct = max(TP_MIN, abs(final_stop_pct) * TP_RATIO * tp_mult * vol_scale)
        
        details: Dict[str, Any] = {
            "entry_price": entry,
            "current_price": current,
            "highest_price": highest,
            "current_pnl_pct": round(current_pnl_pct, 2),
            "highest_pnl_pct": round(highest_pnl_pct, 2),
            "time_stop_pct": round(time_stop_pct, 2),
            "trailing_stop_pct": round(trailing_stop_pct, 2) if trailing_stop_pct else None,
            "strategy": position.strategy,
            "strategy_adj": strat_adj,
            "tp_pct": round(tp_pct, 2),
            "tp_price": round(entry * (1 + tp_pct / 100), 2),
            "vol_scale": round(vol_scale, 2),
            "regime": regime_value,
        }
        
        return StopLossLevel(
            market=position.market,
            stop_price=round(stop_price, 2),
            stop_pct=round(final_stop_pct, 2),
            trigger_price=highest if trailing_stop_pct else 0,
            mode=mode,
            reason=reason,
            urgency=urgency,
            time_held_min=round(held_min, 1),
            details=details,
        )

    def _get_time_based_stop(self, held_min: float) -> float:
        """Return the time-based stop-loss %."""
        for i, (threshold_min, stop_pct) in enumerate(self.time_decay_schedule):
            if i + 1 < len(self.time_decay_schedule):
                next_threshold = self.time_decay_schedule[i + 1][0]
                if threshold_min <= held_min < next_threshold:
                    return stop_pct
            else:
                if held_min >= threshold_min:
                    return stop_pct

        # Default
        return self.time_decay_schedule[-1][1] if self.time_decay_schedule else -5.0

    def _determine_urgency(self, current_pnl_pct: float, stop_pct: float) -> str:
        """Determine urgency."""
        distance_to_stop = current_pnl_pct - stop_pct

        if current_pnl_pct <= stop_pct:
            return "immediate"  # Already reached stop-loss level
        elif distance_to_stop < 1.0:
            return "close"      # Less than 1% to stop-loss
        elif distance_to_stop < 2.0:
            return "watch"      # Less than 2% to stop-loss
        else:
            return "none"       # Plenty of room

    def should_stop_loss(
        self,
        position: PositionInfo,
        mode: StopLossMode = StopLossMode.HYBRID,
    ) -> Tuple[bool, StopLossLevel]:
        """Whether to execute the stop-loss."""
        level = self.calculate_stop_level(position, mode)
        should_stop = position.current_price <= level.stop_price
        return (should_stop, level)

    def batch_check(
        self,
        positions: List[PositionInfo],
        mode: StopLossMode = StopLossMode.HYBRID,
    ) -> List[Tuple[PositionInfo, StopLossLevel, bool]]:
        """Batch-check multiple positions."""
        results = []
        for pos in positions:
            should_stop, level = self.should_stop_loss(pos, mode)
            results.append((pos, level, should_stop))
        return results

    # [MIGRATED 2026-01-23] Market Regime integration
    def get_regime_scale(self, market: str = "BTCUSDT") -> Dict[str, float]:
        """Return the TP/SL scale based on the current market regime."""
        if not REGIME_TP_SL_ENABLED or get_regime_detector is None:
            return {"sl": 1.0, "tp": 1.0, "trail": 1.0}
        
        try:
            detector = get_regime_detector()
            result = detector.detect(market)
            return detector.get_tp_sl_scale(result.regime)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("[DynamicStopLoss] _get_regime_scale failed for %s", market, exc_info=True)
            return {"sl": 1.0, "tp": 1.0, "trail": 1.0}


def get_dynamic_stop_pct(
    entry_ts: float,
    strategy: str = "PINGPONG",
    highest_pnl_pct: float = 0.0,
) -> Tuple[float, str]:
    """Simple dynamic stop-loss % calculation.

    Returns:
        (stop_pct, reason)
    """
    manager = DynamicStopLossManager()

    now = time.time()
    held_min = (now - entry_ts) / 60

    # Time-based stop-loss
    time_stop = manager._get_time_based_stop(held_min)

    # Strategy adjustment
    strat_adj = manager.strategy_adjustments.get(strategy.upper(), 1.0)
    time_stop *= strat_adj

    # Trailing check
    if highest_pnl_pct >= manager.trailing_activation:
        trailing_stop = highest_pnl_pct - manager.trailing_distance
        if trailing_stop > time_stop:
            return (trailing_stop, f"trailing:{highest_pnl_pct:.1f}%→{trailing_stop:.1f}%")
    
    return (time_stop, f"time:{held_min:.0f}min→{time_stop:.1f}%")
