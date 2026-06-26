# ============================================================
# File: app/engine/hyper_engine_coordinator.py
# Autocoin OS v3-H — Engine Coordinator (ORDER-AWARE)
# ------------------------------------------------------------
# - Warm-up + StrategySelector + RiskClassifier
# - So that exit decisions remain possible even in LIVE: even without
#   risk_unlock, engine tick is allowed when holding a position / in RECOVERY mode.
# - PATCH 4/5: Suspicion v1 + Defensive Mode integration
#
# NOTE:
# - In the v3-H architecture there is only one engine, HyperNunnayaEngine.
# - pingpong/ladder/gazua etc. are "strategies",
#   and the Coordinator does not execute them.
# ============================================================

from __future__ import annotations

import logging
import time
import os
import threading
from typing import Dict, Any

logger = logging.getLogger(__name__)

from app.core.currency import Q
from app.engine.hyper_engine_context import HyperEngineContext
from app.engine.hyper_engine_base import HyperEngineBase
from app.strategy.strategy_selector import StrategySelector
from app.manager.risk_classifier import RiskClassifier


def _dbg_enabled() -> bool:
    """Runtime debug switch for coordinator tick logging.

    - Default OFF to avoid log spam / performance degradation.
    - Enable: set env OMA_COORD_DEBUG=1
    """
    v = str(os.getenv("OMA_COORD_DEBUG", "0") or "0").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


