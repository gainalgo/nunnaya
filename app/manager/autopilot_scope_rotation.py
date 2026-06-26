# ============================================================
# File: app/manager/autopilot_scope_rotation.py
# Autocoin OS v3-H — Scope Slot Rotation Mixin (Extracted)
# ============================================================

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from app.manager.oma_market_registry import MarketState
from app.manager.reserved_queue import reserved_queue

logger = logging.getLogger(__name__)


class ScopeRotationMixin:
    """Methods for Scope Slot Rotation (Step 6.5 + Step 7)."""

    # --------------------------------------------------------
    # Step 6.5: win-rate-linked Assist Fire
    # profitable slot ratio >= 50% -> assist_fire ON (aggressive)
    # profitable slot ratio < 50% -> assist_fire OFF (cautious)
    # --------------------------------------------------------
    def _adapt_assist_fire_by_winrate(self) -> None:
        """Auto-toggle assist_fire based on the profit/loss ratio of scope slots.

        Only runs when longshort_scope_assist_fire_auto=True.
        """
        if not bool(getattr(self.system, "longshort_scope_assist_fire_auto", False)):
            return
        from app.manager.sniper_position_store import sniper_store
        from app.core.hyper_price_store import price_store

        all_positions = sniper_store.get_all_as_list()
        holding = 0
        profitable = 0

        for stored in all_positions:
            params = stored.get("params", {}) or {}
            profile = str(params.get("profile") or "").strip().upper()
            source = str(params.get("source") or "").strip().lower()
            if profile != "SNIPERS" or source != "precision_scope":
                continue

            market = str(stored.get("market") or "").strip().upper()
            if not market:
                continue

            ctx = self.system.coordinator.contexts.get(market)
            if not ctx:
                continue
            pos = getattr(ctx, "position", None) or {}
            qty = float(pos.get("qty", 0) or 0)
            if qty <= 0:
                continue

            holding += 1
            avg_price = float(pos.get("avg_price", 0) or pos.get("entry", 0) or 0)
            if avg_price <= 0:
                continue
            current_price = float(price_store.get_price(market) or 0)
            if current_price > avg_price:
                profitable += 1

        if holding == 0:
            return

        ratio = profitable / holding
        prev = bool(getattr(self.system, "longshort_scope_assist_fire", True))
        next_val = ratio >= 0.5

        if prev != next_val:
            self.system.longshort_scope_assist_fire = next_val
            logger.info(
                f"[Autopilot/AssistFire] {'ON' if next_val else 'OFF'} "
                f"(profitable={profitable}/{holding}, ratio={ratio:.0%})"
            )

    # --------------------------------------------------------
    # Step 7: Scope Slot Rotation
    # Rotate out idle SNIPERS(precision_scope) slots
    # --------------------------------------------------------
    def _process_scope_trap_fills(self, sniper_store: Any) -> None:
        """Detect a trap buy fill -> auto-submit a TP limit sell."""
        fsm = getattr(self.system, "order_fsm", None)
        if not fsm:
            return
        all_positions = sniper_store.get_all_as_list()
        for stored in all_positions:
            if not stored.get("trap_mode"):
                continue
            market = str(stored.get("market") or "").strip().upper()
            if not market:
                continue
            sniper_id = str(stored.get("sniper_id") or "")
            trap_tp_price = float(stored.get("trap_tp_price") or 0)
            if trap_tp_price <= 0:
                continue

            ctx = self.system.coordinator.get_context(market)
            if not ctx:
                continue

            # Still has a pending buy order -> awaiting fill -> skip
            order_state = getattr(ctx, "order_state", None) or {}
            if order_state.get("uuid") and order_state.get("side") == "bid":
                continue

            # Position check -- qty > 0 means the buy filled
            pos = getattr(ctx, "position", None) or {}
            qty = float(pos.get("qty", 0) or 0)
            if qty <= 0:
                continue

            # A sell order is already in progress -> skip
            if order_state.get("uuid") and order_state.get("side") == "ask":
                continue

            try:
                ok, msg = fsm.submit_limit_sell(
                    ctx=ctx,
                    market=market,
                    qty=qty,
                    limit_price=trap_tp_price,
                    reason="sniper:scope_trap_tp_sell",
                    timeout_sec=3600.0 * 24,
                )
                if ok:
                    logger.info(
                        f"[ScopeRotation/Trap] TP sell submitted {market} "
                        f"qty={qty:.8f} @ {trap_tp_price:.2f}"
                    )
                    existing = sniper_store.get_position(sniper_id) or {}
                    existing["trap_mode"] = False
                    existing["trap_tp_sell_ts"] = time.time()
                    sniper_store.save_position(sniper_id, existing)
                else:
                    logger.warning("[ScopeRotation/Trap] TP sell FAIL %s: %s", market, msg)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[ScopeRotation/Trap] TP sell error %s: %s", market, exc)

    def _calc_adaptive_cooldown(self, market: str, base_cd_min: int) -> int:
        """Compute a score-based dynamic cooldown.

        Looks up the market's score from the multi-scan result:
        - top 20% (relative score >= 0.8) -> base x 0.25 (very fast re-appearance)
        - top 40% (relative score >= 0.6) -> base x 0.50
        - average (relative score >= 0.4) -> base x 1.00 (default)
        - bottom (relative score < 0.4)   -> base x 1.50 (give new coins a chance)
        """
        try:
            from app.core.hyper_price_store import price_store

            # Look up the score from the recent scan cache
            scan_cache = getattr(self.system, "_scope_scan_cache", None)
            if not scan_cache:
                return base_cd_min

            scores = []
            target_score = 0.0
            for c in scan_cache:
                mk = str(c.get("market") or "").strip().upper()
                sc = float(c.get("composite_score") or c.get("score") or 0)
                scores.append(sc)
                if mk == market:
                    target_score = sc

            if not scores or target_score <= 0:
                return base_cd_min

            max_score = max(scores) if scores else 1.0
            if max_score <= 0:
                return base_cd_min

            relative = target_score / max_score  # 0.0 to 1.0

            if relative >= 0.8:
                mult = 0.25
            elif relative >= 0.6:
                mult = 0.50
            elif relative >= 0.4:
                mult = 1.0
            else:
                mult = 1.5

            result = max(2, int(base_cd_min * mult))
            logger.debug(
                f"[ScopeRotation/AdaptiveCD] {market} score={target_score:.3f} "
                f"relative={relative:.2f} mult={mult} cd={result}min"
            )
            return result

        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[ScopeRotation/AdaptiveCD] fallback for %s: %s", market, exc)
            return base_cd_min

    def _release_scope_sold_slots(self, sniper_store: Any) -> List[str]:
        """Immediately release sold scope slots + apply cooldown.

        Prevents a coin sold for profit from being re-bought in the same slot.
        Frees the slot so autofill can bring in a new candidate.
        """
        released: List[str] = []
        all_positions = sniper_store.get_all_as_list()
        for stored in all_positions:
            params = stored.get("params", {}) or {}
            profile = str(params.get("profile") or "").strip().upper()
            source = str(params.get("source") or "").strip().lower()
            if profile != "SNIPERS" or source != "precision_scope":
                continue

            market = str(stored.get("market") or "").strip().upper()
            sniper_id = str(stored.get("sniper_id") or "")
            if not market:
                continue

            # A sell order is still pending -> skip (awaiting fill)
            ctx = self.system.coordinator.get_context(market)
            if ctx:
                order_state = getattr(ctx, "order_state", None) or {}
                if order_state.get("uuid") and order_state.get("side") == "ask":
                    continue

            # Check whether a position is held
            has_pos = False
            if ctx:
                pos = getattr(ctx, "position", None) or {}
                qty = float(pos.get("qty", 0) or 0)
                has_pos = qty > 0
            if has_pos:
                continue

            # Check whether sniper_last_exit_ts was recorded recently (just after a sell)
            last_exit_ts = 0.0
            if ctx:
                try:
                    last_exit_ts = float(ctx.get_var("sniper_last_exit_ts", 0) or 0)
                except (TypeError, ValueError) as exc:
                    logger.warning("Failed to parse sniper_last_exit_ts for sold slot check", exc_info=True)
            if last_exit_ts <= 0:
                continue

            # [FIX M2] Also clean up exited slots older than 30 min (prevent buildup of stragglers)
            # Coins whose scope loop has been down for 30+ min should be cleaned up too (no cooldown)
            is_stale_exit = (time.time() - last_exit_ts) > 1800

            # Release the slot
            try:
                sniper_store.remove_position(sniper_id)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("Failed to remove sniper position during sold slot release", exc_info=True)
            try:
                self.system.oma_set_market(
                    market=market,
                    state=MarketState.WATCH,
                    reason=["scope_sold_release"],
                )
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("Failed to set market state to WATCH during sold slot release", exc_info=True)
            if ctx:
                try:
                    ctx.update_controls({"strategy": {"enabled": False, "mode": ""}})
                    if hasattr(ctx, "strategy_mode"):
                        ctx.strategy_mode = ""
                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("Failed to disable strategy controls during sold slot release", exc_info=True)
            base_cd = int(
                getattr(self.system, "autopilot_scope_cooldown_min", 60) or 0
            )
            adaptive = bool(
                getattr(self.system, "autopilot_scope_adaptive_cd", True)
            )
            # [FIX M2] Old exited (straggler) slots: just clean up, no cooldown
            if is_stale_exit:
                released.append(market)
                continue
            final_cd = base_cd
            score_info = ""
            if adaptive and base_cd > 0:
                final_cd = self._calc_adaptive_cooldown(market, base_cd)
                score_info = f" adaptive_cd={final_cd}min"
            self.mark_cooldown(market, minutes=final_cd, reason="scope_sold_release")
            try:
                self.system._save_context_state()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("Failed to save context state after sold slot release", exc_info=True)

            released.append(market)
            logger.info(
                f"[ScopeRotation/SoldRelease] {market} released after sell "
                f"(exit_ts={last_exit_ts:.0f}, cd={final_cd}min{score_info})"
            )
            try:
                self.system.ledger.append(
                    "SCOPE_SLOT_SOLD_RELEASE",
                    market=market,
                )
                reserved_queue.add_history({
                    "kind": "SCOPE_SOLD_RELEASE",
                    "source": "autopilot",
                    "market": market,
                })
            except (AttributeError, TypeError) as exc:
                logger.warning("Failed to record ledger/history for sold slot release", exc_info=True)

        return released

    def _process_scope_tp_timeouts(self, sniper_store: Any) -> None:
        """Trap TP sell unfilled timeout -> market-price liquidation."""
        timeout_hours = float(
            getattr(self.system, "autopilot_scope_trap_tp_timeout_hours", 4.0) or 0
        )
        if timeout_hours <= 0:
            return

        fsm = getattr(self.system, "order_fsm", None)
        if not fsm:
            return

        timeout_sec = timeout_hours * 3600.0
        now = time.time()

        all_positions = sniper_store.get_all_as_list()
        for stored in all_positions:
            tp_sell_ts = float(stored.get("trap_tp_sell_ts") or 0)
            if tp_sell_ts <= 0:
                continue
            if (now - tp_sell_ts) < timeout_sec:
                continue

            market = str(stored.get("market") or "").strip().upper()
            sniper_id = str(stored.get("sniper_id") or "")
            if not market:
                continue

            ctx = self.system.coordinator.get_context(market)
            if not ctx:
                continue

            # Only handle the case where the sell order is still pending
            order_state = getattr(ctx, "order_state", None) or {}
            if not (order_state.get("uuid") and order_state.get("side") == "ask"):
                # Already filled -- clear the timestamp
                existing = sniper_store.get_position(sniper_id) or {}
                existing.pop("trap_tp_sell_ts", None)
                sniper_store.save_position(sniper_id, existing)
                continue

            pos = getattr(ctx, "position", None) or {}
            qty = float(pos.get("qty", 0) or 0)
            if qty <= 0:
                existing = sniper_store.get_position(sniper_id) or {}
                existing.pop("trap_tp_sell_ts", None)
                sniper_store.save_position(sniper_id, existing)
                continue

            # 1) Cancel the limit order
            try:
                fsm.force_cancel_pending(
                    ctx=ctx, market=market, reason="scope_trap_tp_timeout",
                )
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.error("[ScopeRotation/TrapTimeout] cancel FAILED %s, proceeding to market sell: %s",
                            market, exc, exc_info=True)

            # 2) Market sell
            try:
                from app.core.hyper_price_store import price_store
                cur_price = float(price_store.get_price(market) or 0)
                ok, msg = fsm.submit_market_sell(
                    ctx=ctx,
                    market=market,
                    qty=qty,
                    expected_price=cur_price if cur_price > 0 else None,
                    reason="sniper:scope_trap_tp_timeout",
                )
                elapsed_h = round((now - tp_sell_ts) / 3600.0, 1)
                if ok:
                    logger.info(
                        f"[ScopeRotation/TrapTimeout] market_sell OK {market} "
                        f"qty={qty:.8f} after {elapsed_h}h"
                    )
                else:
                    logger.warning(
                        f"[ScopeRotation/TrapTimeout] market_sell FAIL {market}: {msg}"
                    )
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[ScopeRotation/TrapTimeout] market_sell error %s: %s", market, exc)

            # Clear the timestamp
            existing = sniper_store.get_position(sniper_id) or {}
            existing.pop("trap_tp_sell_ts", None)
            sniper_store.save_position(sniper_id, existing)

    async def _step_scope_slot_rotation(
        self,
        now: float,
        idle_min: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        Auto-rotation of Precision Scope slots.
        - Detect slots with SNIPERS profile + no position (SCANNING) idle for >= idle_min minutes
        - Search for more promising candidates via multi-scan
        - Stop the existing slot -> deploy the new candidate
        """
        from app.manager.sniper_position_store import sniper_store, generate_sniper_id

        rotated: List[Dict[str, Any]] = []

        # 0a) Immediately release sold slots (prevent re-buy + cooldown)
        sold_released = self._release_scope_sold_slots(sniper_store)
        if sold_released:
            for mk in sold_released:
                rotated.append({
                    "action": "sold_release",
                    "old_market": mk,
                    "new_market": "",
                    "idle_min": 0,
                    "new_daily_est": 0,
                })

        # 0b) Trap mode: auto TP sell order on filled-buy slots + timeout check
        self._process_scope_trap_fills(sniper_store)
        self._process_scope_tp_timeouts(sniper_store)

        idle_sec = idle_min * 60
        scope_target_base = max(
            0,
            int(
                getattr(
                    self.system,
                    "autopilot_scope_target_n",
                    getattr(self.system, "reserved_sniper_n", 0),
                )
                or 0
            ),
        )
        # Admin extension slots (+alpha): default 0, expandable via env var / runtime attribute when needed.
        # e.g. OMA_AUTOPILOT_SCOPE_TARGET_ALPHA=2 -> allow autofill up to target_n + 2
        scope_target_alpha_raw = getattr(
            self.system,
            "autopilot_scope_target_alpha",
            os.getenv("OMA_AUTOPILOT_SCOPE_TARGET_ALPHA", "0"),
        )
        try:
            scope_target_alpha = min(20, max(0, int(scope_target_alpha_raw or 0)))
        except (TypeError, ValueError):
            logger.warning("ScopeRotationMixin._step_scope_slot_rotation suppressed exception", exc_info=True)
            scope_target_alpha = 0
        # Minimum confidence for alpha slots (default 60)
        scope_alpha_min_conf_raw = getattr(
            self.system,
            "autopilot_scope_alpha_min_conf",
            os.getenv("OMA_AUTOPILOT_SCOPE_ALPHA_MIN_CONF", "60"),
        )
        try:
            scope_alpha_min_conf = max(0.0, min(100.0, float(scope_alpha_min_conf_raw or 60.0)))
        except (TypeError, ValueError):
            logger.warning("ScopeRotationMixin._step_scope_slot_rotation suppressed exception", exc_info=True)
            scope_alpha_min_conf = 60.0
        # Minimum confidence for slot autofill -- separate from the UI scan display (longshort_scope_min_conf)
        # The UI's "10" is for the scan list display; autofill uses a separate setting or a hard floor
        autofill_min_conf_raw = getattr(
            self.system,
            "autopilot_scope_autofill_min_conf",
            os.getenv("OMA_SCOPE_AUTOFILL_MIN_CONF", "40"),
        )
        try:
            autofill_min_conf = max(40.0, min(100.0, float(autofill_min_conf_raw or 40.0)))
        except (TypeError, ValueError):
            logger.warning("ScopeRotationMixin._step_scope_slot_rotation suppressed exception", exc_info=True)
            autofill_min_conf = 40.0
        # alpha grant TTL (minutes) = same as the re-appearance/re-entry cooldown (N minutes)
        scope_alpha_ttl_min_raw = getattr(
            self.system,
            "autopilot_scope_cooldown_min",
            os.getenv("OMA_SCOPE_COOLDOWN_MIN", "60"),
        )
        try:
            scope_alpha_ttl_min = max(0, int(scope_alpha_ttl_min_raw or 60))
        except (TypeError, ValueError):
            logger.warning("ScopeRotationMixin._step_scope_slot_rotation suppressed exception", exc_info=True)
            scope_alpha_ttl_min = 60
        scope_alpha_ttl_sec = float(scope_alpha_ttl_min * 60)
        scope_target_cap = min(100, scope_target_base + scope_target_alpha)
        # alpha window start time (seconds). 0 means not started.
        scope_alpha_started_ts_raw = getattr(self.system, "autopilot_scope_alpha_started_ts", 0.0)
        try:
            scope_alpha_started_ts = float(scope_alpha_started_ts_raw or 0.0)
        except (TypeError, ValueError):
            logger.warning("ScopeRotationMixin._step_scope_slot_rotation suppressed exception", exc_info=True)
            scope_alpha_started_ts = 0.0

        # Reserve slots for operator manual deployment (autofill holdback).
        # - default 1 slot (adjustable via env var / runtime)
        # - no holdback when target is small (<=2)
        scope_operator_reserve_raw = getattr(
            self.system,
            "autopilot_scope_operator_reserve_slots",
            os.getenv("OMA_AUTOPILOT_SCOPE_OPERATOR_RESERVE_SLOTS", "1"),
        )
        try:
            scope_operator_reserve = min(5, max(0, int(scope_operator_reserve_raw or 0)))
        except (TypeError, ValueError):
            logger.warning("ScopeRotationMixin._step_scope_slot_rotation suppressed exception", exc_info=True)
            scope_operator_reserve = 1

        # fill target:
        # - while alpha window active: base + alpha
        # - after alpha window expires: base
        # - but has_pos=False slots can be trimmed when over target (not a forced exit: held slots are kept)
        if scope_target_alpha <= 0:
            scope_target_fill = scope_target_base
        elif scope_alpha_started_ts <= 0:
            scope_target_fill = scope_target_cap
        elif scope_alpha_ttl_sec > 0 and (now - scope_alpha_started_ts) < scope_alpha_ttl_sec:
            scope_target_fill = scope_target_cap
        else:
            scope_target_fill = scope_target_base

        # Autofill fills all the way up to the configured target.
        # (Operator manual placement is handled separately as +2 overflow, so no reserve holdback needed)
        effective_operator_reserve = 0
        scope_target_autofill = max(0, int(scope_target_fill))

        # The actual trim/fill hard target follows the "current fill target".
        scope_target_hard = max(0, int(scope_target_fill))

        def _scope_overflow_hold_until(params_obj: Dict[str, Any]) -> float:
            """Compute the hold-expiry time for a manual overflow slot (0 if none)."""
            params_local = params_obj or {}
            try:
                is_overflow = bool(params_local.get("scope_overflow_manual", False))
            except (KeyError, AttributeError, TypeError):
                logger.warning("ScopeRotationMixin._scope_overflow_hold_until suppressed exception", exc_info=True)
                is_overflow = False
            if not is_overflow:
                return 0.0
            try:
                started_ts = float(params_local.get("scope_overflow_started_ts") or 0.0)
            except (TypeError, ValueError):
                logger.warning("ScopeRotationMixin._scope_overflow_hold_until suppressed exception", exc_info=True)
                started_ts = 0.0
            try:
                ttl_min = float(params_local.get("scope_overflow_ttl_min") or scope_alpha_ttl_min or 0.0)
            except (TypeError, ValueError):
                logger.warning("ScopeRotationMixin._scope_overflow_hold_until suppressed exception", exc_info=True)
                ttl_min = float(scope_alpha_ttl_min or 0.0)
            if ttl_min <= 0:
                return 0.0
            if started_ts <= 0:
                started_ts = now
            return float(started_ts + (ttl_min * 60.0))

        def _prune_waiting_scope_slot(*, market: str, sniper_id: str) -> None:
            """Clean up a WAITING (no-position + inactive) scope slot so server autofill can reuse it immediately."""
            removed = False
            try:
                if sniper_id:
                    removed = bool(sniper_store.remove_position(sniper_id))
                else:
                    removed = bool(sniper_store.remove_positions_by_market(market))
            except (AttributeError, TypeError):
                logger.warning("ScopeRotationMixin._prune_waiting_scope_slot suppressed exception", exc_info=True)
                removed = False

            try:
                ctx = self.system.coordinator.get_context(market)
                if ctx:
                    controls = getattr(ctx, "controls", {}) or {}
                    strat = controls.get("strategy", {}) or {}
                    if bool(strat.get("enabled")):
                        ctx.update_controls({"strategy": {"enabled": False, "mode": ""}})
                    if hasattr(ctx, "strategy_mode"):
                        ctx.strategy_mode = ""
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("Failed to disable strategy controls during waiting slot prune", exc_info=True)

            try:
                st = self.system.oma_registry.get_state(market)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.warning("ScopeRotationMixin._prune_waiting_scope_slot suppressed exception", exc_info=True)
                st = None
            try:
                if st in (MarketState.ACTIVE, MarketState.RECOVERY):
                    self.system.oma_set_market(
                        market=market,
                        state=MarketState.WATCH,
                        reason=["scope_waiting_slot_gc"],
                    )
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("Failed to set market state to WATCH during waiting slot GC", exc_info=True)

            try:
                cooldown_min = int(getattr(self.system, "autopilot_scope_cooldown_min", 60) or 0)
                self.mark_cooldown(
                    market,
                    minutes=max(0, cooldown_min),
                    reason="scope_waiting_slot_gc",
                )
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("Failed to mark cooldown during waiting slot GC", exc_info=True)

            if removed:
                try:
                    self.system._save_context_state()
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("Failed to save context state during waiting slot GC", exc_info=True)

        # 1) Classify current SNIPERS slots (all / idle)
        all_positions = sniper_store.get_all_as_list()
        scope_slots: List[Dict[str, Any]] = []
        idle_slots: List[Dict[str, Any]] = []
        scope_seen_markets: Set[str] = set()

        for stored in all_positions:
            market = str(stored.get("market") or "").strip().upper()
            if not market:
                continue
            params = stored.get("params", {}) or {}
            profile = str(params.get("profile") or "").strip().upper()
            source = str(params.get("source") or "").strip().lower()
            if profile != "SNIPERS" or source != "precision_scope":
                continue
            if market in scope_seen_markets:
                continue
            scope_seen_markets.add(market)

            sniper_id = str(stored.get("sniper_id") or "").strip() or market
            ctx = self.system.coordinator.contexts.get(market)
            has_pos = False
            strat_enabled = False
            if ctx:
                try:
                    pos = getattr(ctx, "position", None) or {}
                    qty = float(pos.get("qty", 0) or 0)
                    has_pos = qty > 0
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("ScopeRotationMixin._prune_waiting_scope_slot suppressed exception", exc_info=True)
                    has_pos = False
                try:
                    controls = getattr(ctx, "controls", {}) or {}
                    strat = controls.get("strategy", {}) or {}
                    strat_enabled = bool(strat.get("enabled"))
                except (KeyError, AttributeError, TypeError):
                    logger.warning("ScopeRotationMixin._prune_waiting_scope_slot suppressed exception", exc_info=True)
                    strat_enabled = False

            is_open_state = False
            try:
                st = self.system.oma_registry.get_state(market)
                is_open_state = st in (MarketState.ACTIVE, MarketState.RECOVERY)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.warning("ScopeRotationMixin._prune_waiting_scope_slot suppressed exception", exc_info=True)
                is_open_state = False

            # Clean up WAITING slots in the server loop just like the API lookup did.
            # (Empty-slot autofill must work regardless of whether a browser is connected)
            # [FIX M1] First 5 min after deploy is a grace period - prevents early removal of new slots
            scope_deploy_ts = float(params.get("scope_deploy_ts") or 0.0)
            deploy_grace_sec = 300.0  # 5 min grace period
            just_deployed = (time.time() - scope_deploy_ts) < deploy_grace_sec if scope_deploy_ts > 0 else False
            if just_deployed:
                # Do not touch a slot currently being deployed
                scope_slots.append({"market": market, "sniper_id": sniper_id, "age_min": 0.0,
                                     "budget_usdt": float(stored.get("budget_usdt") or 0.0),
                                     "has_pos": False, "idle_left_sec": float(idle_sec),
                                     "active_age_sec": 0.0, "overflow_hold_until_ts": _scope_overflow_hold_until(params),
                                     "is_manual_overflow": bool(params.get("scope_overflow_manual", False))})
                continue
            if (not has_pos) and ((ctx is not None and not strat_enabled) or (not is_open_state)):
                _prune_waiting_scope_slot(market=market, sniper_id=sniper_id)
                continue

            try:
                budget_usdt = float(stored.get("budget_usdt") or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("ScopeRotationMixin._prune_waiting_scope_slot suppressed exception", exc_info=True)
                budget_usdt = 0.0
            overflow_hold_until_ts = _scope_overflow_hold_until(params)

            is_manual_overflow = bool(params.get("scope_overflow_manual", False))
            slot = {
                "market": market,
                "sniper_id": sniper_id,
                "age_min": 0.0,
                "budget_usdt": budget_usdt,
                "has_pos": False,
                "idle_left_sec": float(idle_sec),
                "active_age_sec": 0.0,
                "overflow_hold_until_ts": overflow_hold_until_ts,
                "is_manual_overflow": is_manual_overflow,
            }
            scope_slots.append(slot)

            try:
                since_active = float(self.system.oma_registry.get_active_since_ts(market) or 0.0)
            except (TypeError, ValueError):
                logger.warning("ScopeRotationMixin._prune_waiting_scope_slot suppressed exception", exc_info=True)
                since_active = 0.0
            age = (now - since_active) if since_active > 0 else 0.0
            slot["active_age_sec"] = round(max(0.0, age), 1)
            slot["has_pos"] = has_pos
            if has_pos:
                continue  # Don't rotate while holding a position

            # [FIX 2026-03-10] A slot with no position = sell completed or buy failed
            # Releasing immediately causes an infinite autofill->release loop.
            # -> Only release after the cooldown (autopilot_scope_cooldown_min, default 60 min).
            # When a buy then sell completes: handled in sold_release. Here we guard against buy failure.
            scope_deploy_ts_slot = float(params.get("scope_deploy_ts") or 0.0)
            slot_age_since_deploy = (now - scope_deploy_ts_slot) if scope_deploy_ts_slot > 0 else age
            # idle_sec = idle_min * 60 (default 5 min * 60 = 300 sec)
            # Must be idle_sec past slot placement to be classified as a release target
            if slot_age_since_deploy < float(idle_sec):
                # Not yet past idle_min -> keep (need enough time to allow a buy attempt)
                continue
            slot["idle_left_sec"] = 0.0
            slot["age_min"] = round(max(0.0, age) / 60, 1)
            idle_slots.append(slot)

        # Supplement SNIPERS slots from context when missing in sniper_store
        for market, ctx in list(self.system.coordinator.contexts.items()):
            mk = str(market or "").strip().upper()
            if not mk or mk in scope_seen_markets:
                continue
            try:
                controls = getattr(ctx, "controls", {}) or {}
                strat = controls.get("strategy", {}) or {}
                if not bool(strat.get("enabled")):
                    continue
                mode_upper = str(strat.get("mode") or "").strip().upper()
                if mode_upper not in ("SNIPER", "SNIPER(S)"):
                    continue
                params = strat.get("params", {}) or {}
                profile = str(params.get("profile") or "").strip().upper()
                source = str(params.get("source") or "").strip().lower()
                if profile != "SNIPERS" or source != "precision_scope":
                    continue
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("Failed to read scope slot context for %s", mk, exc_info=True)
                continue

            scope_seen_markets.add(mk)
            try:
                budget_usdt = float(self.system.oma_registry.get_budget_usdt(mk) or 0.0)
            except (TypeError, ValueError):
                logger.warning("[Autopilot] Scope slot budget lookup failed: %s -> 0 USDT", mk)
                budget_usdt = 0.0
            overflow_hold_until_ts = _scope_overflow_hold_until(params)

            slot = {
                "market": mk,
                "sniper_id": mk,
                "age_min": 0.0,
                "budget_usdt": budget_usdt,
                "has_pos": False,
                "idle_left_sec": float(idle_sec),
                "active_age_sec": 0.0,
                "overflow_hold_until_ts": overflow_hold_until_ts,
            }
            scope_slots.append(slot)

            has_pos = False
            try:
                pos = getattr(ctx, "position", None) or {}
                qty = float(pos.get("qty", 0) or 0)
                has_pos = qty > 0
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("ScopeRotationMixin._prune_waiting_scope_slot suppressed exception", exc_info=True)
                has_pos = False
            try:
                since_active = float(self.system.oma_registry.get_active_since_ts(mk) or 0.0)
            except (TypeError, ValueError):
                logger.warning("ScopeRotationMixin._prune_waiting_scope_slot suppressed exception", exc_info=True)
                since_active = 0.0
            age = (now - since_active) if since_active > 0 else 0.0
            slot["active_age_sec"] = round(max(0.0, age), 1)
            slot["has_pos"] = has_pos
            if has_pos:
                continue
            # [FIX 2026-03-10] Context-based slots also wait idle_sec the same way
            scope_deploy_ts_ctx = float(params.get("scope_deploy_ts") or 0.0)
            ctx_slot_age = (now - scope_deploy_ts_ctx) if scope_deploy_ts_ctx > 0 else age
            if ctx_slot_age < float(idle_sec):
                continue
            slot["idle_left_sec"] = 0.0
            slot["age_min"] = round(max(0.0, age) / 60, 1)
            idle_slots.append(slot)

        # Start the TTL countdown from the moment alpha slots are actually created.
        # If already over base, treat now as the start so it reverts to the base target after expiry.
        if scope_target_alpha > 0 and scope_alpha_started_ts <= 0 and len(scope_slots) > scope_target_base:
            scope_alpha_started_ts = now
            try:
                setattr(self.system, "autopilot_scope_alpha_started_ts", float(scope_alpha_started_ts))
            except (TypeError, ValueError) as exc:
                logger.warning("Failed to set autopilot_scope_alpha_started_ts", exc_info=True)

        def _release_scope_slot(slot: Dict[str, Any], reason_tag: str) -> bool:
            market = str(slot.get("market") or "").strip().upper()
            sniper_id = str(slot.get("sniper_id") or "").strip()
            if not market:
                return False
            try:
                if sniper_id:
                    sniper_store.remove_position(sniper_id)
                else:
                    sniper_store.remove_positions_by_market(market)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[SCOPE] _release_scope_slot fallback: %s", exc, exc_info=True)
            try:
                self.system.oma_set_market(
                    market=market,
                    state=MarketState.WATCH,
                    reason=[reason_tag, f"target:{scope_target_hard}"],
                )
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[Autopilot/ScopeRotation] release %s failed: %s", market, exc)
                return False
            try:
                ctx = self.system.coordinator.get_context(market)
                if ctx:
                    ctx.update_controls({"strategy": {"enabled": False, "mode": ""}})
                    if hasattr(ctx, "strategy_mode"):
                        ctx.strategy_mode = ""
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[SCOPE] _release_scope_slot fallback: %s", exc, exc_info=True)
            try:
                self.mark_cooldown(market, reason="scope_target_trim")
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[SCOPE] fallback: %s", exc, exc_info=True)
            try:
                self.system._save_context_state()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[SCOPE] fallback: %s", exc, exc_info=True)
            return True

        # Return slots exceeding the target count (held slots excluded)
        # [2026-03-07] Manual overflow (scope_overflow_manual=True) slots are excluded from trimming
        # Manually added slots naturally decrease via _release_scope_sold_slots() when a sell completes
        if len(scope_slots) > scope_target_hard:
            over = len(scope_slots) - scope_target_hard
            trimmed_markets: Set[str] = set()
            removable = [
                s
                for s in scope_slots
                if (not bool(s.get("has_pos")))
                and not bool(s.get("is_manual_overflow"))  # protect manual overflow
                and float(s.get("overflow_hold_until_ts") or 0.0) <= now
            ]
            removable.sort(key=lambda s: float(s.get("age_min") or 0.0), reverse=True)
            for slot in removable:
                if over <= 0:
                    break
                market = str(slot.get("market") or "").strip().upper()
                if not market:
                    continue
                if _release_scope_slot(slot, "scope_target_trim"):
                    over -= 1
                    trimmed_markets.add(market)
                    rotated.append({
                        "action": "trim",
                        "old_market": market,
                        "new_market": "",
                        "idle_min": slot.get("age_min", 0),
                        "new_daily_est": 0,
                    })
                    try:
                        self.system.ledger.append(
                            "SCOPE_SLOT_TRIMMED",
                            market=market,
                            target_slots=scope_target_hard,
                        )
                    except (AttributeError, TypeError) as exc:
                        logger.warning("[SCOPE] fallback: %s", exc, exc_info=True)
                    try:
                        reserved_queue.add_history({
                            "kind": "SCOPE_TRIM",
                            "source": "autopilot",
                            "old_market": market,
                            "target_slots": scope_target_hard,
                        })
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("[SCOPE] fallback: %s", exc, exc_info=True)
            if trimmed_markets:
                scope_slots = [
                    s for s in scope_slots
                    if str(s.get("market") or "").strip().upper() not in trimmed_markets
                ]
                idle_slots = [
                    s for s in idle_slots
                    if str(s.get("market") or "").strip().upper() not in trimmed_markets
                ]
            if over > 0:
                hold_count = sum(
                    1 for s in scope_slots
                    if (not bool(s.get("has_pos")))
                    and float(s.get("overflow_hold_until_ts") or 0.0) > now
                )
                logger.info(
                    "[Autopilot/ScopeRotation] over-target but no removable slots "
                    f"(over={over}, target={scope_target_hard}, total={len(scope_slots)}, hold_protected={hold_count})"
                )
        try:
            quick_rotate_min_sec_raw = getattr(
                self.system,
                "autopilot_scope_quick_rotate_min_sec",
                120,
            )
            quick_rotate_min_sec = float(max(30, int(quick_rotate_min_sec_raw or 120)))
        except (TypeError, ValueError):
            logger.warning("ScopeRotationMixin._release_scope_slot suppressed exception", exc_info=True)
            quick_rotate_min_sec = 120.0
        try:
            quick_rotate_min_rank_ratio_raw = getattr(
                self.system,
                "autopilot_scope_quick_rotate_min_rank_ratio",
                0.10,
            )
            quick_rotate_min_rank_ratio = float(max(0.0, quick_rotate_min_rank_ratio_raw or 0.10))
        except (TypeError, ValueError):
            logger.warning("ScopeRotationMixin._release_scope_slot suppressed exception", exc_info=True)
            quick_rotate_min_rank_ratio = 0.10
        try:
            quick_rotate_max_raw = getattr(
                self.system,
                "autopilot_scope_quick_rotate_max_per_cycle",
                2,
            )
            quick_rotate_max = int(min(10, max(0, int(quick_rotate_max_raw or 0))))
        except (TypeError, ValueError):
            logger.warning("ScopeRotationMixin._release_scope_slot suppressed exception", exc_info=True)
            quick_rotate_max = 2

        # [2026-03-07] Boot warm-up: price data is unstable for a few minutes after server boot
        # -> block autofill/rotation (management of restored existing slots continues)
        _BOOT_WARMUP_SEC = 60.0  # 1 min
        boot_ts = getattr(self, "_boot_ts", 0.0) or 0.0
        is_warming_up = boot_ts > 0 and (now - boot_ts) < _BOOT_WARMUP_SEC
        if is_warming_up:
            logger.debug(
                f"[Autopilot/ScopeRotation] boot warm-up ({now - boot_ts:.0f}s / {_BOOT_WARMUP_SEC:.0f}s) — skipping autofill/rotation"
            )
            return rotated

        fill_needed = max(0, scope_target_autofill - len(scope_slots))
        pre_idle_watch_slots = [
            s for s in scope_slots
            if not bool(s.get("has_pos"))
            and float(s.get("active_age_sec") or 0.0) >= float(quick_rotate_min_sec)
            and float(s.get("active_age_sec") or 0.0) < float(idle_sec)
        ]
        # To avoid excessive ticks, run the auto-scan only when there are
        # "actually missing slots (fill_needed>0)" or
        # "replaceable idle slots (idle_slots>0)" or
        # "pre-idle quick-rotate target slots"
        scan_needed = (
            fill_needed > 0
            or len(idle_slots) > 0
            or (quick_rotate_max > 0 and len(pre_idle_watch_slots) > 0)
        )

        if not scan_needed:
            return rotated

        # 2) Search for promising candidates via multi-scan
        # When there are missing slots (fill), looking at only the top few candidates (top 5-10)
        # risks autofill stalling because those markets are already occupied, so widen the lookup.
        scan_count = 100
        scan_top_n = min(scan_count, max(30, len(idle_slots) + fill_needed + 10))
        try:
            from app.api.strategy_router import longshort_multi_scan, evaluate_scope_deploy_candidate

            def _safe_float(v, d=0.0):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    logger.warning("ScopeRotationMixin._safe_float suppressed exception", exc_info=True)
                    return d

            min_price = _safe_float(getattr(self.system, "longshort_scope_min_price", 0.0) or 0.0)
            max_price = _safe_float(getattr(self.system, "longshort_scope_max_price", 0.0) or 0.0)
            if min_price <= 0:
                min_price = _safe_float(getattr(self.system, "reserved_candidate_price_min_usdt", 0.0) or 0.0)
            if max_price <= 0:
                max_price = _safe_float(getattr(self.system, "reserved_candidate_price_max_usdt", 0.0) or 0.0)
            if min_price > 0 and max_price > 0 and max_price < min_price:
                min_price, max_price = max_price, min_price

            class _FakeRequest:
                class app:
                    class state:
                        system = self.system

            scan_result = await asyncio.to_thread(
                functools.partial(
                    longshort_multi_scan,
                    request=_FakeRequest(),
                    top_n=scan_top_n,
                    scan_count=scan_count,
                    force_refresh=True,
                    min_confidence=10.0,  # scan wide; apply min_conf in the autofill filter
                    focus_market="",
                    min_price=min_price,
                    max_price=max_price,
                )
            )
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[Autopilot/ScopeRotation] multi-scan failed: %s", exc)
            return rotated

        if not scan_result or not scan_result.get("ok"):
            return rotated
        candidates = scan_result.get("results", [])
        if not candidates:
            return rotated

        # Update the cache used for adaptive cooldown
        self.system._scope_scan_cache = candidates

        # Exclude in-use / cooldown / existing scope markets
        occupied_set: Set[str] = set()
        try:
            snap = self.system.oma_registry.snapshot()
            # Treating all WATCH as occupied would over-block scope autofill,
            # so exclude only ACTIVE/RECOVERY where real operating conflicts arise.
            for bucket in ("active", "recovery"):
                for row in (snap.get(bucket) or []):
                    mk = (row.get("market") if isinstance(row, dict) else row) or ""
                    if mk:
                        occupied_set.add(str(mk).strip().upper())
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[SCOPE] exclude only ACTIVE/RECOVERY with real operating conflicts: %s", exc, exc_info=True)
        strategy_occupied_set: Set[str] = set()
        try:
            for mk, ctx in list(self.system.coordinator.contexts.items()):
                market = str(mk or "").strip().upper()
                if not market:
                    continue
                ctrls = getattr(ctx, "controls", {}) or {}
                strat = ctrls.get("strategy", {}) or {}
                if not bool(strat.get("enabled")):
                    continue
                mode = str(strat.get("mode") or "").strip().upper()
                params = strat.get("params", {}) or {}
                profile = str(params.get("profile") or "").strip().upper()
                if mode in ("SNIPER", "SNIPER(S)") and profile == "SNIPERS":
                    continue
                strategy_occupied_set.add(market)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[SCOPE] exclude only ACTIVE/RECOVERY with real operating conflicts: %s", exc, exc_info=True)
        cooldown_set = self.get_cooldown_markets(now_ts=now)
        existing_scope_set: Set[str] = {str(s.get("market") or "").strip().upper() for s in scope_slots}

        # [no overlap] Also exclude regular SNIPER (profile != SNIPERS) coins in sniper_store from scope candidates
        regular_sniper_set: Set[str] = set()
        try:
            from app.manager.sniper_position_store import sniper_store as _sniper_store
            for _pos in _sniper_store.get_all_as_list():
                _mk = str(_pos.get("market") or "").strip().upper()
                if not _mk:
                    continue
                _params = _pos.get("params") or {}
                _prof = str(_params.get("profile") or "").strip().upper()
                _src = str(_params.get("source") or "").strip().lower()
                if not (_prof == "SNIPERS" and _src == "precision_scope"):
                    regular_sniper_set.add(_mk)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[SCOPE] [no overlap] regular SNIPER exclusion in sniper_store: %s", exc, exc_info=True)

        # Shared score-calculation helper for slot rotation/fill
        def _score(v: Any) -> float:
            try:
                return float(v or 0.0)
            except (TypeError, ValueError):
                logger.warning("state._score suppressed exception", exc_info=True)
                return 0.0

        def _candidate_score_tuple(c: Optional[Dict[str, Any]]) -> Tuple[float, float, float, float]:
            item = c or {}
            wave = item.get("wave") or {}
            return (
                _score(item.get("rank_score")),
                _score(wave.get("est_daily_profit_pct")),
                _score(item.get("confidence")),
                _score(wave.get("net_profit_per_cycle_pct")),
            )

        # Keep the best snapshot per market from scan results (for referencing existing slots' current scores)
        candidate_by_market: Dict[str, Dict[str, Any]] = {}
        for c in candidates:
            market = str(c.get("market") or "").strip().upper()
            if not market:
                continue
            prev = candidate_by_market.get(market)
            if prev is None or _candidate_score_tuple(c) > _candidate_score_tuple(prev):
                candidate_by_market[market] = c

        # [FIX] Sync existing slots' live scores + supplement fallback for slots missing from the scan
        # - Slots present in the scan: update live score into params (fallback for the next scan miss)
        # - Slots not in the scan: supplement candidate_by_market with stored live/deploy scores
        #   -> prevents the bug where a slot pushed outside scan top-N is misjudged as confidence=0 and rotated out immediately
        for _slot in scope_slots:
            _mk = str(_slot.get("market") or "").strip().upper()
            if not _mk:
                continue
            try:
                _ctx = self.system.coordinator.contexts.get(_mk)
                if not _ctx:
                    continue
                _ctrls = getattr(_ctx, "controls", {}) or {}
                _strat = _ctrls.get("strategy", {}) or {}
                _params = _strat.get("params")
                if not isinstance(_params, dict):
                    continue
                _item = candidate_by_market.get(_mk)
                if _item:
                    # Present in scan results -> update live score into params
                    _params["live_confidence"] = round(float(_item.get("confidence") or 0.0), 2)
                    _params["live_rank_score"] = round(float(_item.get("rank_score") or 0.0), 6)
                else:
                    _eval_result = None
                    if not bool(_slot.get("has_pos")):
                        try:
                            _eval_result = evaluate_scope_deploy_candidate(
                                _mk,
                                self.system,
                                force_refresh=False,
                            )
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                            logger.warning("state._candidate_score_tuple suppressed exception", exc_info=True)
                            _eval_result = None
                    if _eval_result:
                        _conf = float(_eval_result.get("confidence") or 0.0)
                        _rank = float(_eval_result.get("rank_score") or 0.0)
                        _params["live_confidence"] = round(_conf, 2)
                        _params["live_rank_score"] = round(_rank, 6)
                        candidate_by_market[_mk] = {
                            "market": _mk,
                            "confidence": _conf,
                            "rank_score": _rank,
                            "wave": dict(_eval_result.get("wave") or {}),
                            "entry_gate": dict(_eval_result.get("entry_gate") or {}),
                        }
                        continue
                    # Not in scan results -> supplement candidate_by_market with stored scores
                    _conf = float(_params.get("live_confidence") or _params.get("deploy_confidence") or 0.0)
                    _rank = float(_params.get("live_rank_score") or _params.get("deploy_rank_score") or 0.0)
                    if _conf > 0 or _rank > 0:
                        candidate_by_market[_mk] = {
                            "market": _mk,
                            "confidence": _conf,
                            "rank_score": _rank,
                        }
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[SCOPE] not in scan results; supplement candidate_by_market with stored scores: %s", exc, exc_info=True)

        available: List[Dict[str, Any]] = []
        for c in candidates:
            market = str(c.get("market") or "").strip().upper()
            if not market:
                continue
            # entry_gate is only produced by deep analysis.
            # multi_scan (lightweight) has no entry_gate, so check it only when present.
            _eg = c.get("entry_gate")
            if _eg is not None and not bool(_eg.get("ok", False)):
                continue
            if market in occupied_set:
                continue
            if market in strategy_occupied_set:
                continue
            if market in cooldown_set:
                continue
            if market in existing_scope_set:
                continue
            if market in regular_sniper_set:  # [no overlap] exclude coins registered as regular SNIPER
                continue
            available.append(c)

        if available:
            # Autofill/rotation deploys highest-scoring coins first.
            available.sort(
                key=lambda c: _candidate_score_tuple(c),
                reverse=True,
            )

        if not available:
            logger.info(
                "[Autopilot/ScopeRotation] no available candidates "
                f"(fill_needed={fill_needed}, idle_slots={len(idle_slots)}, "
                f"scan_top_n={scan_top_n}, scan_count={scan_count}, "
                f"candidates={len(candidates)}, occupied={len(occupied_set)}, "
                f"strategy_occupied={len(strategy_occupied_set)}, "
                f"cooldown={len(cooldown_set)}, existing_scope={len(existing_scope_set)})"
            )
            return rotated

        def _calc_dynamic_budget(
            *,
            default_budget: float,
            confidence: float,
            price: float,
            acc_vol_24h: float,
        ) -> float:
            """[root4] Dynamic budget allocation: based on confidence / price tier / liquidity.

            - Higher confidence -> invest more (up to 1.5x)
            - Low-price/low-liquidity coins are scaled down to avoid slippage (avoid over-penalizing)
            - min default_budget * 0.3, max default_budget * 2.0
            """
            base = float(default_budget)

            # (a) Confidence multiplier: 55% -> 0.85x, 70% -> 1.0x, 85%+ -> 1.5x
            if confidence >= 85:
                conf_mul = 1.5
            elif confidence >= 75:
                conf_mul = 1.2
            elif confidence >= 65:
                conf_mul = 1.0
            elif confidence >= 55:
                conf_mul = 0.85
            else:
                conf_mul = 0.7

            # (b) Scale down low-price coins: high tick/spread ratio means slippage risk
            #     Relaxed: floor 0.7 (was 0.5 -> avoid over-penalizing under triple multiplication)
            price_mul = 1.0
            if price < 100:
                price_mul = 0.7
            elif price < 500:
                price_mul = 0.8
            elif price < 1000:
                price_mul = 0.9

            # (c) Liquidity-based: scale down when 24h turnover is small
            #     Relaxed: floor 0.6 (was 0.5)
            liq_mul = 1.0
            if acc_vol_24h < 500_000:  # under 500K USDT
                liq_mul = 0.6
            elif acc_vol_24h < 1_000_000:  # under 1M USDT
                liq_mul = 0.75
            elif acc_vol_24h < 3_000_000:  # under 3M USDT
                liq_mul = 0.85

            result = base * conf_mul * price_mul * liq_mul
            # Minimum budget: 30% of default_budget
            floor = max(5.0, base * 0.3)
            return max(floor, min(base * 2.0, round(result, 0)))

        def _build_deploy_params(
            opt_params: Dict[str, Any],
            deploy_confidence: float = 0.0,
            deploy_rank_score: float = 0.0,
        ) -> Dict[str, Any]:
            return {
                "profile": "SNIPERS",
                "side": "LONG",
                "entry_enabled": True,
                "entry_lookback_min": opt_params.get("lookback_min", 60),
                "entry_threshold_pct": opt_params.get("entry_threshold", 0.3),
                "exit_enabled": True,
                "exit_lookback_min": opt_params.get("lookback_min", 60),
                "exit_threshold_pct": opt_params.get("exit_threshold", 0.3),
                "expiry_min": 30,
                "tp_pct": opt_params.get("tp_pct", 2.0),
                "sl_pct": opt_params.get("sl_pct", 1.5),
                "ai_gate_enabled": True,
                "ai_min_score": 0.55,
                "rsi_entry_enabled": True,
                "rsi_exit_enabled": True,
                "auto_reentry": False,
                "no_demote": True,
                "use_limit": True,
                "fallback_to_market": True,
                "cycle_mode": "UP",
                "mode": "near_low",
                "source": "precision_scope",
                "deploy_confidence": float(deploy_confidence or 0.0),
                "deploy_rank_score": float(deploy_rank_score or 0.0),
                "scope_deploy_ts": time.time(),  # [FIX M1] record deploy time (prevent early cleanup)
            }

        def _cancel_scope_pending(market: str) -> None:
            """Cancel any unfilled order during slot rotation."""
            if not hasattr(self.system, "order_fsm") or not self.system.order_fsm:
                return
            ctx = self.system.coordinator.get_context(market)
            if not ctx:
                return
            if not getattr(ctx, "order_state", None):
                return
            try:
                result = self.system.order_fsm.force_cancel_pending(
                    ctx=ctx, market=market, reason="scope_rotation_replace",
                )
                if result.get("cancelled"):
                    logger.info(f"[ScopeRotation] cancelled pending order {market}: {result.get('uuid', '')}")
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[ScopeRotation] cancel pending %s error: %s", market, exc)

        def _deploy_scope_slot(
            *,
            market: str,
            opt_params: Dict[str, Any],
            budget_usdt: float,
            reason_suffix: str,
            support_price: float = 0.0,
            tp_pct: float = 0.0,
            deploy_confidence: float = 0.0,
            deploy_rank_score: float = 0.0,
            elapsed_carry_sec: float = 0.0,
            instant_buy: bool = False,
        ) -> str:
            new_sniper_id = generate_sniper_id(market)
            self.system.oma_set_market(
                market=market,
                state=MarketState.ACTIVE,
                reason=["precision_scope_deploy", reason_suffix],
            )
            self.system.oma_registry.set_state(
                market,
                MarketState.ACTIVE,
                reason=["precision_scope_budget"],
                budget_usdt=float(budget_usdt),
            )

            deploy_params = _build_deploy_params(
                opt_params or {},
                deploy_confidence=deploy_confidence,
                deploy_rank_score=deploy_rank_score,
            )
            ctx = self.system.coordinator.get_context(market)
            if not ctx:
                ctx = self.system.coordinator.ensure_market(market)
            ctx.update_controls({
                "strategy": {
                    "enabled": True,
                    "mode": "SNIPER(s)",
                    "params": deploy_params,
                }
            })
            ctx.strategy_mode = "SNIPER(s)"

            # [2026-03-07] Carry over the previous slot's elapsed time on rotation
            # -> the 20-min relaxation timer & BPS decay don't restart from 0
            if elapsed_carry_sec > 0:
                ctx.set_var("snipers_scope_elapsed_carry", elapsed_carry_sec)

            store_data: Dict[str, Any] = {
                "budget_usdt": float(budget_usdt),
                "params": deploy_params,
            }

            # [2026-03-08] Instant-buy structure:
            # instant_buy=True -> after confirming confidence+Fire conditions in the scan, buy at deploy time
            # instant_buy=False -> manual placement or fallback (SniperPlugin.decide() judges Fire)
            force_buy_now = instant_buy
            fsm = getattr(self.system, "order_fsm", None)

            if fsm:
                from app.core.hyper_price_store import price_store
                current_price = float(price_store.get_price(market) or 0)
                has_pos = False
                try:
                    pos = getattr(ctx, "position", None) or {}
                    has_pos = float(pos.get("qty", 0) or 0) > 0
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[SCOPE] instant_buy=False; manual placement or fallback (SniperPlugin): %s", exc, exc_info=True)

                if not has_pos and current_price > 0:
                    if force_buy_now:  # Auto path: always False -> SniperPlugin judges Fire
                        try:
                            ok, msg = fsm.submit_market_buy(
                                ctx=ctx,
                                market=market,
                                usdt_amount=budget_usdt,
                                expected_price=current_price,
                                reason="sniper:scope_deploy_buy_now",
                            )
                            if ok:
                                logger.info("[ScopeRotation] market_buy OK %s @ %s", market, current_price)
                            else:
                                logger.warning("[ScopeRotation] market_buy FAIL %s: %s", market, msg)
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[ScopeRotation] market_buy error %s: %s", market, exc)

                    # [2026-03-07] trap mode is also disabled on the auto path
                    # SniperPlugin.decide() determines buy timing based on the BPS Fire score
                    # elif deploy_mode == "trap": ...  (disabled)

            sniper_store.save_position(new_sniper_id, store_data)
            self.system._save_context_state()

            return new_sniper_id

        # 3) [2026-03-08] Instant-buy structure: idle slots (WAITING, no position) are only released.
        # Before: idle slot -> rotate to another coin
        # Changed: idle slot -> release -> autofill deploys only coins meeting instant-buy conditions
        # HOLDING slots are already skipped earlier, so idle_slots contains only WAITING.
        used_markets: Set[str] = set()
        rotated_old_markets: Set[str] = set()
        for slot in idle_slots:
            old_market = str(slot.get("market") or "").strip().upper()
            old_sniper_id = str(slot.get("sniper_id") or "")
            if not old_market:
                continue

            try:
                _cancel_scope_pending(old_market)
                _release_scope_slot(slot, "scope_idle_release")
                self.mark_cooldown(old_market, reason="scope_idle_release")
                rotated_old_markets.add(old_market)
                existing_scope_set.discard(old_market)

                rotated.append({
                    "action": "idle_release",
                    "old_market": old_market,
                    "new_market": "",
                    "idle_min": slot.get("age_min", 0),
                })

                self.system.ledger.append(
                    "SCOPE_SLOT_RELEASE",
                    market=old_market,
                    idle_min=slot.get("age_min", 0),
                    reason="idle_waiting_no_buy",
                )
                logger.info(
                    f"[Autopilot/ScopeInstant] released idle WAITING slot {old_market} "
                    f"(idle {slot.get('age_min', 0)}min) — will refill via instant_buy"
                )
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[Autopilot/ScopeInstant] release %s failed: %s", old_market, exc)

        # 3b) [2026-03-08] Quick rotation is unnecessary in the instant-buy structure
        # WAITING slots are already cleaned up in idle release.
        # HOLDING slots cannot be rotated. So this block is skipped.
        # The existing logic is kept but effectively disabled with quick_rotate_max=0.
        quick_rotate_max = 0
        # - Purpose: (disabled) mitigate the problem of a weakened slot holding time while a stronger candidate waits
        # - Protection: never rotate has_pos=True slots
        def _is_better_for_quick_rotate(
            *,
            old_item: Optional[Dict[str, Any]],
            new_item: Optional[Dict[str, Any]],
        ) -> Tuple[bool, float, float, float, float]:
            old_t = _candidate_score_tuple(old_item)
            new_t = _candidate_score_tuple(new_item)
            old_rank, old_conf = old_t[0], old_t[2]
            new_rank, new_conf = new_t[0], new_t[2]

            if new_t <= old_t:
                return False, old_rank, new_rank, old_conf, new_conf

            # Rotate only if both rank_score >= +10% and confidence >= +5%p are satisfied
            conf_gap_required = 5.0
            if old_rank > 0:
                rank_ok = new_rank >= old_rank * (1.0 + quick_rotate_min_rank_ratio)
                conf_ok = (old_conf <= 0.0) or (new_conf >= old_conf + conf_gap_required)
                if rank_ok and conf_ok:
                    return True, old_rank, new_rank, old_conf, new_conf
            else:
                # No current slot score -> allow rotation if the new candidate is meaningful
                if new_rank > 0.0 and new_conf >= autofill_min_conf:
                    return True, old_rank, new_rank, old_conf, new_conf

            return False, old_rank, new_rank, old_conf, new_conf

        candidate_idx = 0  # [FIX 2026-03-15] undefined variable -> initialize (prevents NameError when quick_rotate is active)
        if quick_rotate_max > 0 and candidate_idx < len(available):
            pre_idle_slots: List[Dict[str, Any]] = []
            for slot in scope_slots:
                market = str(slot.get("market") or "").strip().upper()
                if not market:
                    continue
                if market in rotated_old_markets:
                    continue
                if bool(slot.get("has_pos")):
                    continue
                age_sec = float(slot.get("active_age_sec") or 0.0)
                if age_sec >= float(idle_sec):
                    continue
                if age_sec < float(quick_rotate_min_sec):
                    continue
                cur_item = candidate_by_market.get(market) or {}
                cur_t = _candidate_score_tuple(cur_item)
                slot["live_rank_score"] = round(cur_t[0], 6)
                slot["live_confidence"] = round(cur_t[2], 2)
                pre_idle_slots.append(slot)

            # Rotate weakest slots first: match the strongest candidate to the weakest slot
            pre_idle_slots.sort(
                key=lambda s: (
                    float(s.get("live_rank_score") or 0.0),
                    float(s.get("live_confidence") or 0.0),
                    -float(s.get("active_age_sec") or 0.0),
                )
            )

            quick_rotated = 0
            for slot in pre_idle_slots:
                if quick_rotated >= quick_rotate_max:
                    break
                while candidate_idx < len(available):
                    cand = available[candidate_idx]
                    new_market = str(cand.get("market") or "").strip().upper()
                    if (not new_market) or (new_market in used_markets):
                        candidate_idx += 1
                        continue
                    break
                else:
                    break

                old_market = str(slot.get("market") or "").strip().upper()
                old_sniper_id = str(slot.get("sniper_id") or "")
                if not old_market:
                    continue

                candidate = available[candidate_idx]
                better, old_rank, new_rank, old_conf, new_conf = _is_better_for_quick_rotate(
                    old_item=candidate_by_market.get(old_market),
                    new_item=candidate,
                )
                if not better:
                    # Candidates are sorted descending by score and slots ascending by weakness, so stopping here is safe
                    break

                new_market = str(candidate.get("market") or "").strip().upper()
                new_params = candidate.get("optimal_params", {}) or {}
                new_wave = candidate.get("wave", {}) or {}
                candidate_idx += 1

                try:
                    _cancel_scope_pending(old_market)
                    try:
                        sniper_store.remove_position(old_sniper_id or old_market)
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("[SCOPE] candidates sorted desc by score, slots asc by weakness, stopping here is safe: %s", exc, exc_info=True)
                    self.system.oma_set_market(
                        market=old_market,
                        state=MarketState.WATCH,
                        reason=["scope_rotation_replaced", "pre_idle_score_upgrade"],
                    )
                    self.mark_cooldown(old_market, reason="scope_rotation_preidle")

                    old_budget = float(slot.get("budget_usdt") or 0.0)
                    if old_budget <= 0:
                        old_budget = float(self.system.oma_registry.get_budget_usdt(old_market) or 100.0)
                    old_budget = max(5.0, old_budget)

                    new_sr = candidate.get("support_resistance", {}) or {}
                    new_support = float(new_sr.get("support", 0) or 0)
                    new_tp = float(new_params.get("tp_pct", 0) or 0)

                    # Carry over the previous slot's elapsed time
                    _old_deploy_ts2 = float(slot.get("params", {}).get("scope_deploy_ts", 0) or 0)
                    _carry_sec2 = max(0.0, time.time() - _old_deploy_ts2) if _old_deploy_ts2 > 0 else 0.0

                    _deploy_scope_slot(
                        market=new_market,
                        opt_params=new_params,
                        budget_usdt=old_budget,
                        reason_suffix="scope_rotation_preidle",
                        support_price=new_support,
                        tp_pct=new_tp,
                        deploy_confidence=float(candidate.get("confidence") or 0.0),
                        deploy_rank_score=float(candidate.get("rank_score") or 0.0),
                        elapsed_carry_sec=_carry_sec2,
                    )

                    used_markets.add(new_market)
                    rotated_old_markets.add(old_market)
                    existing_scope_set.discard(old_market)
                    existing_scope_set.add(new_market)
                    quick_rotated += 1

                    age_sec = float(slot.get("active_age_sec") or 0.0)
                    age_min = round(age_sec / 60.0, 1)
                    rotated.append({
                        "action": "preidle_rotate",
                        "old_market": old_market,
                        "new_market": new_market,
                        "idle_min": age_min,
                        "new_daily_est": new_wave.get("est_daily_profit_pct", 0),
                    })
                    self.system.ledger.append(
                        "SCOPE_SLOT_ROTATION_PREIDLE",
                        old_market=old_market,
                        new_market=new_market,
                        age_sec=round(age_sec, 1),
                        old_rank_score=round(old_rank, 6),
                        new_rank_score=round(new_rank, 6),
                        old_confidence=round(old_conf, 2),
                        new_confidence=round(new_conf, 2),
                    )
                    reserved_queue.add_history({
                        "kind": "SCOPE_ROTATION_PREIDLE",
                        "source": "autopilot",
                        "old_market": old_market,
                        "new_market": new_market,
                        "age_min": age_min,
                        "old_rank_score": round(old_rank, 6),
                        "new_rank_score": round(new_rank, 6),
                        "old_confidence": round(old_conf, 2),
                        "new_confidence": round(new_conf, 2),
                    })
                    logger.info(
                        f"[Autopilot/ScopeRotation] pre-idle {old_market} -> {new_market} "
                        f"(age={age_min}m, rank {old_rank:.3f}->{new_rank:.3f}, conf {old_conf:.1f}->{new_conf:.1f})"
                    )
                except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                    logger.warning("[Autopilot/ScopeRotation] pre-idle %s->%s failed: %s", old_market, new_market, exc)

        # 4) Instant-buy structure: when there's an empty slot, find a high-confidence coin -> deploy + instant buy
        # [2026-03-08] Wait->instant shift: don't pre-fill a slot and wait for Fire.
        # If confidence at scan time exceeds the instant-buy threshold (instant_buy_min_conf), buy right away.
        # If below threshold, leave the slot empty -- wait until a good opportunity comes.
        # Operator-configured instant-buy threshold (adjustable in the UI)
        instant_buy_base_raw = getattr(
            self.system,
            "autopilot_scope_instant_buy_min_conf",
            os.getenv("OMA_SCOPE_INSTANT_BUY_MIN_CONF", "55"),
        )
        try:
            instant_buy_base = max(30.0, min(95.0, float(instant_buy_base_raw or 55.0)))
        except (TypeError, ValueError):
            logger.warning("state._is_better_for_quick_rotate suppressed exception", exc_info=True)
            instant_buy_base = 55.0

        # BTC regime-based auto-adjustment:
        # TREND/RECOVERY: threshold as-is ~ slightly loose
        # DRIFT: -5%p (lull -> fewer opportunities, so loosen)
        # SHOCK: +10%p (sharp moves -> tighten, prevent reckless entry)
        _btc_regime = "TREND"
        try:
            from app.monitor.btc_leading_signal import get_btc_leading_detector
            _det = get_btc_leading_detector()
            if _det:
                _btc_regime = str(_det.get_regime_for_lightning() or "TREND").upper()
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[SCOPE] SHOCK: +10%%p (sharp moves -> tighten, prevent reckless entry): %s", exc, exc_info=True)

        _regime_adj = {"TREND": 0.0, "RECOVERY": -3.0, "DRIFT": -5.0, "SHOCK": 10.0}.get(_btc_regime, 0.0)
        instant_buy_after_regime = max(30.0, min(95.0, instant_buy_base + _regime_adj))

        # [2026-03-08] Time decay: the longer an empty slot waits, the more the threshold relaxes
        # -2%p every 10 min, floor = configured value * 0.7
        _empty_since = float(getattr(self.system, "_scope_empty_since_ts", 0.0) or 0.0)
        remaining_fill = max(0, scope_target_autofill - len(existing_scope_set))
        if remaining_fill > 0:
            if _empty_since <= 0:
                # Record when the empty slot first appeared
                _empty_since = now
                try:
                    setattr(self.system, "_scope_empty_since_ts", now)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[SCOPE] record when the empty slot first appeared: %s", exc, exc_info=True)
            _wait_min = (now - _empty_since) / 60.0
            _decay_pct = (_wait_min / 10.0) * 2.0  # -2%p every 10 min
            _floor = instant_buy_base * 0.7  # floor = 70% of the configured value
            instant_buy_min_conf = max(_floor, instant_buy_after_regime - _decay_pct)
            instant_buy_min_conf = max(30.0, min(95.0, instant_buy_min_conf))
        else:
            instant_buy_min_conf = instant_buy_after_regime
            # No empty slots -> reset the timer
            if _empty_since > 0:
                try:
                    setattr(self.system, "_scope_empty_since_ts", 0.0)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[SCOPE] no empty slots -> reset the timer: %s", exc, exc_info=True)

        if remaining_fill > 0:
            scope_budgets = [float(s.get("budget_usdt") or 0.0) for s in scope_slots if float(s.get("budget_usdt") or 0.0) > 0]
            default_budget = max(5.0, float(scope_budgets[0]) if scope_budgets else 100.0)

            autofill_candidates = [
                c for c in candidates
                if (c.get("entry_gate") is None or bool((c.get("entry_gate") or {}).get("ok", False)))
                if str(c.get("market") or "").strip().upper() not in occupied_set
                and str(c.get("market") or "").strip().upper() not in strategy_occupied_set
                and str(c.get("market") or "").strip().upper() not in existing_scope_set
                and str(c.get("market") or "").strip().upper() not in used_markets
                and str(c.get("market") or "").strip().upper() not in regular_sniper_set
            ]
            autofill_candidates.sort(key=lambda c: _candidate_score_tuple(c), reverse=True)
            autofill_idx = 0

            for _ in range(remaining_fill):
                is_alpha_slot = scope_target_alpha > 0 and len(existing_scope_set) >= scope_target_base
                while autofill_idx < len(autofill_candidates):
                    new_market = str(autofill_candidates[autofill_idx].get("market") or "").strip().upper()
                    new_params = autofill_candidates[autofill_idx].get("optimal_params", {}) or {}
                    new_wave = autofill_candidates[autofill_idx].get("wave", {}) or {}
                    new_conf = 0.0
                    try:
                        new_conf = float(autofill_candidates[autofill_idx].get("confidence") or 0.0)
                    except (TypeError, ValueError):
                        logger.warning("state._is_better_for_quick_rotate suppressed exception", exc_info=True)
                        new_conf = 0.0
                    autofill_idx += 1
                    if not new_market or new_market in used_markets:
                        continue
                    if new_market in existing_scope_set:
                        continue
                    # Instant-buy minimum confidence: leave the slot empty if below this threshold
                    if new_conf < instant_buy_min_conf:
                        continue
                    if is_alpha_slot and new_conf < scope_alpha_min_conf:
                        continue
                    break
                else:
                    break

                try:
                    _cand = autofill_candidates[autofill_idx - 1]
                    fill_sr = _cand.get("support_resistance", {}) or {}
                    fill_support = float(fill_sr.get("support", 0) or 0)
                    fill_tp = float(new_params.get("tp_pct", 0) or 0)
                    _cand_conf = float(_cand.get("confidence") or 0.0)
                    _cand_rank = float(_cand.get("rank_score") or 0.0)

                    # [root4] Dynamic budget allocation
                    _inst_budget = _calc_dynamic_budget(
                        default_budget=default_budget,
                        confidence=_cand_conf,
                        price=float(_cand.get("price") or 0),
                        acc_vol_24h=float(_cand.get("acc_trade_price_24h") or 0),
                    )

                    _deploy_scope_slot(
                        market=new_market,
                        opt_params=new_params,
                        budget_usdt=_inst_budget,
                        reason_suffix="scope_instant_buy",
                        support_price=fill_support,
                        tp_pct=fill_tp,
                        deploy_confidence=_cand_conf,
                        deploy_rank_score=_cand_rank,
                        instant_buy=True,
                    )
                    used_markets.add(new_market)
                    existing_scope_set.add(new_market)
                    if is_alpha_slot and scope_alpha_started_ts <= 0:
                        scope_alpha_started_ts = now
                        try:
                            setattr(self.system, "autopilot_scope_alpha_started_ts", float(scope_alpha_started_ts))
                        except (TypeError, ValueError) as exc:
                            logger.warning("[SCOPE] [root4] dynamic budget allocation: %s", exc, exc_info=True)

                    rotated.append({
                        "action": "fill",
                        "old_market": "",
                        "new_market": new_market,
                        "idle_min": 0,
                        "new_daily_est": new_wave.get("est_daily_profit_pct", 0),
                    })
                    self.system.ledger.append(
                        "SCOPE_SLOT_AUTOFILL",
                        new_market=new_market,
                        target_slots=scope_target_autofill,
                    )
                    reserved_queue.add_history({
                        "kind": "SCOPE_AUTOFILL",
                        "source": "autopilot",
                        "new_market": new_market,
                        "target_slots": scope_target_autofill,
                    })
                    logger.info(
                        f"[Autopilot/ScopeRotation] autofill + {new_market} "
                        f"(target={scope_target_autofill}, reserve={effective_operator_reserve})"
                    )
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[Autopilot/ScopeRotation] autofill %s failed: %s", new_market, exc)

        return rotated
