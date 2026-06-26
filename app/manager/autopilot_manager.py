# ============================================================
# File: app/manager/autopilot_manager.py
# Autocoin OS v3-H — Autopilot Manager (Extracted)
# ============================================================

from __future__ import annotations

import asyncio
import functools
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple, Set

from app.core.currency import Q

from app.manager.oma_market_registry import MarketState
from app.manager.reserved_queue import reserved_queue
from app.manager.market_controls import apply_engine_controls
from app.manager.autopilot_helpers import (
    normalize_strategy_name as _normalize_strategy_name,
    extract_row_strategy as _extract_row_strategy,
    infer_strategy_from_reason as _infer_strategy_from_reason_impl,
)
from app.manager.autopilot_cooldown import CooldownMixin
from app.manager.autopilot_scanner import ScannerMixin
from app.manager.autopilot_slot_lifecycle import SlotLifecycleMixin
from app.manager.autopilot_approve import ApproveMixin
from app.manager.autopilot_scope_rotation import ScopeRotationMixin
import app.manager.autopilot_scanner as _scanner_mod

import requests
import logging

logger = logging.getLogger(__name__)

# Module-level functions moved to autopilot_scanner.py
_cached_system = None  # backward compat — canonical copy is in _scanner_mod
from app.manager.autopilot_scanner import _fetch_strategy_recommendations, _fetch_surge_coins

from app.manager.performance_budget import PerformanceBudgetRebalancer, PerformanceMetrics
from app.manager.strategy_graduator import StrategyGraduator, MarketContext
from app.manager.ledger_pnl import aggregate_fill_pnl
from app.manager.correlation_guard import CorrelationGuard
from app.manager.time_based_strategy import TimeBasedStrategySelector
from app.manager.risk_budget import RiskBudgetManager
from app.manager.ai_position_sizing import AIPositionSizer, AISignal
from app.manager.dynamic_stoploss import DynamicStopLossManager, PositionInfo, StopLossMode


