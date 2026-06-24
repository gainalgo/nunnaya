# ============================================================
# File: app/strategy/strategy_store.py
# ------------------------------------------------------------
# Central store for strategy policies.
# Passes policies along the system/config_store -> strategy_store -> engine flow.
# ============================================================

from __future__ import annotations
from typing import Dict, Any

from .strategy_types import StrategyPolicy
from ..core.hyper_config_store import config_store


class StrategyStore:
    """
    Central store that holds and serves strategy policies.
    """

    def __init__(self):
        # Currently applied policy per market
        self._policies: Dict[str, StrategyPolicy] = {}

        # Global default policy (based on strategy.json + presets)
        base = config_store.get("strategy", {})
        self.base_policy = StrategyPolicy(base)

    # --------------------------------------------------------
    # Get policy
    # --------------------------------------------------------
    def get_policy(self, market: str) -> StrategyPolicy:
        """
        Return the policy for a given market.
        If none exists, create one based on the default policy.
        """
        if market not in self._policies:
            self._policies[market] = StrategyPolicy(self.base_policy.to_dict())

        return self._policies[market]

    # --------------------------------------------------------
    # Update policy
    # --------------------------------------------------------
    def update_policy(self, market: str, updates: Dict[str, Any]):
        """
        Update part of a market's policy.
        """
        policy = self.get_policy(market)
        policy.update(updates)

    # --------------------------------------------------------
    # Get all policies
    # --------------------------------------------------------
    def all_policies(self) -> Dict[str, StrategyPolicy]:
        return dict(self._policies)


# ------------------------------------------------------------
# Global instance
# ------------------------------------------------------------
strategy_store = StrategyStore()
