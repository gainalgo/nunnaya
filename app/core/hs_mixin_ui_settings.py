# ============================================================
# File: app/core/hs_mixin_ui_settings.py
# Phase 5B: UI settings methods extracted from hyper_system.py
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class UISettingsMixin:
    """UI settings snapshot/apply mixin.

    Expects (from HyperSystem.__init__):
        self.ledger, self.btc_guard_mode, self.btc_guard_enabled,
        self._pre_guard_auto_approve, self.recovery_boost_active,
        self._restore_trailing_stops(), self._deactivate_recovery_boost(),
        and numerous self.* guard/autopilot/reserved attributes.
    """

    def _ui_as_bool(self, v: Any, default: Optional[bool] = None) -> Optional[bool]:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "yes", "y", "on"):
                return True
            if s in ("0", "false", "no", "n", "off"):
                return False
        return default

    def _ui_as_float(self, v: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            if v is None:
                return default
            return float(v)
        except (TypeError, ValueError) as exc:
            try:
                self.ledger.append("SYSTEM_HELPER_ERROR", where="ui_as_float", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc2:
                logger.warning("Failed to log ui_as_float conversion error: %s", exc2)
            return default

    def _ui_as_int(self, v: Any, default: Optional[int] = None) -> Optional[int]:
        try:
            if v is None:
                return default
            return int(float(v))
        except (OverflowError, TypeError, ValueError) as exc:
            try:
                self.ledger.append("SYSTEM_HELPER_ERROR", where="ui_as_int", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc2:
                logger.warning("Failed to log ui_as_int conversion error: %s", exc2)
            return default

    def _ui_as_str(self, v: Any, default: Optional[str] = None) -> Optional[str]:
        if v is None:
            return default
        try:
            s = str(v)
            return s
        except (AttributeError, TypeError, ValueError) as exc:
            try:
                self.ledger.append("SYSTEM_HELPER_ERROR", where="ui_as_str", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc2:
                logger.warning("Failed to log ui_as_str conversion error: %s", exc2)
            return default

    def _apply_bybit_v5_category_setting(self, raw: Any) -> None:
        """Apply Bybit V5 market category from dashboard (spot | linear | follow .env)."""
        from app.core.bybit_trading import (
            get_v5_order_category,
            set_v5_order_category_runtime,
            v5_order_category_from_env_only,
        )

        s = str(raw or "").strip().lower()
        if s in ("", "default", "env", "follow_env", "inherit"):
            set_v5_order_category_runtime(None)
            self.bybit_v5_category = v5_order_category_from_env_only()
        elif s in ("spot", "linear"):
            self.bybit_v5_category = s
            set_v5_order_category_runtime(s)
        else:
            self.bybit_v5_category = get_v5_order_category()
            return
        tc = getattr(self, "trade_client", None)
        if tc is not None and hasattr(tc, "_category"):
            try:
                tc._category = str(self.bybit_v5_category)
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("Failed to apply Bybit V5 category to trade_client: %s", exc)
        pf = getattr(self, "price_feed", None)
        if pf is not None and hasattr(pf, "request_resubscribe"):
            try:
                pf.request_resubscribe()
            except (AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("Failed to resubscribe price feed after Bybit V5 category change: %s", exc)
        try:
            self.ledger.append("BYBIT_V5_CATEGORY_UI", category=str(self.bybit_v5_category))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("Failed to log Bybit V5 category change to ledger: %s", exc)

    def _true_auto_approve(self, strategy: str, default: bool = False) -> bool:
        """BTC Guard 무관하게 사용자의 원래 auto_approve 값 반환."""
        attr = f"autopilot_auto_approve_{strategy}"
        if getattr(self, "btc_guard_mode", False):
            pga = getattr(self, "_pre_guard_auto_approve", {})
            if pga:
                return bool(pga.get(strategy, getattr(self, attr, default)))
        return bool(getattr(self, attr, default))

    def _ui_guard_settings_snapshot(self) -> Dict[str, Any]:
        """Return current *global* guard settings.

        NOTE: runtime state (cooldowns, emergency stop latch, etc.) is excluded.
        """
        from app.core.bybit_trading import is_v5_order_category_runtime_overridden

        _bybit_v5_cat_key = (
            "default"
            if not is_v5_order_category_runtime_overridden()
            else str(getattr(self, "bybit_v5_category", "spot") or "spot")
        )
        return {
            # toggles
            "exit_profit_guard": bool(getattr(self, "exit_profit_guard", False)),
            "entry_ob_guard_enabled": bool(getattr(self, "entry_ob_guard_enabled", False)),
            "entry_ceiling_guard": bool(getattr(self, "entry_ceiling_guard", False)),
            "entry_recent_high_guard": bool(getattr(self, "entry_recent_high_guard", False)),
            "entry_qty_guard": bool(getattr(self, "entry_qty_guard", False)),
            "drawdown_guard": bool(getattr(self, "drawdown_guard", False)),
            "correlation_guard_enabled": bool(getattr(self, "correlation_guard_enabled", False)),
            "risk_budget_enabled": bool(getattr(self, "risk_budget_enabled", False)),
            "dynamic_stoploss_enabled": bool(getattr(self, "dynamic_stoploss_enabled", False)),
            "btc_guard_enabled": bool(getattr(self, "btc_guard_enabled", True)),
            "recovery_boost_enabled": bool(getattr(self, "recovery_boost_enabled", True)),
            "tp_limit_exit_enabled": bool(getattr(self, "tp_limit_exit_enabled", False)),
            "wallet_mode": bool(getattr(self, "wallet_mode", False)),

            # entry / ob guard
            "entry_ob_max_spread_bps": float(getattr(self, "entry_ob_max_spread_bps", 0.0) or 0.0),
            "entry_ob_depth_bps": float(getattr(self, "entry_ob_depth_bps", 0.0) or 0.0),
            "entry_ob_depth_factor": float(getattr(self, "entry_ob_depth_factor", 0.0) or 0.0),
            "entry_ob_stale_sec": float(getattr(self, "entry_ob_stale_sec", 0.0) or 0.0),

            # entry ceiling guard
            "entry_ceiling_apply": str(getattr(self, "entry_ceiling_apply", "NON_BULL") or "NON_BULL"),
            "entry_ceiling_fee_rate": float(getattr(self, "entry_ceiling_fee_rate", 0.0) or 0.0),
            "entry_ceiling_slippage_guard_bps": float(getattr(self, "entry_ceiling_slippage_guard_bps", 0.0) or 0.0),
            "entry_ceiling_spread_guard_bps": float(getattr(self, "entry_ceiling_spread_guard_bps", 0.0) or 0.0),
            "entry_ceiling_extra_bps": float(getattr(self, "entry_ceiling_extra_bps", 0.0) or 0.0),
            "entry_ceiling_max_age_sec": float(getattr(self, "entry_ceiling_max_age_sec", 0.0) or 0.0),
            "entry_ceiling_decay_mode": str(getattr(self, "entry_ceiling_decay_mode", "LINEAR") or "LINEAR"),
            "entry_ceiling_decay_half_life_sec": float(getattr(self, "entry_ceiling_decay_half_life_sec", 0.0) or 0.0),
            "entry_ceiling_cooldown_sec": float(getattr(self, "entry_ceiling_cooldown_sec", 0.0) or 0.0),
            "entry_ceiling_force_on_bull_sec": float(getattr(self, "entry_ceiling_force_on_bull_sec", 0.0) or 0.0),

            # entry recent-high guard
            "entry_recent_high_apply": str(getattr(self, "entry_recent_high_apply", "NON_BULL") or "NON_BULL"),
            "entry_recent_high_lookback_hours": float(getattr(self, "entry_recent_high_lookback_hours", 24.0) or 24.0),
            "entry_recent_high_near_pct": float(getattr(self, "entry_recent_high_near_pct", 0.8) or 0.8),
            "entry_recent_high_cooldown_sec": float(getattr(self, "entry_recent_high_cooldown_sec", 10.0) or 10.0),
            "entry_recent_high_candle_unit_min": int(getattr(self, "entry_recent_high_candle_unit_min", 15) or 15),
            "entry_recent_high_cache_sec": float(getattr(self, "entry_recent_high_cache_sec", 30.0) or 30.0),
            "entry_recent_high_breakout_enabled": bool(getattr(self, "entry_recent_high_breakout_enabled", True)),
            "entry_recent_high_breakout_margin_pct": float(getattr(self, "entry_recent_high_breakout_margin_pct", 0.25) or 0.25),
            "entry_recent_high_breakout_require_bull": bool(getattr(self, "entry_recent_high_breakout_require_bull", True)),
            "entry_recent_high_breakout_min_regime_change_pct": float(getattr(self, "entry_recent_high_breakout_min_regime_change_pct", 0.35) or 0.35),
            "entry_recent_high_breakout_max_spread_bps": float(getattr(self, "entry_recent_high_breakout_max_spread_bps", 18.0) or 18.0),

            # entry qty guard
            "entry_max_qty": float(getattr(self, "entry_max_qty", 0.0) or 0.0),
            "entry_qty_cooldown_sec": float(getattr(self, "entry_qty_cooldown_sec", 0.0) or 0.0),

            # exit profit guard
            "exit_fee_rate": float(getattr(self, "exit_fee_rate", 0.0) or 0.0),
            "exit_slippage_guard_bps": float(getattr(self, "exit_slippage_guard_bps", 0.0) or 0.0),
            "exit_min_net_profit_pct": float(getattr(self, "exit_min_net_profit_pct", 0.0) or 0.0),
            "exit_min_net_profit_usdt": float(getattr(self, "exit_min_net_profit_usdt", 0.0) or 0.0),

            # TP limit exit
            "tp_limit_timeout_sec": float(getattr(self, "tp_limit_timeout_sec", 0.0) or 0.0),
            "tp_limit_max_retries": int(getattr(self, "tp_limit_max_retries", 0) or 0),

            # Entry limit buy (지정가 진입)
            "entry_limit_buy_enabled": bool(getattr(self, "entry_limit_buy_enabled", False)),
            "entry_limit_timeout_sec": float(getattr(self, "entry_limit_timeout_sec", 5.0) or 5.0),
            "entry_limit_price_mode": str(getattr(self, "entry_limit_price_mode", "best_bid") or "best_bid"),
            "entry_limit_cooldown_sec": float(getattr(self, "entry_limit_cooldown_sec", 30.0) or 30.0),
            "btc_guard_down_5m_pct": float(getattr(self, "btc_guard_down_5m_pct", 2.0) or 2.0),
            "btc_guard_down_15m_pct": float(getattr(self, "btc_guard_down_15m_pct", 5.0) or 5.0),
            "btc_guard_trail_tighten_ratio": float(getattr(self, "btc_guard_trail_tighten_ratio", 0.5) or 0.5),

            # misc throttles (useful to diagnose UI)
            "min_order_usdt": float(getattr(self, "min_order_usdt", 5.0) or 5.0),
            "entry_global_gap_sec": float(getattr(self, "entry_global_gap_sec", 0.0) or 0.0),
            "max_pending_orders_total": int(getattr(self, "max_pending_orders_total", 0) or 0),
            "ai_retrain_threshold": float(getattr(self, "ai_retrain_threshold", 0.6) or 0.6),
            # Risk / Smart
            "daily_loss_limit_pct": float(getattr(self, "daily_loss_limit_pct", 2.0) or 2.0),
            "circuit_breaker_loss_pct": float(getattr(self, "circuit_breaker_loss_pct", 10.0) or 10.0),
            "circuit_breaker_cooldown_min": float(getattr(self, "circuit_breaker_cooldown_min", 30.0) or 30.0),
            "max_same_sector": int(getattr(self, "max_same_sector", 2) or 2),
            "high_correlation_threshold": float(getattr(self, "high_correlation_threshold", 0.7) or 0.7),
            "smart_alloc_enabled": bool(getattr(self, "smart_alloc_enabled", True)),
            "smart_alloc_w_profit": float(getattr(self, "smart_alloc_w_profit", 0.5) or 0.5),
            "smart_alloc_w_ai": float(getattr(self, "smart_alloc_w_ai", 0.3) or 0.3),
            "smart_alloc_w_risk": float(getattr(self, "smart_alloc_w_risk", 0.2) or 0.2),
            "smart_alloc_w_momentum": float(getattr(self, "smart_alloc_w_momentum", 0.15) or 0.15),
            "smart_alloc_w_kelly": float(getattr(self, "smart_alloc_w_kelly", 0.15) or 0.15),
            "smart_alloc_w_liquidity": float(getattr(self, "smart_alloc_w_liquidity", 0.15) or 0.15),
            "smart_alloc_min_mult": float(getattr(self, "smart_alloc_min_mult", 0.5) or 0.5),
            "smart_alloc_max_mult": float(getattr(self, "smart_alloc_max_mult", 2.0) or 2.0),
            "smart_alloc_corr_enabled": bool(getattr(self, "smart_alloc_corr_enabled", True)),
            "smart_alloc_sector_enabled": bool(getattr(self, "smart_alloc_sector_enabled", True)),
            "smart_alloc_corr_th": float(getattr(self, "smart_alloc_corr_th", 0.7) or 0.7),

            # --- Reserved / Autopilot ---
            "auto_slot_enabled": bool(getattr(self, "auto_slot_enabled", False)),
            "reserved_pingpong_n": int(getattr(self, "reserved_pingpong_n", 3) or 0),
            "reserved_autoloop_n": int(getattr(self, "reserved_autoloop_n", 3) or 0),
            "reserved_ladder_n": int(getattr(self, "reserved_ladder_n", 0) or 0),
            "reserved_lightning_n": int(getattr(self, "reserved_lightning_n", 0) or 0),
            "reserved_gazua_n": int(getattr(self, "reserved_gazua_n", 0) or 0),
            "reserved_contrarian_n": int(getattr(self, "reserved_contrarian_n", 0) or 0),
            "reserved_sniper_n": int(getattr(self, "reserved_sniper_n", 0) or 0),
            "reserved_whale_n": int(getattr(self, "reserved_whale_n", 0) or 0),
            "reserved_candidate_price_min_usdt": float(getattr(self, "reserved_candidate_price_min_usdt", 0.0) or 0.0),
            "reserved_candidate_price_max_usdt": float(getattr(self, "reserved_candidate_price_max_usdt", 0.0) or 0.0),
            # autopilot_scope_target_n은 아래 scope 블록(L1845)에서 정의 (중복 제거)
            "autopilot_scope_instant_buy_min_conf": float(getattr(self, "autopilot_scope_instant_buy_min_conf", 55.0) or 55.0),
            "sniper_min_surge_pct": float(getattr(self, "sniper_min_surge_pct", 5.0) or 5.0),
            "sniper_scan_timeframe": str(getattr(self, "sniper_scan_timeframe", "1h") or "1h"),
            "sniper_scan_mode": str(getattr(self, "sniper_scan_mode", "relative") or "relative"),
            "reserved_apply_suggested_budget": bool(getattr(self, "reserved_apply_suggested_budget", True)),
            "reserved_promote_to_active": bool(getattr(self, "reserved_promote_to_active", False)),

            "autopilot_enabled": bool(getattr(self, "autopilot_enabled", False)),
            "autopilot_auto_approve": bool(getattr(self, "autopilot_auto_approve", False)),
            # [FIX] BTC Guard 활성 중에는 _pre_guard 원본값 저장 (False로 덮어쓰기 방지)
            "autopilot_auto_approve_pingpong": self._true_auto_approve("pingpong", True),
            "autopilot_auto_approve_autoloop": self._true_auto_approve("autoloop", True),
            "autopilot_auto_approve_ladder": self._true_auto_approve("ladder", False),
            "autopilot_auto_approve_lightning": self._true_auto_approve("lightning", False),
            "autopilot_auto_approve_gazua": self._true_auto_approve("gazua", False),
            "autopilot_auto_approve_contrarian": self._true_auto_approve("contrarian", False),
            "autopilot_auto_approve_sniper": self._true_auto_approve("sniper", False),

            # [2026-02-04] 백테스트 가중치
            "backtest_weight_pingpong": float(getattr(self, "backtest_weight_pingpong", 0.10)),
            "backtest_weight_autoloop": float(getattr(self, "backtest_weight_autoloop", 0.15)),
            "backtest_weight_ladder": float(getattr(self, "backtest_weight_ladder", 0.30)),
            "backtest_weight_lightning": float(getattr(self, "backtest_weight_lightning", 0.15)),
            "backtest_weight_gazua": float(getattr(self, "backtest_weight_gazua", 0.35)),
            "backtest_weight_contrarian": float(getattr(self, "backtest_weight_contrarian", 0.20)),
            "backtest_weight_sniper": float(getattr(self, "backtest_weight_sniper", 0.30)),

            "autopilot_ai_gate_enabled": bool(getattr(self, "autopilot_ai_gate_enabled", False)),
            "autopilot_ai_gate_threshold": float(getattr(self, "autopilot_ai_gate_threshold", 0.55) or 0.55),

            "autopilot_ai_demote_enabled": bool(getattr(self, "autopilot_ai_demote_enabled", False)),
            "autopilot_ai_demote_threshold": float(getattr(self, "autopilot_ai_demote_threshold", 0.45) or 0.45),

            "time_zone_optimizer_enabled": bool(getattr(self, "time_zone_optimizer_enabled", False)),

            "autopilot_idle_demote_enabled": bool(getattr(self, "autopilot_idle_demote_enabled", True)),
            "autopilot_idle_demote_min": int(getattr(self, "autopilot_idle_demote_min", 180) or 0),
            "autopilot_idle_demote_overrides": dict(getattr(self, "autopilot_idle_demote_overrides", {}) or {}),

            # [2026-02-01] 24시간 무거래 → LongHold 자동 전환
            "autopilot_idle_to_longhold_enabled": bool(getattr(self, "autopilot_idle_to_longhold_enabled", True)),
            "autopilot_idle_to_longhold_hours": int(getattr(self, "autopilot_idle_to_longhold_hours", 24) or 24),

            "autopilot_eval_interval_sec": int(getattr(self, "autopilot_eval_interval_sec", 300) or 0),
            "autopilot_grace_sec": int(getattr(self, "autopilot_grace_sec", 900) or 0),
            "autopilot_demote_max_total": int(getattr(self, "autopilot_demote_max_total", 2) or 0),
            "autopilot_demote_max_per_strategy": int(getattr(self, "autopilot_demote_max_per_strategy", 1) or 0),

            "autopilot_window_enabled": bool(getattr(self, "autopilot_window_enabled", False)),
            "autopilot_window_start": str(getattr(self, "autopilot_window_start", "22:00") or "22:00"),
            "autopilot_window_end": str(getattr(self, "autopilot_window_end", "08:00") or "08:00"),

            "autopilot_guard_demote_enabled": bool(getattr(self, "autopilot_guard_demote_enabled", False)),
            "autopilot_guard_demote_window_min": int(getattr(self, "autopilot_guard_demote_window_min", 30) or 0),
            "autopilot_guard_demote_n": int(getattr(self, "autopilot_guard_demote_n", 12) or 0),

            "autopilot_signal_miss_enabled": bool(getattr(self, "autopilot_signal_miss_enabled", False)),
            "autopilot_signal_miss_window_min": int(getattr(self, "autopilot_signal_miss_window_min", 30) or 0),
            "autopilot_signal_miss_min_attempts": int(getattr(self, "autopilot_signal_miss_min_attempts", 6) or 0),

            # Scope Slot Rotation
            "autopilot_scope_rotation_enabled": bool(getattr(self, "autopilot_scope_rotation_enabled", True)),
            "autopilot_scope_idle_min": int(getattr(self, "autopilot_scope_idle_min", 2) or 2),
            "autopilot_scope_deploy_mode": str(getattr(self, "autopilot_scope_deploy_mode", "wait")),
            "autopilot_scope_trap_tp_timeout_hours": float(getattr(self, "autopilot_scope_trap_tp_timeout_hours", 4.0) or 0),
            "autopilot_scope_cooldown_min": int(getattr(self, "autopilot_scope_cooldown_min", 60) or 0),
            "autopilot_scope_adaptive_cd": bool(getattr(self, "autopilot_scope_adaptive_cd", True)),
            "autopilot_scope_target_n": int(getattr(self, "autopilot_scope_target_n", getattr(self, "reserved_sniper_n", 0)) or 0),
            # LONG/SHORT (SNIPER(s) Scope) UI prefs
            "longshort_scope_power": bool(getattr(self, "longshort_scope_power", True)),
            "longshort_scope_auto_fire": bool(getattr(self, "longshort_scope_auto_fire", True)),
            "longshort_scope_assist_fire": bool(getattr(self, "longshort_scope_assist_fire", True)),
            "longshort_scope_assist_fire_auto": bool(getattr(self, "longshort_scope_assist_fire_auto", False)),
            "longshort_scope_slicing": bool(getattr(self, "longshort_scope_slicing", True)),
            "longshort_scope_random_active": bool(getattr(self, "longshort_scope_random_active", True)),
            "longshort_scope_random_interval_sec": int(getattr(self, "longshort_scope_random_interval_sec", 60) or 60),
            "longshort_scope_top_n": int(getattr(self, "longshort_scope_top_n", 5) or 5),
            "longshort_scope_budget_per_slot_usdt": int(getattr(self, "longshort_scope_budget_per_slot_usdt", 100) or 100),
            "longshort_scope_min_conf": float(getattr(self, "longshort_scope_min_conf", 10.0) or 10.0),
            "longshort_scope_auto_scan": bool(getattr(self, "longshort_scope_auto_scan", True)),
            "longshort_scope_min_price": float(getattr(self, "longshort_scope_min_price", 0.0) or 0),
            "longshort_scope_max_price": float(getattr(self, "longshort_scope_max_price", 0.0) or 0),

            # [2026-02-01] 자동 먼지 청소
            "dust_vacuum_enabled": bool(getattr(self, "dust_vacuum_enabled", False)),
            "dust_vacuum_daily_count": int(getattr(self, "dust_vacuum_daily_count", 1) or 1),
            "dust_vacuum_threshold_usdt": float(getattr(self, "dust_vacuum_threshold_usdt", 5.0) or 5.0),

            "reconcile_position_sync_mode": str(getattr(self, "reconcile_position_sync_mode", "OFF") or "OFF"),

            # Bybit REST/WS/order category; "default" = follow BYBIT_V5_CATEGORY env
            "bybit_v5_category": _bybit_v5_cat_key,

            # [2026-03-23] 스마트 리스크 기능 (①②③)
            "dynamic_size_mult_enabled": bool(getattr(self, "dynamic_size_mult_enabled", True)),
            "size_mult_hi_pct": float(os.getenv("OMA_SIZE_MULT_HI_PCT", "-2.0")),
            "size_mult_floor": float(os.getenv("OMA_SIZE_MULT_FLOOR", "0.4")),
            "regime_per_strategy_enabled": bool(getattr(self, "regime_per_strategy_enabled", False)),
            "concentration_limit_enabled": bool(getattr(self, "concentration_limit_enabled", False)),
            "concentration_limit_pct": float(getattr(self, "concentration_limit_pct", 15.0)),
        }

    def _ui_apply_guard_settings(self, patch: Dict[str, Any]) -> None:
        """Apply persisted global guard settings (dashboard overrides)."""
        if not isinstance(patch, dict) or not patch:
            return

        if "bybit_v5_category" in patch:
            self._apply_bybit_v5_category_setting(patch.get("bybit_v5_category"))

        # bool toggles
        for k in (
            "exit_profit_guard",
            "entry_ob_guard_enabled",
            "entry_ceiling_guard",
            "entry_recent_high_guard",
            "entry_qty_guard",
            "drawdown_guard",
            "correlation_guard_enabled",
            "risk_budget_enabled",
            "dynamic_stoploss_enabled",
            "tp_limit_exit_enabled",
            "wallet_mode",
            "entry_limit_buy_enabled",
            "entry_recent_high_breakout_enabled",
            "entry_recent_high_breakout_require_bull",
            "btc_guard_enabled",
            "recovery_boost_enabled",
            "smart_alloc_enabled",
            "smart_alloc_corr_enabled",
            "smart_alloc_sector_enabled",
            # Reserved / Autopilot bools
            "reserved_apply_suggested_budget",
            "reserved_promote_to_active",
            "autopilot_enabled",
            "autopilot_auto_approve",
            "autopilot_auto_approve_pingpong",
            "autopilot_auto_approve_autoloop",
            "autopilot_auto_approve_ladder",
            "autopilot_auto_approve_lightning",
            "autopilot_auto_approve_gazua",
            "autopilot_auto_approve_contrarian",
            "autopilot_auto_approve_sniper",
            "time_zone_optimizer_enabled",
            "autopilot_idle_demote_enabled",
            "autopilot_idle_to_longhold_enabled",
            "night_mode_enabled",
            "autopilot_scope_rotation_enabled",
            "autopilot_scope_adaptive_cd",
            "autopilot_window_enabled",
            "autopilot_guard_demote_enabled",
            "autopilot_signal_miss_enabled",
            "longshort_scope_power",
            "longshort_scope_auto_fire",
            "longshort_scope_assist_fire",
            "longshort_scope_assist_fire_auto",
            "longshort_scope_slicing",
            "longshort_scope_random_active",
            "longshort_scope_auto_scan",
            "dust_vacuum_enabled",
            # [2026-03-23] 스마트 리스크 기능 ON/OFF
            "dynamic_size_mult_enabled",
            "regime_per_strategy_enabled",
            "concentration_limit_enabled",
        ):
            if k in patch:
                b = self._ui_as_bool(patch.get(k))
                if b is not None:
                    setattr(self, k, bool(b))

        # ①② 상호 배타: 둘 다 ON이면 방금 명시적으로 ON한 쪽 우선, 반대쪽 자동 OFF
        if self.dynamic_size_mult_enabled and self.regime_per_strategy_enabled:
            _r_on = self._ui_as_bool(patch.get("regime_per_strategy_enabled"))
            _d_on = self._ui_as_bool(patch.get("dynamic_size_mult_enabled"))
            if _r_on and not _d_on:
                # 유저가 ①을 명시적으로 ON → ② 자동 OFF
                self.dynamic_size_mult_enabled = False
                logger.info("[SmartRisk] ① 레짐스위칭 명시 ON → ② 동적규모 자동 OFF (곱연산 방지)")
            else:
                # 유저가 ②를 ON하거나 둘 다 ON → ① 자동 OFF
                self.regime_per_strategy_enabled = False
                logger.info("[SmartRisk] ② 동적규모 활성 → ① 레짐스위칭 자동 OFF (곱연산 방지)")

        if "btc_guard_enabled" in patch:
            b = self._ui_as_bool(patch.get("btc_guard_enabled"))
            if b is not None:
                self.btc_guard_enabled = bool(b)
                if not self.btc_guard_enabled:
                    self.btc_guard_mode = False
                    self._pre_guard_auto_approve = {}
                    try:
                        self._restore_trailing_stops()
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("Failed to restore trailing stops after BTC guard disable: %s", exc)
                    if self.recovery_boost_active:
                        self._deactivate_recovery_boost(reason="btc_guard_disabled")

        # floats
        for k in (
            "entry_ob_max_spread_bps",
            "entry_ob_depth_bps",
            "entry_ob_depth_factor",
            "entry_ob_stale_sec",
            "entry_ceiling_fee_rate",
            "entry_ceiling_slippage_guard_bps",
            "entry_ceiling_spread_guard_bps",
            "entry_ceiling_extra_bps",
            "entry_ceiling_max_age_sec",
            "entry_ceiling_decay_half_life_sec",
            "entry_ceiling_cooldown_sec",
            "entry_ceiling_force_on_bull_sec",
            "entry_recent_high_lookback_hours",
            "entry_recent_high_near_pct",
            "entry_recent_high_cooldown_sec",
            "entry_recent_high_cache_sec",
            "entry_recent_high_breakout_margin_pct",
            "entry_recent_high_breakout_min_regime_change_pct",
            "entry_recent_high_breakout_max_spread_bps",
            "entry_max_qty",
            "entry_qty_cooldown_sec",
            "exit_fee_rate",
            "exit_slippage_guard_bps",
            "exit_min_net_profit_pct",
            "exit_min_net_profit_usdt",
            "tp_limit_timeout_sec",
            "entry_limit_timeout_sec",
            "entry_limit_cooldown_sec",
            "min_order_usdt",
            "entry_global_gap_sec",
            "ai_retrain_threshold",
            "daily_loss_limit_pct",
            "circuit_breaker_loss_pct",
            "circuit_breaker_cooldown_min",
            "high_correlation_threshold",
            "smart_alloc_w_profit",
            "smart_alloc_w_ai",
            "smart_alloc_w_risk",
            "smart_alloc_w_momentum",
            "smart_alloc_w_kelly",
            "smart_alloc_w_liquidity",
            "smart_alloc_min_mult",
            "smart_alloc_max_mult",
            "smart_alloc_corr_th",
            # SNIPER surge scanner
            "sniper_min_surge_pct",
            "reserved_candidate_price_min_usdt",
            "reserved_candidate_price_max_usdt",
            # 먼지 청소
            "dust_vacuum_threshold_usdt",
            # Night Mode
            "night_mode_entry_score_boost_pct",
            "night_mode_sl_multiplier",
            # Backtest weights
            "backtest_weight_pingpong",
            "backtest_weight_autoloop",
            "backtest_weight_ladder",
            "backtest_weight_lightning",
            "backtest_weight_gazua",
            "backtest_weight_contrarian",
            "backtest_weight_sniper",
        ):
            if k in patch:
                x = self._ui_as_float(patch.get(k))
                if x is not None:
                    setattr(self, k, float(x))

        for k in ("longshort_scope_min_price", "longshort_scope_max_price"):
            if k in patch:
                try:
                    v = float(patch[k])
                    setattr(self, k, max(0.0, v))
                except (TypeError, ValueError) as exc:
                    logger.warning("Failed to apply longshort scope price setting '%s': %s", k, exc)
        if "longshort_scope_min_conf" in patch:
            try:
                v = float(patch["longshort_scope_min_conf"])
                self.longshort_scope_min_conf = max(10.0, min(100.0, v))
            except (TypeError, ValueError) as exc:
                logger.warning("Failed to apply longshort_scope_min_conf setting: %s", exc)

        if "btc_guard_down_5m_pct" in patch:
            x = self._ui_as_float(patch.get("btc_guard_down_5m_pct"))
            if x is not None:
                self.btc_guard_down_5m_pct = max(0.5, min(20.0, abs(float(x))))
        if "btc_guard_down_15m_pct" in patch:
            x = self._ui_as_float(patch.get("btc_guard_down_15m_pct"))
            if x is not None:
                self.btc_guard_down_15m_pct = max(1.0, min(40.0, abs(float(x))))
        if "btc_guard_trail_tighten_ratio" in patch:
            x = self._ui_as_float(patch.get("btc_guard_trail_tighten_ratio"))
            if x is not None:
                self.btc_guard_trail_tighten_ratio = max(0.1, min(1.0, float(x)))
        if "smart_alloc_corr_th" in patch:
            x = self._ui_as_float(patch.get("smart_alloc_corr_th"))
            if x is not None:
                self.smart_alloc_corr_th = max(0.0, min(0.99, float(x)))
        if "autopilot_scope_trap_tp_timeout_hours" in patch:
            x = self._ui_as_float(patch.get("autopilot_scope_trap_tp_timeout_hours"))
            if x is not None:
                self.autopilot_scope_trap_tp_timeout_hours = max(0.0, min(72.0, float(x)))

        # ints
        for k in (
            "tp_limit_max_retries",
            "entry_recent_high_candle_unit_min",
            "max_pending_orders_total",
            "max_same_sector",
            # Reserved / Autopilot ints
            "reserved_pingpong_n",
            "reserved_autoloop_n",
            "reserved_ladder_n",
            "reserved_lightning_n",
            "reserved_gazua_n",
            "reserved_contrarian_n",
            "reserved_sniper_n",
            "reserved_whale_n",
            "autopilot_idle_demote_min",
            "autopilot_idle_to_longhold_hours",
            "autopilot_scope_idle_min",
            "autopilot_scope_target_n",
            "autopilot_scope_cooldown_min",
            "autopilot_eval_interval_sec",
            "autopilot_grace_sec",
            "autopilot_demote_max_total",
            "autopilot_demote_max_per_strategy",
            "autopilot_guard_demote_window_min",
            "autopilot_guard_demote_n",
            "autopilot_signal_miss_window_min",
            "autopilot_signal_miss_min_attempts",
            "longshort_scope_random_interval_sec",
            "longshort_scope_top_n",
            "longshort_scope_budget_per_slot_usdt",
            "dust_vacuum_daily_count",
            # Night Mode
            "night_mode_start_hour",
            "night_mode_end_hour",
        ):
            if k in patch:
                x = self._ui_as_int(patch.get(k))
                if x is not None:
                    setattr(self, k, int(x))

        # [2026-03-23] 스마트 리스크 float 설정
        if "size_mult_hi_pct" in patch:
            x = self._ui_as_float(patch.get("size_mult_hi_pct"))
            if x is not None:
                v = min(-0.1, float(x))  # 반드시 음수
                os.environ["OMA_SIZE_MULT_HI_PCT"] = str(v)
        if "size_mult_floor" in patch:
            x = self._ui_as_float(patch.get("size_mult_floor"))
            if x is not None:
                v = max(0.1, min(0.9, float(x)))
                os.environ["OMA_SIZE_MULT_FLOOR"] = str(v)
        if "concentration_limit_pct" in patch:
            x = self._ui_as_float(patch.get("concentration_limit_pct"))
            if x is not None:
                self.concentration_limit_pct = max(5.0, min(50.0, float(x)))

        # strings
        if "reconcile_position_sync_mode" in patch:
            v = self._ui_as_str(patch.get("reconcile_position_sync_mode"))
            if v:
                vv = v.strip().upper()
                if vv in ("OFF", "ACTIVE", "ALL"):
                    self.reconcile_position_sync_mode = vv

        if "entry_ceiling_apply" in patch:
            s = self._ui_as_str(patch.get("entry_ceiling_apply"))
            if s:
                s2 = s.strip().upper()
                if s2 in ("BEAR", "NON_BULL", "ALWAYS"):
                    self.entry_ceiling_apply = s2

        if "entry_recent_high_apply" in patch:
            s = self._ui_as_str(patch.get("entry_recent_high_apply"))
            if s:
                s2 = s.strip().upper()
                if s2 in ("BEAR", "NON_BULL", "ALWAYS"):
                    self.entry_recent_high_apply = s2

        if "entry_ceiling_decay_mode" in patch:
            s = self._ui_as_str(patch.get("entry_ceiling_decay_mode"))
            if s is not None:
                s2 = str(s).strip().upper()
                if s2 in ("OFF", "FALSE", "0"):
                    s2 = "NONE"
                if s2 in ("NONE", "LINEAR", "EXP"):
                    self.entry_ceiling_decay_mode = s2

        # entry_limit_price_mode
        if "entry_limit_price_mode" in patch:
            s = self._ui_as_str(patch.get("entry_limit_price_mode"))
            if s:
                s2 = s.strip().lower()
                if s2 in ("best_bid", "best_ask"):
                    self.entry_limit_price_mode = s2

        # Autopilot strings
        for k in ("autopilot_window_start", "autopilot_window_end"):
            if k in patch:
                s = self._ui_as_str(patch.get(k))
                if s is not None:
                    setattr(self, k, str(s))

        # Scope deploy mode
        if "autopilot_scope_deploy_mode" in patch:
            s = self._ui_as_str(patch.get("autopilot_scope_deploy_mode"))
            if s and s.strip().lower() in ("wait", "market", "trap"):
                self.autopilot_scope_deploy_mode = s.strip().lower()

        # SNIPER scan settings
        if "sniper_scan_timeframe" in patch:
            s = self._ui_as_str(patch.get("sniper_scan_timeframe"))
            if s and s.strip() in ("5m", "15m", "1h", "4h", "24h"):
                self.sniper_scan_timeframe = s.strip()

        if "sniper_scan_mode" in patch:
            s = self._ui_as_str(patch.get("sniper_scan_mode"))
            if s and s.strip() in ("absolute", "relative", "both"):
                self.sniper_scan_mode = s.strip()

    def _ui_reserved_settings_snapshot(self) -> Dict[str, Any]:
        """Return current Reserved-Queue settings (dashboard overrides)."""
        return {
            "pingpong_n": max(0, int(getattr(self, "reserved_pingpong_n", 0) or 0)),
            "autoloop_n": max(0, int(getattr(self, "reserved_autoloop_n", 0) or 0)),
            "ladder_n": max(0, int(getattr(self, "reserved_ladder_n", 0) or 0)),
            "lightning_n": max(0, int(getattr(self, "reserved_lightning_n", 0) or 0)),
            "gazua_n": max(0, int(getattr(self, "reserved_gazua_n", 0) or 0)),
            "contrarian_n": max(0, int(getattr(self, "reserved_contrarian_n", 0) or 0)),
            "sniper_n": max(0, int(getattr(self, "reserved_sniper_n", 0) or 0)),
            "snipers_n": max(0, int(getattr(self, "autopilot_scope_target_n", getattr(self, "reserved_sniper_n", 0)) or 0)),
            "whale_n": max(0, int(getattr(self, "reserved_whale_n", 0) or 0)),
            # [2026-05-30] Per-strategy ON/OFF toggle persistence (재시작 후 유지)
            "pingpong_enabled": bool(getattr(self, "reserved_pingpong_enabled", True)),
            "autoloop_enabled": bool(getattr(self, "reserved_autoloop_enabled", True)),
            "ladder_enabled": bool(getattr(self, "reserved_ladder_enabled", True)),
            "lightning_enabled": bool(getattr(self, "reserved_lightning_enabled", True)),
            "gazua_enabled": bool(getattr(self, "reserved_gazua_enabled", True)),
            "contrarian_enabled": bool(getattr(self, "reserved_contrarian_enabled", True)),
            "sniper_enabled": bool(getattr(self, "reserved_sniper_enabled", True)),
            "whale_enabled": bool(getattr(self, "reserved_whale_enabled", True)),
            # [2026-05-30] Per-strategy explicit budget persistence
            "pingpong_budget_usdt": float(getattr(self, "reserved_pingpong_budget_usdt", 0.0) or 0.0),
            "autoloop_budget_usdt": float(getattr(self, "reserved_autoloop_budget_usdt", 0.0) or 0.0),
            "ladder_budget_usdt": float(getattr(self, "reserved_ladder_budget_usdt", 0.0) or 0.0),
            "lightning_budget_usdt": float(getattr(self, "reserved_lightning_budget_usdt", 0.0) or 0.0),
            "gazua_budget_usdt": float(getattr(self, "reserved_gazua_budget_usdt", 0.0) or 0.0),
            "contrarian_budget_usdt": float(getattr(self, "reserved_contrarian_budget_usdt", 0.0) or 0.0),
            "sniper_budget_usdt": float(getattr(self, "reserved_sniper_budget_usdt", 0.0) or 0.0),
            "whale_budget_usdt": float(getattr(self, "reserved_whale_budget_usdt", 0.0) or 0.0),
            "candidate_price_min_usdt": float(getattr(self, "reserved_candidate_price_min_usdt", 0.0) or 0.0),
            "candidate_price_max_usdt": float(getattr(self, "reserved_candidate_price_max_usdt", 0.0) or 0.0),
            "apply_suggested_budget": bool(getattr(self, "reserved_apply_suggested_budget", True)),
            "promote_to_active": bool(getattr(self, "reserved_promote_to_active", False)),
        }

    def _ui_autopilot_settings_snapshot(self) -> Dict[str, Any]:
        """Return current Autopilot settings (Reserved/OMA maintenance)."""
        return {
            # [2026-02-06] BTC Guard Mode UI toggle (enabled/disabled)
            "btc_guard_mode": bool(getattr(self, "btc_guard_enabled", False)),
            "btc_guard_active": bool(getattr(self, "btc_guard_mode", False)),
            "recovery_boost_active": bool(getattr(self, "recovery_boost_active", False)),
            "recovery_boost_remaining_sec": max(0.0, float(getattr(self, "recovery_boost_duration_sec", 0)) - (time.time() - float(getattr(self, "recovery_boost_activated_ts", 0) or 0))) if getattr(self, "recovery_boost_active", False) else 0.0,
            "enabled": bool(getattr(self, "autopilot_enabled", False)),
            "auto_approve": bool(getattr(self, "autopilot_auto_approve", False)),
            "idle_demote_enabled": bool(getattr(self, "autopilot_idle_demote_enabled", False)),
            "idle_demote_min": max(0, int(getattr(self, "autopilot_idle_demote_min", 0) or 0)),
            "idle_to_longhold_enabled": bool(getattr(self, "autopilot_idle_to_longhold_enabled", True)),
            "idle_to_longhold_hours": int(getattr(self, "autopilot_idle_to_longhold_hours", 24) or 24),
            "eval_interval_sec": max(0, int(getattr(self, "autopilot_eval_interval_sec", 0) or 0)),
            "grace_sec": max(0, int(getattr(self, "autopilot_grace_sec", 0) or 0)),
            "demote_max_total": max(0, int(getattr(self, "autopilot_demote_max_total", 0) or 0)),
            "demote_max_per_strategy": max(0, int(getattr(self, "autopilot_demote_max_per_strategy", 0) or 0)),
            "window_enabled": bool(getattr(self, "autopilot_window_enabled", False)),
            "window_start": str(getattr(self, "autopilot_window_start", "22:00") or "22:00"),
            "window_end": str(getattr(self, "autopilot_window_end", "08:00") or "08:00"),
            "guard_demote_enabled": bool(getattr(self, "autopilot_guard_demote_enabled", False)),
            "guard_demote_window_min": max(0, int(getattr(self, "autopilot_guard_demote_window_min", 0) or 0)),
            "guard_demote_n": max(0, int(getattr(self, "autopilot_guard_demote_n", 0) or 0)),
            "signal_miss_enabled": bool(getattr(self, "autopilot_signal_miss_enabled", False)),
            "signal_miss_window_min": max(0, int(getattr(self, "autopilot_signal_miss_window_min", 0) or 0)),
            "signal_miss_min_attempts": max(0, int(getattr(self, "autopilot_signal_miss_min_attempts", 0) or 0)),
            "scope_target_n": max(0, int(getattr(self, "autopilot_scope_target_n", getattr(self, "reserved_sniper_n", 0)) or 0)),
            "scope_instant_buy_min_conf": float(getattr(self, "autopilot_scope_instant_buy_min_conf", 55.0) or 55.0),
            "perf_demote_enabled": bool(getattr(self, "autopilot_perf_demote_enabled", False)),
            "perf_window_min": max(0, int(getattr(self, "autopilot_perf_window_min", 0) or 0)),
            "perf_min_trades": max(0, int(getattr(self, "autopilot_perf_min_trades", 0) or 0)),
            "perf_min_sells": max(0, int(getattr(self, "autopilot_perf_min_sells", 0) or 0)),
            "perf_min_net_cash_usdt": float(getattr(self, "autopilot_perf_min_net_cash_usdt", 0.0) or 0.0),
            "perf_min_net_cash_per_trade_usdt": float(getattr(self, "autopilot_perf_min_net_cash_per_trade", 0.0) or 0.0),
            "cooldown_min": max(0, int(getattr(self, "autopilot_cooldown_min", 0) or 0)),
            # 전략별 AutoApprove — BTC Guard 중에도 원본값 저장
            "auto_approve_pingpong": self._true_auto_approve("pingpong", True),
            "auto_approve_autoloop": self._true_auto_approve("autoloop", True),
            "auto_approve_ladder": self._true_auto_approve("ladder", False),
            "auto_approve_lightning": self._true_auto_approve("lightning", False),
            "auto_approve_gazua": self._true_auto_approve("gazua", False),
            "auto_approve_contrarian": self._true_auto_approve("contrarian", False),
            "auto_approve_sniper": self._true_auto_approve("sniper", False),
            "auto_approve_whale": self._true_auto_approve("whale", False),
            # 전략별 최소 신뢰도 %
            "min_confidence_pingpong": float(getattr(self, "autopilot_min_confidence_pingpong", 60.0) or 60.0),
            "min_confidence_autoloop": float(getattr(self, "autopilot_min_confidence_autoloop", 60.0) or 60.0),
            "min_confidence_ladder": float(getattr(self, "autopilot_min_confidence_ladder", 60.0) or 60.0),
            "min_confidence_lightning": float(getattr(self, "autopilot_min_confidence_lightning", 55.0) or 55.0),
            "min_confidence_gazua": float(getattr(self, "autopilot_min_confidence_gazua", 55.0) or 55.0),
            "min_confidence_contrarian": float(getattr(self, "autopilot_min_confidence_contrarian", 55.0) or 55.0),
            "min_confidence_sniper": float(getattr(self, "autopilot_min_confidence_sniper", 65.0) or 65.0),
            "min_confidence_whale": float(getattr(self, "autopilot_min_confidence_whale", 65.0) or 65.0),
            # [2026-02-02] Auto Engine Start on Boot
            "auto_engine_start": bool(getattr(self, "auto_engine_start", False)),
            # SNIPER DCA
            "sniper_dca_step_pct": float(getattr(self, "sniper_dca_step_pct", 0.2) or 0.2),
            "sniper_dca_add_ratio": float(getattr(self, "sniper_dca_add_ratio", 0.5) or 0.5),
            "sniper_dca_max_depth_pct": float(getattr(self, "sniper_dca_max_depth_pct", 1.0) or 1.0),
            # [2026-02-04] Global Profit Take: 모든 ACTIVE 코인 강제 매도
            "global_profit_take": bool(getattr(self, "global_profit_take", False)),
            "global_profit_pct": float(getattr(self, "global_profit_pct", 5.0) or 5.0),
            "global_profit_interval_min": float(getattr(self, "global_profit_interval_min", 10.0) or 10.0),
            "global_min_sl_pct": float(getattr(self, "global_min_sl_pct", -2.5) or -2.5),
            # [2026-03-23] 수익 자동 락인 (④)
            "profit_lock_enabled": bool(getattr(self, "profit_lock_enabled", False)),
            "profit_lock_trigger_pct": float(getattr(self, "profit_lock_trigger_pct", 10.0)),
            "profit_lock_sell_ratio": float(getattr(self, "profit_lock_sell_ratio", 0.3)),
            "profit_lock_cooldown_h": float(getattr(self, "profit_lock_cooldown_sec", 3600.0)) / 3600.0,
            # [2026-03-24] Peak Drawdown Guard
            "peak_drawdown_guard_enabled": bool(getattr(self, "peak_drawdown_guard_enabled", False)),
            "peak_drawdown_activation_pct": float(getattr(self, "peak_drawdown_activation_pct", 80.0)),
            "peak_drawdown_trigger_pct": float(getattr(self, "peak_drawdown_trigger_pct", 50.0)),
            "peak_drawdown_min_profit_pct": float(getattr(self, "peak_drawdown_min_profit_pct", 0.3)),
        }

    def _ui_apply_reserved_settings(self, patch: Dict[str, Any]) -> None:
        """Apply reserved settings loaded from ui_settings.json (best-effort)."""
        if not isinstance(patch, dict):
            return
        try:
            if "auto_slot_enabled" in patch:
                self.auto_slot_enabled = bool(patch.get("auto_slot_enabled"))
            if "pingpong_n" in patch:
                self.reserved_pingpong_n = max(0, min(20, int(patch.get("pingpong_n") or 0)))
            if "autoloop_n" in patch:
                self.reserved_autoloop_n = max(0, min(20, int(patch.get("autoloop_n") or 0)))
            if "ladder_n" in patch:
                self.reserved_ladder_n = max(0, min(20, int(patch.get("ladder_n") or 0)))
            if "lightning_n" in patch:
                self.reserved_lightning_n = max(0, min(20, int(patch.get("lightning_n") or 0)))
            if "gazua_n" in patch:
                self.reserved_gazua_n = max(0, min(20, int(patch.get("gazua_n") or 0)))
            if "contrarian_n" in patch:
                self.reserved_contrarian_n = max(0, min(20, int(patch.get("contrarian_n") or 0)))
            if "sniper_n" in patch:
                self.reserved_sniper_n = max(0, min(20, int(patch.get("sniper_n") or 0)))
            if "whale_n" in patch:
                self.reserved_whale_n = max(0, min(20, int(patch.get("whale_n") or 0)))
            if "snipers_n" in patch:
                self.autopilot_scope_target_n = max(0, min(20, int(patch.get("snipers_n") or 0)))
            # [2026-05-30] Per-strategy ON/OFF toggle
            if "pingpong_enabled" in patch:
                self.reserved_pingpong_enabled = bool(patch.get("pingpong_enabled"))
            if "autoloop_enabled" in patch:
                self.reserved_autoloop_enabled = bool(patch.get("autoloop_enabled"))
            if "ladder_enabled" in patch:
                self.reserved_ladder_enabled = bool(patch.get("ladder_enabled"))
            if "lightning_enabled" in patch:
                self.reserved_lightning_enabled = bool(patch.get("lightning_enabled"))
            if "gazua_enabled" in patch:
                self.reserved_gazua_enabled = bool(patch.get("gazua_enabled"))
            if "contrarian_enabled" in patch:
                self.reserved_contrarian_enabled = bool(patch.get("contrarian_enabled"))
            if "sniper_enabled" in patch:
                self.reserved_sniper_enabled = bool(patch.get("sniper_enabled"))
            if "whale_enabled" in patch:
                self.reserved_whale_enabled = bool(patch.get("whale_enabled"))
            # [2026-05-30] Per-strategy explicit budget
            if "pingpong_budget_usdt" in patch:
                self.reserved_pingpong_budget_usdt = max(0.0, float(patch.get("pingpong_budget_usdt") or 0.0))
            if "autoloop_budget_usdt" in patch:
                self.reserved_autoloop_budget_usdt = max(0.0, float(patch.get("autoloop_budget_usdt") or 0.0))
            if "ladder_budget_usdt" in patch:
                self.reserved_ladder_budget_usdt = max(0.0, float(patch.get("ladder_budget_usdt") or 0.0))
            if "lightning_budget_usdt" in patch:
                self.reserved_lightning_budget_usdt = max(0.0, float(patch.get("lightning_budget_usdt") or 0.0))
            if "gazua_budget_usdt" in patch:
                self.reserved_gazua_budget_usdt = max(0.0, float(patch.get("gazua_budget_usdt") or 0.0))
            if "contrarian_budget_usdt" in patch:
                self.reserved_contrarian_budget_usdt = max(0.0, float(patch.get("contrarian_budget_usdt") or 0.0))
            if "sniper_budget_usdt" in patch:
                self.reserved_sniper_budget_usdt = max(0.0, float(patch.get("sniper_budget_usdt") or 0.0))
            if "whale_budget_usdt" in patch:
                self.reserved_whale_budget_usdt = max(0.0, float(patch.get("whale_budget_usdt") or 0.0))
            if "candidate_price_min_usdt" in patch:
                self.reserved_candidate_price_min_usdt = max(0.0, float(patch.get("candidate_price_min_usdt") or 0.0))
            if "candidate_price_max_usdt" in patch:
                self.reserved_candidate_price_max_usdt = max(0.0, float(patch.get("candidate_price_max_usdt") or 0.0))
            if "apply_suggested_budget" in patch:
                self.reserved_apply_suggested_budget = bool(patch.get("apply_suggested_budget"))
            if "promote_to_active" in patch:
                self.reserved_promote_to_active = bool(patch.get("promote_to_active"))
        except (AttributeError, OverflowError, TypeError, ValueError) as exc:
            logger.warning("Failed to apply reserved slot settings: %s", exc)

    def _ui_apply_autopilot_settings(self, patch: Dict[str, Any]) -> None:
        """Apply autopilot settings loaded from ui_settings.json (best-effort)."""
        # [2026-02-06] BTC Guard Mode UI 연동
        if not isinstance(patch, dict):
            return
        try:
            if "btc_guard_mode" in patch:
                guard_enabled = bool(patch.get("btc_guard_mode"))
                self.btc_guard_enabled = guard_enabled
                if not guard_enabled:
                    # Explicit OFF must also clear runtime guard state.
                    self.btc_guard_mode = False
                    self._pre_guard_auto_approve = {}
                    try:
                        self._restore_trailing_stops()
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("Failed to restore trailing stops on BTC guard explicit OFF: %s", exc)
            if "enabled" in patch:
                self.autopilot_enabled = bool(patch.get("enabled"))
            if "auto_approve" in patch:
                self.autopilot_auto_approve = bool(patch.get("auto_approve"))

            # [2026-02-02] Auto Engine Start on Boot
            if "auto_engine_start" in patch:
                self.auto_engine_start = bool(patch.get("auto_engine_start"))

            if "idle_demote_enabled" in patch:
                self.autopilot_idle_demote_enabled = bool(patch.get("idle_demote_enabled"))
            if "idle_demote_min" in patch:
                self.autopilot_idle_demote_min = max(0, int(patch.get("idle_demote_min") or 0))
            if "idle_to_longhold_enabled" in patch:
                self.autopilot_idle_to_longhold_enabled = bool(patch.get("idle_to_longhold_enabled"))
            if "idle_to_longhold_hours" in patch:
                self.autopilot_idle_to_longhold_hours = max(1, int(patch.get("idle_to_longhold_hours") or 24))

            if "idle_demote_overrides" in patch:
                ov = patch.get("idle_demote_overrides")
                if isinstance(ov, dict):
                    clean = {}
                    for k, v in ov.items():
                        try:
                            clean[str(k).upper()] = max(0, int(v))
                        except (TypeError, ValueError) as exc:
                            logger.warning("Failed to parse idle demote override for '%s': %s", k, exc)
                    self.autopilot_idle_demote_overrides = clean

            if "eval_interval_sec" in patch:
                self.autopilot_eval_interval_sec = max(5, int(patch.get("eval_interval_sec") or 0))
            if "grace_sec" in patch:
                self.autopilot_grace_sec = max(0, int(patch.get("grace_sec") or 0))
            if "demote_max_total" in patch:
                self.autopilot_demote_max_total = max(0, int(patch.get("demote_max_total") or 0))
            if "demote_max_per_strategy" in patch:
                self.autopilot_demote_max_per_strategy = max(0, int(patch.get("demote_max_per_strategy") or 0))

            if "window_enabled" in patch:
                self.autopilot_window_enabled = bool(patch.get("window_enabled"))
            if "window_start" in patch:
                ws = str(patch.get("window_start") or "").strip()
                if ws:
                    self.autopilot_window_start = ws
            if "window_end" in patch:
                we = str(patch.get("window_end") or "").strip()
                if we:
                    self.autopilot_window_end = we

            if "guard_demote_enabled" in patch:
                self.autopilot_guard_demote_enabled = bool(patch.get("guard_demote_enabled"))
            if "guard_demote_window_min" in patch:
                self.autopilot_guard_demote_window_min = max(0, int(patch.get("guard_demote_window_min") or 0))
            if "guard_demote_n" in patch:
                self.autopilot_guard_demote_n = max(0, int(patch.get("guard_demote_n") or 0))

            if "signal_miss_enabled" in patch:
                self.autopilot_signal_miss_enabled = bool(patch.get("signal_miss_enabled"))
            if "signal_miss_window_min" in patch:
                self.autopilot_signal_miss_window_min = max(0, int(patch.get("signal_miss_window_min") or 0))
            if "signal_miss_min_attempts" in patch:
                self.autopilot_signal_miss_min_attempts = max(0, int(patch.get("signal_miss_min_attempts") or 0))
            if "scope_target_n" in patch:
                self.autopilot_scope_target_n = max(0, int(patch.get("scope_target_n") or 0))

            # [2026-03-08] 즉시매수 최소 신뢰도 (운영자 설정 가능)
            if "scope_instant_buy_min_conf" in patch:
                try:
                    self.autopilot_scope_instant_buy_min_conf = max(
                        30.0, min(95.0, float(patch.get("scope_instant_buy_min_conf") or 55.0))
                    )
                except (TypeError, ValueError) as exc:
                    logger.warning("Failed to apply scope_instant_buy_min_conf setting: %s", exc)

            # perf/churn demotion
            if "perf_demote_enabled" in patch:
                self.autopilot_perf_demote_enabled = bool(patch.get("perf_demote_enabled"))
            if "perf_window_min" in patch:
                self.autopilot_perf_window_min = max(0, int(patch.get("perf_window_min") or 0))
            if "perf_min_trades" in patch:
                self.autopilot_perf_min_trades = max(0, int(patch.get("perf_min_trades") or 0))
            if "perf_min_sells" in patch:
                self.autopilot_perf_min_sells = max(0, int(patch.get("perf_min_sells") or 0))
            if "perf_min_net_cash_usdt" in patch:
                try:
                    self.autopilot_perf_min_net_cash_usdt = float(patch.get("perf_min_net_cash_usdt") or 0.0)
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.warning("Failed to apply perf_min_net_cash_usdt setting: %s", exc)
            if "perf_min_net_cash_per_trade_usdt" in patch:
                try:
                    self.autopilot_perf_min_net_cash_per_trade = float(patch.get("perf_min_net_cash_per_trade_usdt") or 0.0)
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.warning("Failed to apply perf_min_net_cash_per_trade_usdt setting: %s", exc)

            # cooldown
            if "cooldown_min" in patch:
                self.autopilot_cooldown_min = max(0, int(patch.get("cooldown_min") or 0))

            # 전략별 AutoApprove
            if "auto_approve_pingpong" in patch:
                self.autopilot_auto_approve_pingpong = bool(patch.get("auto_approve_pingpong"))
            if "auto_approve_autoloop" in patch:
                self.autopilot_auto_approve_autoloop = bool(patch.get("auto_approve_autoloop"))
            if "auto_approve_ladder" in patch:
                self.autopilot_auto_approve_ladder = bool(patch.get("auto_approve_ladder"))
            if "auto_approve_lightning" in patch:
                self.autopilot_auto_approve_lightning = bool(patch.get("auto_approve_lightning"))
            if "auto_approve_gazua" in patch:
                self.autopilot_auto_approve_gazua = bool(patch.get("auto_approve_gazua"))
            if "auto_approve_contrarian" in patch:
                self.autopilot_auto_approve_contrarian = bool(patch.get("auto_approve_contrarian"))
            if "auto_approve_sniper" in patch:
                self.autopilot_auto_approve_sniper = bool(patch.get("auto_approve_sniper"))
            # [FIX 2026-03-23] WHALE auto_approve 복원 누락
            if "auto_approve_whale" in patch:
                self.autopilot_auto_approve_whale = bool(patch.get("auto_approve_whale"))

            # 전략별 최소 신뢰도 %
            # [FIX 2026-03-23] "whale" 추가 — 재시작 시 confidence 65%가 기본값(60%)으로 리셋되던 버그
            for _sk in ("pingpong", "autoloop", "ladder", "lightning", "gazua", "contrarian", "sniper", "whale"):
                _conf_key = f"min_confidence_{_sk}"
                if _conf_key in patch:
                    try:
                        setattr(self, f"autopilot_min_confidence_{_sk}", max(0.0, min(100.0, float(patch.get(_conf_key) or 50.0))))
                    except (TypeError, ValueError) as exc:
                        logger.warning("Failed to apply min_confidence_%s setting: %s", _sk, exc)

            # SNIPER DCA
            if "sniper_dca_step_pct" in patch:
                try:
                    self.sniper_dca_step_pct = max(0.1, min(5.0, float(patch.get("sniper_dca_step_pct") or 0.2)))
                    os.environ["SNIPER_DCA_STEP_PCT"] = str(self.sniper_dca_step_pct)
                except (TypeError, ValueError) as exc:
                    logger.warning("Failed to apply sniper_dca_step_pct setting: %s", exc)
            if "sniper_dca_add_ratio" in patch:
                try:
                    self.sniper_dca_add_ratio = max(0.1, min(2.0, float(patch.get("sniper_dca_add_ratio") or 0.5)))
                    os.environ["SNIPER_DCA_ADD_RATIO"] = str(self.sniper_dca_add_ratio)
                except (TypeError, ValueError) as exc:
                    logger.warning("Failed to apply sniper_dca_add_ratio setting: %s", exc)
            if "sniper_dca_max_depth_pct" in patch:
                try:
                    self.sniper_dca_max_depth_pct = max(0.2, min(10.0, float(patch.get("sniper_dca_max_depth_pct") or 1.0)))
                    os.environ["SNIPER_DCA_MAX_DEPTH_PCT"] = str(self.sniper_dca_max_depth_pct)
                except (TypeError, ValueError) as exc:
                    logger.warning("Failed to apply sniper_dca_max_depth_pct setting: %s", exc)

            # [2026-02-04] Global Profit Take
            if "global_profit_take" in patch:
                self.global_profit_take = bool(patch.get("global_profit_take"))
            if "global_profit_pct" in patch:
                try:
                    self.global_profit_pct = max(1.0, min(100.0, float(patch.get("global_profit_pct") or 5.0)))
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.warning("Failed to apply global_profit_pct setting: %s", exc)
            if "global_profit_interval_min" in patch:
                try:
                    self.global_profit_interval_min = max(1.0, min(60.0, float(patch.get("global_profit_interval_min") or 10.0)))
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.warning("Failed to apply global_profit_interval_min setting: %s", exc)
            if "global_min_sl_pct" in patch:
                try:
                    raw = float(patch.get("global_min_sl_pct") or -2.5)
                    if raw > 0:
                        raw = -abs(raw)
                    self.global_min_sl_pct = max(-95.0, min(-0.1, raw))
                    os.environ["OMA_GLOBAL_MIN_SL_PCT"] = str(self.global_min_sl_pct)
                except (OSError, OverflowError, TypeError, ValueError) as exc:
                    logger.warning("Failed to apply global_min_sl_pct setting: %s", exc)

            # [2026-03-23] 수익 자동 락인 (④)
            if "profit_lock_enabled" in patch:
                self.profit_lock_enabled = bool(patch.get("profit_lock_enabled"))
            if "profit_lock_trigger_pct" in patch:
                try:
                    self.profit_lock_trigger_pct = max(1.0, min(100.0, float(patch.get("profit_lock_trigger_pct") or 10.0)))
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.warning("[UI_SETTINGS] profit_lock_trigger_pct: %s", exc, exc_info=True)
            if "profit_lock_sell_ratio" in patch:
                try:
                    self.profit_lock_sell_ratio = max(0.05, min(0.95, float(patch.get("profit_lock_sell_ratio") or 0.3)))
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.warning("[UI_SETTINGS] profit_lock_sell_ratio: %s", exc, exc_info=True)
            if "profit_lock_cooldown_h" in patch:
                try:
                    h = max(0.016, float(patch.get("profit_lock_cooldown_h") or 1.0))  # 최소 1분
                    self.profit_lock_cooldown_sec = h * 3600.0
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.warning("[UI_SETTINGS] profit_lock_cooldown_h: %s", exc, exc_info=True)

            # [2026-03-24] Peak Drawdown Guard
            if "peak_drawdown_guard_enabled" in patch:
                self.peak_drawdown_guard_enabled = bool(patch.get("peak_drawdown_guard_enabled"))
            if "peak_drawdown_activation_pct" in patch:
                try:
                    self.peak_drawdown_activation_pct = max(10.0, min(100.0, float(patch.get("peak_drawdown_activation_pct") or 80.0)))
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.warning("[UI_SETTINGS] peak_drawdown_activation_pct: %s", exc, exc_info=True)
            if "peak_drawdown_trigger_pct" in patch:
                try:
                    self.peak_drawdown_trigger_pct = max(10.0, min(90.0, float(patch.get("peak_drawdown_trigger_pct") or 50.0)))
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.warning("[UI_SETTINGS] peak_drawdown_trigger_pct: %s", exc, exc_info=True)
            if "peak_drawdown_min_profit_pct" in patch:
                try:
                    self.peak_drawdown_min_profit_pct = max(0.1, float(patch.get("peak_drawdown_min_profit_pct") or 0.3))
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.warning("[UI_SETTINGS] peak_drawdown_min_profit_pct: %s", exc, exc_info=True)
        except (AttributeError, OSError, OverflowError, TypeError, ValueError) as exc:
            logger.warning("[UI_SETTINGS] global profit take settings: %s", exc, exc_info=True)
