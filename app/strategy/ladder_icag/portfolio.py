# ============================================================
# ICAG Portfolio-level guards
#   cluster risk, global budget, BTC correlation
# ============================================================
from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from .config import ICAGConfig
from .state import ICAGMarketState, ICAGPortfolioState

logger = logging.getLogger(__name__)


class ICAGPortfolioGuard:

    def __init__(self, cfg: Optional[ICAGConfig] = None):
        self.cfg = cfg or ICAGConfig()

    def update(
        self,
        states: Dict[str, ICAGMarketState],
        portfolio: ICAGPortfolioState,
        btc_change_5m: float = 0.0,
    ) -> ICAGPortfolioState:
        """Recompute portfolio-level metrics and throttle."""
        cfg = self.cfg

        total_budget = 0.0
        total_used = 0.0
        risk_cut_count = 0

        for st in states.values():
            total_budget += st.budget_allocated
            total_used += st.budget_used
            if st.zone == "RISK_CUT":
                risk_cut_count += 1

        portfolio.total_budget = total_budget
        portfolio.total_used = total_used
        portfolio.total_util = total_used / total_budget if total_budget > 0 else 0.0
        portfolio.risk_cut_count = risk_cut_count

        # --- global buy throttle ---
        throttle = 1.0

        # BTC correlation guard (pre-emptive, faster than cluster counting)
        if btc_change_5m <= cfg.btc_drop_buy_block_pct:
            throttle = 0.0
            logger.warning("ICAG portfolio: BTC %.2f%% → BUY blocked", btc_change_5m)
        elif btc_change_5m <= cfg.btc_drop_buy_reduce_pct:
            throttle = min(throttle, 0.3)

        # portfolio utilization
        if portfolio.total_util >= cfg.portfolio_util_block:
            throttle = 0.0
        elif portfolio.total_util >= cfg.portfolio_util_throttle:
            throttle = min(throttle, 0.5)

        # cluster risk
        if risk_cut_count >= cfg.cluster_risk_severe:
            throttle = min(throttle, 0.0)
        elif risk_cut_count >= cfg.cluster_risk_throttle:
            throttle = min(throttle, 0.5)

        portfolio.global_buy_throttle = throttle
        portfolio.last_update_ts = time.time()
        return portfolio

    def allocate_order_slots(
        self,
        states: Dict[str, ICAGMarketState],
    ) -> Dict[str, int]:
        """Distribute global order slots across markets by priority.

        Returns {symbol: max_orders}.
        """
        cfg = self.cfg
        remaining = cfg.global_max_orders
        allocation: Dict[str, int] = {}

        # sort by: CORE first, then by realized PnL (profitable markets first)
        zone_priority = {"CORE": 0, "EXPANSION": 1, "RISK_CUT": 2}
        ranked = sorted(
            states.items(),
            key=lambda kv: (
                zone_priority.get(kv[1].zone, 9),
                -kv[1].realized_pnl,
            ),
        )

        for symbol, st in ranked:
            if remaining <= 0:
                allocation[symbol] = 0
                continue

            if st.zone == "CORE":
                slots = min(cfg.max_orders_core, remaining)
            elif st.zone == "EXPANSION":
                slots = min(cfg.max_orders_expansion, remaining)
            else:
                slots = min(cfg.max_orders_expansion, 2, remaining)

            allocation[symbol] = slots
            remaining -= slots

        return allocation
