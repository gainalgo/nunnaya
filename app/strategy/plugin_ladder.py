# Extracted from strategy_plugins.py — Phase 2 (file diet)
from __future__ import annotations

import logging
from typing import Any, Dict

from app.strategy.strategy_base import Decision, StrategyPlugin

logger = logging.getLogger(__name__)


# [2026-02-22] ICAG v3 — Grid-managed LADDER (replaces phase-based v2)

class LadderPlugin(StrategyPlugin):
    name: str = "ladder"

    def decide(self, ctx: Any, price: float) -> Decision:
        meta: Dict[str, Any] = {}

        # ICAG v3 grid engine manages all LADDER order placement.
        # This plugin only provides diagnostic info for the tick loop.
        market = getattr(ctx, "market", "BTCUSDT")

        # Check grid_auto_sync (always True for ICAG)
        params: Dict[str, Any] = {}
        try:
            ctrls = getattr(ctx, "controls", None) or {}
            if isinstance(ctrls, dict):
                params = dict((ctrls.get("strategy") or {}).get("params") or {})
        except (KeyError, AttributeError, TypeError):
            logger.warning("[%s] params 추출 실패 → 기본값 사용: %s", self.name if hasattr(self, 'name') else '?', getattr(ctx, 'market', '?'), exc_info=True)
            params = {}

        grid_auto_sync = bool(params.get("grid_auto_sync", False))

        # Read ICAG state for diagnostics
        try:
            system = getattr(ctx, "system", None)
            grid_v3 = getattr(system, "_ladder_grid_v3", None) if system else None
            if grid_v3 and grid_v3 is not False:
                icag_state = grid_v3._get_state(market)
                meta["engine"] = "icag_v3"
                meta["anchor"] = round(icag_state.anchor_price, 2)
                meta["zone"] = icag_state.zone
                meta["bias"] = icag_state.bias
                meta["inv_ratio"] = round(icag_state.inv_ratio, 4)
                meta["atr"] = round(icag_state.atr, 2)
                meta["underwater_mode"] = icag_state.underwater_mode
                meta["realized_pnl"] = round(icag_state.realized_pnl, 2)
                meta["trade_count"] = icag_state.trade_count
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[LADDER] ICAG state 조회 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
            meta["engine"] = "icag_v3"

        if grid_auto_sync:
            return Decision(signal="hold", reason="ladder:icag_grid_managed", meta=meta)

        # Fallback: no grid sync → hold (should not happen in normal operation)
        return Decision(signal="hold", reason="ladder:no_grid_sync", meta=meta)