class HyperEngineCoordinator:
    def __init__(
        self,
        engine: HyperEngineBase,  # must be HyperNunnayaEngine
        *,
        ema_alpha: float = 0.2,
        strategy_refresh_sec: float = 3.0,
    ):
        self.engine = engine
        self.contexts: Dict[str, HyperEngineContext] = {}

        # -----------------------------------------------------------------
        # 🚨 WARMUP CONFIG (DO NOT REMOVE)
        # Some components expect coordinator.min_ticks / coordinator.min_seconds
        # to exist. They are also used as defaults for new market contexts.
        # If you change these, do so deliberately and re-test warmup/readiness.
        # Defaults were self.min_ticks 100, self.min_seconds 300
        # -----------------------------------------------------------------
        self.min_ticks: int = int(os.getenv("WARMUP_MIN_TICKS", "100") or 100)
        self.min_seconds: int = int(os.getenv("WARMUP_MIN_SECONDS", "300") or 300)

        self._lock = threading.RLock()
        self.selector = StrategySelector()
        self.risk = RiskClassifier()

        self.ema_alpha = float(ema_alpha)
        self.strategy_refresh_sec = float(strategy_refresh_sec)

        # ----------------------------------------------------
        # Defensive Mode (Global Entry Guard)
        # ----------------------------------------------------
        self.defensive_mode: bool = False
        self.defensive_red_ratio: float = 0.0

    # --------------------------------------------------------
    # Context management
    # --------------------------------------------------------
    def ensure_market(self, market: str) -> HyperEngineContext:
        with self._lock:
            if market not in self.contexts:
                ctx = HyperEngineContext()
                ctx.market = market

                # Apply coordinator defaults (warmup thresholds)
                try:
                    ctx.min_ticks = int(getattr(self, "min_ticks", 100) or 100)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[COORDINATOR] ensure_market min_ticks: %s", exc, exc_info=True)
                try:
                    ctx.min_seconds = int(getattr(self, "min_seconds", 300) or 300)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[COORDINATOR] ensure_market min_seconds: %s", exc, exc_info=True)
                self.contexts[market] = ctx
            else:
                ctx = self.contexts[market]
                # Force-update an existing context if it is below the coordinator default
                # (prevents the old value being restored after an ENV change + reboot)
                _coord_min = int(getattr(self, "min_ticks", 100) or 100)
                if getattr(ctx, "min_ticks", 0) < _coord_min:
                    ctx.min_ticks = _coord_min

                self.contexts[market] = ctx
            return self.contexts[market]

    def activate_market(self, market: str) -> None:
        self.ensure_market(market)

    def get_context(self, market: str) -> HyperEngineContext:
        return self.ensure_market(market)

    def remove_market(self, market: str) -> None:
        with self._lock:
            self.contexts.pop(market, None)

    def get_contexts(self) -> Dict[str, HyperEngineContext]:
        with self._lock:
            return dict(self.contexts)

    # --------------------------------------------------------
    # Tick
    # --------------------------------------------------------
    def tick(self, market: str, price: float, volume: float = 0.0) -> Dict[str, Any]:
        ctx = self.ensure_market(market)

        # [PERF-TELEMETRY] per-component timing (2026-03-21)
        _t0_tick = time.perf_counter()

        # 1) Record price (Single Source of Truth)
        # - The Coordinator, not the engine, is solely responsible for recording prices.
        # - The engine references the price history already recorded in the context.
        ctx.record_price(price)
        # [PERF] one snapshot per tick — removes 14+ list() copies
        ctx._tick_prices = list(ctx.price_history)
        ctx.record_volume(volume)
        ctx.compute_unrealized(price)
        # legacy / UI compatibility: ctx.ready flag
        try:
            ctx.ready = bool(ctx.is_ready())
        except (AttributeError, TypeError) as exc:
            logger.warning("[COORDINATOR] ctx.ready flag: %s", exc, exc_info=True)


        if _dbg_enabled():
            print(f"[CTRL] market={market} controls={getattr(ctx, 'controls', {})}")
            try:
                rdy = ctx.is_ready()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.warning("[Coordinator] is_ready() check failed for %s", market, exc_info=True)
                rdy = False
            print(f"[READY-CHECK] market={market} ready={rdy} ticks={ctx.ticks} min_ticks={ctx.min_ticks}")

        # 2) Before READY → Warm-up
        # [2026-02-02] optimization: is_ready() returns in O(1) when the _warmup_done flag is True
        # ACTIVE markets have the flag set via the force_ready() call
        warmup_mode = (not ctx.is_ready())
        if warmup_mode:
            ctx.last_signal = "warmup"
            # During warm-up only risk classification runs (engine tick continues below)


        now = time.time()
        # 3) Strategy selection (decide which strategy to use)
        #
        # NOTE:
        # - StrategySelector picks a "recommended strategy" (for telemetry/scoring purposes).
        # - However, when the Dashboard/admin has forced a per-market strategy
        #   (ctx.controls.strategy.enabled + mode), that value is the "actual executing strategy".
        # - Here we sync selected/bias to manual mode so the UI/logs stay consistent.
        _t_selector_ms = 0.0  # [PERF-TELEMETRY]
        if ctx.strategy_ts is None or (now - ctx.strategy_ts) >= self.strategy_refresh_sec:
            _t_sel_start = time.perf_counter()  # [PERF-TELEMETRY]
            # 3-1) Detect admin manual strategy override
            manual_mode = None
            try:
                controls = getattr(ctx, "controls", {}) or {}
                if isinstance(controls, dict):
                    sc = controls.get("strategy", {}) or {}
                    if isinstance(sc, dict) and sc.get("enabled"):
                        manual_mode = str(sc.get("mode") or sc.get("name") or "").strip()
                        if manual_mode:
                            manual_mode = manual_mode.upper()
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[Coordinator] manual strategy mode read failed", exc_info=True)
                manual_mode = None

            # 3-2) Run selector (to collect scores/rationale)
            result = self.selector.select(ctx)

            # 3-3) Update EMA/BIAS
            ctx.update_ema(result.ema_scores, alpha=self.ema_alpha)
            ranked = sorted(ctx.ema_scores.items(), key=lambda x: x[1], reverse=True)

            if manual_mode:
                # If manually specified, force the executing strategy to that
                ctx.bias = manual_mode
                ctx.confidence = 1.0
            else:
                if ranked:
                    ctx.bias = ranked[0][0]
                    ctx.confidence = (
                        ranked[0][1] - ranked[1][1] if len(ranked) > 1 else ranked[0][1]
                    )
                else:
                    ctx.bias = None
                    ctx.confidence = None

            # 3-4) Save snapshot (UI/API contract)
            chosen = manual_mode or result.chosen
            reason = dict(result.reason or {})
            if manual_mode:
                reason = {
                    **reason,
                    "manual_override": True,
                    "manual_mode": manual_mode,
                    "selector_chosen": result.chosen,
                }

            ctx.set_strategy_snapshot(
                selected=chosen,
                scores=result.ema_scores,
                reason=reason,
                ts=now,
            )
            _t_selector_ms = (time.perf_counter() - _t_sel_start) * 1000  # [PERF-TELEMETRY]

        # 4) Risk + Suspicion
        _t_risk_start = time.perf_counter()  # [PERF-TELEMETRY]
        r = self.risk.classify(ctx)
        ctx.set_risk_snapshot(
            band=r["band"],
            unlock=r["unlock"],
            cap_ratio=r.get("cap_ratio", 0.0),
            cap_usdt=r.get("cap_usdt") or 0.0,
            reason={
                **(r.get("reason", {}) or {}),
                "suspicion_score": ctx.suspicion_score,
                "suspicion_level": ctx.suspicion_level,
                "suspicion_group": ctx.suspicion_group,
                "suspicion_intensity": ctx.suspicion_intensity,
            },
        )
        _t_risk_ms = (time.perf_counter() - _t_risk_start) * 1000  # [PERF-TELEMETRY]

        # ----------------------------------------------------
        # 5) Engine tick gate
        # ----------------------------------------------------
        # LEGACY (preserved):
        # New entries allow engine tick only when the conditions below are met
        #
        # allow_engine_tick = bool(self.engine.status.is_active) and (
        #     ctx.position is not None
        #     or str(ctx.market_state).upper() == "RECOVERY"
        #     or (ctx.risk_unlock and not self.defensive_mode)
        # )

        # ----------------------------------------------------
        # TEST / OBSERVABILITY OVERRIDE
        # ----------------------------------------------------
        # Purpose:
        # - Always run the engine decision pipeline to ensure observability
        # - Even in warm-up / risk / defensive states, log "why it didn't happen"
        #
        # Caution:
        # - Actual order safety is guaranteed in HyperSystem._handle_intent().
        # - This block only allows the engine call; it does not force an order.

        controls = getattr(ctx, "controls", {}) or {}

        allow_engine_tick = (
            bool(self.engine.status.is_active)
            and controls.get("enable_engine_tick", True)
        )


        engine_out: Dict[str, Any] | None = None
        _t_engine_ms = 0.0      # [PERF-TELEMETRY]
        _t_engine_cpu_ms = 0.0  # [PERF-TELEMETRY]

        if allow_engine_tick:
            # ------------------------------------------------
            # Engine invocation (single engine)
            # ------------------------------------------------
            # print(
            #     f"[PIPE] market={market} "
            #     f"selected_strategy={ctx.bias} "
            #     f"engine_class={self.engine.__class__.__name__}"
            # )

            _t_engine_start = time.perf_counter()  # [PERF-TELEMETRY]
            _t_engine_cpu_start = time.process_time()  # [PERF-TELEMETRY] CPU time
            out = self.engine.tick(market, price, ctx)
            _t_engine_ms = (time.perf_counter() - _t_engine_start) * 1000  # [PERF-TELEMETRY]
            _t_engine_cpu_ms = (time.process_time() - _t_engine_cpu_start) * 1000  # [PERF-TELEMETRY]

            if isinstance(out, dict):
                engine_out = out
                ctx.last_signal = out.get("signal", ctx.last_signal)
                # strategy_router(/last) compatibility: keep engine AI result
                try:
                    ctx.last_ai = out.get("ai")

                    # NOTE: ensure ctx.strategy_reason is a dict before writing
                    # (prevents TypeError if someone set it to non-dict)
                    if not isinstance(getattr(ctx, "strategy_reason", None), dict):
                        ctx.strategy_reason = {}


                    # ------------------------------------------------
                    # UI contract bridge:
                    # dashboard.js reads ctx.strategy.reason.engine_ai
                    # so mirror engine AI brain snapshot into strategy_reason.
                    # (NO extra computation: just copy)
                    # ------------------------------------------------
                    ai = out.get("ai") or {}
                    if isinstance(ai, dict):
                        brain = ai.get("brain") or ai.get("Brain") or {}
                        if isinstance(brain, dict) and brain:
                            ctx.strategy_reason["engine_ai"] = dict(brain)

                            if isinstance(getattr(ctx, "strategy_state", None), dict):
                                rsn = ctx.strategy_state.get("reason")
                                if not isinstance(rsn, dict):
                                    rsn = {}
                                rsn["engine_ai"] = dict(brain)
                                ctx.strategy_state["reason"] = rsn

                    # ------------------------------------------------
                    # UI diagnostics bridge:
                    # - dashboard wants strategy-level details even when signal=HOLD.
                    # - expose engine's strategy_out (signal/reason/meta) via
                    #   ctx.strategy.reason.strategy_out in system/status.
                    #   (NO extra computation: just copy)
                    # ------------------------------------------------
                    strategy_out = out.get("strategy_out") or {}
                    if isinstance(strategy_out, dict) and strategy_out:
                        ctx.strategy_reason["strategy_out"] = dict(strategy_out)

                        if isinstance(getattr(ctx, "strategy_state", None), dict):
                            rsn = ctx.strategy_state.get("reason")
                            if not isinstance(rsn, dict):
                                rsn = {}
                            rsn["strategy_out"] = dict(strategy_out)
                            ctx.strategy_state["reason"] = rsn

                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("[COORDINATOR] engine AI/strategy bridge: %s", exc, exc_info=True)
            # ------------------------------------------------
            # WARMUP SAFETY:
            # - During warm-up, block actual orders even if the engine emits an intent.
            # - Purpose: prepare indicators/state but do not start trading.
            # ------------------------------------------------
            if 'warmup_mode' in locals() and warmup_mode:
                try:
                    if isinstance(engine_out, dict):
                        # System executes orders based on engine_out.intent, so remove it.
                        engine_out = dict(engine_out)
                        engine_out.pop("intent", None)
                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("[COORDINATOR] warmup intent strip: %s", exc, exc_info=True)
                ctx.last_signal = "warmup"

            else:
                # NOTE: Do NOT overwrite engine-derived signal when not in warmup.
                # The engine already set ctx.last_signal from out['signal'] above.
                # Keeping a forced "hold" here makes signals appear 'variable' or stuck.
                # ctx.last_signal = "hold"  # (disabled)
                pass

        else:
            # Engine inactive state
            ctx.last_signal = "hold"

        # [PERF-TELEMETRY] save per-component elapsed time to ctx (read by TICK_DIAG)
        _t_total_coord_ms = (time.perf_counter() - _t0_tick) * 1000
        ctx._perf_selector_ms = _t_selector_ms
        ctx._perf_risk_ms = _t_risk_ms
        ctx._perf_engine_ms = _t_engine_ms
        ctx._perf_engine_cpu_ms = _t_engine_cpu_ms
        ctx._perf_total_ms = _t_total_coord_ms

        return {
            "signal": ctx.last_signal,
            "engine_out": engine_out,
            "debug_strategy_signal": ctx.last_signal,
        }


    # --------------------------------------------------------
    # Status (UI / API)
    # --------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            out: Dict[str, Any] = {}

            summary = {"RED": 0, "YELLOW": 0, "GREEN": 0}

            for market, ctx in self.contexts.items():
                g = getattr(ctx, "suspicion_group", None)
                if g in summary:
                    summary[g] += 1

                out[market] = {
                    "market_state": ctx.market_state,
                    "trading_mode": ctx.trading_mode,
                    "position": ctx.position,
                    "order_state": getattr(ctx, "order_state", None),
                    "controls": dict(getattr(ctx, "controls", {}) or {}),
                    # cycle (pingpong repeat loop)
                    "cycle": getattr(ctx, "cycle", "IDLE"),
                    "entry_tick": getattr(ctx, "entry_tick", -1),
                    "exit_pending": bool(getattr(ctx, "exit_pending", False)),
                    "allocated_capital": ctx.allocated_capital,
                    "usable_capital": ctx.usable_capital,
                    "unrealized_profit": ctx.unrealized_profit,
                    "total_profit": ctx.total_profit,
                    "last_signal": ctx.last_signal,
                    "readiness": ctx.readiness_status(),
                    "strategy": dict(ctx.strategy_state or {}),
                    "risk": dict(ctx.risk_state or {}),
                    # -----------------------------
                    # GUARD SNAPSHOT (operationally useful)
                    # - These are written/updated by HyperSystem._handle_intent()
                    # - UI can show: where entry/exit is blocked and remaining cooldown
                    # -----------------------------
                    "entry_state": getattr(ctx, "entry_state", None),
                    "entry_block_until_ts": getattr(ctx, "entry_block_until_ts", None),
                    "entry_block_reason": getattr(ctx, "entry_block_reason", None),

                    "exit_state": getattr(ctx, "exit_state", None),
                    "exit_block_until_ts": getattr(ctx, "exit_block_until_ts", None),
                    "exit_block_reason": getattr(ctx, "exit_block_reason", None),

                }

            total = sum(summary.values())
            red_ratio = (summary["RED"] / total) if total > 0 else 0.0
            self.defensive_red_ratio = red_ratio

            if red_ratio >= 0.40:
                self.defensive_mode = True
            elif red_ratio < 0.30:
                self.defensive_mode = False

            out["_oma_defensive"] = {
                "enabled": self.defensive_mode,
                "red_ratio": round(red_ratio, 3),
                "summary": summary,
            }

            return out
