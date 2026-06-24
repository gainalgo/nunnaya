"""
Settings Module
- Structured management of environment variables
- Includes validation
- Tracks source (env/default)

[MIGRATED 2026-01-23] CoinStock → Autocoin
[MIGRATED 2026-03-31] Bybit USDT → Bybit USDT
- min_order: 5 USDT
- max_order: 1,000 USDT
- threshold: 1 USDT
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
import os

from app.core.constants import env_bool, env_float, env_int


@dataclass
class TradingSettings:
    """Trading-related settings"""
    min_order_usdt: float = 5.0
    max_order_usdt: float = 1000.0
    deploy_ratio: float = 0.8
    order_cooldown_sec: float = 3.0
    
    @classmethod
    def from_env(cls) -> "TradingSettings":
        return cls(
            min_order_usdt=env_float("OMA_MIN_ORDER_USDT", default=5.0),
            max_order_usdt=env_float("OMA_MAX_ORDER_USDT", default=1000.0),
            deploy_ratio=env_float("OMA_DEPLOY_RATIO", default=0.8),
            order_cooldown_sec=env_float("OMA_ORDER_COOLDOWN_SEC", default=3.0),
        )


@dataclass
class SmartAllocSettings:
    """Smart Allocation settings"""
    enabled: bool = False
    w_profit: float = 0.5
    w_ai: float = 0.3
    w_risk: float = 0.2
    min_mult: float = 0.5
    max_mult: float = 2.0
    vol_th: float = 0.05
    loss_penalty: float = 0.3
    
    @classmethod
    def from_env(cls) -> "SmartAllocSettings":
        return cls(
            enabled=env_bool("OMA_SMART_ALLOC_ENABLED", default=False),
            w_profit=env_float("OMA_SMART_ALLOC_W_PROFIT", default=0.5),
            w_ai=env_float("OMA_SMART_ALLOC_W_AI", default=0.3),
            w_risk=env_float("OMA_SMART_ALLOC_W_RISK", default=0.2),
            min_mult=env_float("OMA_SMART_ALLOC_MIN_MULT", default=0.5),
            max_mult=env_float("OMA_SMART_ALLOC_MAX_MULT", default=2.0),
            vol_th=env_float("OMA_SMART_ALLOC_VOL_TH", default=0.05),
            loss_penalty=env_float("OMA_SMART_ALLOC_LOSS_PENALTY", default=0.3),
        )


@dataclass
class RegimeSettings:
    """Market Regime settings"""
    enabled: bool = False
    cache_sec: float = 30.0
    min_hold_sec: float = 300.0
    atr_th: float = 3.0
    vol_th: float = 5.0
    bull_ret_th: float = 3.0
    bear_ret_th: float = 3.0
    bull_max_mult_x: float = 1.25
    bear_max_mult_x: float = 0.70
    tp_sl_enabled: bool = False
    
    @classmethod
    def from_env(cls) -> "RegimeSettings":
        return cls(
            enabled=env_bool("OMA_REGIME_ENABLED", default=False),
            cache_sec=env_float("OMA_REGIME_CACHE_SEC", default=30.0),
            min_hold_sec=env_float("OMA_REGIME_MIN_HOLD_SEC", default=300.0),
            atr_th=env_float("OMA_REGIME_ATR_TH", default=3.0),
            vol_th=env_float("OMA_REGIME_VOL_TH", default=5.0),
            bull_ret_th=env_float("OMA_REGIME_BULL_RET_TH", default=3.0),
            bear_ret_th=env_float("OMA_REGIME_BEAR_RET_TH", default=3.0),
            bull_max_mult_x=env_float("OMA_REGIME_BULL_MAX_MULT_X", default=1.25),
            bear_max_mult_x=env_float("OMA_REGIME_BEAR_MAX_MULT_X", default=0.70),
            tp_sl_enabled=env_bool("OMA_REGIME_TP_SL_ENABLED", default=False),
        )


@dataclass
class FearGreedSettings:
    """Fear & Greed settings"""
    enabled: bool = False
    cache_sec: float = 3600.0
    max_stale_sec: float = 21600.0
    extreme_fear_mult: float = 1.30
    fear_mult: float = 1.15
    neutral_mult: float = 1.00
    greed_mult: float = 0.85
    extreme_greed_mult: float = 0.70
    
    @classmethod
    def from_env(cls) -> "FearGreedSettings":
        return cls(
            enabled=env_bool("OMA_FEAR_GREED_ENABLED", default=False),
            cache_sec=env_float("OMA_FEAR_GREED_CACHE_SEC", default=3600.0),
            max_stale_sec=env_float("OMA_FEAR_GREED_MAX_STALE_SEC", default=21600.0),
            extreme_fear_mult=env_float("OMA_FG_EXTREME_FEAR_MULT", default=1.30),
            fear_mult=env_float("OMA_FG_FEAR_MULT", default=1.15),
            neutral_mult=env_float("OMA_FG_NEUTRAL_MULT", default=1.00),
            greed_mult=env_float("OMA_FG_GREED_MULT", default=0.85),
            extreme_greed_mult=env_float("OMA_FG_EXTREME_GREED_MULT", default=0.70),
        )


@dataclass
class AutoRetireSettings:
    """Auto-Retire settings"""
    enabled: bool = True
    threshold_usdt: float = 1.0

    @classmethod
    def from_env(cls) -> "AutoRetireSettings":
        return cls(
            enabled=env_bool("OMA_AUTO_RETIRE_EMPTY", default=True),
            threshold_usdt=env_float("OMA_AUTO_RETIRE_THRESHOLD_USDT", default=1.0),
        )


@dataclass
class SystemSettings:
    """Overall system settings"""
    trading: TradingSettings = field(default_factory=TradingSettings)
    smart_alloc: SmartAllocSettings = field(default_factory=SmartAllocSettings)
    regime: RegimeSettings = field(default_factory=RegimeSettings)
    fear_greed: FearGreedSettings = field(default_factory=FearGreedSettings)
    auto_retire: AutoRetireSettings = field(default_factory=AutoRetireSettings)
    budget_strategy: str = "extreme"
    
    @classmethod
    def from_env(cls) -> "SystemSettings":
        return cls(
            trading=TradingSettings.from_env(),
            smart_alloc=SmartAllocSettings.from_env(),
            regime=RegimeSettings.from_env(),
            fear_greed=FearGreedSettings.from_env(),
            auto_retire=AutoRetireSettings.from_env(),
            budget_strategy=os.getenv("OMA_BUDGET_STRATEGY", "extreme").lower(),
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert settings to a dictionary (for API/logging)"""
        from dataclasses import asdict
        return asdict(self)
    
    def validate(self) -> List[str]:
        """Validate settings. Returns a list of errors."""
        errors = []

        # --- Trading settings ---
        if self.trading.min_order_usdt < 0:
            errors.append("min_order_usdt must be >= 0")
        if self.trading.min_order_usdt > 0 and self.trading.min_order_usdt < 1.0:
            errors.append("min_order_usdt < 1 USDT — below Bybit minimum order amount (1 USDT)")
        if self.trading.max_order_usdt <= 0:
            errors.append("max_order_usdt must be > 0")
        if self.trading.min_order_usdt > self.trading.max_order_usdt:
            errors.append("min_order_usdt must be <= max_order_usdt")
        if self.trading.deploy_ratio < 0 or self.trading.deploy_ratio > 1:
            errors.append("deploy_ratio must be between 0 and 1")
        if self.trading.order_cooldown_sec < 0:
            errors.append("order_cooldown_sec must be >= 0")

        # --- Smart Alloc ---
        if self.smart_alloc.min_mult > self.smart_alloc.max_mult:
            errors.append("smart_alloc.min_mult must be <= max_mult")
        if self.smart_alloc.min_mult <= 0:
            errors.append("smart_alloc.min_mult must be > 0")
        if self.smart_alloc.w_profit < 0 or self.smart_alloc.w_ai < 0 or self.smart_alloc.w_risk < 0:
            errors.append("smart_alloc weights must be >= 0")

        # --- Fear & Greed ---
        if self.fear_greed.extreme_fear_mult <= 0:
            errors.append("fear_greed.extreme_fear_mult must be > 0")
        if self.fear_greed.extreme_greed_mult <= 0:
            errors.append("fear_greed.extreme_greed_mult must be > 0")
        if self.fear_greed.extreme_fear_mult < self.fear_greed.extreme_greed_mult:
            errors.append(
                "fear_greed: extreme_fear_mult should be >= extreme_greed_mult "
                "(should be more aggressive during fear)"
            )

        # --- Auto Retire ---
        if self.auto_retire.threshold_usdt < 0:
            errors.append("auto_retire.threshold_usdt must be >= 0")

        # --- Budget Strategy ---
        valid_strategies = {"extreme", "conservative", "moderate", "balanced"}
        if self.budget_strategy not in valid_strategies:
            errors.append(
                f"budget_strategy '{self.budget_strategy}' invalid; "
                f"choose from {sorted(valid_strategies)}"
            )

        return errors


_settings: Optional[SystemSettings] = None

def get_settings(reload: bool = False) -> SystemSettings:
    """Return the system settings singleton"""
    global _settings
    if _settings is None or reload:
        _settings = SystemSettings.from_env()
    return _settings
