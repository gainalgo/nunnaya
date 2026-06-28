# ============================================================
# File: app/strategy/strategy_plugins.py
# Autocoin OS v3-H — Strategy Plugin Re-export Hub
# ------------------------------------------------------------
# Phase 2 (file diet): 4,863 lines → re-export hub (~80 lines)
#
# All classes/functions are split into individual modules.
# This file only handles re-exports for backward compatibility.
# ============================================================

from __future__ import annotations

from typing import Any, Dict

# ── Base types ────────────────────────────────────────────────
from app.strategy.strategy_base import Decision, Signal, StrategyPlugin  # noqa: F401

# ── Shared helpers ────────────────────────────────────────────
from app.strategy.strategy_helpers import (  # noqa: F401
    send_telegram,
    send_signal_telegram,
    adjust_order_amount_and_price,
    should_buy_global_default,
    reserved_queue,
    adjust_ai_score_for_strategy,
    get_regime_fit,
    _get_calibrator,
    _LONGHOLD_PATH,
    _check_btc_regime_for_longhold,
    _register_longhold,
    _try_convert_to_longhold,
    _unregister_longhold,
    _check_longhold_recovery,
    _restore_longhold_flag_from_config,
    _night_mode_adjust_sl,
    _inject_candle_1m_telemetry,
    _percentile,
    _ema_series,
    _rsi_series,
    _macd_turn_snapshot,
    _has_bullish_divergence,
    _reversal_impulse,
    _evaluate_reversal_buy_guard,
    _detect_regime,
    _check_regime_hysteresis,
    _is_breakout,
    _apply_atr_dynamic_limits,
    _common_dca_check,
    _reset_dca_state,
)

# ── LongHold file lock (used by autopilot_manager) ───────────
from app.core.longhold_file_lock import longhold_file_lock as _longhold_write_lock  # noqa: F401

# ── Plugin classes ────────────────────────────────────────────
from app.strategy.plugin_pingpong import PingPongPlugin      # noqa: F401
from app.strategy.plugin_autoloop import AutoloopPlugin       # noqa: F401
from app.strategy.plugin_lightning import LightningPlugin     # noqa: F401
from app.strategy.plugin_gazua import GazuaPlugin             # noqa: F401
from app.strategy.plugin_contrarian import ContrarianPlugin   # noqa: F401
from app.strategy.plugin_sniper import SniperPlugin           # noqa: F401
from app.strategy.plugin_ladder import LadderPlugin           # noqa: F401
from app.strategy.plugin_whale import WhalePlugin, NotImplementedPlugin  # noqa: F401

# ── Registry (singleton) ─────────────────────────────────────
_PLUGIN_SINGLETONS: Dict[str, StrategyPlugin] = {
    "PINGPONG": PingPongPlugin(),
    "AUTOLOOP": AutoloopPlugin(),
    "LIGHTNING": LightningPlugin(),
    "GAZUA": GazuaPlugin(),
    "LADDER": LadderPlugin(),
    "CONTRARIAN": ContrarianPlugin(),
    "SNIPER": SniperPlugin(),
    "WHALE": WhalePlugin(),
}


def get_plugin(name: str) -> StrategyPlugin:
    """Return the plugin corresponding to the given name.

    - Unspecified/unrecognized: returns pingpong (current operating baseline)
    """
    key = str(name or "").strip().upper()
    if not key:
        return _PLUGIN_SINGLETONS["PINGPONG"]
    return _PLUGIN_SINGLETONS.get(key, _PLUGIN_SINGLETONS["PINGPONG"])