class AutopilotManager(CooldownMixin, ScannerMixin, SlotLifecycleMixin, ApproveMixin, ScopeRotationMixin):
    """
    Manager dedicated to the Autopilot (automated operation) logic.
    Extracted from HyperSystem.
    """

    def __init__(self, system: Any):
        global _cached_system
        self.system = system
        _cached_system = system  # [2026-02-01] system reference for direct function calls
        _scanner_mod._cached_system = system  # also set on the scanner module

        # Runtime state
        self._task: Optional[asyncio.Task] = None
        self._inflight: bool = False
        self.last_run_ts: Optional[float] = None
        self.last_result: Any = None
        self._boot_ts: float = time.time()  # [2026-03-07] server boot time (warm-up control)

        # Cooldown persistence
        self.cooldown_path = str(os.getenv("OMA_AUTOPILOT_COOLDOWN_PATH", "runtime/autopilot_cooldown.json") or "runtime/autopilot_cooldown.json")
        self.cooldown: Dict[str, Dict[str, Any]] = {}
        try:
            self._load_cooldown()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[AUTOPILOT] Cooldown persistence: %s", exc, exc_info=True)
        
        # Quick Rotation: store scores at deploy time (for early rotation of positionless slots per strategy)
        self._deploy_scores: Dict[str, float] = {}

        # [Phase 3-B] track consecutive losses per strategy (Loss-Based Cooldown)
        self._strategy_loss_streak: Dict[str, int] = {}
        self._strategy_loss_cooldown_until: Dict[str, float] = {}

        # Performance-based budget rebalancer
        self.budget_rebalancer = PerformanceBudgetRebalancer()
        
        # Strategy graduation system
        self.strategy_graduator = StrategyGraduator()
        
        # New managers (5 features)
        self.correlation_guard = CorrelationGuard()
        self.time_strategy_selector = TimeBasedStrategySelector()
        self.risk_budget_manager = RiskBudgetManager()
        self.ai_position_sizer = AIPositionSizer()
        self.dynamic_stoploss = DynamicStopLossManager()

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="autopilot_loop")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info("[AUTOPILOT] stopped (shutdown)")
            self._task = None

    # --------------------------------------------------------
    # Loop & Step
    # --------------------------------------------------------
    def _in_window(self, ts: Optional[float] = None) -> bool:
        if not bool(getattr(self.system, "autopilot_window_enabled", False)):
            return True

        def _parse_hhmm(s: str) -> Optional[int]:
            try:
                parts = str(s).strip().split(":")
                if len(parts) != 2:
                    return None
                hh = int(parts[0])
                mm = int(parts[1])
                if hh < 0 or hh > 23 or mm < 0 or mm > 59:
                    return None
                return hh * 60 + mm
            except (KeyError, IndexError, TypeError, ValueError):
                logger.warning("[Autopilot] _parse_hhmm failed", exc_info=True)
                return None

        start_s = str(getattr(self.system, "autopilot_window_start", "22:00") or "22:00")
        end_s = str(getattr(self.system, "autopilot_window_end", "08:00") or "08:00")
        sm = _parse_hhmm(start_s)
        em = _parse_hhmm(end_s)
        if sm is None or em is None:
            return True

        lt = time.localtime(ts if ts is not None else time.time())
        cur = lt.tm_hour * 60 + lt.tm_min

        if sm <= em:
            return sm <= cur <= em
        return (cur >= sm) or (cur <= em)

    async def _loop(self):
        # [2026-03-03] round-robin scheduler: split strategies into 2~3 groups for staggered scanning
        # [2026-05-30] owner insight — remove leftovers from the old hardcoded PINGPONG+AUTOLOOP era.
        # Each round dynamically composes only enabled=True plugins (8-strategy era + WHALE included).
        def _build_scan_rounds() -> List[List[str]]:
            _en = lambda name: bool(getattr(self.system, f"reserved_{name.lower()}_enabled", True))
            fast = [s for s in ("PINGPONG", "AUTOLOOP") if _en(s)]      # fast rotation
            precise = [s for s in ("SNIPER", "CONTRARIAN") if _en(s)]   # precision scan
            ai = [s for s in ("LIGHTNING", "LADDER", "GAZUA", "WHALE") if _en(s)]  # AI/pattern scan
            rounds = [r for r in (fast, precise, ai) if r]
            return rounds if rounds else []

        _current_round = 0
        _roundrobin_enabled = str(os.getenv("OMA_ROUNDROBIN_ENABLED", "true")).strip().lower() in ("1", "true", "yes", "on")

        # [2026-03-07] Scope auto-refill independent timer
        # Regardless of autopilot_enabled, auto-scan + refill when there are empty slots
        _scope_last_run_ts: float = 0.0
        _SCOPE_INDEPENDENT_INTERVAL: int = 60  # check scope slot state every 60 seconds

        # [2026-03-24] stabilization wait right after boot (price_feed + reconcile)
        await asyncio.sleep(15.0)

        while True:
            try:
                await asyncio.sleep(1.0)

                now = time.time()
                autopilot_on = bool(getattr(self.system, "autopilot_enabled", False))

                # ── [2026-03-07] Scope independent loop: auto-refill empty slots even when autopilot is OFF ──
                scope_rotation_en = bool(getattr(self.system, "autopilot_scope_rotation_enabled", True))
                scope_target = max(0, int(
                    getattr(self.system, "autopilot_scope_target_n",
                            getattr(self.system, "reserved_sniper_n", 0)) or 0))
                if (scope_rotation_en
                        and scope_target > 0
                        and not autopilot_on
                        and not self._inflight
                        and (now - _scope_last_run_ts) >= _SCOPE_INDEPENDENT_INTERVAL):
                    _scope_last_run_ts = now
                    try:
                        scope_idle_min = max(2, int(
                            getattr(self.system, "autopilot_scope_idle_min", 2) or 2))
                        scope_result = await self._step_scope_slot_rotation(
                            now=now, idle_min=scope_idle_min)
                        if scope_result:
                            logger.info(
                                f"[Autopilot/ScopeIndependent] scope rotation "
                                f"({len(scope_result)} actions) while autopilot OFF")
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning(f"[Autopilot/ScopeIndependent] error: {exc}", exc_info=True)

                # ── existing Autopilot main loop ──
                if not autopilot_on:
                    continue

                if not self._in_window(now):
                    continue

                interval = int(getattr(self.system, "autopilot_eval_interval_sec", 300) or 300)
                if interval < 5:
                    interval = 5

                # [2026-05-30] dynamic round composition — only enabled=True plugins (old hardcoded removed)
                _scan_rounds = _build_scan_rounds() if _roundrobin_enabled else []

                # [2026-03-03] round-robin: divide interval by the number of rounds to scan more often
                if _roundrobin_enabled and _scan_rounds:
                    effective_interval = max(60, interval // len(_scan_rounds))
                else:
                    effective_interval = interval

                last = float(self.last_run_ts or 0.0)
                if last and (now - last) < effective_interval:
                    continue

                if self._inflight:
                    continue

                if _roundrobin_enabled and _scan_rounds:
                    # scan only the strategies of the current round
                    round_strategies = _scan_rounds[_current_round % len(_scan_rounds)]
                    _current_round += 1
                    await self.step(reason="loop", scan_only=False, round_strategies=round_strategies)
                elif _roundrobin_enabled and not _scan_rounds:
                    # all plugins disabled → autopilot dormant (0 rounds)
                    continue
                else:
                    await self.step(reason="loop", scan_only=False)

            except asyncio.CancelledError:
                raise
            except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                try:
                    self.system.ledger.append("AUTOPILOT_LOOP_ERROR", error=str(exc))
                except (AttributeError, TypeError, ValueError):
                    logger.warning("ledger.append AUTOPILOT_LOOP_ERROR failed", exc_info=True)
                    logger.warning("[AUTOPILOT] loop ledger append failed: %s", exc, exc_info=True)

    async def step(self, *, reason: str = "loop", scan_only: bool = False, round_strategies: Optional[List[str]] = None) -> Dict[str, Any]:
        if self._inflight:
            return {"ok": False, "error": "inflight"}

        self._inflight = True
        t0 = time.time()
        now = time.time()

        try:
            self.prune_cooldown(now_ts=now)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[AUTOPILOT] prune_cooldown: %s", exc, exc_info=True)

        try:
            self.last_run_ts = now
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[AUTOPILOT] last_run_ts assign: %s", exc, exc_info=True)

        result: Dict[str, Any] = {"ok": True, "reason": str(reason), "scan_only": bool(scan_only)}
        try:
            enabled = bool(getattr(self.system, "autopilot_enabled", False))
            if (not scan_only) and (not enabled) and (str(reason).lower() not in ("manual", "api", "debug")):
                result.update({"skipped": True, "skip_reason": "disabled"})
                return result

            if (not scan_only) and bool(getattr(self.system, "emergency_stop", False)):
                result.update({"skipped": True, "skip_reason": "emergency_stop"})
                return result

            # Settings from system
            pp_target = max(0, int(getattr(self.system, "reserved_pingpong_n", 0) or 0))
            al_target = max(0, int(getattr(self.system, "reserved_autoloop_n", 0) or 0))
            ld_target = max(0, int(getattr(self.system, "reserved_ladder_n", 0) or 0))
            lt_target = max(0, int(getattr(self.system, "reserved_lightning_n", 0) or 0))
            gz_target = max(0, int(getattr(self.system, "reserved_gazua_n", 0) or 0))
            ct_target = max(0, int(getattr(self.system, "reserved_contrarian_n", 0) or 0))
            sn_target = max(0, int(getattr(self.system, "reserved_sniper_n", 0) or 0))
            wh_target = max(0, int(getattr(self.system, "reserved_whale_n", 0) or 0))

            # [2026-05-30] Per-strategy ON/OFF — force target 0 when enabled=False (block idle operation)
            if not bool(getattr(self.system, "reserved_pingpong_enabled", True)):
                pp_target = 0
            if not bool(getattr(self.system, "reserved_autoloop_enabled", True)):
                al_target = 0
            if not bool(getattr(self.system, "reserved_ladder_enabled", True)):
                ld_target = 0
            if not bool(getattr(self.system, "reserved_lightning_enabled", True)):
                lt_target = 0
            if not bool(getattr(self.system, "reserved_gazua_enabled", True)):
                gz_target = 0
            if not bool(getattr(self.system, "reserved_contrarian_enabled", True)):
                ct_target = 0
            if not bool(getattr(self.system, "reserved_sniper_enabled", True)):
                sn_target = 0
            if not bool(getattr(self.system, "reserved_whale_enabled", True)):
                wh_target = 0
            promote_to_active = bool(getattr(self.system, "reserved_promote_to_active", False))
            apply_budget = bool(getattr(self.system, "reserved_apply_suggested_budget", True))

            auto_approve = bool(getattr(self.system, "autopilot_auto_approve", False))
            idle_en = bool(getattr(self.system, "autopilot_idle_demote_enabled", False))
            idle_min = max(0, int(getattr(self.system, "autopilot_idle_demote_min", 120) or 120))  # default 120 min (2 hours)
            grace_sec = max(0, int(getattr(self.system, "autopilot_grace_sec", 0) or 0))
            demote_max_total = max(0, int(getattr(self.system, "autopilot_demote_max_total", 0) or 0))
            demote_max_per_strategy = max(0, int(getattr(self.system, "autopilot_demote_max_per_strategy", 0) or 0))

            # Step 1) Scan
            scan_summary: Dict[str, Any] = {}
            target_by_strategy: Dict[str, int] = {
                "PINGPONG": pp_target,
                "AUTOLOOP": al_target,
                "LADDER": ld_target,
                "LIGHTNING": lt_target,
                "GAZUA": gz_target,
                "CONTRARIAN": ct_target,
                "SNIPER": sn_target,
                "WHALE": wh_target,
            }

            # Step 1) Scan — delegated to ScannerMixin
            scan_summary, desired_by_strategy, _longhold_markets = await self._step_scan(
                target_by_strategy=target_by_strategy,
                scan_only=scan_only,
                reason=reason,
                round_strategies=round_strategies,
            )

            result["scan_summary"] = scan_summary

            if scan_only:
                return result

            # Steps 2-3c) Slot Lifecycle — delegated to SlotLifecycleMixin
            snap = {}
            try:
                snap = self.system.oma_registry.snapshot()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.warning("[Autopilot] oma_registry snapshot failed", exc_info=True)
                snap = {}
            _lifecycle_result = await self._step_slot_lifecycle(
                snap=snap,
                longhold_markets=_longhold_markets,
                now=now,
                reason=reason,
                idle_en=idle_en,
                idle_min=idle_min,
                grace_sec=grace_sec,
                demote_max_total=demote_max_total,
                demote_max_per_strategy=demote_max_per_strategy,
            )
            active_markets = _lifecycle_result["active_markets"]
            active_reason_map = _lifecycle_result["active_reason_map"]
            result["orphan_cleaned"] = _lifecycle_result["orphan_cleaned"]
            result["watch_timeout_cleaned"] = _lifecycle_result["watch_timeout_cleaned"]
            result["demoted"] = _lifecycle_result["demoted"]
            result["dust_cleanup_targets"] = _lifecycle_result["dust_cleanup_targets"]
            result["longhold_converted"] = _lifecycle_result["longhold_converted"]
            result["quick_rotated"] = _lifecycle_result["quick_rotated"]


            # Step 4) Auto Approve — delegated to ApproveMixin
            approved = await self._step_approve(
                snap=snap,
                active_markets=active_markets,
                active_reason_map=active_reason_map,
                longhold_markets=_longhold_markets,
                target_by_strategy=target_by_strategy,
                now=now,
                reason=reason,
                pp_target=pp_target,
                al_target=al_target,
                ld_target=ld_target,
                lt_target=lt_target,
                gz_target=gz_target,
                ct_target=ct_target,
                sn_target=sn_target,
                wh_target=wh_target,
                promote_to_active=promote_to_active,
                apply_budget=apply_budget,
                auto_approve=auto_approve,
                desired_by_strategy=desired_by_strategy,
            )
            result["approved"] = approved
            
            # Step 5) Performance-based Budget Rebalancing
            budget_adjustments: List[Dict[str, Any]] = []
            perf_rebalance_en = bool(getattr(self.system, "autopilot_perf_rebalance_enabled", False))
            if perf_rebalance_en and not scan_only:
                try:
                    budget_adjustments = await self._step_performance_rebalance(
                        active_markets=active_markets,
                        active_reason_map=active_reason_map,
                        now=now,
                    )
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    try:
                        self.system.ledger.append("AUTOPILOT_PERF_REBALANCE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[AUTOPILOT] perf rebalance ledger append: %s", exc2, exc_info=True)
            result["budget_adjustments"] = budget_adjustments
            
            # Step 6) Strategy Graduation
            graduations: List[Dict[str, Any]] = []
            graduation_en = bool(getattr(self.system, "autopilot_graduation_enabled", False))
            if graduation_en and not scan_only:
                try:
                    graduations = await self._step_strategy_graduation(
                        active_markets=active_markets,
                        active_reason_map=active_reason_map,
                        now=now,
                    )
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    try:
                        self.system.ledger.append("AUTOPILOT_GRADUATION_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[AUTOPILOT] graduation ledger append: %s", exc2, exc_info=True)
            result["graduations"] = graduations

            # Step 6.5) win-rate-linked Assist Fire — auto-adjust aggressiveness by the ratio of profitable slots
            try:
                self._adapt_assist_fire_by_winrate()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning(f"[Autopilot] assist_fire adapt error: {exc}", exc_info=True)

            # Step 7) Scope Slot Rotation — rotate idle SNIPERS (precision_scope) slots
            scope_rotated: List[Dict[str, Any]] = []
            scope_rotation_en = bool(getattr(self.system, "autopilot_scope_rotation_enabled", True))
            scope_idle_min = max(2, int(getattr(self.system, "autopilot_scope_idle_min", 2) or 2))
            if scope_rotation_en and not scan_only:
                try:
                    scope_rotated = await self._step_scope_slot_rotation(
                        now=now,
                        idle_min=scope_idle_min,
                    )
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    try:
                        self.system.ledger.append("AUTOPILOT_SCOPE_ROTATION_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[AUTOPILOT] scope rotation ledger append: %s", exc2, exc_info=True)
            result["scope_rotated"] = scope_rotated

            return result

        finally:
            try:
                result["elapsed_sec"] = round(time.time() - t0, 3)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[AUTOPILOT] elapsed_sec calc: %s", exc, exc_info=True)
            try:
                self.last_result = dict(result)
            except (KeyError, AttributeError, TypeError):
                logger.warning("[Autopilot] dict(result) failed, using raw", exc_info=True)
                self.last_result = result
            self._inflight = False

    # --------------------------------------------------------
    # Step 5: Performance-based Budget Rebalancing
    # --------------------------------------------------------
    async def _step_performance_rebalance(
        self,
        active_markets: List[str],
        active_reason_map: Dict[str, List[str]],
        now: float,
    ) -> List[Dict[str, Any]]:
        """Performance-based budget rebalancing."""
        adjustments: List[Dict[str, Any]] = []

        # settings
        window_hours = float(getattr(self.system, "autopilot_perf_window_hours", 24) or 24)
        min_trades = int(getattr(self.system, "autopilot_perf_min_trades", 3) or 3)
        apply_auto = bool(getattr(self.system, "autopilot_perf_apply_auto", False))
        
        since_ts = now - (window_hours * 3600)
        
        # collect PnL data
        try:
            records = await asyncio.to_thread(
                functools.partial(
                    self.system.ledger.tail_records,
                    since_ts=since_ts,
                    tail_lines=50000,
                )
            )
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("[Autopilot] budget rebalance ledger read failed", exc_info=True)
            return adjustments

        pnl_agg = aggregate_fill_pnl(records, since_ts=since_ts, until_ts=now, markets=active_markets)

        if not pnl_agg:
            return adjustments

        # collect metrics
        metrics: List[PerformanceMetrics] = []
        for market in active_markets:
            agg = pnl_agg.get(market)
            if not agg:
                continue

            # current budget
            budget_usdt = 0.0
            try:
                state = self.system.oma_registry.get_market_info(market) or {}
                budget_usdt = float(state.get("budget_usdt") or state.get("budget_usdt") or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[AUTOPILOT] current budget read: %s", exc, exc_info=True)
            
            # infer strategy
            strategy = self._infer_strategy_from_reason(active_reason_map.get(market, []))

            # activation timestamp
            active_since = 0.0
            try:
                state = self.system.oma_registry.get_market_info(market) or {}
                active_since = float(state.get("ts") or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[AUTOPILOT] active_since read: %s", exc, exc_info=True)
            
            perf_m = self.budget_rebalancer.calculate_metrics(
                pnl_agg=agg,
                current_budget_usdt=budget_usdt,
                strategy=strategy,
                active_since_ts=active_since,
            )
            metrics.append(perf_m)
        
        if not metrics:
            return adjustments
        
        # compute total capital
        total_capital = 0.0
        try:
            total_capital = float(getattr(self.system, "equity_usdt", 0) or 0) * float(getattr(self.system, "deploy_ratio", 0.8) or 0.8)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[AUTOPILOT] total capital calc: %s", exc, exc_info=True)
        
        if total_capital <= 0:
            return adjustments
        
        # compute adjustments
        adj_list, summary = self.budget_rebalancer.calculate_adjustments(metrics, total_capital)

        # apply
        for adj in adj_list:
            if adj.action == "skip":
                continue
            
            result_entry = {
                "market": adj.market,
                "strategy": adj.strategy,
                "old_budget": adj.old_budget,
                "new_budget": adj.new_budget,
                "change_pct": adj.change_pct,
                "action": adj.action,
                "reason": adj.reason,
                "applied": False,
            }
            
            if apply_auto and adj.action != "hold":
                try:
                    if adj.action == "remove":
                        # evict
                        self.system.oma_set_market(
                            market=adj.market,
                            state=MarketState.WATCH,
                            reason=["perf_rebalance_remove", adj.reason],
                        )
                        try:
                            from app.manager.autopilot_tracker import autopilot_tracker
                            autopilot_tracker.record_decision(adj.market, "ACTIVE", "WATCH", adj.strategy, "perf_rebalance_remove " + str(adj.reason))
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[AUTOPILOT] tracker record_decision: %s", exc, exc_info=True)
                        self.mark_cooldown(adj.market, reason="perf_remove")
                        result_entry["applied"] = True
                    else:
                        # budget adjustment
                        self.system.oma_set_market(
                            market=adj.market,
                            state=MarketState.ACTIVE,
                            budget_usdt=adj.new_budget,
                            reason=["perf_rebalance", adj.reason],
                        )
                        result_entry["applied"] = True
                    
                    self.system.ledger.append(
                        "PERF_BUDGET_ADJUST",
                        market=adj.market,
                        strategy=adj.strategy,
                        old_budget=adj.old_budget,
                        new_budget=adj.new_budget,
                        action=adj.action,
                        reason=adj.reason,
                    )
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[AUTOPILOT] budget adjust apply: %s", exc, exc_info=True)
            
            adjustments.append(result_entry)
        
        return adjustments

    # --------------------------------------------------------
    # Step 6: Strategy Graduation
    # --------------------------------------------------------
    async def _step_strategy_graduation(
        self,
        active_markets: List[str],
        active_reason_map: Dict[str, List[str]],
        now: float,
    ) -> List[Dict[str, Any]]:
        """Strategy graduation handling."""
        graduations: List[Dict[str, Any]] = []

        # settings
        window_hours = float(getattr(self.system, "autopilot_grad_window_hours", 24) or 24)
        apply_auto = bool(getattr(self.system, "autopilot_grad_apply_auto", False))
        
        since_ts = now - (window_hours * 3600)
        
        # collect PnL data
        try:
            records = await asyncio.to_thread(
                functools.partial(
                    self.system.ledger.tail_records,
                    since_ts=since_ts,
                    tail_lines=50000,
                )
            )
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("[Autopilot] graduation ledger read failed", exc_info=True)
            return graduations

        pnl_agg = aggregate_fill_pnl(records, since_ts=since_ts, until_ts=now, markets=active_markets)

        # collect contexts
        contexts: List[MarketContext] = []
        for market in active_markets:
            agg = pnl_agg.get(market)

            # infer strategy
            strategy = self._infer_strategy_from_reason(active_reason_map.get(market, []))

            # budget and activation timestamp
            budget = 0.0
            active_since = 0.0
            try:
                state = self.system.oma_registry.get_market_info(market) or {}
                budget = float(state.get("budget_usdt") or 0.0)
                active_since = float(state.get("ts") or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[AUTOPILOT] budget and activation timestamp: %s", exc, exc_info=True)

            age_hours = (now - active_since) / 3600 if active_since > 0 else 0

            # collect AI features
            momentum = 0.0
            volatility = 0.0
            trend = 0.0
            ai_prediction = 0.5
            rsi = 50.0
            current_price = 0.0
            avg_buy_price = 0.0
            position_qty = 0.0
            position_value = 0.0
            
            try:
                ctx = self.system.coordinator.contexts.get(market)
                if ctx:
                    ai_resp = getattr(ctx, "last_ai_response", {}) or {}
                    momentum = float(ai_resp.get("momentum") or 0.0)
                    volatility = float(ai_resp.get("volatility") or 0.0)
                    trend = float(ai_resp.get("trend") or 0.0)
                    ai_prediction = float(ai_resp.get("ai_prediction") or 0.5)
                    rsi = float(ai_resp.get("rsi") or 50.0)
                    
                    current_price = float(getattr(ctx, "current_price", 0) or 0)
                    position_qty = float(getattr(ctx, "position_qty", 0) or 0)
                    avg_buy_price = float(getattr(ctx, "avg_buy_price", 0) or 0)
                    position_value = float(getattr(ctx, "position_value_usdt", 0) or 0)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[AUTOPILOT] AI feature collection: %s", exc, exc_info=True)

            # compute ROI
            roi_pct = 0.0
            net_cash = 0.0
            trade_count = 0
            sell_count = 0
            if agg:
                net_cash = getattr(agg, 'net_cash_usdt', 0.0)
                trade_count = agg.trade_n
                sell_count = agg.sell_n
                if budget > 0:
                    roi_pct = (net_cash / budget) * 100
            
            ctx_obj = MarketContext(
                market=market,
                current_strategy=strategy,
                roi_pct=roi_pct,
                trade_count=trade_count,
                sell_count=sell_count,
                net_cash_usdt=net_cash,
                active_age_hours=age_hours,
                current_price=current_price,
                avg_buy_price=avg_buy_price,
                momentum=momentum,
                volatility=volatility,
                trend=trend,
                ai_prediction=ai_prediction,
                rsi=rsi,
                position_qty=position_qty,
                position_value_usdt=position_value,
            )
            contexts.append(ctx_obj)
        
        if not contexts:
            return graduations
        
        # evaluate graduation
        decisions, summary = self.strategy_graduator.batch_evaluate(contexts)
        
        for decision in decisions:
            if not decision.should_graduate:
                continue
            
            result_entry = {
                "market": decision.market,
                "from_strategy": decision.from_strategy,
                "to_strategy": decision.to_strategy,
                "path": decision.path.value if decision.path else None,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "conditions_met": decision.conditions_met,
                "applied": False,
            }
            
            if apply_auto and decision.to_strategy:
                try:
                    # apply strategy switch
                    apply_engine_controls(self.system, decision.market, decision.to_strategy)

                    # update OMA state
                    self.system.oma_set_market(
                        market=decision.market,
                        state=MarketState.ACTIVE,
                        reason=[
                            f"strategy:{decision.to_strategy}",
                            "graduation",
                            f"from:{decision.from_strategy}",
                            decision.path.value if decision.path else "",
                        ],
                    )
                    
                    result_entry["applied"] = True
                    
                    self.system.ledger.append(
                        "STRATEGY_GRADUATION",
                        market=decision.market,
                        from_strategy=decision.from_strategy,
                        to_strategy=decision.to_strategy,
                        path=decision.path.value if decision.path else None,
                        confidence=decision.confidence,
                    )
                    
                    reserved_queue.add_history({
                        "kind": "GRADUATE",
                        "source": "autopilot",
                        "market": decision.market,
                        "from_strategy": decision.from_strategy,
                        "to_strategy": decision.to_strategy,
                    })
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[AUTOPILOT] fallback: %s", exc, exc_info=True)
            
            graduations.append(result_entry)
        
        return graduations

    def _infer_strategy_from_reason(self, reasons: List[str]) -> str:
        """Infer the strategy from the reason list."""
        return _infer_strategy_from_reason_impl(reasons)

