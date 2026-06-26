# ============================================================
# File: app/engine/hyper_engine_base.py
# Autocoin OS v3-H — Unified Base Engine (Final / Fixed)
# ============================================================

from __future__ import annotations
from typing import Any, Dict

from app.engine.hyper_engine_status import EngineStatus


class HyperEngineBase:
    """
    Common base class for v3-H engines.
    Subclass engines only need to implement _tick_impl().
    """

    def __init__(self, name: str | None = None, engine_name: str | None = None):
        # Unify the naming convention
        if name is None and engine_name is not None:
            name = engine_name

        if name is None:
            cls = self.__class__.__name__
            base = cls.replace("Hyper", "").replace("Engine", "")
            name = base.lower()

        self.name = name
        self.status = EngineStatus(name=name)

        # v3-H does not use a state string
        self.current_market: str | None = None

    # --------------------------------------------------------
    # Engine start
    # --------------------------------------------------------
    def start(self, market: str):
        """
        v3-H start:
        - Activate EngineStatus
        - Do not touch Context/Coordinator
        """
        self.current_market = market
        self.status.start()

    # --------------------------------------------------------
    # Engine stop
    # --------------------------------------------------------
    def stop(self):
        self.current_market = None
        self.status.stop()

    # --------------------------------------------------------
    # Main tick loop
    # --------------------------------------------------------
    #Engines are stateless,
    #only Context should be stateful.

    # --------------------------------------------------------
    # Function that subclass engines must implement
    # --------------------------------------------------------
    def _tick_impl(self, market: str, price: float, context):
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _tick_impl()"
        )
