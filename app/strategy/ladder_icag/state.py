# ============================================================
# ICAG State — per-market and portfolio-level persistent state
# ============================================================
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_PATH = os.path.join("runtime", "icag_state.json")

@dataclass
class ICAGMarketState:
    symbol: str

    # --- Position ---
    position_qty: float = 0.0
    position_avg_price: float = 0.0
    position_entry_ts: float = 0.0

    # --- Anchor ---
    anchor_price: float = 0.0
    anchor_raw: float = 0.0

    # --- Budget ---
    budget_allocated: float = 0.0
    budget_used: float = 0.0

    # --- Zone & Bias ---
    zone: str = "CORE"                # CORE / EXPANSION / RISK_CUT
    bias: str = "BALANCED"            # BUY / SELL / BALANCED
    bias_last_change_ts: float = 0.0
    underwater_mode: str = "NORMAL"   # NORMAL / DEFENSIVE / DCA_RESCUE / CAPITULATION

    # --- ATR ---
    atr: float = 0.0
    atr_pct: float = 0.0
    vwap: float = 0.0

    # --- Inventory ---
    inv_ratio: float = 0.0

    # --- Metrics ---
    realized_pnl: float = 0.0
    trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    last_fill_ts: float = 0.0
    last_tick_ts: float = 0.0

    # --- Fill history (capped) ---
    fill_history: List[Dict[str, Any]] = field(default_factory=list)

    # --- Active order tracking ---
    open_buy_count: int = 0
    open_sell_count: int = 0

    # --- Entry timeout ---
    activation_ts: float = 0.0          # first tick timestamp (for entry timeout)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ICAGMarketState":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)

@dataclass
class ICAGPortfolioState:
    total_budget: float = 0.0
    total_used: float = 0.0
    total_util: float = 0.0
    risk_cut_count: int = 0
    global_buy_throttle: float = 1.0    # 1.0 = normal, 0.0 = blocked
    last_update_ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

# ============================================================
# Persistence helpers
# ============================================================

_MAX_FILL_HISTORY = 200

def load_icag_states() -> Dict[str, ICAGMarketState]:
    """Load all market states from disk."""
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        states = {}
        for symbol, data in raw.items():
            if symbol.startswith("__"):
                continue
            if isinstance(data, dict):
                states[symbol] = ICAGMarketState.from_dict(data)
        return states
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("Failed to load ICAG state: %s", e)
        return {}

def save_icag_states(states: Dict[str, ICAGMarketState]) -> None:
    """Save all market states to disk atomically."""
    from app.core.io_utils import safe_write_json
    data = {}
    for symbol, st in states.items():
        d = st.to_dict()
        # Cap fill history
        if len(d.get("fill_history", [])) > _MAX_FILL_HISTORY:
            d["fill_history"] = d["fill_history"][-_MAX_FILL_HISTORY:]
        data[symbol] = d

    try:
        safe_write_json(STATE_PATH, data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning("Failed to save ICAG state: %s", e)
