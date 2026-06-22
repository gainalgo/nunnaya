# ============================================================
# File: app/manager/market_controls.py
# Autocoin OS v3-H — Market Controls Helpers
# ------------------------------------------------------------
# - Strategy mode control payload builder
# - Apply per-market controls to Engine Context and persist
#
# Why this exists:
# - Reserved Queue approval and Autopilot both need to set a market's
#   Strategy mode in a consistent way.
# - Keeping this logic outside API routers avoids circular imports.
# ============================================================

from __future__ import annotations


import json
import logging
import os
from typing import Any, Dict, Optional

from app.core.constants import env_bool, env_float

logger = logging.getLogger(__name__)


def _load_plugin_params_override(system: Any) -> Dict[str, Any]:
    """UI-set 전략별 튜닝 오버라이드 (runtime/strategy_plugin_params.json) — system 속성에 캐시.
    POST /api/reserved/plugin-params 가 system._plugin_params_override 도 갱신 → 재시작 없이 다음 slot-fill 반영.
    오버라이드 미설정 시 {} 반환(동작 변화 0)."""
    ov = getattr(system, "_plugin_params_override", None)
    if ov is not None:
        return ov if isinstance(ov, dict) else {}
    ov = {}
    try:
        _p = os.path.join("runtime", "strategy_plugin_params.json")
        if os.path.exists(_p):
            with open(_p, "r", encoding="utf-8") as _f:
                ov = json.load(_f) or {}
    except (OSError, ValueError):
        logger.warning("[MarketControls] plugin_params_override load failed", exc_info=True)
        ov = {}
    if not isinstance(ov, dict):
        ov = {}
    try:
        system._plugin_params_override = ov
    except (AttributeError, TypeError):
        pass
    return ov


