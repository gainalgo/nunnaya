# ============================================================
# File: app/engine/hyper_nunnaya_engine.py
# Autocoin OS v3-H — Hyper Nunnaya Engine (Final AI Edition)
# NOTE:
# 이 엔진은 '판단자' 역할만 한다.
# 거래 빈도는 StrategyPipeline / Risk / Allocation 설정에 강하게 의존한다.
# 0건이 나오는 것은 버그가 아니라 설계 결과일 수 있다.
# ============================================================
from __future__ import annotations  # ✅ must be the first import (Python requirement)
import json
import logging
import os
import time
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

from app.core.currency import Q
from app.engine.hyper_engine_base import HyperEngineBase
from app.strategy.strategy_initializer import StrategyPipeline
from app.engine.hyper_engine_context import HyperEngineContext
from app.strategy.strategy_plugins import get_plugin


_TPSL_POLICY_FILE = os.path.join("runtime", "strategy_tp_sl_policy.json")
_TPSL_STRATEGIES = (
    "PINGPONG",
    "AUTOLOOP",
    "LADDER",
    "LIGHTNING",
    "GAZUA",
    "CONTRARIAN",
    "SNIPER",
)


class HyperNunnayaEngine(HyperEngineBase):
    """
    Autocoin OS v3-H 최종 엔진.
    - 단일 엔진
    - AI StrategyPipeline 기반
    - 시장 적응형 리스크 제어
    - 정책 자동 최적화
    - Context(State Machine) 완전 통합
    """

    VERSION = "v3-H-FINAL"

    def __init__(self):
        super().__init__(engine_name="nunnaya")
        self.pipeline = StrategyPipeline()
        self.tp_sl_policy: Dict[str, Any] = self._load_tp_sl_policy_from_file()

    def _default_tp_sl_policy(self) -> Dict[str, Any]:
        per = {s: {"tp_pct": 1.2, "sl_pct": 2.5} for s in _TPSL_STRATEGIES}
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
            "per_strategy": per,
        }

    def _normalize_tp_sl_policy(self, raw: Any) -> Dict[str, Any]:
        base = self._default_tp_sl_policy()
        if not isinstance(raw, dict):
            return base

        def _to_float(v: Any, dv: float) -> float:
            if v is None:
                return float(dv)
            try:
                return float(v)
            except (TypeError, ValueError):
                logger.warning("[Engine] _to_float conversion failed: %r, using default %s", v, dv, exc_info=True)
                return float(dv)

        def _to_int(v: Any, dv: int) -> int:
            if v is None:
                return int(dv)
            try:
                return int(float(v))
            except (TypeError, ValueError):
                logger.warning("[Engine] _to_int conversion failed: %r, using default %s", v, dv, exc_info=True)
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
        return out

    def _load_tp_sl_policy_from_file(self) -> Dict[str, Any]:
        if not os.path.exists(_TPSL_POLICY_FILE):
            return self._default_tp_sl_policy()
        try:
            with open(_TPSL_POLICY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("[Engine] TP/SL policy file load failed: %s", _TPSL_POLICY_FILE, exc_info=True)
            return self._default_tp_sl_policy()
        return self._normalize_tp_sl_policy(data)

    def set_tp_sl_policy(self, policy: Dict[str, Any]) -> None:
        self.tp_sl_policy = self._normalize_tp_sl_policy(policy)

    def _strategy_mode_from_context(self, context: HyperEngineContext) -> str:
        try:
            ctrls = getattr(context, "controls", None) or {}
            if isinstance(ctrls, dict):
                st = ctrls.get("strategy", {})
                if isinstance(st, dict):
                    return str(st.get("mode") or st.get("name") or "").strip().upper()
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] _strategy_mode_from_context fallback: %s", exc)
        return ""

    def _apply_tp_sl_policy(
        self,
        context: HyperEngineContext,
        strategy_mode: str,
        tp_pct: float,
        sl_pct: float,
    ) -> Tuple[float, float, Dict[str, Any]]:
        policy = self._normalize_tp_sl_policy(getattr(self, "tp_sl_policy", {}))

        tp_val = abs(float(tp_pct))
        sl_abs = abs(float(sl_pct))

        if not policy.get("enabled", True):
            return tp_val, -sl_abs, {"policy_enabled": False}

        tp_floor = float(policy.get("tp_floor_pct", 1.2) or 1.2)
        sl_floor = abs(float(policy.get("sl_floor_pct", 2.5) or 2.5))
        per = policy.get("per_strategy") if isinstance(policy.get("per_strategy"), dict) else {}
        p = per.get(strategy_mode, {}) if isinstance(per, dict) else {}
        strat_tp = float(p.get("tp_pct", tp_floor) or tp_floor)
        strat_sl = abs(float(p.get("sl_pct", sl_floor) or sl_floor))

        effective_tp = max(tp_val, tp_floor, strat_tp)
        effective_sl_abs = max(sl_abs, sl_floor, strat_sl)

        step_idx = 0
        hold_hours = 0.0
        # GAZUA는 장기보유 전략 — time_relax SL 축소 적용하지 않음
        _skip_time_relax = (strategy_mode.upper() == "GAZUA")
        if not _skip_time_relax and bool(policy.get("time_relax_enabled", True)):
            try:
                entry_ts = float((context.position or {}).get("entry_ts") or 0.0)
            except (TypeError, ValueError):
                logger.warning("[Engine] entry_ts parse failed for time_relax", exc_info=True)
                entry_ts = 0.0
            if entry_ts > 0:
                hold_hours = max(0.0, (time.time() - entry_ts) / 3600.0)
                step_hours = max(0.25, float(policy.get("time_relax_step_hours", 12.0) or 12.0))
                steps = max(1, int(policy.get("time_relax_steps", 5) or 5))
                step_idx = min(steps, int(hold_hours // step_hours))
                if step_idx > 0:
                    tp_step = max(0.0, float(policy.get("time_relax_tp_step", 0.1) or 0.1))
                    sl_step = max(0.0, float(policy.get("time_relax_sl_step", 0.5) or 0.5))
                    min_tp = max(0.1, float(policy.get("time_relax_min_tp_pct", 0.8) or 0.8))
                    min_sl = max(0.1, abs(float(policy.get("time_relax_min_sl_pct", 0.5) or 0.5)))
                    effective_tp = max(min_tp, effective_tp - (tp_step * step_idx))
                    effective_sl_abs = max(min_sl, effective_sl_abs - (sl_step * step_idx))

        meta = {
            "policy_enabled": True,
            "strategy_mode": strategy_mode,
            "effective_tp_pct": round(effective_tp, 4),
            "effective_sl_pct": round(effective_sl_abs, 4),
            "relax_step_idx": int(step_idx),
            "hold_hours": round(hold_hours, 4),
        }
        return effective_tp, -effective_sl_abs, meta

    # --------------------------------------------------------
    # ✅ REQUIRED: Public tick adapter
    # --------------------------------------------------------
    def tick(self, market: str, price: float, *args, **kwargs) -> Dict[str, Any]:
        """
        TickLoop / Coordinator가 호출하는 공개 tick 메서드.
        """
        context = self._resolve_context(market, args, kwargs)
        return self._tick_impl(market, price, context)

    def _resolve_context(self, market: str, args: tuple, kwargs: dict) -> HyperEngineContext:
        """Context 해석 및 생성."""
        context: Optional[HyperEngineContext] = None
        
        # 1) args에서 context 찾기
        if args and isinstance(args[0], HyperEngineContext):
            context = args[0]
        
        # 2) kwargs에서 context 찾기
        if context is None:
            ctx_kw = kwargs.get("context")
            if isinstance(ctx_kw, HyperEngineContext):
                context = ctx_kw

        # 3) base에서 context 가져오기
        if context is None:
            if hasattr(self, "get_context"):
                try:
                    context = self.get_context(market)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[Engine] get_context(%s) failed: %s", market, exc)

            if context is None and hasattr(self, "get_or_create_context"):
                try:
                    context = self.get_or_create_context(market)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[Engine] get_or_create_context(%s) failed: %s", market, exc)

            if context is None and hasattr(self, "contexts"):
                try:
                    ctx_map = getattr(self, "contexts")
                    if isinstance(ctx_map, dict):
                        context = ctx_map.get(market)
                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("[Engine] contexts dict lookup for %s failed: %s", market, exc)

        # 4) 최후 수단: 새로 생성
        if context is None:
            try:
                context = HyperEngineContext(market=market, engine_name=getattr(self, "engine_name", "nunnaya"))
            except (KeyError, AttributeError, TypeError):
                logger.warning("[Engine] HyperEngineContext creation with market=%s failed, using bare context", market, exc_info=True)
                context = HyperEngineContext()

            if hasattr(self, "contexts"):
                try:
                    ctx_map = getattr(self, "contexts")
                    if isinstance(ctx_map, dict):
                        ctx_map[market] = context
                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("[Engine] context store-back for %s failed: %s", market, exc)

        return context

    # --------------------------------------------------------
    # 초기 정책 설정
    # --------------------------------------------------------
    def on_initialize(self, context: HyperEngineContext):
        """context가 시장별로 생성될 때 호출됨. 초기 정책(Preset)을 지정한다."""
        context.policy = {
            "name": "nunnaya",
            "params": {
                "rsi_low": 25,
                "rsi_high": 75,
                "tp": 1.2,
                "sl": -2.5,
                "size": 10000.0,
                "vol_factor": 0.4,
                "trail_factor": 0.25
            }
        }

    # --------------------------------------------------------
    # v3-H 핵심 tick 구현 (리팩토링됨)
    # --------------------------------------------------------
    def _tick_impl(self, market: str, price: float, context: HyperEngineContext) -> Dict[str, Any]:
        """
        HyperNunnayaEngine의 실제 엔진 로직.
        Brain → Judge → Risk → Optimizer → Fusion → Position Logic
        """
        # 0) 초기화
        if not context.policy:
            self.on_initialize(context)

        params = context.policy.get("params", {})

        # 1) AI 파이프라인 실행
        ai = self.pipeline.run(market=market, price=price, context=context)
        context.current_ai = ai

        # 2) 신호 결정 (Arbiter)
        signal, strategy_out = self._resolve_signal(context, price, ai)

        # 3) 의도(Intent) 생성
        intent, profit = self._build_intent(context, price, params, ai, signal, strategy_out)

        # 4) 진단 정보 부착
        self._attach_diagnostics(context, ai, strategy_out)

        # 5) 정책 업데이트 및 마무리
        self._finalize_tick(context, params, signal, price)

        return {
            "signal": signal,
            "profit": profit,
            "position": context.position,
            "policy": context.policy,
            "ai": ai,
            "strategy_out": strategy_out,
            "intent": intent,
        }

    # --------------------------------------------------------
    # Phase 2: Signal Resolution (Arbiter)
    # --------------------------------------------------------
    def _resolve_signal(
        self, context: HyperEngineContext, price: float, ai: Dict[str, Any]
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        신호 결정 로직.
        우선순위: BASELINE > STRATEGY > AI > hold
        """
        ai_signal = ai["signal"]
        final_signal = "hold"
        strategy_out: Optional[Dict[str, Any]] = None

        controls = getattr(context, "controls", None)
        if not isinstance(controls, dict):
            return final_signal, strategy_out

        base = controls.get("baseline", {})
        risk_ctrl = controls.get("risk", {})
        ai_ctrl = controls.get("ai", {})
        strategy_ctrl = controls.get("strategy", {})

        # 0) STRATEGY 플러그인 실행
        strategy_signal: Optional[str] = None
        if isinstance(strategy_ctrl, dict) and strategy_ctrl.get("enabled"):
            try:
                mode = str(strategy_ctrl.get("mode") or strategy_ctrl.get("name") or "").strip()
                plugin = get_plugin(mode)
                dec = plugin.decide(context, price)
                strategy_signal = dec.signal
                strategy_out = {
                    "mode": mode or plugin.name,
                    "plugin": plugin.name,
                    "signal": dec.signal,
                    "reason": dec.reason,
                    "meta": dec.meta,
                }
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.warning("[Engine] strategy plugin execution failed: %s", e, exc_info=True)
                strategy_signal = "hold"
                strategy_out = {
                    "mode": str(strategy_ctrl.get("mode") or strategy_ctrl.get("name") or ""),
                    "plugin": "error",
                    "signal": "hold",
                    "reason": f"strategy_error:{type(e).__name__}",
                    "meta": {},
                }

        # 1) BASELINE (최우선: 수동 강제 진입용)
        if base.get("enabled") and context.position is None and base.get("level", 0) >= 1:
            final_signal = "buy"
        else:
            # 2) 기본 신호 소스 선택
            if isinstance(strategy_ctrl, dict) and strategy_ctrl.get("enabled") and strategy_signal in ("buy", "sell", "hold", "reserve"):
                final_signal = str(strategy_signal)
            elif ai_ctrl.get("enabled"):
                final_signal = ai_signal
            else:
                final_signal = "hold"

            # 3) RISK 차단 (sell은 차단하지 않음)
            if risk_ctrl.get("enabled") and final_signal == "buy":
                brain = ai.get("brain", {})
                vol = brain.get("volatility", 0)
                threshold = max(0.5, 5 - risk_ctrl.get("level", 0) * 0.3)
                if vol is not None and vol > threshold:
                    final_signal = "hold"

        return final_signal, strategy_out

    # --------------------------------------------------------
    # Phase 3: Intent Building
    # --------------------------------------------------------
    def _build_intent(
        self,
        context: HyperEngineContext,
        price: float,
        params: Dict[str, Any],
        ai: Dict[str, Any],
        signal: str,
        strategy_out: Optional[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], float]:
        """주문 의도(Intent) 생성."""
        intent = None
        profit = 0.0
        
        is_live = str(getattr(context, "trading_mode", "")).upper() == "LIVE"
        has_position = bool(context.position)
        is_paper_position = has_position and context.position.get("source") == "paper"

        # TP/SL 판단
        should_sell, tp_hit, sl_hit, change_pct = self._check_tp_sl(context, price, params, strategy_out)

        # [FIX 2026-01-28] user_sell_only / hold_sell (GAZUA LOCK/HOLD)
        # 실제 설정값은 context.controls.strategy.params에 저장됨
        strategy_params = {}
        try:
            controls = getattr(context, "controls", None)
            if isinstance(controls, dict):
                strategy_ctrl = controls.get("strategy", {})
                if isinstance(strategy_ctrl, dict):
                    strategy_params = strategy_ctrl.get("params", {}) or {}
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[Engine] controls.strategy.params 읽기 실패: %s", exc)
        
        # user_sell_only (LOCK): 모든 자동 매도 비활성화
        user_sell_only = bool(strategy_params.get("user_sell_only", False)) or bool(params.get("user_sell_only", False))
        # hold_sell (HOLD): TP 자동 매도만 비활성화 (SL은 작동)
        hold_sell = bool(strategy_params.get("hold_sell", False)) or bool(params.get("hold_sell", False))
        
        # 2026-01-30: SL Grace Period - 매수 후 5분간 SL 비활성화 (사자마자 손절 방지)
        # [FIX 2026-02-19] position["entry_ts"]만 사용 (position["ts"]는 매도 시에도 갱신되므로 위험)
        sl_grace_sec = float(strategy_params.get("sl_grace_sec", 300.0))  # 기본 5분
        in_grace_period = False
        if has_position and sl_hit and sl_grace_sec > 0:
            try:
                import time
                entry_ts = context.position.get("entry_ts") or 0
                if entry_ts > 0:
                    elapsed = time.time() - float(entry_ts)
                    if elapsed < sl_grace_sec:
                        in_grace_period = True
                        # Grace Period 동안 SL 비활성화
                        sl_hit = False
                        should_sell = tp_hit  # TP는 허용
            except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                logger.warning("[Engine] Grace Period SL 비활성화 처리 실패: %s", exc)
        
        if user_sell_only and should_sell and not hold_sell:
            # LOCK: 자동 TP/SL 매도 완전 차단
            should_sell = False
            tp_hit = False
            sl_hit = False
        elif hold_sell and tp_hit and not sl_hit:
            # HOLD: TP만 차단, SL은 허용
            should_sell = False
            tp_hit = False

        # [FIX 2026-03-05] SNIPER sl_confirm 진행 중 → 엔진 즉시 SL 억제
        # 플러그인이 "sniper:sl_confirming" reason으로 hold를 반환한 경우, 엔진 레벨 SL을 무시한다.
        # 이를 통해 SniperPlugin의 sl_confirm_ticks(기본 3틱) noise defense가 실제 동작하게 된다.
        # [2026-03-14] LongHold 전환/활성 중에도 엔진 TP/SL 억제 (꺼내주기 전에는 매도 금지)
        # [2026-03-18] 플러그인 trailing 중 엔진 TP 매도 억제
        #   플러그인이 arm_trail/trailing/trailing_active 등으로 hold를 반환하면
        #   엔진이 독자적으로 tp_hit 매도하면 플러그인의 trailing stop이 무시됨
        if (sl_hit or tp_hit) and signal == "hold" and strategy_out:
            _sl_reason = str(strategy_out.get("reason") or "")
            if "sl_confirming" in _sl_reason:
                sl_hit = False
                should_sell = False
            elif "longhold" in _sl_reason:
                sl_hit = False
                tp_hit = False
                should_sell = False
            elif tp_hit and any(k in _sl_reason for k in ("trailing", "arm_trail", "trail_armed")):
                tp_hit = False
                should_sell = bool(sl_hit)

        # [FIX 2026-03-15] 플러그인이 hold를 반환했으면 엔진 독자 SL 매도 억제
        # 플러그인이 sl_confirming, longhold_active 등으로 hold를 반환한 경우
        # 엔진이 독자적으로 SL 매도하면 플러그인의 DCA/LongHold 전환이 무시됨
        if signal == "hold" and has_position and sl_hit:
            _hold_reason = str((strategy_out or {}).get("reason") or "")
            # sl_confirming, longhold, hold_active 등 보호 reason이면 억제
            if any(k in _hold_reason for k in ("sl_confirming", "longhold", "hold_active", "dca")):
                sl_hit = False
                should_sell = False
        if has_position and sl_hit and not in_grace_period:
            _sl_lh_default = bool(os.environ.get("OMA_SL_TO_LONGHOLD", "1") in ("1", "true", "True", "yes", "on"))
            sl_to_longhold = bool(strategy_params.get("sl_to_longhold", _sl_lh_default)) or bool(params.get("sl_to_longhold", False))
            if sl_to_longhold:
                intent = {
                    "action": "convert_to_longhold",
                    "reason": "sl_to_longhold",
                    "meta": {
                        "exit_kind": "sl",
                        "change_pct": float(change_pct) if change_pct is not None else None,
                        "sl": float(params.get("sl", -2.5)),
                        "tp": float(params.get("tp", 1.0)),
                        "original_strategy": str(params.get("strategy_name", "")),
                    },
                }
            else:
                intent = self._build_sell_intent(context, price, params, strategy_out, has_position, tp_hit, sl_hit, change_pct, should_sell)
        elif signal == "buy":
            intent = self._build_buy_intent(context, price, params, signal, strategy_out, has_position, is_live, is_paper_position)
        elif signal == "sell" or should_sell:
            intent = self._build_sell_intent(context, price, params, strategy_out, has_position, tp_hit, sl_hit, change_pct, should_sell)
        elif signal == "reserve" and strategy_out:
            so_meta = strategy_out.get("meta") or {}
            step_price = float(so_meta.get("step_price", price))
            step_budget = float(so_meta.get("amount", 0) or so_meta.get("step_budget", 0))
            intent = {
                "type": "reserve",
                "side": str(so_meta.get("side", "buy")),
                "price": step_price,
                "amount": step_budget,
                "fallback_to_market": bool(so_meta.get("fallback_to_market", True)),
                "meta": so_meta,
            }

        return intent, profit

    def _check_tp_sl(
        self,
        context: HyperEngineContext,
        price: float,
        params: Dict[str, Any],
        strategy_out: Optional[Dict[str, Any]],
    ) -> Tuple[bool, bool, bool, Optional[float]]:
        """TP/SL 히트 여부 확인."""
        should_sell = False
        tp_hit = False
        sl_hit = False
        change_pct: Optional[float] = None

        if not context.position:
            return should_sell, tp_hit, sl_hit, change_pct

        entry = float(context.position.get("entry") or 0.0)
        if entry <= 0:
            return should_sell, tp_hit, sl_hit, change_pct

        change_pct = (price - entry) / entry * 100.0
        tp = float(params.get("tp", 1.2))
        # 2026-01-30: 기본 SL -1.0% → -2.5%로 완화 (사자마자 손절 방지)
        sl = float(params.get("sl", -2.5))

        # [FIX 2026-02-01] 전략 플러그인의 params에서 sl/tp 우선 적용
        # 이전에는 엔진 기본값(-2.5%)만 사용되어 GAZUA(-50%)가 무시됨
        if isinstance(strategy_out, dict):
            meta = strategy_out.get("meta") or {}
            # 전략 meta에서 sl_pct/tp_pct 확인 (전략 플러그인이 설정한 값)
            if meta.get("sl_pct") is not None:
                try:
                    sl = float(meta["sl_pct"])
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[TP/SL] sl_pct 파싱 실패: %s → 엔진 기본값 %.1f%% 유지", meta.get("sl_pct"), sl)
            if meta.get("tp_pct") is not None:
                try:
                    tp = float(meta["tp_pct"])
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[TP/SL] tp_pct 파싱 실패: %s → 엔진 기본값 %.1f%% 유지", meta.get("tp_pct"), tp)
            # dynamic_sl/dynamic_tp (ATR 기반)이 있으면 최종 덮어씀
            # GAZUA는 장기보유 전략이므로 ATR dynamic_sl 적용 제외 (-25% SL 유지)
            _strat_name = str(strategy_out.get("mode") or strategy_out.get("strategy") or "").upper()
            d_sl = meta.get("dynamic_sl")
            d_tp = meta.get("dynamic_tp")
            if d_sl is not None and _strat_name != "GAZUA":
                try:
                    sl = float(d_sl)
                except (TypeError, ValueError):
                    logger.warning("[TP/SL] dynamic_sl 파싱 실패: %s → 이전 SL %.1f%% 유지", d_sl, sl)
            if d_tp is not None:
                try:
                    tp = float(d_tp)
                except (TypeError, ValueError):
                    logger.warning("[TP/SL] dynamic_tp 파싱 실패: %s → 이전 TP %.1f%% 유지", d_tp, tp)

        # SL은 항상 음수 손익 임계값으로 비교한다.
        # (예: sl_pct=2.0 이 들어와도 -2.0%로 보정)
        if sl > 0:
            sl = -abs(sl)
        if tp < 0:
            tp = abs(tp)

        strategy_mode = self._strategy_mode_from_context(context)
        tp, sl, policy_meta = self._apply_tp_sl_policy(context, strategy_mode, tp, sl)

        # [2026-03-30] 레짐 TP/SL multiplier 적용 (메모리 캐시, HTTP 없음)
        try:
            _policy = getattr(self, "_tp_sl_policy_cache", None)
            if _policy is None:
                import json as _json
                _path = "runtime/strategy_tp_sl_policy.json"
                with open(_path, "r", encoding="utf-8") as _f:
                    _policy = _json.load(_f)
                self._tp_sl_policy_cache = _policy
            if _policy.get("enabled"):
                from app.core.market_regime import RegimeDetector
                _det = getattr(self, "_regime_detector", None)
                if _det is None:
                    _det = RegimeDetector()
                    self._regime_detector = _det
                _regime = str(_det.detect("BTCUSDT") or "SIDEWAYS").upper()
                _tp_mults = _policy.get("regime_tp_multiplier", {})
                _sl_mults = _policy.get("regime_sl_multiplier", {})
                tp *= float(_tp_mults.get(_regime, 1.0))
                sl *= float(_sl_mults.get(_regime, 1.0))
                policy_meta["regime"] = _regime
                policy_meta["regime_tp_mult"] = float(_tp_mults.get(_regime, 1.0))

                # [2026-03-30] 수수료 인식 TP: 스프레드 반영 (orderbook_store 메모리 조회)
                if _policy.get("fee_aware_tp"):
                    _fee = float(_policy.get("fee_rate", 0.001))
                    _spread_bps = 0.0
                    try:
                        from app.core.hyper_price_store import orderbook_store
                        _ob = orderbook_store.get(str(getattr(context, "market", "")))
                        if _ob:
                            _bid = float(_ob.get("best_bid", 0) or 0)
                            _ask = float(_ob.get("best_ask", 0) or 0)
                            if _bid > 0 and _ask > 0:
                                _spread_bps = (_ask - _bid) / _bid * 10000
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[Engine] 수수료 인식 TP 스프레드 조회 실패: %s", exc)
                    _fee_cost_pct = (_fee * 2 + _spread_bps / 10000) * 100  # 왕복 수수료 + 스프레드
                    if tp > 0 and tp < _fee_cost_pct * 1.2:
                        tp = _fee_cost_pct * 1.2  # 수수료 이하로 TP 설정 방지
                    policy_meta["fee_aware_tp_floor"] = round(_fee_cost_pct * 1.2, 4)
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] 수수료 인식 TP 정책 로드 실패: %s", exc)

        if isinstance(strategy_out, dict):
            out_meta = strategy_out.get("meta")
            if not isinstance(out_meta, dict):
                out_meta = {}
            out_meta.update(policy_meta)
            strategy_out["meta"] = out_meta

        tp_hit = bool(change_pct >= tp)
        sl_hit = bool(change_pct <= sl)

        # [2026-03-30] TP 모멘텀 체크: 상승 중이면 매도 보류 (2×TP 초과 시 무조건 매도)
        if tp_hit and not sl_hit:
            _prices = getattr(context, "_tick_prices", None) or \
                      list(getattr(context, "price_history", []) or [])
            if len(_prices) >= 4:
                try:
                    _p3 = [float(x) for x in _prices[-3:]]
                    if _p3[0] < _p3[1] < _p3[2] and _p3[2] <= price:
                        # 연속 상승 중 — TP 매도 보류 (2×TP 미만일 때만)
                        if change_pct < tp * 2.0:
                            tp_hit = False
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    logger.warning("[Engine] 연속 상승 TP 보류 판단 실패: %s", exc)

        should_sell = bool(tp_hit or sl_hit)

        return should_sell, tp_hit, sl_hit, change_pct

    def _build_buy_intent(
        self,
        context: HyperEngineContext,
        price: float,
        params: Dict[str, Any],
        signal: str,
        strategy_out: Optional[Dict[str, Any]],
        has_position: bool,
        is_live: bool,
        is_paper_position: bool,
    ) -> Optional[Dict[str, Any]]:
        """BUY 의도 생성."""
        # 자본 계산
        cap_alloc = float(getattr(context, "allocated_capital", 0.0) or 0.0)
        cap_usable = getattr(context, "usable_capital", None)
        try:
            cap_usable_f = float(cap_usable) if cap_usable is not None else None
        except (TypeError, ValueError):
            logger.warning("[Engine] usable_capital float conversion failed: %r", cap_usable, exc_info=True)
            cap_usable_f = None

        usdt_raw = min(cap_alloc, cap_usable_f) if cap_usable_f is not None else cap_alloc
        usdt = int(usdt_raw)

        s_ctrl = (getattr(context, "controls", {}) or {}).get("strategy", {}) or {}
        mode = str(s_ctrl.get("mode") or s_ctrl.get("name") or "").upper()

        # 전략별 사이즈 스케일링
        skip_global_size_scale = False
        if isinstance(strategy_out, dict):
            meta = strategy_out.get("meta") or {}
            # GAZUA 추가매수는 전용 핸들러에서 size_scale을 적용하므로
            # 여기서 또 곱하면 이중 스케일링이 된다.
            if has_position and mode == "GAZUA" and bool(meta.get("allow_add_buy", False)):
                skip_global_size_scale = True
            scale = meta.get("size_scale")
            if scale is not None and not skip_global_size_scale:
                usdt = int(usdt * float(scale))

        allow_buy = False
        buy_reason = "engine_buy"

        # AUTOLOOP 분할매수 로직
        allow_buy, usdt, buy_reason = self._handle_autoloop_buy(context, price, usdt, signal, strategy_out, has_position, allow_buy, buy_reason)
        # GAZUA 2단 진입(탐색/확인) 추가매수 로직
        allow_buy, usdt, buy_reason = self._handle_gazua_buy(context, price, usdt, signal, strategy_out, has_position, allow_buy, buy_reason)
        # SNIPER/LIGHTNING probe→confirm 추가매수 로직
        allow_buy, usdt, buy_reason = self._handle_staged_probe_confirm_buy(
            context, usdt, signal, strategy_out, has_position, allow_buy, buy_reason
        )

        # 최종 BUY intent 생성
        if usdt > 0 and (not has_position or (is_live and is_paper_position) or allow_buy):
            intent: Dict[str, Any] = {
                "action": "buy",
                "buy_usdt": usdt,
                "reason": buy_reason
            }
            # 보유 중 추가매수는 시스템 레이어에서 별도 화이트리스트 검증 후 허용한다.
            if has_position and allow_buy:
                intent["meta"] = {"allow_add_buy": True}
            return intent

        return None

    def _handle_autoloop_buy(
        self,
        context: HyperEngineContext,
        price: float,
        usdt: int,
        signal: str,
        strategy_out: Optional[Dict[str, Any]],
        has_position: bool,
        allow_buy: bool,
        buy_reason: str,
    ) -> Tuple[bool, int, str]:
        """AUTOLOOP 분할매수 처리."""
        try:
            s_ctrl = (getattr(context, "controls", {}) or {}).get("strategy", {}) or {}
            s_params = s_ctrl.get("params", {}) or {}
            mode = str(s_ctrl.get("mode") or s_ctrl.get("name") or "").upper()

            if mode != "AUTOLOOP":
                return allow_buy, usdt, buy_reason

            buy_splits = s_params.get("buy_splits") or [1.0]

            # [⑤] 안티마틴게일: DCA 횟수↑ → 추가 매수 규모↓ (마틴게일 반대)
            # 트리아지 DCA("triage" in buy_reason)는 복구 목적이므로 적용 제외
            _anti_enabled = bool(s_params.get("anti_martingale_enabled",
                str(os.getenv("OMA_ANTI_MARTINGALE_ENABLED", "false")).lower() in ("1", "true", "yes")))
            if _anti_enabled and "triage" not in str(buy_reason).lower():
                _decay = float(s_params.get("anti_martingale_decay", os.getenv("OMA_ANTI_MARTINGALE_DECAY", "0.7")))
                _floor = float(s_params.get("anti_martingale_floor", os.getenv("OMA_ANTI_MARTINGALE_FLOOR", "0.3")))
                _dca_n = max(0, int(context.get_var("autoloop_entry_stage", 0)) - 1)  # 0=첫DCA, 1=두번째...
                _adj = max(_floor, _decay ** _dca_n)   # 1→0.7→0.49→floor
                buy_splits = [float(x) * _adj for x in buy_splits]

            add_trigs = s_params.get("add_buy_drop_pcts") or []
            stage_max = int(s_params.get("entry_stage_max") or len(buy_splits) or 1)

            stage = int(context.get_var("autoloop_entry_stage", 0))
            stage = max(0, min(stage, stage_max - 1))

            now_ts = time.time()
            last_add = float(context.get_var("autoloop_last_add_ts", 0.0))
            add_cooldown = float(s_params.get("add_buy_cooldown_sec", 60.0))
            can_add = (now_ts - last_add) >= add_cooldown

            if not has_position:
                if signal == "buy":
                    allow_buy = True
                    context.set_var("autoloop_entry_ref", float(price))
            else:
                if stage >= 1 and stage < len(buy_splits) and can_add:
                    entry = float(context.position.get("entry") or 0.0)
                    dd_pct = (price / entry - 1.0) * 100.0 if entry > 0 else 0.0

                    idx = stage - 1
                    trig = float(add_trigs[idx]) if idx < len(add_trigs) else None

                    meta = (strategy_out or {}).get("meta") if isinstance(strategy_out, dict) else {}
                    rsi = meta.get("rsi")
                    hist = meta.get("macd_hist")
                    hist_prev = meta.get("macd_hist_prev")
                    rsi_buy = float(s_params.get("rsi_buy", 28.0))

                    macd_turn_up = (hist is not None and hist_prev is not None and float(hist) > float(hist_prev))
                    rsi_ok = (rsi is not None and float(rsi) <= (rsi_buy + 2.0))
                    dd_ok = (trig is not None and dd_pct <= trig)

                    if dd_ok and macd_turn_up and rsi_ok:
                        allow_buy = True

            if allow_buy:
                f = float(buy_splits[stage]) if stage < len(buy_splits) else 1.0
                f = max(0.05, min(1.0, f))
                usdt = int(usdt * f)

                if has_position:
                    buy_reason = f"autoloop:add_buy:stage_{stage}"

                context.set_var("autoloop_entry_stage", min(stage + 1, stage_max - 1))
                context.set_var("autoloop_last_add_ts", time.time())

        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
            logger.warning("[Engine] AUTOLOOP 분할매수 처리 실패: %s", getattr(context, "market", "?"), exc_info=True)

        return allow_buy, usdt, buy_reason

    def _handle_gazua_buy(
        self,
        context: HyperEngineContext,
        price: float,
        usdt: int,
        signal: str,
        strategy_out: Optional[Dict[str, Any]],
        has_position: bool,
        allow_buy: bool,
        buy_reason: str,
    ) -> Tuple[bool, int, str]:
        """GAZUA 2단 진입(확인 추가매수) 처리."""
        try:
            s_ctrl = (getattr(context, "controls", {}) or {}).get("strategy", {}) or {}
            s_params = s_ctrl.get("params", {}) or {}
            mode = str(s_ctrl.get("mode") or s_ctrl.get("name") or "").upper()

            if mode != "GAZUA":
                return allow_buy, usdt, buy_reason
            if signal != "buy":
                return allow_buy, usdt, buy_reason

            # 신규 포지션 진입은 일반 경로에서 처리; 여기서는 보유 중 추가매수만 허용.
            if not has_position:
                return allow_buy, usdt, buy_reason

            meta = (strategy_out or {}).get("meta") if isinstance(strategy_out, dict) else {}
            if not isinstance(meta, dict) or not bool(meta.get("allow_add_buy", False)):
                return allow_buy, usdt, buy_reason

            now_ts = time.time()
            last_add = float(context.get_var("gazua_last_add_ts", 0.0) or 0.0)
            add_cooldown = float(s_params.get("add_buy_cooldown_sec", 180.0) or 180.0)
            if (now_ts - last_add) < add_cooldown:
                return allow_buy, usdt, buy_reason

            scale = meta.get("size_scale")
            if scale is not None:
                scale_f = max(0.05, min(1.0, float(scale)))
                usdt = int(usdt * scale_f)

            if usdt <= 0:
                return allow_buy, usdt, buy_reason

            context.set_var("gazua_last_add_ts", now_ts)
            allow_buy = True
            buy_reason = str(meta.get("buy_reason") or "gazua:add_buy")

            # V2 DCA 평단가 재계산 힌트 (실행 레이어에서 사용)
            if "dca" in buy_reason:
                try:
                    entry_price = float(context.position.get("entry", 0.0))
                    old_qty = float(context.position.get("qty", 0.0))
                    new_qty = usdt / price if price > 0 else 0
                    if old_qty + new_qty > 0 and entry_price > 0:
                        avg_price = (old_qty * entry_price + new_qty * price) / (old_qty + new_qty)
                        meta["avg_entry_price"] = avg_price
                        meta["dca_stage"] = 2
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[Engine] GAZUA DCA 평단가 재계산 실패: %s", getattr(context, "market", "?"), exc_info=True)

        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
            logger.warning("[Engine] GAZUA 추가매수 처리 실패: %s", getattr(context, "market", "?"), exc_info=True)

        return allow_buy, usdt, buy_reason

    def _handle_staged_probe_confirm_buy(
        self,
        context: HyperEngineContext,
        usdt: int,
        signal: str,
        strategy_out: Optional[Dict[str, Any]],
        has_position: bool,
        allow_buy: bool,
        buy_reason: str,
    ) -> Tuple[bool, int, str]:
        """SNIPER/LIGHTNING의 probe->confirm 2단 진입을 실행 레이어에 연결."""
        try:
            if signal != "buy":
                return allow_buy, usdt, buy_reason

            s_ctrl = (getattr(context, "controls", {}) or {}).get("strategy", {}) or {}
            s_params = s_ctrl.get("params", {}) or {}
            mode = str(s_ctrl.get("mode") or s_ctrl.get("name") or "").upper()
            if mode not in ("SNIPER", "SNIPER(S)", "LIGHTNING"):
                return allow_buy, usdt, buy_reason

            meta = (strategy_out or {}).get("meta") if isinstance(strategy_out, dict) else {}
            if not isinstance(meta, dict):
                return allow_buy, usdt, buy_reason

            # 신규 probe 진입: size_scale이 없다면 probe_ratio를 보조 사용.
            if not has_position:
                if meta.get("size_scale") is None:
                    probe_ratio = meta.get("probe_ratio")
                    if probe_ratio is not None:
                        probe_f = max(0.05, min(1.0, float(probe_ratio)))
                        usdt = int(usdt * probe_f)
                allow_buy = True
                buy_reason = str((strategy_out or {}).get("reason") or buy_reason)
                return allow_buy, usdt, buy_reason

            # 보유 중 confirm 추가매수: 전략 meta의 allow_add_buy 신호가 있어야 한다.
            if not bool(meta.get("allow_add_buy", False)):
                return allow_buy, usdt, buy_reason

            mode_key = "".join(ch.lower() for ch in mode if ch.isalnum()) or "staged"
            last_add_key = f"{mode_key}_last_add_ts"
            now_ts = time.time()
            last_add = float(context.get_var(last_add_key, 0.0) or 0.0)
            add_cooldown = float(s_params.get("add_buy_cooldown_sec", 60.0) or 60.0)
            if (now_ts - last_add) < add_cooldown:
                return allow_buy, usdt, buy_reason

            # size_scale은 _build_buy_intent 상단에서 이미 반영됨.
            # 없을 때만 confirm_buy_ratio를 보조 적용.
            if meta.get("size_scale") is None:
                confirm_ratio = meta.get("confirm_buy_ratio")
                if confirm_ratio is not None:
                    confirm_f = max(0.05, min(1.0, float(confirm_ratio)))
                    usdt = int(usdt * confirm_f)

            if usdt <= 0:
                return allow_buy, usdt, buy_reason

            context.set_var(last_add_key, now_ts)
            allow_buy = True
            buy_reason = str(meta.get("buy_reason") or (strategy_out or {}).get("reason") or buy_reason)
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
            logger.warning("[Engine] %s probe/confirm 매수 처리 실패: %s", mode if 'mode' in dir() else "STAGED", getattr(context, "market", "?"), exc_info=True)

        return allow_buy, usdt, buy_reason

    def _build_sell_intent(
        self,
        context: HyperEngineContext,
        price: float,
        params: Dict[str, Any],
        strategy_out: Optional[Dict[str, Any]],
        has_position: bool,
        tp_hit: bool,
        sl_hit: bool,
        change_pct: Optional[float],
        should_sell: bool,
    ) -> Optional[Dict[str, Any]]:
        """SELL 의도 생성."""
        if not has_position:
            return None

        qty = float(context.position.get("qty") or 0.0)
        
        # AUTOLOOP reset 처리
        reset_staged_entry = self._should_reset_autoloop(context, should_sell)

        # GAZUA V2 다단계 부분매도 처리
        gazua_partial = False
        so_reason = ""
        so_meta: Dict[str, Any] = {}
        if isinstance(strategy_out, dict):
            so_reason = str(strategy_out.get("reason", ""))
            so_meta = strategy_out.get("meta") or {}
            if not isinstance(so_meta, dict):
                so_meta = {}

        if "gazua_partial" in so_reason:
            sell_fraction = float(so_meta.get("sell_fraction", 0.3))
            target_stage = int(so_meta.get("stage", 1))
            qty = qty * max(0.05, min(1.0, sell_fraction))
            gazua_partial = True
            try:
                context.set_var("gazua_partial_stage", target_stage)
                context.set_var("gazua_partial_sold", True)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.error("[Engine] GAZUA V2 다단계 부분매도 상태 저장 실패: %s", exc)
        else:
            # 부분매도 처리 (AUTOLOOP/GAZUA generic)
            qty = self._apply_sell_fraction(context, qty)

        if qty <= 0:
            return None

        # Exit kind 분류
        exit_kind, pp_exit_meta = self._classify_exit_kind(strategy_out, sl_hit, tp_hit)
        if gazua_partial:
            exit_kind = "gazua_partial"

        intent = {
            "action": "sell",
            "sell_qty": qty,
            "reason": f"engine_sell:{exit_kind}",
            "force_exit": bool(sl_hit) or bool(exit_kind in ("pp_trail", "pp_dampen")),
            "meta": {
                "exit_kind": exit_kind,
                "change_pct": float(change_pct) if change_pct is not None else None,
                "tp": float(params.get("tp", 1.0)),
                "sl": float(params.get("sl", -1.0)),
                "pp_exit": pp_exit_meta if isinstance(pp_exit_meta, dict) else None,
            },
        }

        if gazua_partial:
            intent["partial_sell"] = True
            intent["meta"]["sell_fraction"] = float(so_meta.get("sell_fraction", 0.3))
            intent["meta"]["stage"] = int(so_meta.get("stage", 1))

        # AUTOLOOP 상태 리셋
        if reset_staged_entry:
            self._reset_autoloop_state(context)

        return intent

    def _should_reset_autoloop(self, context: HyperEngineContext, should_sell: bool) -> bool:
        """AUTOLOOP 상태 리셋 필요 여부."""
        try:
            s_ctrl = (getattr(context, "controls", {}) or {}).get("strategy", {}) or {}
            mode = str(s_ctrl.get("mode") or s_ctrl.get("name") or "").upper()
            if mode == "AUTOLOOP" and should_sell:
                return True
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] _should_reset_autoloop 판단 실패: %s", exc)
        return False

    def _apply_sell_fraction(self, context: HyperEngineContext, qty: float) -> float:
        """부분매도 비율 적용 (AUTOLOOP/GAZUA)."""
        try:
            s_ctrl = (getattr(context, "controls", {}) or {}).get("strategy", {}) or {}
            s_params = s_ctrl.get("params", {}) or {}
            mode = str(s_ctrl.get("mode") or s_ctrl.get("name") or "").upper()
            if mode in ("AUTOLOOP", "GAZUA"):
                f = float(s_params.get("sell_fraction", 1.0))
                if 0.05 <= f <= 1.0:
                    qty = qty * f
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] _apply_sell_fraction 처리 실패: %s", exc)
        return qty

    def _classify_exit_kind(
        self, strategy_out: Optional[Dict[str, Any]], sl_hit: bool, tp_hit: bool
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Exit kind 분류."""
        exit_kind = "signal"
        pp_exit_meta = None

        try:
            if isinstance(strategy_out, dict):
                mode_s = str(strategy_out.get("mode") or "").upper()
                if mode_s == "PINGPONG":
                    meta_s = strategy_out.get("meta") or {}
                    lv = meta_s.get("levels") if isinstance(meta_s, dict) else None
                    ex = lv.get("exit") if isinstance(lv, dict) else None
                    if isinstance(ex, dict) and bool(ex.get("triggered")):
                        pp_exit_meta = ex
                        m0 = str(ex.get("mode") or "").upper()
                        if m0 == "TRAIL":
                            exit_kind = "pp_trail"
                        elif m0 == "DAMPEN":
                            exit_kind = "pp_dampen"
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] _classify_exit_kind 분류 실패: %s", exc)

        if sl_hit:
            exit_kind = "sl"
        elif tp_hit:
            exit_kind = "tp"

        return exit_kind, pp_exit_meta

    def _reset_autoloop_state(self, context: HyperEngineContext):
        """AUTOLOOP 상태 리셋."""
        try:
            context.set_var("autoloop_entry_stage", 0)
            context.set_var("autoloop_last_add_ts", 0.0)
            context.set_var("autoloop_entry_ref", 0.0)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] _reset_autoloop_state 리셋 실패: %s", exc)

    # --------------------------------------------------------
    # Phase 4: Diagnostics
    # --------------------------------------------------------
    def _attach_diagnostics(
        self, context: HyperEngineContext, ai: Dict[str, Any], strategy_out: Optional[Dict[str, Any]]
    ):
        """진단 정보를 context에 부착."""
        try:
            sr = context.strategy_reason if isinstance(getattr(context, "strategy_reason", None), dict) else {}
            sr = dict(sr)
            sr["engine_ai"] = ai.get("brain", {})
            sr["engine_scores"] = ai.get("scores", {})

            if strategy_out is not None:
                sr["strategy_out"] = strategy_out

            p = context.policy.get("params", {})
            if "base_size_scale" in p:
                sr["base_size_scale"] = float(p["base_size_scale"])

            context.strategy_reason = sr

            if isinstance(getattr(context, "strategy_state", None), dict):
                rsn = context.strategy_state.get("reason")
                if not isinstance(rsn, dict):
                    rsn = {}
                rsn.update(sr)
                context.strategy_state["reason"] = rsn
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] _attach_diagnostics 진단 첨부 실패: %s", exc)

    # --------------------------------------------------------
    # Phase 5: Finalize
    # --------------------------------------------------------
    def _finalize_tick(self, context: HyperEngineContext, params: Dict[str, Any], signal: str, price: float):
        """Tick 마무리: 정책 업데이트 및 상태 저장."""
        # AI 기반 추가 리스크 처리 (placeholder)
        # 향후 변동성/모멘텀 기반 로직 확장 가능

        # 정책 자동 개선
        context.update_policy({
            "name": context.policy["name"],
            "params": {**params}
        })

        # Tick 후 상태 finalize
        context.finalize_tick(signal, price)
