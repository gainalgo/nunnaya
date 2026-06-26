# ============================================================
# File: app/engine/hyper_engine_runner.py
# Autocoin OS v3-H — Hyper Engine Runner (compat wrapper)
# ------------------------------------------------------------
# Purpose:
# - Provide a stable adapter for legacy callers (e.g., _deprecated_hyper_manager.py)
# - Delegate tick() calls to the injected engine instance
#
# NOTE:
# - The engine implementation lives in app/engine/hyper_nunnaya_engine.py
# - This file MUST NOT define HyperNunnayaEngine (avoid import confusion).
# ============================================================

from __future__ import annotations

from typing import Any, Dict

from app.engine.hyper_engine_base import HyperEngineBase


class HyperEngineRunner:
    """Thin wrapper around a HyperEngineBase.

    Historically some modules imported `HyperEngineRunner` to execute `engine.tick()`.
    Keeping this wrapper avoids large refactors while ensuring imports remain correct.
    """

    def __init__(self, engine: HyperEngineBase):
        if engine is None:
            raise ValueError("engine is required")
        self.engine = engine

    def tick(self, market: str, price: float, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.engine.tick(market, price, *args, **kwargs)


__all__ = ["HyperEngineRunner"]
