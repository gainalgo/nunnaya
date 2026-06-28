# ============================================================
# File: app/engine/hyper_engine_registry.py
# Autocoin OS v3-H — Engine Registry (Final Edition)
# ============================================================

from __future__ import annotations
from typing import Dict, Optional

from app.engine.hyper_engine_base import HyperEngineBase


class HyperEngineRegistry:
    """
    A single registry that registers and looks up engines by name.
    In v3-H there is only one engine, so duplicate registration is allowed.
    """

    def __init__(self):
        self._engines: Dict[str, HyperEngineBase] = {}

    # --------------------------------------------------------
    def register(self, name: str, engine: HyperEngineBase):
        """
        Allow duplicate engine registration.
        Creating HyperSystem() multiple times must not raise an error.
        """
        # Ignore if the engine is already registered
        if name in self._engines:
            return
        self._engines[name] = engine

    # --------------------------------------------------------
    def get(self, name: str) -> Optional[HyperEngineBase]:
        return self._engines.get(name)

    # --------------------------------------------------------
    def exists(self, name: str) -> bool:
        return name in self._engines

    # --------------------------------------------------------
    def list(self):
        return list(self._engines.keys())


# Global singleton instance
engine_registry = HyperEngineRegistry()
