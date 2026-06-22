# ============================================================
# File: app/api/reserved_router.py
# Autocoin OS v3-H — Reserved Queue API (+ Autopilot)
# ------------------------------------------------------------
# - Bybit public 데이터 기반 후보 자동 선별
# - Reserved Queue 조회/승인/거절
# - (옵션) 야간 자동 운용(Autopilot):
#     - 거래/체결이 너무 뜸한 ACTIVE 코인 자동 WATCH 강등
#     - Bybit 재스캔 → 후보 자동 승인(WATCH/ACTIVE) 반복
#
# 원칙:
# - 이 라우터는 주문/체결/실거래 트리거를 절대 수행하지 않는다.
# - 승인(Approve)은 OMA Registry의 상태/예산/전략 모드만 조정한다.
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request

from app.manager.oma_market_registry import MarketState
from app.manager.reserved_queue import reserved_queue
from app.manager.reserved_selector import build_reserved_candidates, fetch_candles_minutes, _calc_rsi_macd_from_candles
from app.manager.market_controls import apply_engine_controls
import requests

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/reserved",
    tags=["reserved"],
)


def _b(x: Any, default: Optional[bool] = None) -> Optional[bool]:
    if x is None:
        return default
    s = str(x).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _i(x: Any, default: Optional[int] = None) -> Optional[int]:
    if x is None:
        return default
    try:
        return int(float(x))
    except (TypeError, ValueError):
        logger.warning("reserved_router._i L58 except", exc_info=True)
        return default


def _f(x: Any, default: Optional[float] = None) -> Optional[float]:
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        logger.warning("reserved_router._f L67 except", exc_info=True)
        return default


_TPSL_STRATEGIES = (
    "PINGPONG",
    "AUTOLOOP",
    "LADDER",
    "LIGHTNING",
    "GAZUA",
    "CONTRARIAN",
    "SNIPER",
)
_TPSL_POLICY_PATH = os.path.join("runtime", "strategy_tp_sl_policy.json")


def _default_strategy_tp_sl_settings() -> Dict[str, Any]:
    per_strategy = {s: {"tp_pct": 1.2, "sl_pct": 2.5} for s in _TPSL_STRATEGIES}
    return {
        "enabled": True,
        "tp_floor_pct": 1.2,
        "sl_floor_pct": 2.5,
        "time_relax_enabled": True,
        "time_relax_step_hours": 12.0,
        "time_relax_steps": 5,
        "time_relax_tp_step": 0.1,
        "time_relax_sl_step": 0.5,
        "time_relax_min_tp_pct": 0.8,
        "time_relax_min_sl_pct": 0.5,
        "per_strategy": per_strategy,
    }


def _normalize_strategy_tp_sl_settings(raw: Any) -> Dict[str, Any]:
    base = _default_strategy_tp_sl_settings()
    if not isinstance(raw, dict):
        return base

    def _to_float(v: Any, dv: float) -> float:
        if v is None:
            return float(dv)
        try:
            return float(v)
        except (TypeError, ValueError):
            logger.warning("reserved_router._to_float failed: %r, default %s", v, dv, exc_info=True)
            return float(dv)

    def _to_int(v: Any, dv: int) -> int:
        if v is None:
            return int(dv)
        try:
            return int(float(v))
        except (TypeError, ValueError):
            logger.warning("reserved_router._to_int failed: %r, default %s", v, dv, exc_info=True)
            return int(dv)

    tp_floor = max(0.1, _to_float(raw.get("tp_floor_pct"), base["tp_floor_pct"]))
    sl_floor = max(0.1, abs(_to_float(raw.get("sl_floor_pct"), base["sl_floor_pct"])))

    out: Dict[str, Any] = {
        "enabled": bool(raw.get("enabled", base["enabled"])),
        "tp_floor_pct": round(tp_floor, 4),
        "sl_floor_pct": round(sl_floor, 4),
        "time_relax_enabled": bool(raw.get("time_relax_enabled", base["time_relax_enabled"])),
        "time_relax_step_hours": round(max(0.25, _to_float(raw.get("time_relax_step_hours"), base["time_relax_step_hours"])), 4),
        "time_relax_steps": max(1, min(24, _to_int(raw.get("time_relax_steps"), base["time_relax_steps"]))),
        "time_relax_tp_step": round(max(0.0, _to_float(raw.get("time_relax_tp_step"), base["time_relax_tp_step"])), 4),
        "time_relax_sl_step": round(max(0.0, _to_float(raw.get("time_relax_sl_step"), base["time_relax_sl_step"])), 4),
        "time_relax_min_tp_pct": round(max(0.1, _to_float(raw.get("time_relax_min_tp_pct"), base["time_relax_min_tp_pct"])), 4),
        "time_relax_min_sl_pct": round(max(0.1, abs(_to_float(raw.get("time_relax_min_sl_pct"), base["time_relax_min_sl_pct"]))), 4),
    }

    per_in = raw.get("per_strategy") if isinstance(raw.get("per_strategy"), dict) else {}
    per_out: Dict[str, Dict[str, float]] = {}
    for strategy in _TPSL_STRATEGIES:
        cfg_raw = per_in.get(strategy)
        if cfg_raw is None:
            cfg_raw = per_in.get(strategy.lower())
        cfg = cfg_raw if isinstance(cfg_raw, dict) else {}
        tp_val = _to_float(cfg.get("tp_pct"), tp_floor)
        sl_val = abs(_to_float(cfg.get("sl_pct"), sl_floor))
        per_out[strategy] = {
            "tp_pct": round(max(tp_floor, tp_val), 4),
            "sl_pct": round(max(sl_floor, sl_val), 4),
        }
    out["per_strategy"] = per_out

    # ★ 1-2: 레짐 배율 + 수수료 키 패스스루 (엔진이 사용하지만 UI 정규화 대상 아님)
    for passthrough_key in ("regime_tp_multiplier", "regime_sl_multiplier", "fee_aware_tp", "fee_rate"):
        if passthrough_key in raw:
            out[passthrough_key] = raw[passthrough_key]

    return out


def _load_strategy_tp_sl_settings() -> Dict[str, Any]:
    default = _default_strategy_tp_sl_settings()
    path = _TPSL_POLICY_PATH
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logger.warning("reserved_router._load_strategy_tp_sl_settings L158 except", exc_info=True)
        return default
    return _normalize_strategy_tp_sl_settings(data)


def _save_strategy_tp_sl_settings(policy: Dict[str, Any]) -> None:
    from app.core.io_utils import safe_write_json
    path = _TPSL_POLICY_PATH
    if not path:
        return
    try:
        safe_write_json(path, policy)
    except OSError as exc:
        logger.warning("Failed to save strategy TP/SL settings: %s", exc)


def _get_strategy_tp_sl_settings(system: Any) -> Dict[str, Any]:
    raw = getattr(system, "strategy_tp_sl", None)
    if isinstance(raw, dict):
        norm = _normalize_strategy_tp_sl_settings(raw)
    else:
        norm = _load_strategy_tp_sl_settings()
    setattr(system, "strategy_tp_sl", norm)
    return norm


def _clamp_ctx_params_for_strategy(params: Dict[str, Any], mode: str, tp_floor: float, sl_floor: float) -> bool:
    changed = False
    tp_keys = ["tp", "tp_pct"]
    sl_keys = ["sl", "sl_pct"]

    if mode == "PINGPONG":
        tp_keys += ["pp_tp_pct", "pp_exit_gap_pct"]
        sl_keys += ["pp_sl_pct"]

    for key in tp_keys:
        if key not in params:
            continue
        try:
            val = float(params.get(key))
        except (TypeError, ValueError):
            logger.warning("reserved_router._clamp_ctx_params_for_strategy L212 except", exc_info=True)
            continue
        if val < tp_floor:
            params[key] = round(tp_floor, 4)
            changed = True

    for key in sl_keys:
        if key not in params:
            continue
        try:
            raw_val = float(params.get(key))
        except (TypeError, ValueError):
            logger.warning("reserved_router._clamp_ctx_params_for_strategy L223 except", exc_info=True)
            continue
        if abs(raw_val) >= sl_floor:
            continue
        sign = 1.0 if raw_val > 0 else -1.0
        if raw_val == 0:
            sign = -1.0
        params[key] = round(sign * sl_floor, 4)
        changed = True

    return changed


def _apply_strategy_tp_sl_to_contexts(system: Any, policy: Dict[str, Any]) -> None:
    try:
        coordinator = getattr(system, "coordinator", None)
        contexts = getattr(coordinator, "contexts", {}) if coordinator else {}
        if not isinstance(contexts, dict):
            return

        per = policy.get("per_strategy") if isinstance(policy.get("per_strategy"), dict) else {}
        base_tp_floor = float(policy.get("tp_floor_pct", 1.2) or 1.2)
        base_sl_floor = abs(float(policy.get("sl_floor_pct", 2.5) or 2.5))

        for ctx in contexts.values():
            if ctx is None:
                continue

            mode = ""
            ctrls = getattr(ctx, "controls", None) or {}
            if isinstance(ctrls, dict):
                st = ctrls.get("strategy", {})
                if isinstance(st, dict):
                    mode = str(st.get("mode") or st.get("name") or "").strip().upper()
            if not mode:
                continue

            s_cfg = per.get(mode, {}) if isinstance(per, dict) else {}
            tp_floor = max(base_tp_floor, float(s_cfg.get("tp_pct", base_tp_floor) or base_tp_floor))
            sl_floor = max(base_sl_floor, abs(float(s_cfg.get("sl_pct", base_sl_floor) or base_sl_floor)))

            # 1) Engine policy params
            pol = getattr(ctx, "policy", None)
            if not isinstance(pol, dict):
                pol = {"name": "nunnaya", "params": {}}
            pparams = pol.get("params")
            if not isinstance(pparams, dict):
                pparams = {}

            policy_changed = False
            try:
                cur_tp = float(pparams.get("tp", 0.0))
            except (TypeError, ValueError):
                logger.warning("reserved_router._apply_strategy_tp_sl_to_contexts L275 except", exc_info=True)
                cur_tp = 0.0
            if cur_tp < tp_floor:
                pparams["tp"] = round(tp_floor, 4)
                policy_changed = True

            try:
                cur_sl = float(pparams.get("sl", 0.0))
            except (TypeError, ValueError):
                logger.warning("reserved_router._apply_strategy_tp_sl_to_contexts L283 except", exc_info=True)
                cur_sl = 0.0
            if abs(cur_sl) < sl_floor:
                pparams["sl"] = round(-sl_floor, 4)
                policy_changed = True

            if policy_changed:
                pol["params"] = pparams
                if hasattr(ctx, "update_policy"):
                    ctx.update_policy(pol)
                else:
                    ctx.policy = pol

            # 2) Strategy params (plugin paths)
            if isinstance(ctrls, dict):
                st = ctrls.get("strategy", {})
                if isinstance(st, dict):
                    params = st.get("params", {})
                    if isinstance(params, dict):
                        if _clamp_ctx_params_for_strategy(params, mode, tp_floor, sl_floor):
                            st["params"] = params
                            ctrls["strategy"] = st
                            try:
                                setattr(ctx, "controls", ctrls)
                            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                                logger.warning("Failed to set strategy params on context controls", exc_info=True)
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("Failed to apply strategy params to plugin paths", exc_info=True)


