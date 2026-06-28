# ============================================================
# File: app/manager/autopilot_slot_lifecycle.py
# Autocoin OS — Slot Lifecycle Mixin (extracted from autopilot_manager.py)
# ============================================================

from __future__ import annotations

import asyncio
import functools
import os
import time
from typing import Any, Dict, List, Optional, Tuple, Set

from app.core.currency import Q

from app.manager.oma_market_registry import MarketState
from app.manager.reserved_queue import reserved_queue
from app.manager.market_controls import apply_engine_controls
from app.manager.autopilot_helpers import (
    normalize_strategy_name as _normalize_strategy_name,
)

import logging

logger = logging.getLogger(__name__)


class SlotLifecycleMixin:

    def _infer_strategy(self, market: str, active_reason_map: Dict[str, List[str]]) -> str:
        rs = active_reason_map.get(market) or []
        for r in rs:
            if isinstance(r, str) and r.upper().startswith("STRATEGY:"):
                return r.split(":", 1)[1].strip().upper() or "UNKNOWN"
        try:
            ctx = self.system.coordinator.contexts.get(market)
            ctrls = getattr(ctx, "controls", {}) or {}
            sc = ctrls.get("strategy") or {}
            if isinstance(sc, dict) and bool(sc.get("enabled")):
                md = str(sc.get("mode") or "").strip().upper()
                if md: return md
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("Failed to infer strategy from controls for %s: %s", market, exc)
        try:
            ctx = self.system.coordinator.contexts.get(market)
            sel = str(getattr(ctx, "selected_strategy", "") or "").strip().upper()
            if sel: return sel
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("Failed to infer strategy from selected_strategy for %s: %s", market, exc)
        return "UNKNOWN"

    def _is_demote_protected(self, market: str) -> bool:
        """Check whether the market is excluded from Autopilot demote/idle-longhold.

        - Strategy params no_demote/sticky/sniper_sticky
        - Operated separately from the existing user_sell_only protection rule
        """
        try:
            ctx = self.system.coordinator.contexts.get(market)
            if not ctx:
                return False
            ctrls = getattr(ctx, "controls", {}) or {}
            sp = (ctrls.get("strategy", {}) or {}).get("params", {}) or {}
            return bool(
                sp.get("no_demote")
                or sp.get("sticky")
                or sp.get("sniper_sticky")
            )
        except (KeyError, AttributeError, TypeError):
            logger.warning("SlotLifecycleMixin._is_demote_protected suppressed exception", exc_info=True)
            return False

    def _position_snapshot(self, market: str) -> Tuple[bool, float, float]:
        """Return current position held flag / quantity / valuation (USDT)."""
        qty = 0.0
        value_usdt = 0.0
        try:
            ctx = self.system.coordinator.contexts.get(market)
            if not ctx:
                return False, 0.0, 0.0
            pos = getattr(ctx, "position", None)
            if not pos:
                return False, 0.0, 0.0
            qty = float(pos.get("qty", 0) or 0)
            if qty <= 0:
                return False, 0.0, 0.0
            avg_buy = float(pos.get("avg_price", 0) or pos.get("entry", 0) or 0)
            cur = 0.0
            try:
                from app.core.hyper_price_store import price_store
                cur = float(price_store.get_price(market) or 0.0)
            except (TypeError, ValueError):
                logger.warning("SlotLifecycleMixin._position_snapshot suppressed exception", exc_info=True)
                cur = 0.0
            if cur <= 0:
                cur = avg_buy
            value_usdt = float(qty) * float(cur or 0.0)
            return True, float(qty), float(max(0.0, value_usdt))
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("SlotLifecycleMixin._position_snapshot suppressed exception", exc_info=True)
            return False, 0.0, 0.0

    async def _step_slot_lifecycle(
        self,
        *,
        snap: Dict[str, Any],
        longhold_markets: Set[str],
        now: float,
        reason: str,
        idle_en: bool,
        idle_min: int,
        grace_sec: int,
        demote_max_total: int,
        demote_max_per_strategy: int,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {}

        # Step 2) Active map
        active_rows = snap.get("active") or []
        active_reason_map: Dict[str, List[str]] = {}
        active_markets: List[str] = []

        for row in active_rows:
            if isinstance(row, dict):
                m = str(row.get("market") or "").strip().upper()
                if not m: continue
                active_markets.append(m)
                rs = row.get("reason")
                if isinstance(rs, list):
                    active_reason_map[m] = [str(x) for x in rs]
                else:
                    active_reason_map[m] = []
            elif isinstance(row, str):
                m = str(row).strip().upper()
                if m: active_markets.append(m)

        # Step 2a) Orphan cleanup (stuck/empty contexts)
        # - If RECOVERY but position/order are empty, clean up to WATCH
        # - If ACTIVE but strategy unassigned (OFF/UNKNOWN) + position/order empty, clean up to WATCH
        orphan_cleaned: List[Dict[str, Any]] = []
        orphan_cleanup_en = str(os.getenv("OMA_AUTOPILOT_ORPHAN_CLEANUP_ENABLED", "1")).strip().lower() in ("1", "true", "yes", "on")
        if orphan_cleanup_en:
            recovery_rows = snap.get("recovery") or []
            active_rows_for_orphan = snap.get("active") or []

            def _iter_markets(rows: List[Any], bucket: str) -> List[Tuple[str, str]]:
                out: List[Tuple[str, str]] = []
                for row in rows:
                    try:
                        if isinstance(row, dict):
                            mk = str(row.get("market") or "").strip().upper()
                        else:
                            mk = str(row or "").strip().upper()
                        if not mk:
                            continue
                        out.append((mk, bucket))
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("Failed to parse market row in _iter_markets: %s", exc)
                        continue
                return out

            orphan_targets = _iter_markets(recovery_rows, "RECOVERY")
            orphan_targets.extend(_iter_markets(active_rows_for_orphan, "ACTIVE"))

            for mkt, bucket in orphan_targets:
                try:
                    if self._is_demote_protected(mkt):
                        continue

                    ctx = self.system.coordinator.contexts.get(mkt)
                    if not ctx:
                        continue

                    # Exclude LongHold / user_sell_only markets from cleanup
                    is_longhold = False
                    user_sell_only = False
                    try:
                        ladder_mgr = getattr(self.system, "ladder_manager", None)
                        if ladder_mgr:
                            lh_cfg = ladder_mgr.get_longhold_config(mkt)
                            if lh_cfg and lh_cfg.get("enabled"):
                                is_longhold = True
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.warning("Failed to check LongHold config for %s: %s", mkt, exc)
                    try:
                        ctrls0 = getattr(ctx, "controls", {}) or {}
                        sp0 = (ctrls0.get("strategy", {}) or {}).get("params", {}) or {}
                        user_sell_only = bool(sp0.get("user_sell_only", False))
                    except (KeyError, AttributeError, TypeError):
                        logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                        user_sell_only = False
                    if is_longhold or user_sell_only:
                        continue

                    # Check position/order state
                    has_position = False
                    try:
                        pos = getattr(ctx, "position", None)
                        if pos and float(pos.get("qty", 0) or 0) > 0:
                            has_position = True
                    except (KeyError, AttributeError, TypeError, ValueError):
                        logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                        has_position = False
                    has_order = bool(getattr(ctx, "order_state", None))

                    if has_position or has_order:
                        continue

                    strat = self._infer_strategy(mkt, active_reason_map)
                    # RECOVERY: if context is empty, clean up regardless of strategy
                    # ACTIVE: clean up only when strategy is unassigned
                    if bucket == "ACTIVE" and strat not in ("", "OFF", "UNKNOWN"):
                        continue

                    self.system.oma_set_market(
                        market=mkt,
                        state=MarketState.WATCH,
                        reason=[
                            "autopilot_orphan_cleanup",
                            f"from:{bucket}",
                            f"strategy:{strat or 'UNKNOWN'}",
                            f"source:{reason}",
                        ],
                    )
                    try:
                        from app.manager.autopilot_tracker import autopilot_tracker
                        _r = " ".join([str(x) for x in ["autopilot_orphan_cleanup", f"from:{bucket}", f"strategy:{strat or 'UNKNOWN'}", f"source:{reason}"]])
                        autopilot_tracker.record_decision(mkt, bucket, "WATCH", strat or "UNKNOWN", _r)
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("Failed to record orphan cleanup decision for %s: %s", mkt, exc)
                    orphan_cleaned.append({
                        "market": mkt,
                        "from": bucket,
                        "strategy": strat or "UNKNOWN",
                    })

                    try:
                        self.mark_cooldown(mkt, reason="orphan_cleanup")
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("Failed to mark cooldown after orphan cleanup for %s: %s", mkt, exc, exc_info=True)

                    try:
                        reserved_queue.add_history({
                            "kind": "CLEANUP",
                            "source": "autopilot",
                            "market": mkt,
                            "reason": "orphan",
                            "from": bucket,
                            "strategy": strat or "UNKNOWN",
                        })
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("Failed to add orphan cleanup history for %s: %s", mkt, exc)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.system.ledger.append("AUTOPILOT_ORPHAN_CLEANUP_ERROR", market=mkt, error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("Failed to log orphan cleanup error for %s: %s", mkt, exc2)

        result["orphan_cleaned"] = orphan_cleaned

        # Step 2.5) WATCH timeout -> auto cleanup to DISABLED
        # 2026-03-10: Remove coins that remain in WATCH after selling, after a set time,
        # to release the strategy slot/ownership and prevent repeated selection of the same coin
        watch_timeout_cleaned = 0
        try:
            _watch_timeout_sec = int(os.getenv("AUTOPILOT_WATCH_TIMEOUT_SEC", "3600"))  # default 1 hour
            if _watch_timeout_sec > 0:
                _snap_w = self.system.oma_registry.snapshot()
                for _wr in (_snap_w.get("watch") or []):
                    if not isinstance(_wr, dict):
                        continue
                    _wm = str(_wr.get("market") or "").strip().upper()
                    if not _wm:
                        continue
                    _wts = 0.0
                    try:
                        _wts = float(_wr.get("watch_since_ts") or 0.0)
                    except (TypeError, ValueError):
                        logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                        _wts = 0.0
                    if _wts <= 0:
                        continue
                    if (now - _wts) < _watch_timeout_sec:
                        continue
                    # Do not touch if a position is held
                    try:
                        _wctx = self.system.coordinator.get_context(_wm)
                        _wqty = float(getattr(_wctx, "position", None) and getattr(_wctx.position, "qty", 0) or 0)
                        if _wqty > 0:
                            continue
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("Failed to check position for watch-timeout market %s: %s", _wm, exc, exc_info=True)
                    try:
                        self.system.oma_registry.set_state(
                            _wm, MarketState.DISABLED,
                            reason=["watch_timeout_auto_disabled"],
                        )
                        watch_timeout_cleaned += 1
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.error("Failed to set DISABLED state for watch-timeout market %s: %s", _wm, exc, exc_info=True)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.error("Failed to process watch-timeout cleanup: %s", exc, exc_info=True)
        result["watch_timeout_cleaned"] = watch_timeout_cleaned

        # Step 3) Demote Idle
        demoted: List[Dict[str, Any]] = []
        dust_cleanup_targets: List[Dict[str, Any]] = []
        if idle_en and idle_min > 0:
            max_idle = idle_min
            overrides = getattr(self.system, "autopilot_idle_demote_overrides", {}) or {}
            if overrides:
                max_idle = max(max_idle, max(overrides.values()))
            try:
                dust_threshold_usdt = float(
                    getattr(self.system, "dust_vacuum_threshold_usdt", 5.0) or 5.0
                )
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                dust_threshold_usdt = 5.0
            try:
                min_order_usdt = float(getattr(self.system, "min_order_usdt", 5.0) or 5.0)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                min_order_usdt = 5.0
            dust_threshold_usdt = max(1.0, min(float(dust_threshold_usdt), float(min_order_usdt)))

            max_window_sec = int(max_idle) * 60
            since_ts = now - float(max_window_sec)

            records: List[Dict[str, Any]] = []
            try:
                records = await asyncio.to_thread(
                    functools.partial(self.system.ledger.tail_records, since_ts=since_ts, tail_lines=int(os.getenv("OMA_AUTOPILOT_IDLE_TAIL_LINES", "50000")))
                )
            except (TypeError, ValueError):
                logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                records = []

            last_fill_ts_map: Dict[str, float] = {}
            for rec in records:
                try:
                    ev = str(rec.get("event") or "")
                    if ev not in ("FILL_BUY", "FILL_SELL"): continue
                    mk = str(rec.get("market") or rec.get("data", {}).get("market") or "").strip().upper()
                    if not mk: continue
                    ts = float(rec.get("ts") or 0.0)
                    if ts > last_fill_ts_map.get(mk, 0.0):
                        last_fill_ts_map[mk] = ts
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("Failed to parse fill record for idle demote: %s", exc)
                    continue

            # (age_sec, strategy, market, idle_limit_min, rule)
            # rule: idle_no_position | dust_cleanup
            candidates: List[Tuple[float, str, str, int, str]] = []
            for mkt in active_markets:
                try:
                    since_active = float(self.system.oma_registry.get_active_since_ts(mkt) or 0.0)
                except (TypeError, ValueError):
                    logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                    since_active = 0.0

                strat = self._infer_strategy(mkt, active_reason_map)
                limit_min = overrides.get(strat, idle_min)
                limit_sec = limit_min * 60

                last_fill = last_fill_ts_map.get(mkt, 0.0)
                idle_duration = now - max(last_fill, since_active)
                age = (now - since_active) if since_active > 0 else 0.0

                if grace_sec > 0 and age > 0 and age < grace_sec:
                    continue
                if idle_duration < limit_sec:
                    continue

                # [PROTECTED] GAZUA strategy is for user manual trading, so exclude it from demote
                # DO NOT MODIFY: this logic is protected by user instruction (2026-01-23)
                if strat == "GAZUA":
                    continue

                # [2026-02-01] LongHold markets are also excluded from demote (respect long-hold intent)
                try:
                    ladder_mgr = getattr(self.system, "ladder_manager", None)
                    if ladder_mgr:
                        lh_cfg = ladder_mgr.get_longhold_config(mkt)
                        if lh_cfg and lh_cfg.get("enabled"):
                            continue
                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("Failed to check LongHold config for demote exclusion %s: %s", mkt, exc)

                # [2026-02-01] markets with user_sell_only=True are also excluded from demote
                try:
                    ctx = self.system.coordinator.contexts.get(mkt)
                    if ctx:
                        ctrls = getattr(ctx, "controls", {}) or {}
                        sp = ctrls.get("strategy", {}).get("params", {}) or {}
                        if sp.get("user_sell_only"):
                            continue
                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("Failed to check user_sell_only for demote exclusion %s: %s", mkt, exc)

                # [SNIPER(s)] exclude from demote when no_demote/sticky is set
                if self._is_demote_protected(mkt):
                    continue

                # LADDER is excluded from this common rule (keep as is).
                if strat == "LADDER":
                    continue

                has_pos, pos_qty, pos_value_usdt = self._position_snapshot(mkt)
                if has_pos:
                    # If held, do not demote immediately:
                    # - if dust, send to WATCH as a cleanup target to rotate the slot
                    # - normal holdings are handled in Step 3a (LongHold conversion)
                    if pos_value_usdt < dust_threshold_usdt:
                        candidates.append((age, strat, mkt, int(limit_min), "dust_cleanup"))
                    continue

                # If not held, quietly demote to WATCH and pass it on as a slot-refill target.
                candidates.append((age, strat, mkt, int(limit_min), "idle_no_position"))

            candidates.sort(key=lambda x: x[0], reverse=True)

            total_limit = demote_max_total if demote_max_total > 0 else 10_000
            per_limit = demote_max_per_strategy if demote_max_per_strategy > 0 else 10_000
            per_cnt: Dict[str, int] = {}

            for age, strat, mkt, limit_min_local, rule in candidates:
                if len(demoted) >= total_limit: break
                if per_cnt.get(strat, 0) >= per_limit: continue

                try:
                    reason_tags = [
                        "autopilot_demote_idle",
                        f"idle_min:{int(limit_min_local)}",
                        f"active_age_sec:{int(age)}",
                        f"source:{reason}",
                    ]
                    if rule == "dust_cleanup":
                        reason_tags.insert(0, "autopilot_dust_cleanup_target")
                    self.system.oma_set_market(
                        market=mkt,
                        state=MarketState.WATCH,
                        reason=reason_tags,
                    )
                    try:
                        from app.manager.autopilot_tracker import autopilot_tracker
                        _r = " ".join([str(x) for x in reason_tags])
                        autopilot_tracker.record_decision(mkt, "ACTIVE", "WATCH", strat, _r)
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("Failed to record demote decision for %s: %s", mkt, exc)
                    demoted.append({
                        "market": mkt,
                        "strategy": strat,
                        "active_age_sec": int(age),
                        "idle_min": int(limit_min_local),
                        "rule": str(rule),
                    })
                    per_cnt[strat] = int(per_cnt.get(strat, 0) + 1)
                    if rule == "dust_cleanup":
                        _, _, pv = self._position_snapshot(mkt)
                        dust_cleanup_targets.append({
                            "market": mkt,
                            "strategy": strat,
                            "position_value_usdt": round(float(pv), 2),
                            "dust_threshold_usdt": round(float(dust_threshold_usdt), 2),
                        })

                    try:
                        self.mark_cooldown(
                            mkt,
                            reason="demote_dust_cleanup" if rule == "dust_cleanup" else "demote_idle",
                        )
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("Failed to mark cooldown after demote for %s: %s", mkt, exc, exc_info=True)

                    try:
                        reserved_queue.add_history({
                            "kind": "DEMOTE",
                            "source": "autopilot",
                            "market": mkt,
                            "strategy": strat,
                            "reason": "dust_cleanup" if rule == "dust_cleanup" else "idle",
                            "idle_min": int(limit_min_local),
                            "active_age_sec": int(age),
                        })
                    except (TypeError, ValueError) as exc:
                        logger.warning("Failed to add demote history for %s: %s", mkt, exc)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.system.ledger.append("AUTOPILOT_DEMOTE_ERROR", market=mkt, error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("Failed to log demote error for %s: %s", mkt, exc2)

        # Step 3a) 24h+ no trades + held position -> auto-convert to LongHold (common rule, LADDER excluded)
        longhold_converted: List[Dict[str, Any]] = []
        idle_to_longhold_en = bool(getattr(self.system, "autopilot_idle_to_longhold_enabled", True))
        idle_to_longhold_hours = max(1, int(getattr(self.system, "autopilot_idle_to_longhold_hours", 24) or 24))
        idle_to_longhold_sec = idle_to_longhold_hours * 3600

        longhold_candidates: List[str] = []
        if idle_to_longhold_en:
            # Exclude already-demoted markets
            demoted_markets = {str(d.get("market") or "").strip().upper() for d in demoted if d.get("market")}
            recovery_rows = snap.get("recovery") or []
            recovery_markets: List[str] = []
            for row in recovery_rows:
                try:
                    if isinstance(row, dict):
                        mk = str(row.get("market") or "").strip().upper()
                    else:
                        mk = str(row or "").strip().upper()
                    if mk:
                        recovery_markets.append(mk)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("Failed to parse recovery row for LongHold candidates: %s", exc)
                    continue

            seen_longhold: set[str] = set()
            for mk in list(active_markets) + recovery_markets:
                mku = str(mk or "").strip().upper()
                if not mku or mku in seen_longhold:
                    continue
                seen_longhold.add(mku)
                if mku in demoted_markets:
                    continue
                longhold_candidates.append(mku)

        if idle_to_longhold_en and longhold_candidates:
            # Query FILL records over a 24-hour window
            since_ts_24h = now - float(idle_to_longhold_sec)

            records_24h: List[Dict[str, Any]] = []
            try:
                records_24h = await asyncio.to_thread(
                    functools.partial(
                        self.system.ledger.tail_records,
                        since_ts=since_ts_24h,
                        tail_lines=int(os.getenv("OMA_AUTOPILOT_IDLE_TAIL_LINES", "80000"))
                    )
                )
            except (TypeError, ValueError):
                logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                records_24h = []

            last_fill_ts_24h: Dict[str, float] = {}
            for rec in records_24h:
                try:
                    ev = str(rec.get("event") or "")
                    if ev not in ("FILL_BUY", "FILL_SELL"):
                        continue
                    mk = str(rec.get("market") or rec.get("data", {}).get("market") or "").strip().upper()
                    if not mk:
                        continue
                    ts = float(rec.get("ts") or 0.0)
                    if ts > last_fill_ts_24h.get(mk, 0.0):
                        last_fill_ts_24h[mk] = ts
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("Failed to parse fill record for LongHold check: %s", exc)
                    continue

            for mkt in longhold_candidates:
                # [SNIPER(s)] exclude from idle->LongHold conversion when no_demote/sticky is set
                if self._is_demote_protected(mkt):
                    continue

                strat = self._infer_strategy(mkt, active_reason_map)

                # LADDER keeps its separate operating policy (excluded from the common rotation rule)
                if strat == "LADDER":
                    continue

                # Exclude markets that are already LongHold
                try:
                    ladder_mgr = getattr(self.system, "ladder_manager", None)
                    if ladder_mgr:
                        lh_cfg = ladder_mgr.get_longhold_config(mkt)
                        if lh_cfg and lh_cfg.get("enabled"):
                            continue
                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("Failed to check existing LongHold config for %s: %s", mkt, exc)

                # GAZUA is a manual strategy too, so it is already excluded from demote (PROTECTED)
                # But whether to convert to LongHold is judged separately: GAZUA is not excluded

                try:
                    since_active = float(self.system.oma_registry.get_active_since_ts(mkt) or 0.0)
                except (TypeError, ValueError):
                    logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                    since_active = 0.0

                last_fill = last_fill_ts_24h.get(mkt, 0.0)
                idle_duration = now - max(last_fill, since_active)
                age = (now - since_active) if since_active > 0 else 0.0

                # Check for no trades for 24h or more
                if idle_duration < idle_to_longhold_sec:
                    continue

                # A position must exist for LongHold conversion to be meaningful
                has_position = False
                avg_buy_price = 0.0
                position_qty = 0.0
                position_value_usdt = 0.0
                try:
                    ctx = self.system.coordinator.contexts.get(mkt)
                    if ctx:
                        pos = getattr(ctx, "position", None)
                        if pos:
                            position_qty = float(pos.get("qty", 0) or 0)
                            avg_buy_price = float(pos.get("avg_price", 0) or pos.get("entry", 0) or 0)
                            has_position = position_qty > 0
                            # Compute position value at current price
                            try:
                                from app.core.hyper_price_store import price_store
                                current_price = price_store.get_price(mkt) or avg_buy_price
                                position_value_usdt = position_qty * current_price
                            except (ImportError, AttributeError, TypeError):
                                logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                                position_value_usdt = position_qty * avg_buy_price
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.error("Failed to compute position value for LongHold candidate %s: %s", mkt, exc, exc_info=True)

                if not has_position:
                    continue

                # Dust positions are not sent to LongHold, only classified as periodic cleanup targets.
                # (the slot is demoted to WATCH to guarantee rotation)
                try:
                    dust_threshold_usdt = float(
                        getattr(self.system, "dust_vacuum_threshold_usdt", 5.0) or 5.0
                    )
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                    dust_threshold_usdt = 5.0
                if position_value_usdt < dust_threshold_usdt:
                    try:
                        self.system.oma_set_market(
                            market=mkt,
                            state=MarketState.WATCH,
                            reason=[
                                "autopilot_dust_cleanup_target",
                                f"position_value_usdt:{round(float(position_value_usdt), 2)}",
                                f"threshold_usdt:{round(float(dust_threshold_usdt), 2)}",
                                f"source:{reason}",
                            ],
                        )
                        try:
                            from app.manager.autopilot_tracker import autopilot_tracker
                            _r = "autopilot_dust_cleanup_target position_value_usdt:" + str(round(float(position_value_usdt), 2))
                            autopilot_tracker.record_decision(mkt, "ACTIVE", "WATCH", strat, _r)
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("Failed to record dust cleanup decision for %s: %s", mkt, exc)
                        self.mark_cooldown(mkt, reason="demote_dust_cleanup")
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("Failed to demote dust position for %s: %s", mkt, exc, exc_info=True)
                    dust_cleanup_targets.append({
                        "market": mkt,
                        "strategy": strat,
                        "position_value_usdt": round(float(position_value_usdt), 2),
                        "dust_threshold_usdt": round(float(dust_threshold_usdt), 2),
                    })
                    continue

                # Execute LongHold conversion
                try:
                    ladder_mgr = getattr(self.system, "ladder_manager", None)
                    if ladder_mgr:
                        # Create LongHold config
                        ladder_mgr.save_longhold_config({
                            "market": mkt,
                            "enabled": True,
                            "strategy": "GAZUA",
                            "target_profit_pct": 50.0,
                            "budget_usdt": 0,
                            "repeat": True,
                        })

                        # Set user_sell_only=True in context_state
                        from app.manager.market_controls import apply_engine_controls
                        apply_engine_controls(
                            self.system,
                            mkt,
                            "GAZUA",
                            user_sell_only=True,
                            stoploss_pct=-50.0,
                        )

                        # Update OMA state (keep ACTIVE, only change reason)
                        self.system.oma_set_market(
                            market=mkt,
                            state=MarketState.ACTIVE,
                            reason=[
                                "strategy:GAZUA",
                                "autopilot_idle_to_longhold",
                                f"idle_hours:{idle_to_longhold_hours}",
                                f"from:{strat}",
                            ],
                        )

                        longhold_converted.append({
                            "market": mkt,
                            "from_strategy": strat,
                            "idle_hours": round(idle_duration / 3600, 1),
                            "avg_buy_price": avg_buy_price,
                            "position_qty": position_qty,
                        })

                        self.system.ledger.append(
                            "AUTOPILOT_IDLE_TO_LONGHOLD",
                            market=mkt,
                            from_strategy=strat,
                            idle_hours=round(idle_duration / 3600, 1),
                            avg_buy_price=avg_buy_price,
                            position_qty=position_qty,
                        )

                        reserved_queue.add_history({
                            "kind": "IDLE_TO_LONGHOLD",
                            "source": "autopilot",
                            "market": mkt,
                            "from_strategy": strat,
                            "idle_hours": round(idle_duration / 3600, 1),
                        })

                        logger.info(
                            f"[Autopilot] {mkt} → LongHold (idle {idle_duration/3600:.1f}h, from {strat})"
                        )
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning(f"[Autopilot] Failed to convert {mkt} to LongHold: {exc}")

        result["longhold_converted"] = longhold_converted

        # Step 3b) Perf Demote
        perf_en = bool(getattr(self.system, "autopilot_perf_demote_enabled", False))
        perf_window_min = max(0, int(getattr(self.system, "autopilot_perf_window_min", 0) or 0))
        if perf_en and perf_window_min > 0 and active_markets:
            perf_min_trades = max(0, int(getattr(self.system, "autopilot_perf_min_trades", 0) or 0))
            perf_min_sells = max(0, int(getattr(self.system, "autopilot_perf_min_sells", 0) or 0))
            try:
                perf_min_net_cash = float(getattr(self.system, "autopilot_perf_min_net_cash_usdt", 0.0) or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                perf_min_net_cash = 0.0
            try:
                perf_min_net_cash_per_trade = float(getattr(self.system, "autopilot_perf_min_net_cash_per_trade", 0.0) or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                perf_min_net_cash_per_trade = 0.0

            window_sec = int(perf_window_min) * 60
            since_ts = now - float(window_sec)

            records: List[Dict[str, Any]] = []
            try:
                records = await asyncio.to_thread(
                    functools.partial(self.system.ledger.tail_records, since_ts=since_ts, tail_lines=int(os.getenv("OMA_AUTOPILOT_PERF_TAIL_LINES", "80000")))
                )
            except (TypeError, ValueError):
                logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                records = []

            try:
                from app.manager.ledger_pnl import aggregate_fill_pnl
            except (ImportError, AttributeError, TypeError):
                logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                aggregate_fill_pnl = None

            aggs: Dict[str, Dict[str, Any]] = {}
            if callable(aggregate_fill_pnl):
                try:
                    aggs = aggregate_fill_pnl(records, since_ts=since_ts, until_ts=now, markets=active_markets)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                    logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                    aggs = {}

            total_limit = demote_max_total if demote_max_total > 0 else 10_000
            per_limit = demote_max_per_strategy if demote_max_per_strategy > 0 else 10_000
            per_cnt2: Dict[str, int] = {}
            for d in demoted:
                try:
                    s0 = str(d.get("strategy") or "UNKNOWN").upper()
                    per_cnt2[s0] = int(per_cnt2.get(s0, 0) + 1)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("Failed to count demoted strategy for perf demote: %s", exc)
                    continue

            perf_candidates: List[Tuple[float, float, int, str, str, float, int, int, int, float]] = []
            for mkt in active_markets:
                strat = self._infer_strategy(mkt, active_reason_map)
                # LADDER is excluded from the common rotation/demote rule (keep as is)
                if strat == "LADDER":
                    continue
                if self._is_demote_protected(mkt):
                    continue
                # Held positions are excluded from perf-demote.
                # (long-held positions are handled by LongHold conversion in Step 3a)
                has_pos, _, _ = self._position_snapshot(mkt)
                if has_pos:
                    continue

                try:
                    since_active = float(self.system.oma_registry.get_active_since_ts(mkt) or 0.0)
                except (TypeError, ValueError):
                    logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                    since_active = 0.0
                age = (now - since_active) if since_active > 0 else 0.0
                if grace_sec > 0 and age > 0 and age < grace_sec:
                    continue

                a = aggs.get(mkt) or {}
                try:
                    trade_n = int(a.get("trade_n") or 0)
                    sell_n = int(a.get("sell_n") or 0)
                    buy_n = int(a.get("buy_n") or 0)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                    trade_n, sell_n, buy_n = 0, 0, 0

                if perf_min_trades > 0 and trade_n < perf_min_trades: continue
                if perf_min_sells > 0 and sell_n < perf_min_sells: continue

                try:
                    net_cash = float(a.get("net_cash_usdt") or a.get("net_cash_usdt") or 0.0)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                    net_cash = 0.0
                try:
                    fees = float(a.get("fees_usdt") or a.get("fees_usdt") or 0.0)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                    fees = 0.0

                net_per_trade = float(net_cash) / float(trade_n or 1)

                if float(net_cash) >= float(perf_min_net_cash): continue
                if float(perf_min_net_cash_per_trade) > 0 and float(net_per_trade) >= float(perf_min_net_cash_per_trade): continue

                perf_candidates.append((float(net_per_trade), float(net_cash), int(trade_n), strat, mkt, float(fees), int(trade_n), int(buy_n), int(sell_n), float(age)))

            perf_candidates.sort(key=lambda x: (x[0], x[1], -x[2]))

            for net_per_trade, net_cash, trade_n0, strat, mkt, fees, trade_n, buy_n, sell_n, age in perf_candidates:
                if len(demoted) >= total_limit: break
                if per_cnt2.get(strat, 0) >= per_limit: continue

                try:
                    self.system.oma_set_market(
                        market=mkt,
                        state=MarketState.WATCH,
                        reason=[
                            "autopilot_demote_underperf",
                            f"window_min:{int(perf_window_min)}",
                            f"net_cash_usdt:{round(float(net_cash), 2)}",
                            f"source:{reason}",
                        ],
                    )
                    try:
                        from app.manager.autopilot_tracker import autopilot_tracker
                        _r = "autopilot_demote_underperf window_min:" + str(int(perf_window_min))
                        autopilot_tracker.record_decision(mkt, "ACTIVE", "WATCH", strat, _r)
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("Failed to record underperf demote decision for %s: %s", mkt, exc)
                    demoted.append({
                        "market": mkt,
                        "strategy": strat,
                        "active_age_sec": int(age),
                        "rule": "underperf",
                    })
                    per_cnt2[strat] = int(per_cnt2.get(strat, 0) + 1)

                    try:
                        self.mark_cooldown(mkt, reason="demote_underperf")
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("Failed to mark cooldown after underperf demote for %s: %s", mkt, exc, exc_info=True)

                    try:
                        reserved_queue.add_history({
                            "kind": "DEMOTE",
                            "source": "autopilot",
                            "market": mkt,
                            "strategy": strat,
                            "reason": "underperf",
                        })
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("Failed to add underperf demote history for %s: %s", mkt, exc)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.error("Failed to process underperf demote for market: %s", exc, exc_info=True)

        result["demoted"] = demoted
        result["dust_cleanup_targets"] = dust_cleanup_targets

        # Step 3c) Quick Rotation: early swap of position-less ACTIVE slots
        # - Same concept as SNIPER(s) Scope Slot Quick Rotation
        # - Swap a not-yet-bought slot for a stronger candidate before the idle limit
        # - Conditions: has_pos=False, age >= 2min, new_score >= old_score * 1.10
        # - [Phase 2-C] PINGPONG/AUTOLOOP dedicated rank_score system completed
        quick_rotated_general: List[Dict[str, Any]] = []
        _qr_strategies = {"PINGPONG", "AUTOLOOP", "LIGHTNING", "CONTRARIAN", "GAZUA"}
        try:
            _qr_min_age_sec = float(max(30, int(
                getattr(self.system, "autopilot_quick_rotate_min_sec", 120) or 120
            )))
        except (TypeError, ValueError):
            logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
            _qr_min_age_sec = 120.0
        try:
            _qr_score_ratio = float(max(0.0,
                getattr(self.system, "autopilot_quick_rotate_min_score_ratio", 0.10) or 0.10
            ))
        except (TypeError, ValueError):
            logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
            _qr_score_ratio = 0.10
        try:
            _qr_max = int(min(10, max(0, int(
                getattr(self.system, "autopilot_quick_rotate_max_per_cycle", 2) or 2
            ))))
        except (TypeError, ValueError):
            logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
            _qr_max = 2

        demoted_markets_set = {str(d.get("market") or "").strip().upper() for d in demoted if d.get("market")}
        _qr_idle_overrides = getattr(self.system, "autopilot_idle_demote_overrides", {}) or {}

        if idle_en and _qr_max > 0:
            _qr_slots: List[Tuple[str, str, float, float]] = []
            for mkt in active_markets:
                if mkt in demoted_markets_set:
                    continue
                strat = self._infer_strategy(mkt, active_reason_map)
                if strat not in _qr_strategies:
                    continue
                if self._is_demote_protected(mkt):
                    continue
                has_pos, _, _ = self._position_snapshot(mkt)
                if has_pos:
                    continue
                try:
                    since_active = float(self.system.oma_registry.get_active_since_ts(mkt) or 0.0)
                except (TypeError, ValueError):
                    logger.warning("SlotLifecycleMixin._iter_markets suppressed exception", exc_info=True)
                    since_active = 0.0
                age_sec = (now - since_active) if since_active > 0 else 0.0
                if age_sec < _qr_min_age_sec:
                    continue
                limit_sec = float(_qr_idle_overrides.get(strat, idle_min)) * 60.0
                if age_sec >= limit_sec:
                    continue
                deploy_score = self._deploy_scores.get(mkt, 0.0)
                _qr_slots.append((mkt, strat, age_sec, deploy_score))

            _qr_slots.sort(key=lambda x: x[3])

            queue_snap_qr = reserved_queue.snapshot()
            queue_items_qr = queue_snap_qr.get("items") or []
            _qr_used: Set[str] = set()

            for mkt, strat, age_sec, deploy_score in _qr_slots:
                if len(quick_rotated_general) >= _qr_max:
                    break
                best_cand = None
                best_score = 0.0
                for qi in queue_items_qr:
                    qi_strat = str(qi.get("strategy") or qi.get("recommended_strategy") or "").strip().upper()
                    if qi_strat != strat:
                        continue
                    qi_mkt = str(qi.get("market") or "").strip().upper()
                    if not qi_mkt or qi_mkt in _qr_used or qi_mkt == mkt:
                        continue
                    qi_score = float(qi.get("rank_score") or qi.get("score") or 0.0)
                    if qi_score > best_score:
                        best_score = qi_score
                        best_cand = qi

                if not best_cand:
                    continue

                # When _deploy_scores is unset (0), supplement with the queue's live score as baseline
                # — UI-deployed/boot-restored slots have deploy_score=0 (did not pass Step4) -> prevent swapping for any arbitrary candidate
                _eff_deploy_score = deploy_score
                if _eff_deploy_score <= 0:
                    for _qi in queue_items_qr:
                        if str(_qi.get("market") or "").strip().upper() == mkt:
                            _eff_deploy_score = float(_qi.get("rank_score") or _qi.get("score") or 0.0)
                            break

                if _eff_deploy_score > 0:
                    if best_score < _eff_deploy_score * (1.0 + _qr_score_ratio):
                        continue
                else:
                    if best_score <= 0:
                        continue

                new_market = str(best_cand.get("market") or "").strip().upper()
                try:
                    budget_usdt = None
                    try:
                        from app.manager.reserved_selector import _suggest_budget
                        _qr_metrics = best_cand.get("metrics") or {}
                        _qr_eq = float(getattr(self.system, "_last_equity_usdt", 0) or 0)
                        if _qr_eq <= 0:
                            _qr_eq = float(getattr(self.system, "equity_usdt", 0) or 0)
                        _qr_dr = float(getattr(self.system, "deploy_ratio", 1.0) or 1.0)
                        _qr_cap = _qr_eq * _qr_dr
                        _qr_active = len(self.system.oma_registry.list_active())

                        if _qr_cap > 0 and _qr_metrics:
                            _qr_recalc = _suggest_budget(
                                strategy=strat,
                                base_usdt=0.0,
                                vol24_usdt=float(_qr_metrics.get("vol24_usdt") or 0),
                                vol_median_usdt=float(_qr_metrics.get("vol24_usdt") or 0),
                                min_order_usdt=float(Q.config.min_order),
                                max_budget_usdt=_qr_cap * 0.20,
                                price=float(_qr_metrics.get("price") or 0),
                                entry_qty_guard_on=False,
                                entry_max_qty=0.0,
                                depth_factor=0.0,
                                depth_ask_usdt=float(_qr_metrics.get("depth_ask_usdt") or 0),
                                depth_bid_usdt=float(_qr_metrics.get("depth_bid_usdt") or 0),
                                total_capital_usdt=_qr_cap,
                                existing_markets_count=_qr_active,
                                spread_bps=float(_qr_metrics.get("spread_bps") or 0),
                                range_ratio_24h=float(_qr_metrics.get("range_ratio_24h") or 0),
                            )
                            if _qr_recalc and _qr_recalc > 0:
                                budget_usdt = _qr_recalc
                                if getattr(self.system, "recovery_boost_active", False):
                                    _boost = float(getattr(self.system, "recovery_boost_budget_mult", 1.0) or 1.0)
                                    if _boost > 1.0:
                                        budget_usdt = round(budget_usdt * _boost, 0)
                    except (KeyError, AttributeError, TypeError, ValueError):
                        logger.warning("[Autopilot] QR budget recalc failed for %s — fallback", new_market, exc_info=True)

                    if budget_usdt is None:
                        try:
                            b = float(best_cand.get("suggested_budget_usdt") or best_cand.get("budget") or 0.0)
                            if b > 0:
                                budget_usdt = b
                        except (TypeError, ValueError):
                            logger.warning("[Autopilot] QR budget extraction failed: %s -> budget=None", new_market)
                            budget_usdt = None

                    self.system.oma_set_market(
                        market=mkt,
                        state=MarketState.WATCH,
                        reason=["autopilot_quick_rotate", f"strategy:{strat}", "pre_idle_score_upgrade"],
                    )
                    try:
                        from app.manager.autopilot_tracker import autopilot_tracker
                        autopilot_tracker.record_decision(mkt, "ACTIVE", "WATCH", strat, "autopilot_quick_rotate")
                    except (ImportError, AttributeError, TypeError) as exc:
                        logger.warning("[SLOT_LIFECYCLE] tracker record ACTIVE->WATCH: %s", exc, exc_info=True)
                    self.mark_cooldown(mkt, reason="quick_rotate")

                    self.system.oma_set_market(
                        market=new_market,
                        state=MarketState.ACTIVE,
                        reason=["reserved_approve", "autopilot_quick_rotate", f"strategy:{strat}"],
                        budget_usdt=budget_usdt,
                    )
                    try:
                        from app.manager.autopilot_tracker import autopilot_tracker
                        autopilot_tracker.record_decision(new_market, "WATCH", "ACTIVE", strat, "autopilot_quick_rotate")
                    except (ImportError, AttributeError, TypeError) as exc:
                        logger.warning("[SLOT_LIFECYCLE] tracker record WATCH->ACTIVE: %s", exc, exc_info=True)

                    recommended_params = None
                    try:
                        rp = best_cand.get("recommended_params")
                        if rp and isinstance(rp, dict):
                            recommended_params = rp
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.warning("[SLOT_LIFECYCLE] recommended_params extract: %s", exc, exc_info=True)
                    try:
                        apply_engine_controls(self.system, new_market, strat, recommended_params)
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("[SLOT_LIFECYCLE] apply_engine_controls: %s", exc, exc_info=True)

                    self._deploy_scores.pop(mkt, None)
                    self._deploy_scores[new_market] = best_score
                    _qr_used.add(new_market)

                    age_min = round(age_sec / 60.0, 1)
                    quick_rotated_general.append({
                        "old_market": mkt,
                        "new_market": new_market,
                        "strategy": strat,
                        "age_min": age_min,
                        "old_score": round(deploy_score, 4),
                        "new_score": round(best_score, 4),
                    })
                    self.system.ledger.append(
                        "QUICK_ROTATION",
                        old_market=mkt,
                        new_market=new_market,
                        strategy=strat,
                        age_sec=round(age_sec, 1),
                        old_score=round(deploy_score, 4),
                        new_score=round(best_score, 4),
                    )
                    reserved_queue.add_history({
                        "kind": "QUICK_ROTATION",
                        "source": "autopilot",
                        "old_market": mkt,
                        "new_market": new_market,
                        "strategy": strat,
                        "age_min": age_min,
                        "old_score": round(deploy_score, 4),
                        "new_score": round(best_score, 4),
                    })
                    logger.info(
                        f"[Autopilot/QuickRotation] {strat} {mkt} -> {new_market} "
                        f"(age={age_min}m, score {deploy_score:.4f}->{best_score:.4f})"
                    )
                except Exception as exc:
                    logger.warning(f"[Autopilot/QuickRotation] {strat} {mkt}->{new_market} failed: {exc}")

        result["quick_rotated"] = quick_rotated_general

        result["active_markets"] = active_markets
        result["active_reason_map"] = active_reason_map

        return result
