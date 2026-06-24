"""
HyperSystem Configuration Module
- Load environment variables
- Apply UI settings
- Validate config

[MIGRATED 2026-01-23] CoinStock → Autocoin
[MIGRATED 2026-03-31] Bybit USDT → Bybit USDT
- min_order: 5 USDT
- recovery_min_value: 5 USDT
"""

from __future__ import annotations
import logging
from typing import Dict, Any, Optional, List
import os

logger = logging.getLogger(__name__)

from app.core.constants import (
    env_bool as _env_bool,
    env_float as _env_float,
    env_int as _env_int,
)

# env_json_dict may not exist - handle safely
try:
    from app.core.constants import env_json_dict as _env_json_dict
except ImportError:
    logger.info("[hyper_system_config] env_json_dict not available in constants, using local fallback")
    def _env_json_dict(key: str, default: Optional[Dict] = None) -> Dict:
        import json
        val = os.getenv(key, "")
        if not val:
            return default or {}
        try:
            return json.loads(val)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("[hyper_system_config] Failed to parse env var %s as JSON", key, exc_info=True)
            return default or {}


def load_hyper_system_config() -> Dict[str, Any]:
    """Load HyperSystem config.

    Reads all settings from environment variables and returns them as a dict.
    """
    config = {}
    
    # Trading Mode
    config["trading_mode"] = "LIVE" if _env_bool("AUTOBOT_LIVE", default=False) else "DRY"
    from app.core.bybit_trading import get_v5_order_category

    config["bybit_v5_category"] = get_v5_order_category()
    
    # Capital Management
    config["deploy_ratio"] = _env_float("OMA_DEPLOY_RATIO", default=0.80)
    config["min_order_usdt"] = _env_float("OMA_MIN_ORDER_USDT", default=5.0)
    config["fixed_principal"] = _env_bool("OMA_FIXED_PRINCIPAL", default=False)
    config["wallet_mode"] = _env_bool("OMA_WALLET_MODE", default=False)
    
    # Reconcile
    config["reconcile_interval"] = _env_float("OMA_RECONCILE_INTERVAL_SEC", default=30.0)
    config["reconcile_position_sync"] = os.getenv("OMA_RECONCILE_POSITION_SYNC", "ACTIVE").upper()
    if config["reconcile_position_sync"] not in ("OFF", "ACTIVE", "ALL"):
        config["reconcile_position_sync"] = "ACTIVE"
    
    # Recovery
    config["recovery_policy"] = os.getenv("OMA_RECOVERY_POLICY", "HOLD").upper()
    config["recovery_min_value_usdt"] = _env_float("OMA_RECOVERY_MIN_VALUE_USDT", default=5.0)
    
    # Order FSM
    config["order_timeout_sec_buy"] = _env_float("OMA_ORDER_TIMEOUT_BUY_SEC", default=8.0)
    config["order_timeout_sec_sell"] = _env_float("OMA_ORDER_TIMEOUT_SELL_SEC", default=10.0)
    config["order_cooldown_sec"] = _env_float("OMA_ORDER_COOLDOWN_SEC", default=3.0)
    
    # Context State
    config["context_state_stale_reset_sec"] = _env_float("OMA_CONTEXT_STATE_STALE_RESET_SEC", default=3600.0)
    config["context_state_max_prices"] = _env_int("OMA_CONTEXT_STATE_MAX_PRICES", default=100)
    
    # Smart Allocation
    config["smart_alloc_enabled"] = _env_bool("OMA_SMART_ALLOC_ENABLED", default=False)
    config["smart_alloc_w_profit"] = _env_float("OMA_SMART_ALLOC_W_PROFIT", default=0.5)
    config["smart_alloc_w_ai"] = _env_float("OMA_SMART_ALLOC_W_AI", default=0.3)
    config["smart_alloc_w_risk"] = _env_float("OMA_SMART_ALLOC_W_RISK", default=0.2)
    config["smart_alloc_min_mult"] = _env_float("OMA_SMART_ALLOC_MIN_MULT", default=0.5)
    config["smart_alloc_max_mult"] = _env_float("OMA_SMART_ALLOC_MAX_MULT", default=2.0)
    config["smart_alloc_vol_th"] = _env_float("OMA_SMART_ALLOC_VOL_TH", default=0.05)
    config["smart_alloc_loss_penalty"] = _env_float("OMA_SMART_ALLOC_LOSS_PENALTY", default=0.3)
    
    # Budget Strategy
    config["budget_strategy"] = os.getenv("OMA_BUDGET_STRATEGY", "extreme").lower()
    
    # Auto-Retire
    config["auto_retire_empty"] = _env_bool("OMA_AUTO_RETIRE_EMPTY", default=True)
    
    # Reserved Scanner
    config["reserved_pingpong_n"] = _env_int("OMA_RESERVED_PINGPONG_N", default=5)
    config["reserved_autoloop_n"] = _env_int("OMA_RESERVED_AUTOLOOP_N", default=3)
    config["reserved_ladder_n"] = _env_int("OMA_RESERVED_LADDER_N", default=2)
    config["reserved_lightning_n"] = _env_int("OMA_RESERVED_LIGHTNING_N", default=0)
    config["reserved_gazua_n"] = _env_int("OMA_RESERVED_GAZUA_N", default=0)
    config["reserved_contrarian_n"] = _env_int("OMA_RESERVED_CONTRARIAN_N", default=0)

    # [2026-05-30] Per-strategy ON/OFF toggle (9-month insight — slot 0 also blocks idle operation)
    # enabled=False → target forced to 0 → no candidate scan → plugin.decide() also returns hold immediately
    config["reserved_pingpong_enabled"] = _env_bool("OMA_RESERVED_PINGPONG_ENABLED", default=True)
    config["reserved_autoloop_enabled"] = _env_bool("OMA_RESERVED_AUTOLOOP_ENABLED", default=True)
    config["reserved_ladder_enabled"] = _env_bool("OMA_RESERVED_LADDER_ENABLED", default=True)
    config["reserved_lightning_enabled"] = _env_bool("OMA_RESERVED_LIGHTNING_ENABLED", default=True)
    config["reserved_gazua_enabled"] = _env_bool("OMA_RESERVED_GAZUA_ENABLED", default=True)
    config["reserved_contrarian_enabled"] = _env_bool("OMA_RESERVED_CONTRARIAN_ENABLED", default=True)
    config["reserved_sniper_enabled"] = _env_bool("OMA_RESERVED_SNIPER_ENABLED", default=True)
    config["reserved_whale_enabled"] = _env_bool("OMA_RESERVED_WHALE_ENABLED", default=True)

    # [2026-05-30] Per-strategy explicit budget (decision — "assume all strategies run simultaneously")
    # budget_usdt=0 → legacy auto allocation (compatibility fallback)
    # budget_usdt>0 → manual override = isolate plugin's own pool + auto-allocate within it
    config["reserved_pingpong_budget_usdt"] = _env_float("OMA_RESERVED_PINGPONG_BUDGET_USDT", default=0.0)
    config["reserved_autoloop_budget_usdt"] = _env_float("OMA_RESERVED_AUTOLOOP_BUDGET_USDT", default=0.0)
    config["reserved_ladder_budget_usdt"] = _env_float("OMA_RESERVED_LADDER_BUDGET_USDT", default=0.0)
    config["reserved_lightning_budget_usdt"] = _env_float("OMA_RESERVED_LIGHTNING_BUDGET_USDT", default=0.0)
    config["reserved_gazua_budget_usdt"] = _env_float("OMA_RESERVED_GAZUA_BUDGET_USDT", default=0.0)
    config["reserved_contrarian_budget_usdt"] = _env_float("OMA_RESERVED_CONTRARIAN_BUDGET_USDT", default=0.0)
    config["reserved_sniper_budget_usdt"] = _env_float("OMA_RESERVED_SNIPER_BUDGET_USDT", default=0.0)
    config["reserved_whale_budget_usdt"] = _env_float("OMA_RESERVED_WHALE_BUDGET_USDT", default=0.0)

    return config