def _get_longhold_target_pct(system: Any) -> float:
    """LongHold 목표 수익률 로드"""
    try:
        ladder_mgr = getattr(system, "ladder_manager", None)
        if ladder_mgr:
            store = ladder_mgr._load_longhold_store()
            return float(store.get("defaults", {}).get("target_profit_pct", 5.0))
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("Failed to load LongHold target profit pct", exc_info=True)
    return 5.0


def _get_longhold_check_interval(system: Any) -> float:
    """LongHold 체크 주기 로드"""
    try:
        ladder_mgr = getattr(system, "ladder_manager", None)
        if ladder_mgr:
            store = ladder_mgr._load_longhold_store()
            return float(store.get("defaults", {}).get("auto_sell_check_interval_min", 10.0))
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("Failed to load LongHold check interval", exc_info=True)
    return 10.0


def _get_longhold_stop_loss_pct(system: Any) -> float:
    """LongHold 손절 기준 로드"""
    try:
        ladder_mgr = getattr(system, "ladder_manager", None)
        if ladder_mgr:
            store = ladder_mgr._load_longhold_store()
            return float(store.get("defaults", {}).get("stop_loss_pct", -30.0))
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("Failed to load LongHold stop loss pct", exc_info=True)
    return -30.0


def _settings_snapshot(system: Any) -> Dict[str, Any]:
    """Build a settings snapshot from HyperSystem attributes.

    We avoid importing HyperSystem here to prevent circular imports.
    """

    def _get(name: str, default: Any) -> Any:
        return getattr(system, name, default)

    snap = {
        "auto_slot_enabled": bool(_get("auto_slot_enabled", False)),
        "equity_usdt": float(_get("_last_equity_usdt", 0.0) or 0.0),
        "pingpong_n": int(_get("reserved_pingpong_n", 3) or 0),
        "autoloop_n": int(_get("reserved_autoloop_n", 3) or 0),
        "ladder_n": int(_get("reserved_ladder_n", 0) or 0),
        "lightning_n": int(_get("reserved_lightning_n", 0) or 0),
        "gazua_n": int(_get("reserved_gazua_n", 0) or 0),
        "contrarian_n": int(_get("reserved_contrarian_n", 0) or 0),
        "sniper_n": int(_get("reserved_sniper_n", 0) or 0),
        "snipers_n": int(_get("autopilot_scope_target_n", _get("reserved_sniper_n", 0)) or 0),
        "whale_n": int(_get("reserved_whale_n", 0) or 0),
        # [2026-05-30] Per-strategy ON/OFF toggle
        "pingpong_enabled": bool(_get("reserved_pingpong_enabled", True)),
        "autoloop_enabled": bool(_get("reserved_autoloop_enabled", True)),
        "ladder_enabled": bool(_get("reserved_ladder_enabled", True)),
        "lightning_enabled": bool(_get("reserved_lightning_enabled", True)),
        "gazua_enabled": bool(_get("reserved_gazua_enabled", True)),
        "contrarian_enabled": bool(_get("reserved_contrarian_enabled", True)),
        "sniper_enabled": bool(_get("reserved_sniper_enabled", True)),
        "whale_enabled": bool(_get("reserved_whale_enabled", True)),
        # [2026-05-30] Per-strategy explicit budget (0=auto, >0=manual)
        "pingpong_budget_usdt": float(_get("reserved_pingpong_budget_usdt", 0.0) or 0.0),
        "autoloop_budget_usdt": float(_get("reserved_autoloop_budget_usdt", 0.0) or 0.0),
        "ladder_budget_usdt": float(_get("reserved_ladder_budget_usdt", 0.0) or 0.0),
        "lightning_budget_usdt": float(_get("reserved_lightning_budget_usdt", 0.0) or 0.0),
        "gazua_budget_usdt": float(_get("reserved_gazua_budget_usdt", 0.0) or 0.0),
        "contrarian_budget_usdt": float(_get("reserved_contrarian_budget_usdt", 0.0) or 0.0),
        "sniper_budget_usdt": float(_get("reserved_sniper_budget_usdt", 0.0) or 0.0),
        "whale_budget_usdt": float(_get("reserved_whale_budget_usdt", 0.0) or 0.0),
        "candidate_price_min_usdt": float(_get("reserved_candidate_price_min_usdt", 0.0) or 0.0),
        "candidate_price_max_usdt": float(_get("reserved_candidate_price_max_usdt", 0.0) or 0.0),
        "apply_suggested_budget": bool(_get("reserved_apply_suggested_budget", True)),
        "promote_to_active": bool(_get("reserved_promote_to_active", False)),
        "autopilot": {
            # [2026-02-06] BTC Guard Mode (UI toggle = enabled/disabled)
            "btc_guard_mode": bool(_get("btc_guard_enabled", True)),
            "enabled": bool(_get("autopilot_enabled", False)),
            "auto_approve": bool(_get("autopilot_auto_approve", False)),
            "idle_demote_enabled": bool(_get("autopilot_idle_demote_enabled", True)),
            "idle_demote_min": int(_get("autopilot_idle_demote_min", 180) or 0),
            "idle_demote_overrides": dict(_get("autopilot_idle_demote_overrides", {}) or {}),
            
            # [2026-02-01] 24시간 무거래 → LongHold 자동 전환
            "idle_to_longhold_enabled": bool(_get("autopilot_idle_to_longhold_enabled", True)),
            "idle_to_longhold_hours": int(_get("autopilot_idle_to_longhold_hours", 24) or 24),
            
            "eval_interval_sec": int(_get("autopilot_eval_interval_sec", 300) or 0),
            "grace_sec": int(_get("autopilot_grace_sec", 900) or 0),
            "demote_max_total": int(_get("autopilot_demote_max_total", 2) or 0),
            "demote_max_per_strategy": int(_get("autopilot_demote_max_per_strategy", 1) or 0),

            # time window (local time)
            "window_enabled": bool(_get("autopilot_window_enabled", False)),
            "window_start": str(_get("autopilot_window_start", "22:00") or "22:00"),
            "window_end": str(_get("autopilot_window_end", "08:00") or "08:00"),

            # demotion rules
            "guard_demote_enabled": bool(_get("autopilot_guard_demote_enabled", False)),
            "guard_demote_window_min": int(_get("autopilot_guard_demote_window_min", 30) or 0),
            "guard_demote_n": int(_get("autopilot_guard_demote_n", 12) or 0),

            "signal_miss_enabled": bool(_get("autopilot_signal_miss_enabled", False)),
            "signal_miss_window_min": int(_get("autopilot_signal_miss_window_min", 30) or 0),
            "signal_miss_min_attempts": int(_get("autopilot_signal_miss_min_attempts", 6) or 0),

            # 전략별 AutoApprove
            "auto_approve_pingpong": bool(_get("autopilot_auto_approve_pingpong", False)),
            "auto_approve_autoloop": bool(_get("autopilot_auto_approve_autoloop", False)),
            "auto_approve_ladder": bool(_get("autopilot_auto_approve_ladder", False)),
            "auto_approve_lightning": bool(_get("autopilot_auto_approve_lightning", False)),
            "auto_approve_gazua": bool(_get("autopilot_auto_approve_gazua", False)),
            "auto_approve_contrarian": bool(_get("autopilot_auto_approve_contrarian", False)),
            "auto_approve_sniper": bool(_get("autopilot_auto_approve_sniper", False)),
            "auto_approve_whale": bool(_get("autopilot_auto_approve_whale", False)),

            # 전략별 최소 신뢰도 %
            "auto_approve_min_confidence_pingpong": float(_get("autopilot_min_confidence_pingpong", 60.0)),
            "auto_approve_min_confidence_autoloop": float(_get("autopilot_min_confidence_autoloop", 60.0)),
            "auto_approve_min_confidence_ladder": float(_get("autopilot_min_confidence_ladder", 60.0)),
            "auto_approve_min_confidence_lightning": float(_get("autopilot_min_confidence_lightning", 55.0)),
            "auto_approve_min_confidence_gazua": float(_get("autopilot_min_confidence_gazua", 55.0)),
            "auto_approve_min_confidence_contrarian": float(_get("autopilot_min_confidence_contrarian", 55.0)),
            "auto_approve_min_confidence_sniper": float(_get("autopilot_min_confidence_sniper", 65.0)),
            "auto_approve_min_confidence_whale": float(_get("autopilot_min_confidence_whale", 65.0)),
            "auto_engine_start": bool(_get("auto_engine_start", False)),

            # [2026-02-04] LongHold 목표 달성 시 자동 매도
            "longhold_auto_sell": bool(_get("longhold_auto_sell", True)),
            "longhold_target_pct": float(_get_longhold_target_pct(system)),
            "longhold_check_interval_min": float(_get_longhold_check_interval(system)),
            "longhold_stop_loss_pct": float(_get_longhold_stop_loss_pct(system)),
            
            # [2026-02-04] Global Profit Take: 모든 ACTIVE 코인 강제 매도
            "global_profit_take": bool(_get("global_profit_take", False)),
            "global_profit_pct": float(_get("global_profit_pct", 5.0)),
            "global_profit_interval_min": float(_get("global_profit_interval_min", 10.0)),
            "global_min_sl_pct": float(_get("global_min_sl_pct", -2.5)),
            # [2026-03-23] 수익 자동 락인 (④)
            "profit_lock_enabled": bool(_get("profit_lock_enabled", False)),
            "profit_lock_trigger_pct": float(_get("profit_lock_trigger_pct", 10.0)),
            "profit_lock_sell_ratio": float(_get("profit_lock_sell_ratio", 0.3)),
            "profit_lock_cooldown_h": float(_get("profit_lock_cooldown_sec", 3600.0)) / 3600.0,
        },
        # [2026-02-04] 백테스트 가중치 (0.0~1.0)
        "backtest_weights": {
            "pingpong": float(_get("backtest_weight_pingpong", 0.10)),
            "autoloop": float(_get("backtest_weight_autoloop", 0.15)),
            "ladder": float(_get("backtest_weight_ladder", 0.30)),
            "lightning": float(_get("backtest_weight_lightning", 0.15)),
            "gazua": float(_get("backtest_weight_gazua", 0.35)),
            "contrarian": float(_get("backtest_weight_contrarian", 0.20)),
            "sniper": float(_get("backtest_weight_sniper", 0.30)),
        },
        # [2026-03-02] SNIPER DCA 설정
        "sniper_dca": {
            # [2026-05-30] SNIPER DCA 보수화 (AUTOLOOP 4️⃣ 패턴 일관): add 0.5→0.4, depth 1.0→2.0
            "dca_step_pct": float(_get("sniper_dca_step_pct", 0.2)),
            "dca_add_ratio": float(_get("sniper_dca_add_ratio", 0.4)),
            "dca_max_depth_pct": float(_get("sniper_dca_max_depth_pct", 2.0)),
        },
        "strategy_tp_sl": _get_strategy_tp_sl_settings(system),
        "stats": {
            "last_run_ts": _get("autopilot_last_run_ts", None),
            "last_result": _get("autopilot_last_result", None),
        },
    }

    # clamp
    snap["pingpong_n"] = max(0, min(20, int(snap["pingpong_n"])))
    snap["autoloop_n"] = max(0, min(20, int(snap["autoloop_n"])))
    snap["ladder_n"] = max(0, min(20, int(snap["ladder_n"])))
    snap["lightning_n"] = max(0, min(20, int(snap["lightning_n"])))
    snap["gazua_n"] = max(0, min(20, int(snap["gazua_n"])))
    snap["contrarian_n"] = max(0, min(20, int(snap["contrarian_n"])))
    snap["sniper_n"] = max(0, min(10, int(snap["sniper_n"])))
    snap["snipers_n"] = max(0, min(20, int(snap["snipers_n"])))
    snap["whale_n"] = max(0, min(20, int(snap.get("whale_n") or 0)))
    snap["candidate_price_min_usdt"] = max(0.0, float(snap.get("candidate_price_min_usdt") or 0.0))
    snap["candidate_price_max_usdt"] = max(0.0, float(snap.get("candidate_price_max_usdt") or 0.0))
    if snap["candidate_price_min_usdt"] > 0 and snap["candidate_price_max_usdt"] > 0 and snap["candidate_price_max_usdt"] < snap["candidate_price_min_usdt"]:
        snap["candidate_price_min_usdt"], snap["candidate_price_max_usdt"] = snap["candidate_price_max_usdt"], snap["candidate_price_min_usdt"]
    snap["autopilot"]["idle_demote_min"] = max(0, int(snap["autopilot"]["idle_demote_min"]))
    snap["autopilot"]["eval_interval_sec"] = max(5, int(snap["autopilot"]["eval_interval_sec"]))
    snap["autopilot"]["grace_sec"] = max(0, int(snap["autopilot"]["grace_sec"]))
    snap["autopilot"]["demote_max_total"] = max(0, min(50, int(snap["autopilot"].get("demote_max_total") or 0)))
    snap["autopilot"]["demote_max_per_strategy"] = max(0, min(50, int(snap["autopilot"].get("demote_max_per_strategy") or 0)))

    # extra clamps
    snap["autopilot"]["guard_demote_window_min"] = max(0, int(snap["autopilot"].get("guard_demote_window_min") or 0))
    snap["autopilot"]["guard_demote_n"] = max(0, int(snap["autopilot"].get("guard_demote_n") or 0))
    snap["autopilot"]["signal_miss_window_min"] = max(0, int(snap["autopilot"].get("signal_miss_window_min") or 0))
    snap["autopilot"]["signal_miss_min_attempts"] = max(0, int(snap["autopilot"].get("signal_miss_min_attempts") or 0))

    return snap


