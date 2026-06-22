# -*- coding: utf-8 -*-
"""
Centralized runtime paths management.

Provides exchange-namespaced paths for all runtime files to support
multi-exchange operation without file conflicts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RuntimePaths:
    """
    Centralized runtime file paths.
    
    All paths are prefixed with the exchange namespace to prevent
    conflicts when running multiple exchanges.
    
    Example:
        paths = RuntimePaths(exchange="bybit")
        paths.ledger  # -> "runtime/bybit/trade_ledger.jsonl"

        # Legacy mode (no namespace)
        paths = RuntimePaths(exchange=None)
        paths.ledger  # -> "runtime/trade_ledger.jsonl"
    """
    
    exchange: Optional[str] = None  # None = legacy (no namespace)
    base_dir: str = "runtime"
    
    # Computed paths (set in __post_init__)
    root: str = field(init=False)
    ledger: str = field(init=False)
    context_state: str = field(init=False)
    oma_state: str = field(init=False)
    ui_settings: str = field(init=False)
    reserved_queue: str = field(init=False)
    recovery_state: str = field(init=False)
    risk_budget_state: str = field(init=False)
    ladder_config: str = field(init=False)
    longhold_config: str = field(init=False)
    ladder_orders: str = field(init=False)
    autopilot_cooldown: str = field(init=False)
    market_status_state: str = field(init=False)
    external_sync_state: str = field(init=False)
    pnl_baseline: str = field(init=False)
    ledger_reactor_offset: str = field(init=False)
    ai_model_meta: str = field(init=False)
    
    def __post_init__(self) -> None:
        # Determine root directory
        if self.exchange:
            self.root = os.path.join(self.base_dir, self.exchange)
        else:
            self.root = self.base_dir
        
        # Set all paths
        self.ledger = self._path("trade_ledger.jsonl")
        self.context_state = self._path("context_state.json")
        self.oma_state = self._path("oma_state.json")
        self.ui_settings = self._path("ui_settings.json")
        self.reserved_queue = self._path("reserved_queue.json")
        self.recovery_state = self._path("recovery_state.json")
        self.risk_budget_state = self._path("risk_budget_state.json")
        self.ladder_config = self._path("ladder_config.json")
        self.longhold_config = self._path("longhold_config.json")
        self.ladder_orders = self._path("ladder_orders.json")
        self.autopilot_cooldown = self._path("autopilot_cooldown.json")
        self.market_status_state = self._path("market_status_state.json")
        self.external_sync_state = self._path("external_sync_state.json")
        self.pnl_baseline = self._path("pnl_baseline.json")
        self.ledger_reactor_offset = self._path("ledger_reactor.offset")
        self.ai_model_meta = self._path("ai_model_meta.json")
        
        # Ensure directory exists
        os.makedirs(self.root, exist_ok=True)
    
    def _path(self, filename: str) -> str:
        """Get full path for a filename."""
        return os.path.join(self.root, filename)
    
    def custom(self, filename: str) -> str:
        """Get path for a custom filename."""
        return self._path(filename)
    
    @classmethod
    def from_env(cls, exchange: Optional[str] = None) -> "RuntimePaths":
        """Create RuntimePaths from environment variables.
        
        Environment variables:
            OMA_RUNTIME_BASE_DIR: Base runtime directory (default: "runtime")
            OMA_EXCHANGE: Exchange name for namespacing (default: None)
        """
        base_dir = os.getenv("OMA_RUNTIME_BASE_DIR", "runtime")
        if exchange is None:
            exchange = os.getenv("OMA_EXCHANGE") or None
        return cls(exchange=exchange, base_dir=base_dir)
    
    @classmethod
    def legacy(cls) -> "RuntimePaths":
        """Create legacy RuntimePaths without namespace."""
        return cls(exchange=None)
    
    @classmethod
    def bybit(cls) -> "RuntimePaths":
        """Create Bybit RuntimePaths."""
        return cls(exchange="bybit")


# Default singleton (legacy mode for backwards compatibility)
# Will be replaced with exchange-specific instance in HyperSystem
_default_paths: Optional[RuntimePaths] = None


def get_runtime_paths(exchange: Optional[str] = None) -> RuntimePaths:
    """Get RuntimePaths instance.
    
    Args:
        exchange: Exchange name for namespacing. If None, returns legacy paths.
    """
    global _default_paths
    
    if exchange:
        return RuntimePaths(exchange=exchange)
    
    if _default_paths is None:
        _default_paths = RuntimePaths.from_env()
    
    return _default_paths


def set_default_runtime_paths(paths: RuntimePaths) -> None:
    """Set the default RuntimePaths instance."""
    global _default_paths
    _default_paths = paths