def _apply_entry_relaxed_overrides(mode_upper: str, strat_params: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a mild entry-relaxed profile for newly applied strategy params."""
    p = dict(strat_params or {})
    m = str(mode_upper or "").upper()
    try:
        if m == "PINGPONG":
            p["rsi_buy"] = max(float(p.get("rsi_buy", 30.0)), 34.0)

        elif m == "AUTOLOOP":
            p["rsi_buy"] = max(float(p.get("rsi_buy", 28.0)), 33.5)
            p["z_buy"] = min(float(p.get("z_buy", 1.5)), 1.2)

        elif m == "LIGHTNING":
            p["burst_threshold"] = min(float(p.get("burst_threshold", 1.5)), 1.2)
            p["min_ai_confidence"] = min(float(p.get("min_ai_confidence", 0.2)), 0.15)

        elif m == "GAZUA":
            p["ai_buy_threshold"] = min(float(p.get("ai_buy_threshold", 0.75)), 0.6)
            p["ai_score_min"] = min(float(p.get("ai_score_min", 0.7)), 0.6)
            p["rsi_min"] = min(float(p.get("rsi_min", 30.0)), 28.0)
            p["rsi_max"] = max(float(p.get("rsi_max", 40.0)), 55.0)
            p["momentum_min"] = min(float(p.get("momentum_min", 0.3)), 0.15)
            p["bounce_pct_min"] = min(float(p.get("bounce_pct_min", 0.3)), 0.2)
            p["profile_mode"] = str(p.get("profile_mode", "auto") or "auto").strip().lower()
            p["sideways_ai_score_min"] = min(float(p.get("sideways_ai_score_min", 0.58)), 0.58)
            p["sideways_rsi_max"] = max(float(p.get("sideways_rsi_max", 55.0)), 55.0)
            p["sideways_bounce_pct_min"] = min(float(p.get("sideways_bounce_pct_min", 0.15)), 0.2)
            p["sideways_momentum_min"] = min(float(p.get("sideways_momentum_min", 0.05)), 0.1)
            p["scale_in_enabled"] = bool(p.get("scale_in_enabled", True))
            p["entry_probe_frac"] = max(0.2, min(0.5, float(p.get("entry_probe_frac", 0.35))))
            p["entry_confirm_frac"] = max(0.5, min(0.8, float(p.get("entry_confirm_frac", 0.65))))
            p["confirm_window_sec"] = max(300.0, min(3600.0, float(p.get("confirm_window_sec", 1200.0))))
            p["confirm_profit_pct"] = max(0.1, min(1.0, float(p.get("confirm_profit_pct", 0.35))))
            p["confirm_ai_threshold"] = min(max(float(p.get("confirm_ai_threshold", 0.64)), 0.5), 0.75)
            p["confirm_momentum_min"] = min(max(float(p.get("confirm_momentum_min", 0.05)), -0.2), 0.3)
            p["add_buy_cooldown_sec"] = max(30.0, min(1800.0, float(p.get("add_buy_cooldown_sec", 180.0))))

        elif m == "CONTRARIAN":
            p["min_score"] = min(int(p.get("min_score", 2)), 1)
            p["ema_cross_enabled"] = False
            p["rsi_filter"] = False
            p["rsi_max"] = max(float(p.get("rsi_max", 65.0)), 70.0)

        elif m == "SNIPER":
            p["rsi_entry_enabled"] = False
            p["entry_lookback_min"] = min(int(p.get("entry_lookback_min", 360)), 240)
            p["entry_threshold_pct"] = max(float(p.get("entry_threshold_pct", 0.5)), 0.6)
            p["ai_min_score"] = min(float(p.get("ai_min_score", 0.55)), 0.45)

        elif m == "LADDER":
            # Keep spacing from selector/operator; over-tightening increases churn.
            p["step_pct"] = max(0.5, float(p.get("step_pct", 1.0)))
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[MarketControls] _apply_entry_relaxed_overrides(%s) failed", mode_upper, exc_info=True)
        return p
    return p


def build_strategy_controls_payload(mode: str) -> Dict[str, Any]:
    """Build a controls patch that mirrors dashboard defaults.

    - mode: "AI" | "PINGPONG" | "AUTOLOOP" | ...
    """

    m = str(mode or "AI").strip().upper()

    payload: Dict[str, Any] = {
        "baseline": {"enabled": False, "level": 10},
        "ai": {"enabled": True, "level": 10},
        "strategy": {"enabled": False, "level": 5, "mode": ""},
    }

    if m and m not in ("AI", "NONE"):
        payload["ai"]["enabled"] = False
        payload["strategy"]["enabled"] = True
        payload["strategy"]["mode"] = m

        # Reasonable per-mode defaults (operator can override later)
        if m == "AUTOLOOP":
            payload["strategy"]["params"] = {
                "bootstrap": True,
                "bar_sec": 180,
                "max_bars": 600,
                "rsi_len": 14,
                "rsi_buy": 28,
                "rsi_sell": 58,
                "macd_fast": 12,
                "macd_slow": 26,
                "macd_signal": 9,
                "anchor_len": 50,
                "z_len": 20,
                "z_buy": 1.5,
                "max_vol_pct": 1.8,
                "repeat_cooldown_sec": 3.0,
                # Trend pullback tactic (BULL regime) - 완만한 상승장 진입용이하게 함
                "pb_enabled": True,
                "pb_rsi_min": 38,
                # pb_rsi_max: 55,
                # pb_dev_min_pct: 0.15,
                "pb_dev_max_pct": 0.8,
                "pb_slope_bars": 5,
                "pb_min_slope_pct": 0.05,
                "pb_macd_floor": 0.0,
                # pb_z_buy: 0.6,
                # pb_require_bounce: true,
                "pb_rsi_max": 60,
                "pb_z_buy": 0.3,
                "pb_dev_min_pct": 0.10,
                "pb_require_bounce": False,
                # staged add-buy (conservative in current market)
                "buy_splits": [0.30, 0.30, 0.40],
                "add_buy_drop_pcts": [-1.2, -3.0],
                "entry_stage_max": 3,
                "add_buy_cooldown_sec": 120.0,
                "martingale": 1.0,    # 1.0=Disable, >1.0=Enable (e.g. 1.5)
                # Telemetry snapshot throttling (trade_ledger)
                "telemetry_interval_sec": 60.0,
            }
        
        elif m == "LADDER":
            payload["strategy"]["params"] = {
                "step_pct": 1.0,      # 가격이 1% 하락할 때마다 추가 매수
                "max_steps": 10,      # 최대 10회 분할 매수
                "tp": 2.0,            # 평단 대비 2% 수익 시 전량 매도
                "sl": -5.0,           # 손절 % (기본값)
                "max_down_buys": 3,   # 연속 하락 추종 매수 제한
                "reversal_pct": 1.5,  # 반등 전환 인식 기준
                "martingale": 1.0,    # 매수 금액 증액 배수 (1.0=고정, 1.5=1.5배씩 증가)
                "min_order_usdt": 10.0,
                "ai_influence": 0.0,  # Ladder는 기계적 대응 우선
                # Trailing Entry (이동식 사다리)
                "trailing_entry": True,      # 하락 추세 따라가다 반등 시 시작
                "trailing_entry_pct": 0.5,   # 최저점 대비 0.5% 반등 시 진입
                "reset_on_exit": True,       # 익절 후 기준가 리셋 (무한 루프)
                "step_gap_atr_enabled": False, # ATR 기반 간격 자동 설정
                "step_gap_atr_period": 14,
                "step_gap_atr_mult": 1.0,      # ATR * 1.0 만큼을 간격으로 사용
                # GridV2 auto sync (Active Window 유지)
                "grid_auto_sync": True,
                "auto_center": True,
                "profit_borrow_enabled": False,
                "profit_borrow_max": 3,
                "emergency_last_step_enabled": True,
                "emergency_last_step_gap_mult": 2.0,
                "emergency_last_step_buy_mult": 0.5,
            }
        
        elif m == "LIGHTNING":
            payload["strategy"]["params"] = {
                "burst_window": 5,
                "burst_threshold": 1.5,
                "tp": 3.0,              # 익절 %
                "sl": -2.0,             # 손절 %
                "atr_period": 14,
                "atr_burst_mult": 3.0,
                "ai_influence": 0.5,
                "base_size_scale": 1.0,  # 기본 100% 진입
                "min_order_usdt": 10.0,
                "min_ai_confidence": 0.2,
            }
        
        elif m == "GAZUA":
            payload["strategy"]["params"] = {
                "tp": 25.0,           # V2: 큰 파동용
                "sl": -25.0,          # V2: 넓은 SL
                "sl_price": 0.0,      # 손절 가격 (USDT, 0이면 sl % 사용)
                "tp_price": 0.0,      # 목표 가격 (USDT, 0이면 tp % 사용)
                "manual_exit": False, # True=알림만(수동), False=자동청산
                "sell_fraction": 1.0,       # V2: 전량매도 기본, 부분매도는 gazua_partial 메커니즘으로
                "trail_tp_enabled": True,
                "trail_dist_pct": 3.0,     # V2 기본 콜백
                "hold_sell": False,
                "user_sell_only": False,
                # V2: 다단계 부분매도
                "partial_sell_trigger_pct": 20.0,    # Stage 1: +20% → 30% 매도
                "partial_sell_fraction": 0.3,        # Stage 1 매도 비율
                "partial_sell_trigger2_pct": 35.0,   # Stage 2: +35% → 40% 매도
                "partial_sell_fraction2": 0.4,       # Stage 2 매도 비율
                # V2: Trailing Stop (TimeVolatility 연동)
                "gazua_trailing_activate_pct": 10.0, # Trailing 활성화 +10%
                "gazua_trailing_callback_pct": 3.0,  # 콜백 3% (시간대별 자동 조절)
                # V2: DCA 분할 매수
                "gazua_initial_ratio": 0.6,          # 초기 매수 예산의 60%
                "gazua_dca_trigger_pct": -5.0,       # DCA 1단계: -5% 하락 시 40% 추가 매수
                "gazua_dca_ratio": 0.4,              # DCA 1단계 비율
                "gazua_dca2_trigger_pct": -10.0,     # DCA 2단계: -10% 하락 시 20% 추가 매수
                "gazua_dca2_ratio": 0.2,             # DCA 2단계 비율
                # Regime-aware entry profile
                "profile_mode": "auto",
                "sideways_ai_score_min": 0.58,
                "sideways_rsi_max": 55,
                "sideways_bounce_pct_min": 0.15,
                "sideways_momentum_min": 0.05,
                "sideways_ema_cross_required": False,
                "trend_ai_score_min": 0.68,
                "trend_rsi_max": 60,
                "trend_bounce_pct_min": 0.25,
                "trend_momentum_min": 0.15,
                "trend_ema_cross_required": True,
                # 2-stage entry
                "scale_in_enabled": True,
                "entry_probe_frac": 0.60,   # V2: 초기 60%
                "entry_confirm_frac": 0.40,  # V2: DCA 40%
                "confirm_window_sec": 1200,
                "confirm_profit_pct": 0.35,
                "confirm_ai_threshold": 0.64,
                "confirm_momentum_min": 0.05,
                "add_buy_cooldown_sec": 180,
                # Analysis params (optional, for scoring)
                "breakout_window": 20,
                "vol_filter": 0.3,
                "ai_influence": 0.5,
            }

        elif m == "PINGPONG":
            payload["strategy"]["params"] = {
                "tp": 3.0,            # 익절 % (기본 3%)
                "sl": -2.0,           # 손절 %
                "rsi_buy": 30,        # RSI 매수 기준
                "rsi_sell": 70,       # RSI 매도 기준
                "rsi_len": 14,
                "macd_fast": 12,
                "macd_slow": 26,
                "macd_signal": 9,
                "min_order_usdt": 10.0,
                "ai_influence": 0.3,
            }

        elif m == "CONTRARIAN":
            payload["strategy"]["params"] = {
                "tp": 15.0,           # 역행 목표 익절 % (Global Profit Take로 조기 회수 가능)
                "sl": -50.0,          # 역행 기본 손절 % (깊은 보호선)
                "trail_tp_enabled": False,  # TP 도달 즉시 청산 기본
                "trail_dist_pct": 0.3,      # Trail 사용 시 촘촘한 간격
                "use_atr": False,           # TP/SL 일관성 우선
                "atr_period": 14,
                "rsi_filter": False,
                "rsi_max": 70,
                "min_score": 1,
                "cooldown_sec": 300,        # 재진입 쿨다운 (5분)
                "min_order_usdt": 10.0,
            }

        elif m == "SNIPER":
            payload["strategy"]["params"] = {
                # Entry (저격 매수)
                "entry_enabled": True,
                "entry_lookback_min": 360,    # 6시간 최저가 기준
                "entry_threshold_pct": 0.5,   # 최저가 + 0.5% 이내
                # Exit (저격 매도)
                "exit_enabled": True,
                "exit_lookback_min": 360,     # 6시간 최고가 기준
                "exit_threshold_pct": 0.5,    # 최고가 - 0.5% 이내
                # TP/SL
                "tp_pct": 3.0,
                "sl_pct": 2.0,
                # Trail
                "trail_tp": True,
                "trail_dist_pct": 1.2,
                # 필터
                "ai_gate_enabled": True,
                "ai_min_score": 0.45,
                "rsi_entry_enabled": True,
                "rsi_exit_enabled": True,
                # 주문
                "use_limit": True,
                "fallback_to_market": True,
                "expiry_min": 180,
                "min_order_usdt": 10.0,
            }

        elif m == "WHALE":
            # [2026-05-30] WHALE elif 누락 fix — Linear 마이그 7 plugin 일괄 작업의 일환.
            # plugin_whale.py 본문의 params.get(..., default) 와 동일값 명시 (dashboard 표시/조정 가능).
            payload["strategy"]["params"] = {
                # Entry — Ichimoku + StochRSI + Volume spike (LONG only)
                "rsi_period": 14,
                "rsi_entry_max": 30.0,
                "rsi_entry_lookback": 5,
                "ichimoku_tenkan": 9,
                "ichimoku_kijun": 26,
                "ichimoku_senkou_b": 52,
                "cloud_min_thickness_pct": 1.5,
                "vol_lookback": 20,
                "vol_spike_ratio": 2.0,
                "stoch_rsi_period": 14,
                "stoch_k_smooth": 3,
                "stoch_d_smooth": 3,
                # Exit
                "rsi_exit_min": 65.0,
                # TP/SL
                "tp_pct": 2.0,
                "sl_pct": 3.0,
                # 캔들 단위 (3분봉)
                "candle_unit": 3,
                # 주문
                "min_order_usdt": 10.0,
                "ai_influence": 0.3,
            }

    return payload


def apply_engine_controls(
    system: Any,
    market: str,
    mode: str,
    recommended_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply a controls patch to a market context, and persist runtime state.

    Notes:
    - This never places orders.
    - This only updates ctx.controls so that StrategySelector / Engine reads it.
    - If recommended_params is provided, it overrides the default strategy params.
    """

    market = str(market or "").strip().upper()
    mode_upper = str(mode or "").strip().upper()
    controls = build_strategy_controls_payload(mode)
    contrarian_force_longhold = False
    try:
        contrarian_force_longhold = env_bool("OMA_CONTRARIAN_FORCE_LONGHOLD", default=False)
    except (AttributeError, TypeError):
        logger.warning("[MarketControls] contrarian_force_longhold env parse failed", exc_info=True)
        contrarian_force_longhold = False

    # [2026-02-11] Legacy fallback: 필요 시 CONTRARIAN 강한 LongHold 보호를 강제 적용.
    if mode_upper == "CONTRARIAN" and contrarian_force_longhold:
        strat_params = controls.get("strategy", {}).get("params", {})
        strat_params["tp"] = 15.0           # TP 15% 고정
        strat_params["sl"] = -50.0          # SL -50% 고정 (장기 보유용)
        strat_params["hold_sell"] = True    # Hold 모드
        strat_params["user_sell_only"] = True  # 사용자만 매도 가능
        strat_params["trail_tp_enabled"] = True
        strat_params["trail_dist_pct"] = 4.0  # Trail 4%
        controls["strategy"]["params"] = strat_params
    
    # [2026-06-01] UI-set 전략별 튜닝 오버라이드 (PINGPONG/AUTOLOOP/WHALE 등) — recommended_params 직전 merge.
    #   slot-fill 자동 적용. 오버라이드 없으면 no-op (additive·guarded). recommended_params(per-coin)는 이 위에 덮음.
    try:
        _ov = _load_plugin_params_override(system).get(mode_upper)
        if isinstance(_ov, dict) and _ov:
            _sp = controls.setdefault("strategy", {}).setdefault("params", {})
            for _ok, _ov2 in _ov.items():
                _sp[_ok] = _ov2
    except Exception:
        logger.warning("[MarketControls] plugin_params_override merge failed", exc_info=True)

    # 추천 파라미터가 있으면 기본값 덮어쓰기 (단, CONTRARIAN의 TP/SL은 제외)
    if recommended_params and isinstance(recommended_params, dict):
        strat_params = controls.get("strategy", {}).get("params", {})
        if strat_params:
            # tp_pct → tp, sl_pct → sl 매핑
            # [2026-02-11] CONTRARIAN LongHold 강제 모드에서는 TP/SL 고정값 유지.
            if "tp_pct" in recommended_params and not (mode_upper == "CONTRARIAN" and contrarian_force_longhold):
                strat_params["tp"] = float(recommended_params["tp_pct"])
            if "sl_pct" in recommended_params and not (mode_upper == "CONTRARIAN" and contrarian_force_longhold):
                strat_params["sl"] = float(recommended_params["sl_pct"])
            # LADDER 전용 파라미터
            if "step_pct" in recommended_params:
                strat_params["step_pct"] = float(recommended_params["step_pct"])
            if "steps" in recommended_params:
                strat_params["max_steps"] = int(recommended_params["steps"])
            if "max_down_buys" in recommended_params:
                strat_params["max_down_buys"] = max(1, int(recommended_params["max_down_buys"]))
            if "reversal_pct" in recommended_params:
                strat_params["reversal_pct"] = max(0.1, float(recommended_params["reversal_pct"]))
            if "martingale" in recommended_params:
                strat_params["martingale"] = float(recommended_params["martingale"])
            if "use_atr" in recommended_params:
                strat_params["step_gap_atr_enabled"] = bool(recommended_params["use_atr"])
            if "atr_mult" in recommended_params:
                strat_params["step_gap_atr_mult"] = float(recommended_params["atr_mult"])
            if "grid_auto_sync" in recommended_params:
                strat_params["grid_auto_sync"] = bool(recommended_params["grid_auto_sync"])
            if "auto_center" in recommended_params:
                strat_params["auto_center"] = bool(recommended_params["auto_center"])
            if "profit_borrow_enabled" in recommended_params:
                strat_params["profit_borrow_enabled"] = bool(recommended_params["profit_borrow_enabled"])
            if "profit_borrow_max" in recommended_params:
                strat_params["profit_borrow_max"] = max(0, int(recommended_params["profit_borrow_max"]))
            if "emergency_last_step_enabled" in recommended_params:
                strat_params["emergency_last_step_enabled"] = bool(recommended_params["emergency_last_step_enabled"])
            if "emergency_last_step_gap_mult" in recommended_params:
                strat_params["emergency_last_step_gap_mult"] = float(recommended_params["emergency_last_step_gap_mult"])
            if "emergency_last_step_buy_mult" in recommended_params:
                strat_params["emergency_last_step_buy_mult"] = float(recommended_params["emergency_last_step_buy_mult"])
            if "spacing_mode" in recommended_params:
                strat_params["spacing_mode"] = str(recommended_params["spacing_mode"]).upper()
            if "spacing_value" in recommended_params:
                strat_params["spacing_value"] = float(recommended_params["spacing_value"])
            # AUTOLOOP 전용
            if "rsi_buy" in recommended_params:
                strat_params["rsi_buy"] = int(recommended_params["rsi_buy"])
            if "rsi_sell" in recommended_params:
                strat_params["rsi_sell"] = int(recommended_params["rsi_sell"])
            # GAZUA/LIGHTNING
            if "manual_exit" in recommended_params:
                strat_params["manual_exit"] = bool(recommended_params["manual_exit"])
            if "hold_sell" in recommended_params:
                strat_params["hold_sell"] = bool(recommended_params["hold_sell"])
            if "user_sell_only" in recommended_params:
                strat_params["user_sell_only"] = bool(recommended_params["user_sell_only"])
            if "sell_fraction" in recommended_params:
                strat_params["sell_fraction"] = float(recommended_params["sell_fraction"])
            if "profile_mode" in recommended_params:
                strat_params["profile_mode"] = str(recommended_params["profile_mode"])
            if "sideways_ai_score_min" in recommended_params:
                strat_params["sideways_ai_score_min"] = float(recommended_params["sideways_ai_score_min"])
            if "sideways_rsi_max" in recommended_params:
                strat_params["sideways_rsi_max"] = int(recommended_params["sideways_rsi_max"])
            if "sideways_bounce_pct_min" in recommended_params:
                strat_params["sideways_bounce_pct_min"] = float(recommended_params["sideways_bounce_pct_min"])
            if "sideways_momentum_min" in recommended_params:
                strat_params["sideways_momentum_min"] = float(recommended_params["sideways_momentum_min"])
            if "sideways_ema_cross_required" in recommended_params:
                strat_params["sideways_ema_cross_required"] = bool(recommended_params["sideways_ema_cross_required"])
            if "trend_ai_score_min" in recommended_params:
                strat_params["trend_ai_score_min"] = float(recommended_params["trend_ai_score_min"])
            if "trend_rsi_max" in recommended_params:
                strat_params["trend_rsi_max"] = int(recommended_params["trend_rsi_max"])
            if "trend_bounce_pct_min" in recommended_params:
                strat_params["trend_bounce_pct_min"] = float(recommended_params["trend_bounce_pct_min"])
            if "trend_momentum_min" in recommended_params:
                strat_params["trend_momentum_min"] = float(recommended_params["trend_momentum_min"])
            if "trend_ema_cross_required" in recommended_params:
                strat_params["trend_ema_cross_required"] = bool(recommended_params["trend_ema_cross_required"])
            if "scale_in_enabled" in recommended_params:
                strat_params["scale_in_enabled"] = bool(recommended_params["scale_in_enabled"])
            if "entry_probe_frac" in recommended_params:
                strat_params["entry_probe_frac"] = float(recommended_params["entry_probe_frac"])
            if "entry_confirm_frac" in recommended_params:
                strat_params["entry_confirm_frac"] = float(recommended_params["entry_confirm_frac"])
            if "confirm_window_sec" in recommended_params:
                strat_params["confirm_window_sec"] = float(recommended_params["confirm_window_sec"])
            if "confirm_profit_pct" in recommended_params:
                strat_params["confirm_profit_pct"] = float(recommended_params["confirm_profit_pct"])
            if "confirm_ai_threshold" in recommended_params:
                strat_params["confirm_ai_threshold"] = float(recommended_params["confirm_ai_threshold"])
            if "confirm_momentum_min" in recommended_params:
                strat_params["confirm_momentum_min"] = float(recommended_params["confirm_momentum_min"])
            if "add_buy_cooldown_sec" in recommended_params:
                strat_params["add_buy_cooldown_sec"] = float(recommended_params["add_buy_cooldown_sec"])
            # SNIPER 전용 파라미터 매핑
            if "entry_enabled" in recommended_params:
                strat_params["entry_enabled"] = bool(recommended_params["entry_enabled"])
            if "entry_lookback_min" in recommended_params:
                strat_params["entry_lookback_min"] = int(recommended_params["entry_lookback_min"])
            if "entry_threshold_pct" in recommended_params:
                strat_params["entry_threshold_pct"] = float(recommended_params["entry_threshold_pct"])
            if "exit_enabled" in recommended_params:
                strat_params["exit_enabled"] = bool(recommended_params["exit_enabled"])
            if "exit_lookback_min" in recommended_params:
                strat_params["exit_lookback_min"] = int(recommended_params["exit_lookback_min"])
            if "exit_threshold_pct" in recommended_params:
                strat_params["exit_threshold_pct"] = float(recommended_params["exit_threshold_pct"])
            if "trail_tp" in recommended_params:
                strat_params["trail_tp"] = bool(recommended_params["trail_tp"])
            if "trail_dist_pct" in recommended_params:
                strat_params["trail_dist_pct"] = float(recommended_params["trail_dist_pct"])
            if "ai_gate_enabled" in recommended_params:
                strat_params["ai_gate_enabled"] = bool(recommended_params["ai_gate_enabled"])
            if "ai_min_score" in recommended_params:
                strat_params["ai_min_score"] = float(recommended_params["ai_min_score"])
            if "rsi_entry_enabled" in recommended_params:
                strat_params["rsi_entry_enabled"] = bool(recommended_params["rsi_entry_enabled"])
            if "rsi_exit_enabled" in recommended_params:
                strat_params["rsi_exit_enabled"] = bool(recommended_params["rsi_exit_enabled"])
            if "use_limit" in recommended_params:
                strat_params["use_limit"] = bool(recommended_params["use_limit"])
            if "fallback_to_market" in recommended_params:
                strat_params["fallback_to_market"] = bool(recommended_params["fallback_to_market"])
            if "expiry_min" in recommended_params:
                strat_params["expiry_min"] = int(recommended_params["expiry_min"])
            controls["strategy"]["params"] = strat_params

    # Keep new market setup aligned with current "mild relaxed entry" baseline.
    try:
        sblock = controls.get("strategy") if isinstance(controls, dict) else None
        if isinstance(sblock, dict):
            sp = sblock.get("params")
            if isinstance(sp, dict):
                sblock["params"] = _apply_entry_relaxed_overrides(mode_upper, sp)
                controls["strategy"] = sblock
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[MARKET_CTRL] Keep new market setup aligned with current mild relaxed entry baseline: %s", exc, exc_info=True)

    # CONTRARIAN 운영 기본값 정규화
    # - longhold 강제가 꺼져있을 때는 최소 1.2% 회수 중심으로 통일한다.
    if mode_upper == "CONTRARIAN" and not contrarian_force_longhold:
        try:
            sblock = controls.get("strategy") if isinstance(controls, dict) else None
            if isinstance(sblock, dict):
                sp = sblock.get("params")
                if not isinstance(sp, dict):
                    sp = {}

                min_tp = max(0.1, float(env_float("OMA_CONTRARIAN_MIN_TP_PCT", default=15.0)))
                lock_min_tp = bool(env_bool("OMA_CONTRARIAN_LOCK_MIN_TP", default=True))
                default_sl = -abs(float(env_float("OMA_CONTRARIAN_DEFAULT_SL_PCT", default=50.0)))
                force_trail = bool(env_bool("OMA_CONTRARIAN_FORCE_TRAIL", default=False))
                trail_dist_pct = max(0.05, float(env_float("OMA_CONTRARIAN_TRAIL_DIST_PCT", default=0.3)))

                if lock_min_tp:
                    sp["tp"] = float(min_tp)
                else:
                    sp["tp"] = max(float(min_tp), float(sp.get("tp", min_tp)))

                sl_cur = float(sp.get("sl", default_sl))
                sp["sl"] = -abs(sl_cur) if sl_cur != 0 else float(default_sl)

                # CONTRARIAN은 자동회수 전략으로 기본 설정.
                sp["hold_sell"] = False
                sp["user_sell_only"] = False

                if force_trail:
                    sp["trail_tp_enabled"] = True
                    sp["trail_dist_pct"] = float(trail_dist_pct)
                else:
                    if "trail_tp_enabled" not in sp:
                        sp["trail_tp_enabled"] = False
                    if "trail_dist_pct" not in sp:
                        sp["trail_dist_pct"] = float(trail_dist_pct)

                sblock["params"] = sp
                controls["strategy"] = sblock
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[MARKET_CTRL] CONTRARIAN은 자동회수 전략으로 기본 설정: %s", exc, exc_info=True)

        # CONTRARIAN 진입 타이밍 우선 모드:
        # - 필요 시 orderbook guard를 market-level로 우회 가능하게 한다.
        try:
            bypass_ob_guard = bool(env_bool("OMA_CONTRARIAN_BYPASS_OB_GUARD", default=True))
        except (AttributeError, TypeError):
            logger.warning("[MarketControls] bypass_ob_guard env parse failed", exc_info=True)
            bypass_ob_guard = True
        try:
            if isinstance(recommended_params, dict) and ("entry_ob_guard_enabled" in recommended_params):
                bypass_ob_guard = not bool(recommended_params.get("entry_ob_guard_enabled"))
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[MARKET_CTRL] orderbook guard 우회 판단: %s", exc, exc_info=True)

        if bypass_ob_guard:
            try:
                g = controls.get("guards")
                if not isinstance(g, dict):
                    g = {}
                g["entry_ob_guard_enabled"] = False
                controls["guards"] = g
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[MARKET_CTRL] orderbook guard 우회 적용: %s", exc, exc_info=True)

    # Ensure context exists
    try:
        ctx = system.coordinator.ensure_market(market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
        logger.warning("ensure_market failed for %s, falling back to get_context", market, exc_info=True)
        # ensure_market이 실패하면 get_context 시도 (이미 존재할 수 있음)
        # 그래도 없으면 controls 적용 불가하므로 종료
        ctx = system.coordinator.get_context(market)

    try:
        if hasattr(ctx, "update_controls"):
            ctx.update_controls(controls)
        else:
            # legacy shallow merge
            try:
                for k, v in controls.items():
                    if isinstance(v, dict):
                        ctx.controls[k] = dict(v)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[MARKET_CTRL] legacy shallow merge: %s", exc, exc_info=True)
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[MARKET_CTRL] legacy shallow merge: %s", exc, exc_info=True)

    # Keep engine TP/SL policy aligned with effective strategy controls.
    # This prevents stale baseline policy values (e.g. -0.8) from triggering early exits.
    try:
        if ctx is not None:
            c = getattr(ctx, "controls", None) or {}
            s = c.get("strategy", {}) if isinstance(c, dict) else {}
            sp = s.get("params", {}) if isinstance(s, dict) else {}
            if isinstance(sp, dict):
                tp_val = sp.get("tp")
                if tp_val is None:
                    tp_val = sp.get("tp_pct")
                sl_val = sp.get("sl")
                if sl_val is None:
                    sl_val = sp.get("sl_pct")

                pol = getattr(ctx, "policy", None)
                if not isinstance(pol, dict):
                    pol = {"name": "nunnaya", "params": {}}
                pp = pol.get("params")
                if not isinstance(pp, dict):
                    pp = {}

                changed = False
                if tp_val is not None:
                    pp["tp"] = float(tp_val)
                    changed = True
                if sl_val is not None:
                    sl_num = float(sl_val)
                    pp["sl"] = -abs(sl_num) if sl_num > 0 else sl_num
                    if mode_upper == "LADDER" and float(pp["sl"]) > -5.0:
                        pp["sl"] = -5.0
                    changed = True

                if changed:
                    pol["name"] = str(pol.get("name") or "nunnaya")
                    pol["params"] = pp
                    if hasattr(ctx, "update_policy"):
                        ctx.update_policy(pol)
                    else:
                        ctx.policy = pol
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[MARKET_CTRL] stale baseline policy values prevention: %s", exc, exc_info=True)

    # Keep ladder runtime config aligned with effective controls so newly
    # approved markets don't inherit stale/invalid ladder settings.
    if mode_upper == "LADDER":
        try:
            sp: Dict[str, Any] = {}
            c = getattr(ctx, "controls", None) or {}
            if isinstance(c, dict):
                st = c.get("strategy", {})
                if isinstance(st, dict):
                    sp = st.get("params", {}) if isinstance(st.get("params"), dict) else {}

            mgr = getattr(system, "ladder_manager", None)
            if mgr is None:
                from app.manager.ladder_manager import LadderManager
                mgr = LadderManager(system=system)
                system.ladder_manager = mgr

            cfg = mgr.get_config(market)
            cfg["enabled"] = bool((c.get("strategy", {}) or {}).get("enabled", True)) if isinstance(c, dict) else True

            max_levels = max(1, int(sp.get("max_steps") or cfg.get("max_levels") or 10))
            cfg["max_levels"] = max_levels

            spacing_mode = str(sp.get("spacing_mode") or cfg.get("spacing_mode") or "PERCENT").upper()
            if spacing_mode not in ("PERCENT", "FIXED"):
                spacing_mode = "PERCENT"
            cfg["spacing_mode"] = spacing_mode

            spacing_raw = sp.get("spacing_value")
            if spacing_raw is None:
                spacing_raw = sp.get("step_pct")
            try:
                spacing_value = float(spacing_raw)
            except (TypeError, ValueError):
                logger.warning("[MarketControls] spacing_value parse failed for %s", market, exc_info=True)
                spacing_value = float(cfg.get("spacing_value") or 0.0)
            if spacing_value <= 0:
                if spacing_mode == "PERCENT":
                    spacing_value = max(0.1, float(sp.get("step_pct") or cfg.get("spacing_value") or 1.0))
                else:
                    spacing_value = max(1.0, float(cfg.get("spacing_value") or 100.0))
            cfg["spacing_value"] = spacing_value

            order_usdt = int(float(cfg.get("order_usdt") or 0))
            if order_usdt <= 0:
                alloc = 0.0
                try:
                    alloc = float(getattr(ctx, "allocated_capital", 0.0) or 0.0) if ctx is not None else 0.0
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[MarketControls] allocated_capital read failed for %s", market, exc_info=True)
                    alloc = 0.0
                if alloc > 0:
                    order_usdt = int(max(5, alloc / max_levels))
                else:
                    order_usdt = int(float(sp.get("min_order_usdt") or 10.0))
            cfg["order_usdt"] = max(5, order_usdt)
            cfg["ladder_fixed_order_usdt"] = cfg["order_usdt"]

            cfg["grid_auto_sync"] = bool(sp.get("grid_auto_sync", cfg.get("grid_auto_sync", True)))
            cfg["auto_center"] = bool(sp.get("auto_center", cfg.get("auto_center", True)))
            cfg["max_down_buys"] = max(1, int(sp.get("max_down_buys", cfg.get("max_down_buys", 3))))
            cfg["reversal_pct"] = max(0.1, float(sp.get("reversal_pct", cfg.get("reversal_pct", 1.5))))
            cfg["profit_borrow_enabled"] = bool(sp.get("profit_borrow_enabled", cfg.get("profit_borrow_enabled", False)))
            cfg["profit_borrow_max"] = max(0, int(sp.get("profit_borrow_max", cfg.get("profit_borrow_max", 3))))
            cfg["emergency_last_step_enabled"] = bool(
                sp.get("emergency_last_step_enabled", cfg.get("emergency_last_step_enabled", True))
            )
            cfg["emergency_last_step_gap_mult"] = float(
                sp.get("emergency_last_step_gap_mult", cfg.get("emergency_last_step_gap_mult", 2.0))
            )
            cfg["emergency_last_step_buy_mult"] = float(
                sp.get("emergency_last_step_buy_mult", cfg.get("emergency_last_step_buy_mult", 0.5))
            )

            lower = float(cfg.get("lower_bound") or 0.0)
            upper = float(cfg.get("upper_bound") or 0.0)
            if lower <= 0 or upper <= lower:
                try:
                    from app.core.hyper_price_store import price_store
                    cur = float(price_store.get_price(market) or 0.0)
                except (TypeError, ValueError):
                    logger.warning("[MarketControls] price_store.get_price(%s) parse failed", market, exc_info=True)
                    cur = 0.0
                if cur > 0:
                    per_side = max(1, max_levels // 2)
                    if spacing_mode == "FIXED":
                        lower = cur - (spacing_value * per_side)
                        upper = cur + (spacing_value * per_side)
                    else:
                        lower = cur * (1.0 - (spacing_value / 100.0) * per_side)
                        upper = cur * (1.0 + (spacing_value / 100.0) * per_side)
                    if lower > 0 and upper > lower:
                        cfg["lower_bound"] = round(lower, 2)
                        cfg["upper_bound"] = round(upper, 2)

            # save_config() 내부에서 유령 그리드 필터링 처리
            mgr.save_config(cfg)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[MARKET_CTRL] save_config() 유령 그리드 필터링: %s", exc, exc_info=True)

    # Ledger (best-effort)
    try:
        system.ledger.append("ENGINE_CONTROLS_SET", market=market, patch=controls)
    except (AttributeError, TypeError) as exc:
        logger.warning("[MARKET_CTRL] Ledger best-effort: %s", exc, exc_info=True)

    # Persist context_state.json (best-effort)
    try:
        system._save_context_state()  # noqa: SLF001
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[MARKET_CTRL] Persist context_state.json best-effort: %s", exc, exc_info=True)

    return controls