def _apply_settings(system: Any, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Apply settings patch to HyperSystem attributes and persist."""

    if not isinstance(patch, dict):
        return _settings_snapshot(system)

    # Auto slot toggle
    ase = _b(patch.get("auto_slot_enabled"), None)
    if ase is not None:
        setattr(system, "auto_slot_enabled", bool(ase))
        if bool(ase):
            from app.manager.auto_slot_allocator import compute_auto_slots
            equity = float(getattr(system, "_last_equity_usdt", 0.0) or 0.0)
            auto_slots = compute_auto_slots(equity)
            _slot_map = {
                "pingpong_n": "reserved_pingpong_n",
                "autoloop_n": "reserved_autoloop_n",
                "ladder_n": "reserved_ladder_n",
                "lightning_n": "reserved_lightning_n",
                "gazua_n": "reserved_gazua_n",
                "contrarian_n": "reserved_contrarian_n",
                "sniper_n": "reserved_sniper_n",
                "whale_n": "reserved_whale_n",
            }
            for key, attr in _slot_map.items():
                setattr(system, attr, int(auto_slots.get(key, 0)))
            setattr(system, "_auto_slot_last_equity", equity)

    # top-level (skip manual slot assignment when auto mode is active)
    _auto_on = bool(getattr(system, "auto_slot_enabled", False))
    pp = _i(patch.get("pingpong_n"), None)
    al = _i(patch.get("autoloop_n"), None)
    ld = _i(patch.get("ladder_n"), None)
    lt = _i(patch.get("lightning_n"), None)
    gz = _i(patch.get("gazua_n"), None)
    ct = _i(patch.get("contrarian_n"), None)
    sn = _i(patch.get("sniper_n"), None)
    sns = _i(patch.get("snipers_n"), None)
    wh_n = _i(patch.get("whale_n"), None)
    cmin = _f(patch.get("candidate_price_min_usdt"), None)
    cmax = _f(patch.get("candidate_price_max_usdt"), None)
    apb = _b(patch.get("apply_suggested_budget"), None)
    p2a = _b(patch.get("promote_to_active"), None)

    if not _auto_on:
        if pp is not None:
            setattr(system, "reserved_pingpong_n", max(0, min(20, int(pp))))
        if al is not None:
            setattr(system, "reserved_autoloop_n", max(0, min(20, int(al))))
        if ld is not None:
            setattr(system, "reserved_ladder_n", max(0, min(20, int(ld))))
        if lt is not None:
            setattr(system, "reserved_lightning_n", max(0, min(20, int(lt))))
        if gz is not None:
            setattr(system, "reserved_gazua_n", max(0, min(20, int(gz))))
        if ct is not None:
            setattr(system, "reserved_contrarian_n", max(0, min(20, int(ct))))
        if sn is not None:
            setattr(system, "reserved_sniper_n", max(0, min(10, int(sn))))
        if wh_n is not None:
            setattr(system, "reserved_whale_n", max(0, min(20, int(wh_n))))

    # [2026-05-30] Per-strategy ON/OFF toggle (auto_slot 모드와 무관 적용 — 부모님이 언제든 끄고 켤 수 있게)
    _pp_en = _b(patch.get("pingpong_enabled"), None)
    _al_en = _b(patch.get("autoloop_enabled"), None)
    _ld_en = _b(patch.get("ladder_enabled"), None)
    _lt_en = _b(patch.get("lightning_enabled"), None)
    _gz_en = _b(patch.get("gazua_enabled"), None)
    _ct_en = _b(patch.get("contrarian_enabled"), None)
    _sn_en = _b(patch.get("sniper_enabled"), None)
    _wh_en = _b(patch.get("whale_enabled"), None)
    if _pp_en is not None:
        setattr(system, "reserved_pingpong_enabled", bool(_pp_en))
    if _al_en is not None:
        setattr(system, "reserved_autoloop_enabled", bool(_al_en))
    if _ld_en is not None:
        setattr(system, "reserved_ladder_enabled", bool(_ld_en))
    if _lt_en is not None:
        setattr(system, "reserved_lightning_enabled", bool(_lt_en))
    if _gz_en is not None:
        setattr(system, "reserved_gazua_enabled", bool(_gz_en))
    if _ct_en is not None:
        setattr(system, "reserved_contrarian_enabled", bool(_ct_en))
    if _sn_en is not None:
        setattr(system, "reserved_sniper_enabled", bool(_sn_en))
    if _wh_en is not None:
        setattr(system, "reserved_whale_enabled", bool(_wh_en))

    # [2026-05-30] Per-strategy explicit budget (0=auto, >0=manual pool)
    _pp_b = _f(patch.get("pingpong_budget_usdt"), None)
    _al_b = _f(patch.get("autoloop_budget_usdt"), None)
    _ld_b = _f(patch.get("ladder_budget_usdt"), None)
    _lt_b = _f(patch.get("lightning_budget_usdt"), None)
    _gz_b = _f(patch.get("gazua_budget_usdt"), None)
    _ct_b = _f(patch.get("contrarian_budget_usdt"), None)
    _sn_b = _f(patch.get("sniper_budget_usdt"), None)
    _wh_b = _f(patch.get("whale_budget_usdt"), None)
    if _pp_b is not None:
        setattr(system, "reserved_pingpong_budget_usdt", max(0.0, float(_pp_b)))
    if _al_b is not None:
        setattr(system, "reserved_autoloop_budget_usdt", max(0.0, float(_al_b)))
    if _ld_b is not None:
        setattr(system, "reserved_ladder_budget_usdt", max(0.0, float(_ld_b)))
    if _lt_b is not None:
        setattr(system, "reserved_lightning_budget_usdt", max(0.0, float(_lt_b)))
    if _gz_b is not None:
        setattr(system, "reserved_gazua_budget_usdt", max(0.0, float(_gz_b)))
    if _ct_b is not None:
        setattr(system, "reserved_contrarian_budget_usdt", max(0.0, float(_ct_b)))
    if _sn_b is not None:
        setattr(system, "reserved_sniper_budget_usdt", max(0.0, float(_sn_b)))
    if _wh_b is not None:
        setattr(system, "reserved_whale_budget_usdt", max(0.0, float(_wh_b)))

    if sns is not None:
        setattr(system, "autopilot_scope_target_n", max(0, min(20, int(sns))))
    if cmin is not None or cmax is not None:
        cur_min = float(getattr(system, "reserved_candidate_price_min_usdt", 0.0) or 0.0)
        cur_max = float(getattr(system, "reserved_candidate_price_max_usdt", 0.0) or 0.0)
        if cmin is not None:
            cur_min = max(0.0, float(cmin))
        if cmax is not None:
            cur_max = max(0.0, float(cmax))
        if cur_min > 0 and cur_max > 0 and cur_max < cur_min:
            cur_min, cur_max = cur_max, cur_min
        setattr(system, "reserved_candidate_price_min_usdt", float(cur_min))
        setattr(system, "reserved_candidate_price_max_usdt", float(cur_max))
    if apb is not None:
        setattr(system, "reserved_apply_suggested_budget", bool(apb))
    if p2a is not None:
        setattr(system, "reserved_promote_to_active", bool(p2a))

    ap = patch.get("autopilot") if isinstance(patch.get("autopilot"), dict) else patch
    # [2026-02-06] BTC Guard Mode (toggle enable/disable)
    btc_guard_mode = _b(ap.get("btc_guard_mode"), None)
    if btc_guard_mode is not None:
        enabled = bool(btc_guard_mode)
        setattr(system, "btc_guard_enabled", enabled)
        if not enabled:
            # When disabling guard, make sure runtime block state is released too.
            try:
                if bool(getattr(system, "btc_guard_mode", False)):
                    pre = getattr(system, "_pre_guard_auto_approve", {}) or {}
                    if isinstance(pre, dict):
                        try:
                            system.autopilot_auto_approve_pingpong = bool(pre.get("pingpong", system.autopilot_auto_approve_pingpong))
                            system.autopilot_auto_approve_autoloop = bool(pre.get("autoloop", system.autopilot_auto_approve_autoloop))
                            system.autopilot_auto_approve_ladder = bool(pre.get("ladder", system.autopilot_auto_approve_ladder))
                            system.autopilot_auto_approve_lightning = bool(pre.get("lightning", system.autopilot_auto_approve_lightning))
                            system.autopilot_auto_approve_gazua = bool(pre.get("gazua", system.autopilot_auto_approve_gazua))
                            system.autopilot_auto_approve_sniper = bool(pre.get("sniper", system.autopilot_auto_approve_sniper))
                            system.autopilot_auto_approve_whale = bool(pre.get("whale", getattr(system, "autopilot_auto_approve_whale", False)))
                        except (KeyError, AttributeError, TypeError) as exc:
                            logger.warning("Failed to restore pre-guard auto-approve settings", exc_info=True)
                setattr(system, "btc_guard_mode", False)
                setattr(system, "_pre_guard_auto_approve", {})
                if hasattr(system, "_restore_trailing_stops"):
                    try:
                        system._restore_trailing_stops()
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("Failed to restore trailing stops after guard disable", exc_info=True)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("Failed to disable BTC guard mode", exc_info=True)

    en = _b(ap.get("enabled"), None)
    aa = _b(ap.get("auto_approve"), None)
    ide = _b(ap.get("idle_demote_enabled"), None)
    idm = _i(ap.get("idle_demote_min"), None)
    evs = _i(ap.get("eval_interval_sec"), None)
    grc = _i(ap.get("grace_sec"), None)
    dmt = _i(ap.get("demote_max_total"), None)
    dms = _i(ap.get("demote_max_per_strategy"), None)

    # time window
    wen = _b(ap.get("window_enabled"), None)
    wst = ap.get("window_start")
    wed = ap.get("window_end")

    # demotion rules
    gden = _b(ap.get("guard_demote_enabled"), None)
    gdw = _i(ap.get("guard_demote_window_min"), None)
    gdn = _i(ap.get("guard_demote_n"), None)

    smen = _b(ap.get("signal_miss_enabled"), None)
    smw = _i(ap.get("signal_miss_window_min"), None)
    sma = _i(ap.get("signal_miss_min_attempts"), None)

    if en is not None:
        setattr(system, "autopilot_enabled", bool(en))
    if aa is not None:
        setattr(system, "autopilot_auto_approve", bool(aa))
    if ide is not None:
        setattr(system, "autopilot_idle_demote_enabled", bool(ide))
    if idm is not None:
        setattr(system, "autopilot_idle_demote_min", max(0, int(idm)))
    
    # [2026-02-01] 24시간 무거래 → LongHold 자동 전환
    itl_en = _b(ap.get("idle_to_longhold_enabled"), None)
    itl_hrs = _i(ap.get("idle_to_longhold_hours"), None)
    if itl_en is not None:
        setattr(system, "autopilot_idle_to_longhold_enabled", bool(itl_en))
    if itl_hrs is not None:
        setattr(system, "autopilot_idle_to_longhold_hours", max(1, int(itl_hrs)))
    
    ido = ap.get("idle_demote_overrides")
    if ido is not None and isinstance(ido, dict):
        clean = {}
        for k, v in ido.items():
            try:
                clean[str(k).upper()] = max(0, int(v))
            except (TypeError, ValueError) as exc:
                logger.warning("Failed to parse idle demote override value", exc_info=True)
        setattr(system, "autopilot_idle_demote_overrides", clean)

    if evs is not None:
        setattr(system, "autopilot_eval_interval_sec", max(5, int(evs)))
    if grc is not None:
        setattr(system, "autopilot_grace_sec", max(0, int(grc)))
    if dmt is not None:
        setattr(system, "autopilot_demote_max_total", max(0, min(50, int(dmt))))
    if dms is not None:
        setattr(system, "autopilot_demote_max_per_strategy", max(0, min(50, int(dms))))


    # time window
    if wen is not None:
        setattr(system, "autopilot_window_enabled", bool(wen))
    if wst is not None:
        try:
            s = str(wst).strip()
            if s:
                setattr(system, "autopilot_window_start", s)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("Failed to set autopilot window start time", exc_info=True)
    if wed is not None:
        try:
            s = str(wed).strip()
            if s:
                setattr(system, "autopilot_window_end", s)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("Failed to set autopilot window end time", exc_info=True)

    # demotion rules
    if gden is not None:
        setattr(system, "autopilot_guard_demote_enabled", bool(gden))
    if gdw is not None:
        setattr(system, "autopilot_guard_demote_window_min", max(1, int(gdw)))
    if gdn is not None:
        setattr(system, "autopilot_guard_demote_n", max(1, int(gdn)))

    if smen is not None:
        setattr(system, "autopilot_signal_miss_enabled", bool(smen))
    if smw is not None:
        setattr(system, "autopilot_signal_miss_window_min", max(1, int(smw)))
    if sma is not None:
        setattr(system, "autopilot_signal_miss_min_attempts", max(1, int(sma)))

    # 전략별 AutoApprove
    aa_pp = _b(ap.get("auto_approve_pingpong"), None)
    aa_al = _b(ap.get("auto_approve_autoloop"), None)
    aa_ld = _b(ap.get("auto_approve_ladder"), None)
    aa_lt = _b(ap.get("auto_approve_lightning"), None)
    aa_gz = _b(ap.get("auto_approve_gazua"), None)
    aa_ct = _b(ap.get("auto_approve_contrarian"), None)
    aa_sn = _b(ap.get("auto_approve_sniper"), None)
    aa_wh = _b(ap.get("auto_approve_whale"), None)
    if aa_pp is not None:
        setattr(system, "autopilot_auto_approve_pingpong", bool(aa_pp))
    if aa_al is not None:
        setattr(system, "autopilot_auto_approve_autoloop", bool(aa_al))
    if aa_ld is not None:
        setattr(system, "autopilot_auto_approve_ladder", bool(aa_ld))
    if aa_lt is not None:
        setattr(system, "autopilot_auto_approve_lightning", bool(aa_lt))
    if aa_gz is not None:
        setattr(system, "autopilot_auto_approve_gazua", bool(aa_gz))
    if aa_ct is not None:
        setattr(system, "autopilot_auto_approve_contrarian", bool(aa_ct))
    if aa_sn is not None:
        setattr(system, "autopilot_auto_approve_sniper", bool(aa_sn))
    if aa_wh is not None:
        setattr(system, "autopilot_auto_approve_whale", bool(aa_wh))

    # 전략별 최소 신뢰도 %
    # [FIX 2026-03-23] "whale" 추가 — 재시작 시 confidence가 기본값으로 리셋되던 버그
    for _skey in ("pingpong", "autoloop", "ladder", "lightning", "gazua", "contrarian", "sniper", "whale"):
        _mc_val = _f(ap.get(f"auto_approve_min_confidence_{_skey}"), None)
        if _mc_val is not None:
            setattr(system, f"autopilot_min_confidence_{_skey}", max(0.0, min(100.0, float(_mc_val))))

    # [2026-02-02] Auto Engine Start on Boot
    aes = _b(ap.get("auto_engine_start"), None)
    print(f"[DEBUG] auto_engine_start: ap.get={ap.get('auto_engine_start')!r}, _b={aes!r}")
    if aes is not None:
        setattr(system, "auto_engine_start", bool(aes))
    
    # [2026-02-04] LongHold 목표 달성 시 자동 매도
    lhas = _b(ap.get("longhold_auto_sell"), None)
    lh_target = _f(ap.get("longhold_target_pct"), None)
    lh_interval = _f(ap.get("longhold_check_interval_min"), None)
    lh_sl = _f(ap.get("longhold_stop_loss_pct"), None)

    if lhas is not None or lh_target is not None or lh_interval is not None or lh_sl is not None:
        # longhold_config.json의 defaults에 저장
        try:
            ladder_mgr = getattr(system, "ladder_manager", None)
            if ladder_mgr:
                store = ladder_mgr._load_longhold_store()
                if lhas is not None:
                    store["defaults"]["auto_sell_on_target"] = bool(lhas)
                    setattr(system, "longhold_auto_sell", bool(lhas))
                if lh_target is not None and lh_target > 0:
                    store["defaults"]["target_profit_pct"] = float(lh_target)
                if lh_interval is not None and lh_interval > 0:
                    store["defaults"]["auto_sell_check_interval_min"] = float(lh_interval)
                if lh_sl is not None:
                    store["defaults"]["stop_loss_pct"] = float(lh_sl)
                ladder_mgr._save_longhold_store(store)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("Failed to save LongHold config defaults", exc_info=True)
    
    # [2026-02-04] Global Profit Take 설정
    gpt_enabled = _b(ap.get("global_profit_take"), None)
    gpt_pct = _f(ap.get("global_profit_pct"), None)
    gpt_interval = _f(ap.get("global_profit_interval_min"), None)
    gpt_min_sl = _f(ap.get("global_min_sl_pct"), None)
    
    if gpt_enabled is not None:
        setattr(system, "global_profit_take", bool(gpt_enabled))
    if gpt_pct is not None and gpt_pct > 0:
        setattr(system, "global_profit_pct", float(gpt_pct))
    if gpt_interval is not None and gpt_interval > 0:
        setattr(system, "global_profit_interval_min", float(gpt_interval))
    if gpt_min_sl is not None:
        sl_floor = float(gpt_min_sl)
        if sl_floor > 0:
            sl_floor = -abs(sl_floor)
        sl_floor = max(-95.0, min(-0.1, sl_floor))
        setattr(system, "global_min_sl_pct", float(sl_floor))
        os.environ["OMA_GLOBAL_MIN_SL_PCT"] = str(sl_floor)

        # Apply to current contexts immediately.
        try:
            coordinator = getattr(system, "coordinator", None)
            contexts = getattr(coordinator, "contexts", {}) if coordinator else {}
            if isinstance(contexts, dict):
                for ctx in contexts.values():
                    if ctx is None:
                        continue
                    if hasattr(ctx, "update_policy"):
                        pol = getattr(ctx, "policy", None)
                        if not isinstance(pol, dict):
                            pol = {"name": "nunnaya", "params": {}}
                        ctx.update_policy(pol)
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("Failed to apply global min SL to current contexts", exc_info=True)
    
    # [2026-02-04] 백테스트 가중치
    bw = patch.get("backtest_weights")
    if isinstance(bw, dict):
        for strategy_key, value in [
            ("pingpong", "backtest_weight_pingpong"),
            ("autoloop", "backtest_weight_autoloop"),
            ("ladder", "backtest_weight_ladder"),
            ("lightning", "backtest_weight_lightning"),
            ("gazua", "backtest_weight_gazua"),
            ("contrarian", "backtest_weight_contrarian"),
            ("sniper", "backtest_weight_sniper"),
        ]:
            if strategy_key in bw and bw[strategy_key] is not None:
                try:
                    weight = max(0.0, min(1.0, float(bw[strategy_key])))
                    setattr(system, value, weight)
                except (TypeError, ValueError) as exc:
                    logger.warning("Failed to set backtest weight for strategy", exc_info=True)

    # [2026-03-23] 수익 자동 락인 (④ Profit Lock)
    pl_enabled = _b(ap.get("profit_lock_enabled"), None)
    if pl_enabled is not None:
        setattr(system, "profit_lock_enabled", bool(pl_enabled))
    pl_trigger = _f(ap.get("profit_lock_trigger_pct"), None)
    if pl_trigger is not None:
        setattr(system, "profit_lock_trigger_pct", max(1.0, min(100.0, float(pl_trigger))))
    pl_ratio = _f(ap.get("profit_lock_sell_ratio"), None)
    if pl_ratio is not None:
        setattr(system, "profit_lock_sell_ratio", max(0.05, min(0.95, float(pl_ratio))))
    pl_cooldown_h = _f(ap.get("profit_lock_cooldown_h"), None)
    if pl_cooldown_h is not None:
        setattr(system, "profit_lock_cooldown_sec", max(60.0, float(pl_cooldown_h) * 3600.0))

    # [2026-03-02] SNIPER DCA 설정 (flat keys from UI form)
    dca_step = _f(patch.get("sniper_dca_step_pct"), None)
    if dca_step is not None:
        v = max(0.1, min(5.0, dca_step))
        setattr(system, "sniper_dca_step_pct", v)
        os.environ["SNIPER_DCA_STEP_PCT"] = str(v)
    dca_ratio = _f(patch.get("sniper_dca_add_ratio"), None)
    if dca_ratio is not None:
        v = max(0.1, min(2.0, dca_ratio))
        setattr(system, "sniper_dca_add_ratio", v)
        os.environ["SNIPER_DCA_ADD_RATIO"] = str(v)
    dca_depth = _f(patch.get("sniper_dca_max_depth_pct"), None)
    if dca_depth is not None:
        v = max(0.2, min(10.0, dca_depth))
        setattr(system, "sniper_dca_max_depth_pct", v)
        os.environ["SNIPER_DCA_MAX_DEPTH_PCT"] = str(v)

    # [2026-02-28] Strategy TP/SL guard policy
    policy_raw = patch.get("strategy_tp_sl")
    policy = _get_strategy_tp_sl_settings(system)
    if policy_raw is not None:
        policy = _normalize_strategy_tp_sl_settings(policy_raw)
        setattr(system, "strategy_tp_sl", policy)
        _save_strategy_tp_sl_settings(policy)

    try:
        engine = getattr(system, "engine", None)
        if engine is not None:
            setter = getattr(engine, "set_tp_sl_policy", None)
            if callable(setter):
                setter(policy)
            else:
                setattr(engine, "tp_sl_policy", policy)
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("Failed to set TP/SL guard policy on engine", exc_info=True)

    _apply_strategy_tp_sl_to_contexts(system, policy)

    # persist (best-effort)
    try:
        system.persist_ui_settings()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("Failed to persist UI settings", exc_info=True)

    return _settings_snapshot(system)


@router.get(
    "/settings",
    summary="Get reserved queue settings",
    responses={
        200: {"description": "Current reserved queue and autopilot settings"},
    },
)
def get_settings(request: Request) -> Dict[str, Any]:
    """
    Retrieve all reserved queue and autopilot configuration settings.
    """
    system = request.app.state.system
    return {"ok": True, "settings": _settings_snapshot(system)}


# ── [2026-06-01] 전략별 튜닝 오버라이드 (PINGPONG/AUTOLOOP/WHALE 등) — slot-fill 시 market_controls 가 읽어 적용 ──
_PLUGIN_PARAMS_PATH = os.path.join("runtime", "strategy_plugin_params.json")


@router.get("/plugin-params", summary="Get per-strategy tuning param overrides")
def get_plugin_params(request: Request) -> Dict[str, Any]:
    try:
        if os.path.exists(_PLUGIN_PARAMS_PATH):
            with open(_PLUGIN_PARAMS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
                return {"ok": True, "params": data if isinstance(data, dict) else {}}
    except (OSError, ValueError):
        logger.warning("get_plugin_params load failed", exc_info=True)
    return {"ok": True, "params": {}}


@router.post("/plugin-params", summary="Set per-strategy tuning param overrides (JSON via 'data' query)")
def set_plugin_params(request: Request, data: str = Query(..., description="JSON object {STRATEGY: {param: value}}")) -> Dict[str, Any]:
    try:
        incoming = json.loads(data) if data else {}
    except ValueError:
        return {"ok": False, "error": "invalid JSON"}
    if not isinstance(incoming, dict):
        return {"ok": False, "error": "data must be a JSON object"}
    cur: Dict[str, Any] = {}
    try:
        if os.path.exists(_PLUGIN_PARAMS_PATH):
            with open(_PLUGIN_PARAMS_PATH, "r", encoding="utf-8") as f:
                cur = json.load(f) or {}
    except (OSError, ValueError):
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    for k, v in incoming.items():   # top-level per-strategy merge (한 전략 저장이 다른 전략 안 지움)
        if isinstance(v, dict):
            cur[str(k).upper()] = v
    try:
        os.makedirs("runtime", exist_ok=True)
        with open(_PLUGIN_PARAMS_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    try:
        request.app.state.system._plugin_params_override = cur   # 재시작 없이 다음 slot-fill 반영
    except (AttributeError, TypeError):
        logger.warning("set_plugin_params: cache update failed", exc_info=True)
    return {"ok": True, "params": cur}


@router.post(
    "/settings",
    summary="Update reserved queue settings",
    responses={
        200: {"description": "Settings updated successfully"},
    },
)
def set_settings(
    request: Request,
    auto_slot_enabled: Optional[bool] = Query(None, description="Capital-based auto slot allocation"),
    pingpong_n: Optional[int] = Query(None, ge=0, le=20, description="Number of PINGPONG candidates"),
    autoloop_n: Optional[int] = Query(None, ge=0, le=20),
    ladder_n: Optional[int] = Query(None, ge=0, le=20, description="Number of LADDER candidates"),
    lightning_n: Optional[int] = Query(None, ge=0, le=20, description="Number of LIGHTNING candidates"),
    gazua_n: Optional[int] = Query(None, ge=0, le=20, description="Number of GAZUA candidates"),
    contrarian_n: Optional[int] = Query(None, ge=0, le=20, description="Number of CONTRARIAN candidates"),
    sniper_n: Optional[int] = Query(None, ge=0, le=10, description="Number of SNIPER candidates"),
    snipers_n: Optional[int] = Query(None, ge=0, le=20, description="Number of SNIPER(s) scope slots"),
    whale_n: Optional[int] = Query(None, ge=0, le=20, description="Number of WHALE candidates"),
    # [2026-05-30] Per-strategy ON/OFF toggle (router signature — POST 받기 위해 필수)
    pingpong_enabled: Optional[bool] = Query(None),
    autoloop_enabled: Optional[bool] = Query(None),
    ladder_enabled: Optional[bool] = Query(None),
    lightning_enabled: Optional[bool] = Query(None),
    gazua_enabled: Optional[bool] = Query(None),
    contrarian_enabled: Optional[bool] = Query(None),
    sniper_enabled: Optional[bool] = Query(None),
    whale_enabled: Optional[bool] = Query(None),
    # [2026-05-30] Per-strategy explicit budget (router signature)
    pingpong_budget_usdt: Optional[float] = Query(None, ge=0),
    autoloop_budget_usdt: Optional[float] = Query(None, ge=0),
    ladder_budget_usdt: Optional[float] = Query(None, ge=0),
    lightning_budget_usdt: Optional[float] = Query(None, ge=0),
    gazua_budget_usdt: Optional[float] = Query(None, ge=0),
    contrarian_budget_usdt: Optional[float] = Query(None, ge=0),
    sniper_budget_usdt: Optional[float] = Query(None, ge=0),
    whale_budget_usdt: Optional[float] = Query(None, ge=0),
    candidate_price_min_usdt: Optional[float] = Query(None, ge=0, description="Global candidate minimum price (USDT, 0=no limit)"),
    candidate_price_max_usdt: Optional[float] = Query(None, ge=0, description="Global candidate maximum price (USDT, 0=no limit)"),
    apply_suggested_budget: Optional[bool] = Query(None),
    promote_to_active: Optional[bool] = Query(None),

    btc_guard_mode: Optional[bool] = Query(None, description="BTC Guard Mode toggle"),

    autopilot_enabled: Optional[bool] = Query(None),

    # preferred names
    autopilot_auto_approve: Optional[bool] = Query(None),
    autopilot_idle_demote_enabled: Optional[bool] = Query(None),
    autopilot_idle_demote_min: Optional[int] = Query(None, ge=0, le=24 * 60),
    autopilot_idle_demote_overrides: Optional[str] = Query(None), # JSON string
    
    # [2026-02-01] 24시간 무거래 → LongHold 자동 전환
    autopilot_idle_to_longhold_enabled: Optional[bool] = Query(None),
    autopilot_idle_to_longhold_hours: Optional[int] = Query(None, ge=1, le=168),
    
    autopilot_eval_interval_sec: Optional[int] = Query(None, ge=5, le=24 * 60 * 60),
    autopilot_grace_sec: Optional[int] = Query(None, ge=0, le=24 * 60 * 60),
    autopilot_demote_max_total: Optional[int] = Query(None, ge=0, le=50),
    autopilot_demote_max_per_strategy: Optional[int] = Query(None, ge=0, le=50),

    # legacy / UI aliases (dashboard.js pre-fix8)
    auto_approve: Optional[bool] = Query(None),
    idle_demote_enabled: Optional[bool] = Query(None),
    idle_demote_min: Optional[int] = Query(None, ge=0, le=24 * 60),
    idle_demote_overrides: Optional[str] = Query(None),  # legacy alias
    eval_interval_sec: Optional[int] = Query(None, ge=5, le=24 * 60 * 60),
    grace_sec: Optional[int] = Query(None, ge=0, le=24 * 60 * 60),
    autopilot_demote_max_per_run: Optional[int] = Query(None, ge=0, le=50),  # legacy alias

    # time window
    autopilot_window_enabled: Optional[bool] = Query(None),
    autopilot_window_start: Optional[str] = Query(None),
    autopilot_window_end: Optional[str] = Query(None),

    # demotion rules
    autopilot_guard_demote_enabled: Optional[bool] = Query(None),
    autopilot_guard_demote_window_min: Optional[int] = Query(None, ge=0, le=24 * 60),
    autopilot_guard_demote_n: Optional[int] = Query(None, ge=0, le=999),

    autopilot_signal_miss_enabled: Optional[bool] = Query(None),
    autopilot_signal_miss_window_min: Optional[int] = Query(None, ge=0, le=24 * 60),
    autopilot_signal_miss_min_attempts: Optional[int] = Query(None, ge=0, le=999),

    # 전략별 AutoApprove
    auto_approve_pingpong: Optional[bool] = Query(None),
    auto_approve_autoloop: Optional[bool] = Query(None),
    auto_approve_ladder: Optional[bool] = Query(None),
    auto_approve_lightning: Optional[bool] = Query(None),
    auto_approve_gazua: Optional[bool] = Query(None),
    auto_approve_contrarian: Optional[bool] = Query(None),
    auto_approve_sniper: Optional[bool] = Query(None),
    auto_approve_whale: Optional[bool] = Query(None),

    # 전략별 최소 신뢰도 %
    auto_approve_min_confidence_pingpong: Optional[float] = Query(None, ge=0, le=100),
    auto_approve_min_confidence_autoloop: Optional[float] = Query(None, ge=0, le=100),
    auto_approve_min_confidence_ladder: Optional[float] = Query(None, ge=0, le=100),
    auto_approve_min_confidence_lightning: Optional[float] = Query(None, ge=0, le=100),
    auto_approve_min_confidence_gazua: Optional[float] = Query(None, ge=0, le=100),
    auto_approve_min_confidence_contrarian: Optional[float] = Query(None, ge=0, le=100),
    auto_approve_min_confidence_sniper: Optional[float] = Query(None, ge=0, le=100),
    auto_approve_min_confidence_whale: Optional[float] = Query(None, ge=0, le=100),

    # [2026-02-02] Auto Engine Start on Boot
    auto_engine_start: Optional[bool] = Query(None),
    
    # [2026-02-04] LongHold 목표 달성 시 자동 매도
    longhold_auto_sell: Optional[bool] = Query(None),
    longhold_target_pct: Optional[float] = Query(None),
    longhold_check_interval_min: Optional[float] = Query(None),
    longhold_stop_loss_pct: Optional[float] = Query(None),
    
    # [2026-02-04] Global Profit Take: 모든 ACTIVE 코인 강제 매도
    global_profit_take: Optional[bool] = Query(None),
    global_profit_pct: Optional[float] = Query(None),
    global_profit_interval_min: Optional[float] = Query(None),
    global_min_sl_pct: Optional[float] = Query(None),
    
    # [2026-02-04] 백테스트 가중치 (0.0~1.0)
    backtest_weight_pingpong: Optional[float] = Query(None, ge=0.0, le=1.0),
    backtest_weight_autoloop: Optional[float] = Query(None, ge=0.0, le=1.0),
    backtest_weight_ladder: Optional[float] = Query(None, ge=0.0, le=1.0),
    backtest_weight_lightning: Optional[float] = Query(None, ge=0.0, le=1.0),
    backtest_weight_gazua: Optional[float] = Query(None, ge=0.0, le=1.0),
    backtest_weight_contrarian: Optional[float] = Query(None, ge=0.0, le=1.0),
    backtest_weight_sniper: Optional[float] = Query(None, ge=0.0, le=1.0),
    strategy_tp_sl: Optional[str] = Query(None, description="JSON policy for per-strategy TP/SL + time relaxation"),

    # SNIPER DCA
    sniper_dca_step_pct: Optional[float] = Query(None, ge=0.1, le=5.0, description="SNIPER DCA step %"),
    sniper_dca_add_ratio: Optional[float] = Query(None, ge=0.1, le=2.0, description="SNIPER DCA add ratio"),
    sniper_dca_max_depth_pct: Optional[float] = Query(None, ge=0.2, le=10.0, description="SNIPER DCA max depth %"),
    # [2026-06-01] 수익 자동 락인 — GET snapshot(461~) 엔 있으나 POST 미노출이던 누락 보완.
    # _apply_settings(896~) 가 이미 ap 에서 읽어 적용하므로, 시그니처+patch 만 연결하면 됨 (중복 X).
    profit_lock_enabled: Optional[bool] = Query(None),
    profit_lock_trigger_pct: Optional[float] = Query(None, ge=1.0, le=100.0),
    profit_lock_sell_ratio: Optional[float] = Query(None, ge=0.05, le=0.95),
    profit_lock_cooldown_h: Optional[float] = Query(None, ge=0.0),
) -> Dict[str, Any]:
    system = request.app.state.system

    # merge aliases
    aa_val = autopilot_auto_approve if autopilot_auto_approve is not None else auto_approve
    ide_val = autopilot_idle_demote_enabled if autopilot_idle_demote_enabled is not None else idle_demote_enabled
    idm_val = autopilot_idle_demote_min if autopilot_idle_demote_min is not None else idle_demote_min
    evs_val = autopilot_eval_interval_sec if autopilot_eval_interval_sec is not None else eval_interval_sec
    grc_val = autopilot_grace_sec if autopilot_grace_sec is not None else grace_sec
    dmt_val = autopilot_demote_max_total if autopilot_demote_max_total is not None else autopilot_demote_max_per_run

    ido_dict = None
    ido_raw = autopilot_idle_demote_overrides if autopilot_idle_demote_overrides else idle_demote_overrides
    if ido_raw:
        try:
            ido_dict = json.loads(ido_raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to parse idle demote overrides JSON", exc_info=True)

    tp_sl_policy = None
    if strategy_tp_sl:
        try:
            tp_sl_policy = json.loads(strategy_tp_sl)
        except (json.JSONDecodeError, TypeError):
            logger.warning("reserved_router.set_settings L1006 except", exc_info=True)
            tp_sl_policy = None

    patch = {
        "auto_slot_enabled": auto_slot_enabled,
        "pingpong_n": pingpong_n,
        "autoloop_n": autoloop_n,
        "ladder_n": ladder_n,
        "lightning_n": lightning_n,
        "gazua_n": gazua_n,
        "contrarian_n": contrarian_n,
        "sniper_n": sniper_n,
        "snipers_n": snipers_n,
        "whale_n": whale_n,
        # [2026-05-30] Per-strategy ON/OFF toggle (patch dict — _apply_settings 에 전달)
        "pingpong_enabled": pingpong_enabled,
        "autoloop_enabled": autoloop_enabled,
        "ladder_enabled": ladder_enabled,
        "lightning_enabled": lightning_enabled,
        "gazua_enabled": gazua_enabled,
        "contrarian_enabled": contrarian_enabled,
        "sniper_enabled": sniper_enabled,
        "whale_enabled": whale_enabled,
        # [2026-05-30] Per-strategy explicit budget (patch dict)
        "pingpong_budget_usdt": pingpong_budget_usdt,
        "autoloop_budget_usdt": autoloop_budget_usdt,
        "ladder_budget_usdt": ladder_budget_usdt,
        "lightning_budget_usdt": lightning_budget_usdt,
        "gazua_budget_usdt": gazua_budget_usdt,
        "contrarian_budget_usdt": contrarian_budget_usdt,
        "sniper_budget_usdt": sniper_budget_usdt,
        "whale_budget_usdt": whale_budget_usdt,
        "candidate_price_min_usdt": candidate_price_min_usdt,
        "candidate_price_max_usdt": candidate_price_max_usdt,
        "apply_suggested_budget": apply_suggested_budget,
        "promote_to_active": promote_to_active,
        "autopilot": {
            "btc_guard_mode": btc_guard_mode,
            "enabled": autopilot_enabled,
            "auto_approve": aa_val,
            "idle_demote_enabled": ide_val,
            "idle_demote_min": idm_val,
            "idle_demote_overrides": ido_dict,
            
            # [2026-02-01] 24시간 무거래 → LongHold 자동 전환
            "idle_to_longhold_enabled": autopilot_idle_to_longhold_enabled,
            "idle_to_longhold_hours": autopilot_idle_to_longhold_hours,
            
            "eval_interval_sec": evs_val,
            "grace_sec": grc_val,
            "demote_max_total": dmt_val,
            "demote_max_per_strategy": autopilot_demote_max_per_strategy,

            "window_enabled": autopilot_window_enabled,
            "window_start": autopilot_window_start,
            "window_end": autopilot_window_end,

            "guard_demote_enabled": autopilot_guard_demote_enabled,
            "guard_demote_window_min": autopilot_guard_demote_window_min,
            "guard_demote_n": autopilot_guard_demote_n,

            "signal_miss_enabled": autopilot_signal_miss_enabled,
            "signal_miss_window_min": autopilot_signal_miss_window_min,
            "signal_miss_min_attempts": autopilot_signal_miss_min_attempts,

            "auto_approve_pingpong": auto_approve_pingpong,
            "auto_approve_autoloop": auto_approve_autoloop,
            "auto_approve_ladder": auto_approve_ladder,
            "auto_approve_lightning": auto_approve_lightning,
            "auto_approve_gazua": auto_approve_gazua,
            "auto_approve_contrarian": auto_approve_contrarian,
            "auto_approve_sniper": auto_approve_sniper,
            "auto_approve_whale": auto_approve_whale,

            # 전략별 최소 신뢰도 %
            "auto_approve_min_confidence_pingpong": auto_approve_min_confidence_pingpong,
            "auto_approve_min_confidence_autoloop": auto_approve_min_confidence_autoloop,
            "auto_approve_min_confidence_ladder": auto_approve_min_confidence_ladder,
            "auto_approve_min_confidence_lightning": auto_approve_min_confidence_lightning,
            "auto_approve_min_confidence_gazua": auto_approve_min_confidence_gazua,
            "auto_approve_min_confidence_contrarian": auto_approve_min_confidence_contrarian,
            "auto_approve_min_confidence_sniper": auto_approve_min_confidence_sniper,
            "auto_approve_min_confidence_whale": auto_approve_min_confidence_whale,

            # [2026-02-02] Auto Engine Start on Boot
            "auto_engine_start": auto_engine_start,
            
            # [2026-02-04] LongHold 목표 달성 시 자동 매도
            "longhold_auto_sell": longhold_auto_sell,
            "longhold_target_pct": longhold_target_pct,
            "longhold_check_interval_min": longhold_check_interval_min,
            "longhold_stop_loss_pct": longhold_stop_loss_pct,
            
            # [2026-02-04] Global Profit Take
            "global_profit_take": global_profit_take,
            "global_profit_pct": global_profit_pct,
            "global_profit_interval_min": global_profit_interval_min,
            "global_min_sl_pct": global_min_sl_pct,
            # [2026-06-01] 수익 자동 락인 (④) — _apply_settings:896 가 ap 에서 읽어 system attr 에 적용
            "profit_lock_enabled": profit_lock_enabled,
            "profit_lock_trigger_pct": profit_lock_trigger_pct,
            "profit_lock_sell_ratio": profit_lock_sell_ratio,
            "profit_lock_cooldown_h": profit_lock_cooldown_h,
        },
        # [2026-02-04] 백테스트 가중치
        "backtest_weights": {
            "pingpong": backtest_weight_pingpong,
            "autoloop": backtest_weight_autoloop,
            "ladder": backtest_weight_ladder,
            "lightning": backtest_weight_lightning,
            "gazua": backtest_weight_gazua,
            "contrarian": backtest_weight_contrarian,
            "sniper": backtest_weight_sniper,
        },
        "strategy_tp_sl": tp_sl_policy,
        "sniper_dca_step_pct": sniper_dca_step_pct,
        "sniper_dca_add_ratio": sniper_dca_add_ratio,
        "sniper_dca_max_depth_pct": sniper_dca_max_depth_pct,
    }

    settings = _apply_settings(system, patch)
    return {"ok": True, "settings": settings}


@router.get(
    "/list",
    summary="List reserved queue items",
    responses={
        200: {"description": "Current reserved queue snapshot"},
    },
)
def list_reserved() -> Dict[str, Any]:
    """
    Get the current reserved queue snapshot including all pending candidates.
    2026-01-30: RSI/MACD 갱신 제거 - 속도 최적화
    필요시 별도 API로 요청하거나, background job에서 갱신
    """
    snap = reserved_queue.snapshot()
    return {"ok": True, **snap}


@router.post(
    "/history/clear",
    summary="Clear reserved queue history",
    responses={
        200: {"description": "History cleared successfully"},
    },
)
def clear_reserved_history(request: Request) -> Dict[str, Any]:
    """
    Clear the reserved queue activity history.

    - Does not affect current queue items
    - Only clears the historical log
    """
    system = request.app.state.system
    try:
        reserved_queue.clear_history()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("Failed to clear reserved queue history", exc_info=True)
    return {"ok": True, "history_cleared": True, "settings": _settings_snapshot(system), **reserved_queue.snapshot()}


@router.post(
    "/clear",
    summary="Clear all reserved queue items",
    responses={
        200: {"description": "Queue cleared successfully"},
    },
)
def clear_reserved(request: Request) -> Dict[str, Any]:
    """
    Clear all items from the reserved queue.

    - Removes all pending candidates
    - Logs the clear action to history
    """
    system = request.app.state.system
    reserved_queue.clear()

    # Log (keep visibility even when AutoApprove consumes items immediately)
    try:
        reserved_queue.add_history({"kind": "CLEAR", "source": "api"})
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[RESERVED_API] Log (keep visibility even when AutoApprove consumes items immediately): %s", exc, exc_info=True)

    # Clear is an intent; do not alter settings.
    return {"ok": True, "cleared": True, "settings": _settings_snapshot(system)}


@router.post(
    "/refresh",
    summary="Refresh reserved queue with new candidates",
    responses={
        200: {"description": "Queue refreshed with new candidates"},
    },
)
def refresh_reserved(
    request: Request,
    pingpong_n: int = Query(5, ge=0, le=20, description="Number of PINGPONG candidates"),
    autoloop_n: int = Query(5, ge=0, le=20, description="Number of AUTOLOOP candidates"),
    ladder_n: int = Query(0, ge=0, le=20, description="Number of LADDER candidates"),
    lightning_n: int = Query(0, ge=0, le=20, description="Number of LIGHTNING candidates"),
    gazua_n: int = Query(0, ge=0, le=20, description="Number of GAZUA candidates"),
    contrarian_n: int = Query(0, ge=0, le=10, description="Number of CONTRARIAN candidates (bear market preferred, sideways relaxed mode supported)"),
    sniper_n: int = Query(0, ge=0, le=10, description="Number of SNIPER candidates (oversold + near low)"),
    persist_defaults: bool = Query(True, description="Save these values as defaults"),
    force_fill: bool = Query(False, description="Force fill slots ignoring conditions (RUN NOW mode)"),
) -> Dict[str, Any]:
    """
    Scan exchange markets and populate the reserved queue with candidates.

    - Scans for top candidates per strategy type
    - Replaces current queue with new candidates
    - Optionally persists the N values as defaults
    """
    system = request.app.state.system

    t0 = time.time()
    try:
        items, summary = build_reserved_candidates(system, pingpong_n=pingpong_n, autoloop_n=autoloop_n, ladder_n=ladder_n, lightning_n=lightning_n, gazua_n=gazua_n, contrarian_n=contrarian_n, sniper_n=sniper_n, force_fill=force_fill)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        import traceback
        tb = traceback.format_exc()
        logger.error("[RESERVED_REFRESH] build_reserved_candidates FAILED:\n%s", tb)
        return {"ok": False, "error": str(exc), "traceback": tb}
    summary = dict(summary or {})
    summary["elapsed_sec"] = round(time.time() - t0, 3)

    reserved_queue.replace(items, summary=summary)

    # Log a concise scan event (best-effort)
    try:
        reserved_queue.add_history({
            "kind": "SCAN",
            "source": "api",
            "picked_pingpong": int(summary.get("picked_pingpong") or 0),
            "picked_autoloop": int(summary.get("picked_autoloop") or 0),
            "picked_ladder": int(summary.get("picked_ladder") or 0),
            "picked_lightning": int(summary.get("picked_lightning") or 0),
            "picked_gazua": int(summary.get("picked_gazua") or 0),
            "picked_contrarian": int(summary.get("picked_contrarian") or 0),
            "picked_sniper": int(summary.get("picked_sniper") or 0),
            "elapsed_sec": summary.get("elapsed_sec"),
        })
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[RESERVED_API] Log a concise scan event (best-effort): %s", exc, exc_info=True)

    if persist_defaults:
        try:
            setattr(system, "reserved_pingpong_n", int(pingpong_n))
            setattr(system, "reserved_autoloop_n", int(autoloop_n))
            setattr(system, "reserved_ladder_n", int(ladder_n))
            setattr(system, "reserved_lightning_n", int(lightning_n))
            setattr(system, "reserved_gazua_n", int(gazua_n))
            setattr(system, "reserved_contrarian_n", int(contrarian_n))
            setattr(system, "reserved_sniper_n", int(sniper_n))
            system.persist_ui_settings()
        except (TypeError, ValueError) as exc:
            logger.warning("[RESERVED_API] Log a concise scan event (best-effort): %s", exc, exc_info=True)

    return {"ok": True, "items": items, "summary": summary, "settings": _settings_snapshot(system)}


@router.post(
    "/reject",
    summary="Reject a reserved candidate",
    responses={
        200: {"description": "Candidate rejected and removed from queue"},
        404: {"description": "Candidate not found"},
    },
)
def reject_reserved(
    rid: str = Query(..., min_length=8, description="Reserved item ID"),
) -> Dict[str, Any]:
    """
    Reject and remove a candidate from the reserved queue.
    """
    it = reserved_queue.pop(rid)
    if not it:
        return {"ok": False, "error": "not_found"}

    try:
        reserved_queue.add_history({
            "kind": "REJECT",
            "source": "api",
            "rid": str(rid),
            "market": it.get("market"),
            "strategy": it.get("strategy"),
        })
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[RESERVED_API] reserved_router.reject_reserved fallback: %s", exc, exc_info=True)
    return {"ok": True, "removed": it}


@router.post(
    "/approve",
    summary="Approve a reserved candidate",
    responses={
        200: {"description": "Candidate approved and promoted"},
        404: {"description": "Candidate not found"},
    },
)
def approve_reserved(
    request: Request,
    rid: str = Query(..., min_length=8, description="Reserved item ID"),
    to_state: Optional[MarketState] = Query(None, description="Target state (WATCH or ACTIVE)"),
    apply_budget: Optional[bool] = Query(None, description="Apply suggested budget"),
) -> Dict[str, Any]:
    """
    Approve a reserved candidate and promote to WATCH or ACTIVE.

    - Sets OMA state for the market
    - Applies strategy controls based on candidate type
    - Does NOT submit orders (safe operation)
    - ACTIVE promotion requires the promote_to_active setting
    """

    system = request.app.state.system

    it = reserved_queue.pop(rid)
    if not it:
        return {"ok": False, "error": "not_found"}

    market = str(it.get("market") or "").strip().upper()
    strategy = str(it.get("strategy") or "AI").strip().upper()

    promote_to_active = bool(getattr(system, "reserved_promote_to_active", False))
    apply_suggested_budget = bool(getattr(system, "reserved_apply_suggested_budget", True))

    # Determine target state
    target_state: MarketState
    if to_state is None:
        target_state = MarketState.ACTIVE if promote_to_active else MarketState.WATCH
    else:
        target_state = MarketState(to_state)

    # Hard safety: ACTIVE is allowed only when the toggle is ON
    if target_state == MarketState.ACTIVE and not promote_to_active:
        target_state = MarketState.WATCH

    # Determine budget behaviour
    use_budget = apply_suggested_budget if apply_budget is None else bool(apply_budget)

    budget_usdt = it.get("suggested_budget_usdt") or it.get("suggested_budget_usdt") if use_budget else None
    try:
        budget_f = float(budget_usdt) if budget_usdt is not None else None
    except (TypeError, ValueError):
        logger.warning("reserved_router.approve_reserved L1329 except", exc_info=True)
        budget_f = None

    # 1) OMA state
    try:
        system.oma_set_market(
            market=market,
            state=target_state,
            reason=["reserved_approve", f"strategy:{strategy}"],
            budget_usdt=budget_f,
        )
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("reserved_router.approve_reserved L1340: %s", e)
        return {"ok": False, "error": "oma_set_failed", "message": str(e), "item": it}

    # 2) Persist intended strategy mode on the market context
    # 추천 파라미터(TP/SL, step_pct 등)를 엔진 컨트롤에 적용
    recommended_params = it.get("recommended_params") or {}
    applied_params = {}
    try:
        applied_controls = apply_engine_controls(system, market, strategy, recommended_params)
        # 실제 적용된 파라미터 추출
        strat_params = applied_controls.get("strategy", {}).get("params", {})
        if strat_params:
            applied_params = {
                "tp_pct": strat_params.get("tp"),
                "sl_pct": strat_params.get("sl"),
                "step_pct": strat_params.get("step_pct"),
                "max_steps": strat_params.get("max_steps"),
                "martingale": strat_params.get("martingale"),
                "rsi_buy": strat_params.get("rsi_buy"),
                "rsi_sell": strat_params.get("rsi_sell"),
                "manual_exit": strat_params.get("manual_exit"),
            }
            # None 값 제거
            applied_params = {k: v for k, v in applied_params.items() if v is not None}
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[RESERVED_API] None 값 제거: %s", exc, exc_info=True)

    # Log approval (best-effort)
    try:
        reserved_queue.add_history({
            "kind": "APPROVE",
            "source": "api",
            "rid": str(rid),
            "market": market,
            "strategy": strategy,
            "to_state": str(target_state.value),
            "budget_usdt": budget_f,
            "apply_budget": bool(use_budget),
            "applied_params": applied_params,
        })
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning("[RESERVED_API] Log approval (best-effort): %s", exc, exc_info=True)

    return {
        "ok": True,
        "approved": {
            "market": market,
            "state": str(target_state.value),
            "strategy": strategy,
            "budget_usdt": budget_f,
        },
        "applied_params": applied_params,
        "recommended_params": recommended_params,
        "item": it,
        "settings": _settings_snapshot(system),
    }


@router.post(
    "/autopilot/run",
    summary="Run autopilot step manually",
    responses={
        200: {"description": "Autopilot step executed"},
        400: {"description": "Autopilot not supported or failed"},
    },
)
async def autopilot_run(
    request: Request,
    scan_only: bool = Query(False, description="Only scan, do not approve/demote"),
) -> Dict[str, Any]:
    """
    Manually trigger one autopilot step.

    - Scans for candidates
    - Optionally auto-approves based on settings
    - Demotes idle markets if enabled
    """
    system = request.app.state.system

    # [FIX] AutopilotManager가 있으면 그쪽 step() 사용 (중복 실행 방지)
    autopilot_mgr = getattr(system, "autopilot_manager", None)
    if autopilot_mgr is not None and hasattr(autopilot_mgr, "step"):
        try:
            result = await autopilot_mgr.step(reason="api", scan_only=bool(scan_only))
            return {"ok": True, "result": result, "settings": _settings_snapshot(system), "reserved": reserved_queue.snapshot()}
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            try:
                system.ledger.append("AUTOPILOT_RUN_ERROR", error=str(e))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[RESERVED_API] [FIX] AutopilotManager가 있으면 그쪽 step() 사용 (중복 실행 방지): %s", exc, exc_info=True)
            return {"ok": False, "error": "autopilot_failed", "message": str(e), "settings": _settings_snapshot(system)}
    
    # Fallback: Some deployments may not include autopilot yet.
    step = getattr(system, "autopilot_step", None)
    if not callable(step):
        return {"ok": False, "error": "not_supported", "message": "autopilot_step not implemented"}

    try:
        result = await step(reason="api", scan_only=bool(scan_only))
        return {"ok": True, "result": result, "settings": _settings_snapshot(system), "reserved": reserved_queue.snapshot()}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        try:
            system.ledger.append("AUTOPILOT_RUN_ERROR", error=str(e))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[RESERVED_API] Fallback: Some deployments may not include autopilot yet.: %s", exc, exc_info=True)
        return {"ok": False, "error": "autopilot_failed", "message": str(e), "settings": _settings_snapshot(system)}


@router.post(
    "/enqueue",
    summary="추천 코인을 reserved_queue 앞줄에 수동 등록 (autopilot 우선 검토)",
    responses={200: {"description": "Enqueued"}},
)
def reserved_enqueue(request: Request, item: Dict[str, Any]) -> Dict[str, Any]:
    """추천 코인을 reserved_queue 앞줄(우선)에 등록 → autopilot 이 해당 전략(PINGPONG/AUTOLOOP 등) 로직으로 검토·진입.

    ★ 반자동: autopilot 의 AI·conviction 게이트는 그대로 적용 (무조건 진입 아님 — 네가 고른 코인이 줄 앞에 서고, 품질 통과 시 그 로직으로 진입).
    """
    try:
        if not isinstance(item, dict) or not item:
            return {"ok": False, "error": "empty_item"}
        market = str(item.get("market") or "").strip().upper()
        strategy = str(item.get("strategy") or item.get("recommended_strategy") or "").strip().upper()
        if not market or not strategy:
            return {"ok": False, "error": "market_and_strategy_required"}
        it = dict(item)
        it["market"] = market
        it["strategy"] = strategy
        it["recommended_strategy"] = strategy
        # confidence 게이트용 — 추천 아이템엔 confidence 키가 없을 수 있어 adjusted/ai_score 로 매핑
        if not it.get("confidence"):
            try:
                it["confidence"] = float(it.get("ai_adjusted_score") or it.get("ai_score") or 0.0)
            except (TypeError, ValueError):
                it["confidence"] = 0.0
        it["manual_enqueue"] = True
        rid = reserved_queue.push(it, front=True)   # front=True → 우선순위(줄 앞)
        try:
            request.app.state.system.ledger.append("RESERVED_MANUAL_ENQUEUE", market=market, strategy=strategy, rid=rid)
        except (AttributeError, TypeError, ValueError):
            pass
        return {"ok": True, "rid": rid, "market": market, "strategy": strategy, "reserved": reserved_queue.snapshot()}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("reserved_router.reserved_enqueue L1650: %s", e)
        return {"ok": False, "error": str(e)}
