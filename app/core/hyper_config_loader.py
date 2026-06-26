# ============================================================
# File: app/core/hyper_config_loader.py
# ------------------------------------------------------------
# HyperConfigLoader
# - Reads JSON files under app/data and feeds them into the ConfigStore.
# - Shared utility for loading various JSON files: strategy policies, presets, engine settings, etc.
# ============================================================

from __future__ import annotations
import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


class HyperConfigLoader:
    """
    JSON-based config loader.
    Loads JSON files under app/data and returns them as dicts.
    """

    def __init__(self, base_path: str):
        self.base_path = base_path

    # --------------------------------------------------------
    # Load a JSON file
    # --------------------------------------------------------
    def load(self, filename: str) -> Dict[str, Any]:
        """
        filename: e.g. "strategy.json", "autoloop_config.json"
        """

        full_path = os.path.join(self.base_path, filename)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Config file not found: {full_path}")

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning("JSON parsing error in %s", filename, exc_info=True)
            raise ValueError(f"JSON parsing error in {filename}: {e}")

    # --------------------------------------------------------
    # Load multiple files (optional)
    # --------------------------------------------------------
    def load_multiple(self, files: list[str]) -> Dict[str, Any]:
        data = {}
        for f in files:
            data[f] = self.load(f)
        return data
