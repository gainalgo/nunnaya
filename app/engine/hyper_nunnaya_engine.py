# ============================================================
# File: app/engine/hyper_nunnaya_engine.py
# Autocoin OS v3-H — Hyper Nunnaya Engine (Final AI Edition)
# NOTE:
# This engine acts only as a 'decision maker'.
# Trade frequency depends strongly on StrategyPipeline / Risk / Allocation settings.
# Zero trades may be a design outcome rather than a bug.
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
    Autocoin OS v3-H final engine.
    - Single engine
    - Based on AI StrategyPipeline
    - Market-adaptive risk control
    - Automatic policy optimization
    - Full Context(State Machine) integration
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
        # GAZUA is a long-hold strategy — do not apply time_relax SL reduction
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
        Public tick method called by TickLoop / Coordinator.
        """
        context = self._resolve_context(market, args, kwargs)
        return self._tick_impl(market, price, context)

    def _resolve_context(self, market: str, args: tuple, kwargs: dict) -> HyperEngineContext:
        """Resolve and create the context."""
        context: Optional[HyperEngineContext] = None

        # 1) Find context in args
        if args and isinstance(args[0], HyperEngineContext):
            context = args[0]

        # 2) Find context in kwargs
        if context is None:
            ctx_kw = kwargs.get("context")
            if isinstance(ctx_kw, HyperEngineContext):
                context = ctx_kw

        # 3) Get context from base
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

        # 4) Last resort: create a new one
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
    # Initial policy setup
    # --------------------------------------------------------
    def on_initialize(self, context: HyperEngineContext):
        """Called when a context is created per market. Sets the initial policy (Preset)."""
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
    # v3-H core tick implementation (refactored)
    # --------------------------------------------------------
    def _tick_impl(self, market: str, price: float, context: HyperEngineContext) -> Dict[str, Any]:
        """
        Actual engine logic of HyperNunnayaEngine.
        Brain → Judge → Risk → Optimizer → Fusion → Position Logic
        """
        # 0) Initialize
        if not context.policy:
            self.on_initialize(context)

        params = context.policy.get("params", {})

        # 1) Run AI pipeline
        ai = self.pipeline.run(market=market, price=price, context=context)
        context.current_ai = ai

        # 2) Signal resolution (Arbiter)
        signal, strategy_out = self._resolve_signal(context, price, ai)

        # 3) Build intent
        intent, profit = self._build_intent(context, price, params, ai, signal, strategy_out)

        # 4) Attach diagnostics
        self._attach_diagnostics(context, ai, strategy_out)

        # 5) Policy update and finalize
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
        Signal resolution logic.
        Priority: BASELINE > STRATEGY > AI > hold
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

        # 0) Run STRATEGY plugin
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

        # 1) BASELINE (highest priority: for manual forced entry)
        if base.get("enabled") and context.position is None and base.get("level", 0) >= 1:
            final_signal = "buy"
        else:
            # 2) Select the default signal source
            if isinstance(strategy_ctrl, dict) and strategy_ctrl.get("enabled") and strategy_signal in ("buy", "sell", "hold", "reserve"):
                final_signal = str(strategy_signal)
            elif ai_ctrl.get("enabled"):
                final_signal = ai_signal
            else:
                final_signal = "hold"

            # 3) RISK block (does not block sell)
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
        """Build the order intent."""
        intent = None
        profit = 0.0

        is_live = str(getattr(context, "trading_mode", "")).upper() == "LIVE"
        has_position = bool(context.position)
        is_paper_position = has_position and context.position.get("source") == "paper"

        # TP/SL decision
        should_sell, tp_hit, sl_hit, change_pct = self._check_tp_sl(context, price, params, strategy_out)

        # [FIX 2026-01-28] user_sell_only / hold_sell (GAZUA LOCK/HOLD)
        # The actual setting values are stored in context.controls.strategy.params
        strategy_params = {}
        try:
            controls = getattr(context, "controls", None)
            if isinstance(controls, dict):
                strategy_ctrl = controls.get("strategy", {})
                if isinstance(strategy_ctrl, dict):
                    strategy_params = strategy_ctrl.get("params", {}) or {}
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[Engine] failed to read controls.strategy.params: %s", exc)

        # user_sell_only (LOCK): disable all automatic selling
        user_sell_only = bool(strategy_params.get("user_sell_only", False)) or bool(params.get("user_sell_only", False))
        # hold_sell (HOLD): disable only TP auto-sell (SL still works)
        hold_sell = bool(strategy_params.get("hold_sell", False)) or bool(params.get("hold_sell", False))

        # 2026-01-30: SL Grace Period - disable SL for 5 min after buy (prevent instant stop-loss)
        # [FIX 2026-02-19] use only position["entry_ts"] (position["ts"] is also updated on sell, so it is risky)
        sl_grace_sec = float(strategy_params.get("sl_grace_sec", 300.0))  # default 5 min
        in_grace_period = False
        if has_position and sl_hit and sl_grace_sec > 0:
            try:
                import time
                entry_ts = context.position.get("entry_ts") or 0
                if entry_ts > 0:
                    elapsed = time.time() - float(entry_ts)
                    if elapsed < sl_grace_sec:
                        in_grace_period = True
                        # Disable SL during the Grace Period
                        sl_hit = False
                        should_sell = tp_hit  # TP is allowed
            except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                logger.warning("[Engine] Grace Period SL disable handling failed: %s", exc)

        if user_sell_only and should_sell and not hold_sell:
            # LOCK: fully block automatic TP/SL selling
            should_sell = False
            tp_hit = False
            sl_hit = False
        elif hold_sell and tp_hit and not sl_hit:
            # HOLD: block only TP, allow SL
            should_sell = False
            tp_hit = False

        # [FIX 2026-03-05] SNIPER sl_confirm in progress → suppress engine SL immediately
        # When the plugin returns hold with the "sniper:sl_confirming" reason, ignore engine-level SL.
        # This lets SniperPlugin's sl_confirm_ticks (default 3 ticks) noise defense actually work.
        # [2026-03-14] Also suppress engine TP/SL during LongHold transition/active (no sell before release)
        # [2026-03-18] Suppress engine TP sell while the plugin is trailing
        #   If the plugin returns hold via arm_trail/trailing/trailing_active etc.,
        #   the engine selling on tp_hit independently would ignore the plugin's trailing stop
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

        # [FIX 2026-03-15] If the plugin returned hold, suppress the engine's independent SL sell
        # When the plugin returns hold via sl_confirming, longhold_active, etc.,
        # the engine selling on SL independently would ignore the plugin's DCA/LongHold transition
        if signal == "hold" and has_position and sl_hit:
            _hold_reason = str((strategy_out or {}).get("reason") or "")
            # Suppress if it is a protective reason such as sl_confirming, longhold, hold_active
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
        """Check whether TP/SL is hit."""
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
        # 2026-01-30: default SL relaxed from -1.0% → -2.5% (prevent instant stop-loss)
        sl = float(params.get("sl", -2.5))

        # [FIX 2026-02-01] Apply sl/tp from the strategy plugin's params with priority
        # Previously only the engine default (-2.5%) was used, so GAZUA (-50%) was ignored
        if isinstance(strategy_out, dict):
            meta = strategy_out.get("meta") or {}
            # Check sl_pct/tp_pct from strategy meta (values set by the strategy plugin)
            if meta.get("sl_pct") is not None:
                try:
                    sl = float(meta["sl_pct"])
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[TP/SL] sl_pct parse failed: %s → keeping engine default %.1f%%", meta.get("sl_pct"), sl)
            if meta.get("tp_pct") is not None:
                try:
                    tp = float(meta["tp_pct"])
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[TP/SL] tp_pct parse failed: %s → keeping engine default %.1f%%", meta.get("tp_pct"), tp)
            # If dynamic_sl/dynamic_tp (ATR-based) exists, override at the end
            # GAZUA is a long-hold strategy, so exclude ATR dynamic_sl (keep -25% SL)
            _strat_name = str(strategy_out.get("mode") or strategy_out.get("strategy") or "").upper()
            d_sl = meta.get("dynamic_sl")
            d_tp = meta.get("dynamic_tp")
            if d_sl is not None and _strat_name != "GAZUA":
                try:
                    sl = float(d_sl)
                except (TypeError, ValueError):
                    logger.warning("[TP/SL] dynamic_sl parse failed: %s → keeping previous SL %.1f%%", d_sl, sl)
            if d_tp is not None:
                try:
                    tp = float(d_tp)
                except (TypeError, ValueError):
                    logger.warning("[TP/SL] dynamic_tp parse failed: %s → keeping previous TP %.1f%%", d_tp, tp)

        # SL is always compared as a negative PnL threshold.
        # (e.g. even if sl_pct=2.0 comes in, it is corrected to -2.0%)
        if sl > 0:
            sl = -abs(sl)
        if tp < 0:
            tp = abs(tp)

        strategy_mode = self._strategy_mode_from_context(context)
        tp, sl, policy_meta = self._apply_tp_sl_policy(context, strategy_mode, tp, sl)

        # [2026-03-30] Apply regime TP/SL multiplier (memory cache, no HTTP)
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

                # [2026-03-30] Fee-aware TP: reflect spread (orderbook_store memory lookup)
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
                        logger.warning("[Engine] fee-aware TP spread lookup failed: %s", exc)
                    _fee_cost_pct = (_fee * 2 + _spread_bps / 10000) * 100  # round-trip fee + spread
                    if tp > 0 and tp < _fee_cost_pct * 1.2:
                        tp = _fee_cost_pct * 1.2  # prevent setting TP below fee cost
                    policy_meta["fee_aware_tp_floor"] = round(_fee_cost_pct * 1.2, 4)
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] fee-aware TP policy load failed: %s", exc)

        if isinstance(strategy_out, dict):
            out_meta = strategy_out.get("meta")
            if not isinstance(out_meta, dict):
                out_meta = {}
            out_meta.update(policy_meta)
            strategy_out["meta"] = out_meta

        tp_hit = bool(change_pct >= tp)
        sl_hit = bool(change_pct <= sl)

        # [2026-03-30] TP momentum check: defer sell while rising (always sell once above 2×TP)
        if tp_hit and not sl_hit:
            _prices = getattr(context, "_tick_prices", None) or \
                      list(getattr(context, "price_history", []) or [])
            if len(_prices) >= 4:
                try:
                    _p3 = [float(x) for x in _prices[-3:]]
                    if _p3[0] < _p3[1] < _p3[2] and _p3[2] <= price:
                        # Consecutive uptrend — defer TP sell (only when below 2×TP)
                        if change_pct < tp * 2.0:
                            tp_hit = False
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    logger.warning("[Engine] consecutive-uptrend TP defer decision failed: %s", exc)

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
        """Build the BUY intent."""
        # Capital calculation
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

        # Per-strategy size scaling
        skip_global_size_scale = False
        if isinstance(strategy_out, dict):
            meta = strategy_out.get("meta") or {}
            # GAZUA add-buy applies size_scale in its dedicated handler,
            # so multiplying again here would cause double scaling.
            if has_position and mode == "GAZUA" and bool(meta.get("allow_add_buy", False)):
                skip_global_size_scale = True
            scale = meta.get("size_scale")
            if scale is not None and not skip_global_size_scale:
                usdt = int(usdt * float(scale))

        allow_buy = False
        buy_reason = "engine_buy"

        # AUTOLOOP split-buy logic
        allow_buy, usdt, buy_reason = self._handle_autoloop_buy(context, price, usdt, signal, strategy_out, has_position, allow_buy, buy_reason)
        # GAZUA 2-stage entry (probe/confirm) add-buy logic
        allow_buy, usdt, buy_reason = self._handle_gazua_buy(context, price, usdt, signal, strategy_out, has_position, allow_buy, buy_reason)
        # SNIPER/LIGHTNING probe→confirm add-buy logic
        allow_buy, usdt, buy_reason = self._handle_staged_probe_confirm_buy(
            context, usdt, signal, strategy_out, has_position, allow_buy, buy_reason
        )

        # Build the final BUY intent
        if usdt > 0 and (not has_position or (is_live and is_paper_position) or allow_buy):
            intent: Dict[str, Any] = {
                "action": "buy",
                "buy_usdt": usdt,
                "reason": buy_reason
            }
            # Add-buy while holding is allowed only after a separate whitelist check in the system layer.
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
        """Handle AUTOLOOP split-buy."""
        try:
            s_ctrl = (getattr(context, "controls", {}) or {}).get("strategy", {}) or {}
            s_params = s_ctrl.get("params", {}) or {}
            mode = str(s_ctrl.get("mode") or s_ctrl.get("name") or "").upper()

            if mode != "AUTOLOOP":
                return allow_buy, usdt, buy_reason

            buy_splits = s_params.get("buy_splits") or [1.0]

            # [⑤] Anti-martingale: more DCA count → smaller add-buy size (opposite of martingale)
            # Triage DCA ("triage" in buy_reason) is for recovery, so it is excluded
            _anti_enabled = bool(s_params.get("anti_martingale_enabled",
                str(os.getenv("OMA_ANTI_MARTINGALE_ENABLED", "false")).lower() in ("1", "true", "yes")))
            if _anti_enabled and "triage" not in str(buy_reason).lower():
                _decay = float(s_params.get("anti_martingale_decay", os.getenv("OMA_ANTI_MARTINGALE_DECAY", "0.7")))
                _floor = float(s_params.get("anti_martingale_floor", os.getenv("OMA_ANTI_MARTINGALE_FLOOR", "0.3")))
                _dca_n = max(0, int(context.get_var("autoloop_entry_stage", 0)) - 1)  # 0=first DCA, 1=second...
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
            logger.warning("[Engine] AUTOLOOP split-buy handling failed: %s", getattr(context, "market", "?"), exc_info=True)

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
        """Handle GAZUA 2-stage entry (confirm add-buy)."""
        try:
            s_ctrl = (getattr(context, "controls", {}) or {}).get("strategy", {}) or {}
            s_params = s_ctrl.get("params", {}) or {}
            mode = str(s_ctrl.get("mode") or s_ctrl.get("name") or "").upper()

            if mode != "GAZUA":
                return allow_buy, usdt, buy_reason
            if signal != "buy":
                return allow_buy, usdt, buy_reason

            # New position entry is handled in the normal path; here only add-buy while holding is allowed.
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

            # V2 DCA average-price recalculation hint (used by the execution layer)
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
                    logger.warning("[Engine] GAZUA DCA average-price recalculation failed: %s", getattr(context, "market", "?"), exc_info=True)

        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
            logger.warning("[Engine] GAZUA add-buy handling failed: %s", getattr(context, "market", "?"), exc_info=True)

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
        """Wire SNIPER/LIGHTNING probe->confirm 2-stage entry to the execution layer."""
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

            # New probe entry: if size_scale is missing, use probe_ratio as a fallback.
            if not has_position:
                if meta.get("size_scale") is None:
                    probe_ratio = meta.get("probe_ratio")
                    if probe_ratio is not None:
                        probe_f = max(0.05, min(1.0, float(probe_ratio)))
                        usdt = int(usdt * probe_f)
                allow_buy = True
                buy_reason = str((strategy_out or {}).get("reason") or buy_reason)
                return allow_buy, usdt, buy_reason

            # Confirm add-buy while holding: requires the allow_add_buy signal in strategy meta.
            if not bool(meta.get("allow_add_buy", False)):
                return allow_buy, usdt, buy_reason

            mode_key = "".join(ch.lower() for ch in mode if ch.isalnum()) or "staged"
            last_add_key = f"{mode_key}_last_add_ts"
            now_ts = time.time()
            last_add = float(context.get_var(last_add_key, 0.0) or 0.0)
            add_cooldown = float(s_params.get("add_buy_cooldown_sec", 60.0) or 60.0)
            if (now_ts - last_add) < add_cooldown:
                return allow_buy, usdt, buy_reason

            # size_scale was already applied at the top of _build_buy_intent.
            # Apply confirm_buy_ratio as a fallback only when it is missing.
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
            logger.warning("[Engine] %s probe/confirm buy handling failed: %s", mode if 'mode' in dir() else "STAGED", getattr(context, "market", "?"), exc_info=True)

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
        """Build the SELL intent."""
        if not has_position:
            return None

        qty = float(context.position.get("qty") or 0.0)

        # AUTOLOOP reset handling
        reset_staged_entry = self._should_reset_autoloop(context, should_sell)

        # GAZUA V2 multi-stage partial-sell handling
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
                logger.error("[Engine] GAZUA V2 multi-stage partial-sell state save failed: %s", exc)
        else:
            # Partial-sell handling (AUTOLOOP/GAZUA generic)
            qty = self._apply_sell_fraction(context, qty)

        if qty <= 0:
            return None

        # Exit kind classification
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

        # AUTOLOOP state reset
        if reset_staged_entry:
            self._reset_autoloop_state(context)

        return intent

    def _should_reset_autoloop(self, context: HyperEngineContext, should_sell: bool) -> bool:
        """Whether AUTOLOOP state reset is needed."""
        try:
            s_ctrl = (getattr(context, "controls", {}) or {}).get("strategy", {}) or {}
            mode = str(s_ctrl.get("mode") or s_ctrl.get("name") or "").upper()
            if mode == "AUTOLOOP" and should_sell:
                return True
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] _should_reset_autoloop decision failed: %s", exc)
        return False

    def _apply_sell_fraction(self, context: HyperEngineContext, qty: float) -> float:
        """Apply partial-sell fraction (AUTOLOOP/GAZUA)."""
        try:
            s_ctrl = (getattr(context, "controls", {}) or {}).get("strategy", {}) or {}
            s_params = s_ctrl.get("params", {}) or {}
            mode = str(s_ctrl.get("mode") or s_ctrl.get("name") or "").upper()
            if mode in ("AUTOLOOP", "GAZUA"):
                f = float(s_params.get("sell_fraction", 1.0))
                if 0.05 <= f <= 1.0:
                    qty = qty * f
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] _apply_sell_fraction handling failed: %s", exc)
        return qty

    def _classify_exit_kind(
        self, strategy_out: Optional[Dict[str, Any]], sl_hit: bool, tp_hit: bool
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Classify exit kind."""
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
            logger.warning("[Engine] _classify_exit_kind classification failed: %s", exc)

        if sl_hit:
            exit_kind = "sl"
        elif tp_hit:
            exit_kind = "tp"

        return exit_kind, pp_exit_meta

    def _reset_autoloop_state(self, context: HyperEngineContext):
        """Reset AUTOLOOP state."""
        try:
            context.set_var("autoloop_entry_stage", 0)
            context.set_var("autoloop_last_add_ts", 0.0)
            context.set_var("autoloop_entry_ref", 0.0)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Engine] _reset_autoloop_state reset failed: %s", exc)

    # --------------------------------------------------------
    # Phase 4: Diagnostics
    # --------------------------------------------------------
    def _attach_diagnostics(
        self, context: HyperEngineContext, ai: Dict[str, Any], strategy_out: Optional[Dict[str, Any]]
    ):
        """Attach diagnostic info to the context."""
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
            logger.warning("[Engine] _attach_diagnostics attach failed: %s", exc)

    # --------------------------------------------------------
    # Phase 5: Finalize
    # --------------------------------------------------------
    def _finalize_tick(self, context: HyperEngineContext, params: Dict[str, Any], signal: str, price: float):
        """Finalize the tick: update policy and save state."""
        # AI-based additional risk handling (placeholder)
        # Volatility/momentum-based logic can be expanded in the future

        # Automatic policy improvement
        context.update_policy({
            "name": context.policy["name"],
            "params": {**params}
        })

        # Finalize state after the tick
        context.finalize_tick(signal, price)
