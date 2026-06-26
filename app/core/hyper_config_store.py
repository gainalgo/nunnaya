# ============================================================
# File: app/core/hyper_config_store.py
# ------------------------------------------------------------
# HyperConfigStore
# - Central store that holds various configs (strategy, presets, etc.)
#   loaded from JSON so they can be queried across the whole system.
# ============================================================

from __future__ import annotations
import logging
from typing import Any, Dict

from app.core.hyper_config_loader import HyperConfigLoader
import os

logger = logging.getLogger(__name__)


class HyperConfigStore:
    """
    Stores various JSON configs (strategy.json, presets, market lists,
    engine config, etc.) so they can be used system-wide.
    """

    def __init__(self):
        base_path = os.path.join("app", "data")
        self.loader = HyperConfigLoader(base_path=base_path)
        self._store: Dict[str, Any] = {}

        self._load_all()

    # --------------------------------------------------------
    # Load all configs
    # --------------------------------------------------------
    def _load_all(self):
        """
        Load the JSON files at once and keep them in an internal dict.
        Files can be added/removed as needed.
        """

        files = {
            "strategy": "strategy.json",
            "strategy_presets": "strategy_presets.json",
            "autoloop_config": "autoloop_config.json",
            "autoloop_markets": "autoloop_markets.json",
            "engine_presets": "engine_presets.json",
            "bybit_markets": "bybit_markets.json",
        }

        for key, filename in files.items():
            try:
                self._store[key] = self.loader.load(filename)
            except FileNotFoundError:
                # Missing configs are set to None so callers can fall back
                logger.info("[ConfigStore] Config file not found: %s (using fallback)", filename)
                self._store[key] = None

    # --------------------------------------------------------
    # Get config
    # --------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """
        key: "strategy", "strategy_presets", ...
        """
        return self._store.get(key, default)

    # --------------------------------------------------------
    # Update config
    # --------------------------------------------------------
    def set(self, key: str, value: Any):
        self._store[key] = value

    # --------------------------------------------------------
    # Get all
    # --------------------------------------------------------
    def all(self) -> Dict[str, Any]:
        return dict(self._store)


# ------------------------------------------------------------
# Global instance
# ------------------------------------------------------------
config_store = HyperConfigStore()