def validate_config(config: Dict[str, Any]) -> List[str]:
    """Validate config. Returns a list of errors."""
    errors = []
    
    if config.get("deploy_ratio", 0) < 0 or config.get("deploy_ratio", 0) > 1:
        errors.append("deploy_ratio must be between 0 and 1")
    
    if config.get("min_order_usdt", 0) < 0:
        errors.append("min_order_usdt must be >= 0")
    
    if config.get("smart_alloc_min_mult", 0) > config.get("smart_alloc_max_mult", 0):
        errors.append("smart_alloc_min_mult must be <= max_mult")
    
    return errors


def apply_ui_settings(config: Dict[str, Any], ui_settings: Dict[str, Any]) -> Dict[str, Any]:
    """Apply values configured in the UI.

    UI settings take precedence over environment variables.
    """
    result = dict(config)

    # mapping: ui_key -> config_key
    mappings = {
        "deploy_ratio": "deploy_ratio",
        "min_order_usdt": "min_order_usdt",
        "smart_alloc_enabled": "smart_alloc_enabled",
        "smart_alloc_w_profit": "smart_alloc_w_profit",
        "smart_alloc_w_ai": "smart_alloc_w_ai",
        "smart_alloc_w_risk": "smart_alloc_w_risk",
        "pingpong_n": "reserved_pingpong_n",
        "autoloop_n": "reserved_autoloop_n",
        "ladder_n": "reserved_ladder_n",
        "lightning_n": "reserved_lightning_n",
        "gazua_n": "reserved_gazua_n",
        "contrarian_n": "reserved_contrarian_n",
    }
    
    for ui_key, config_key in mappings.items():
        if ui_key in ui_settings and ui_settings[ui_key] is not None:
            result[config_key] = ui_settings[ui_key]
    
    return result
