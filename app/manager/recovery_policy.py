# ============================================================
# File: app/manager/recovery_policy.py
# Autocoin OS v3-H — Recovery Policy Engine (Policy Table + Reactor)
# ------------------------------------------------------------
# Purpose:
# - Use ledger events (ORDER_FINAL / EXIT_UNRESOLVED / SLIPPAGE_HARD_BREACH)
#   as triggers to promote a market into RECOVERY (recovery mode) and
#   (optionally) perform automatic/conditional liquidation.
# - Minimal-change principle: without tearing apart existing engine/strategy
#   logic, add a safety rail of "ledger event -> policy decision -> execution".
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple, List

logger = logging.getLogger(__name__)

class RecoveryPolicyMode(str, Enum):
    """RECOVERY policy mode."""

    MANUAL = "MANUAL"  # manual: promote to RECOVERY only, no auto-sell
    CONDITIONAL = "CONDITIONAL"  # conditional: auto-liquidate when conditions are met
    AUTO = "AUTO"  # auto: attempt auto-liquidation immediately on RECOVERY entry

# ------------------------------------------------------------
# Policy Table
# ------------------------------------------------------------
# Input events:
# - SLIPPAGE_HARD_BREACH
# - EXIT_UNRESOLVED
# - ORDER_FINAL
#
# Output actions:
# - ENTER_RECOVERY(market)
# - SET_EMERGENCY_STOP(global)
# - ATTEMPT_LIQUIDATE(market)
#
# ====== Decision Table (summary) ======
#
# 1) SLIPPAGE_HARD_BREACH
#    - SELL(ask) hard breach:
#         MANUAL       -> ENTER_RECOVERY + SET_EMERGENCY_STOP
#         CONDITIONAL  -> ENTER_RECOVERY + SET_EMERGENCY_STOP + (conditional) LIQUIDATE
#         AUTO         -> ENTER_RECOVERY + SET_EMERGENCY_STOP + LIQUIDATE
#    - BUY(bid) hard breach:
#         MANUAL       -> ENTER_RECOVERY (entry blocked)  (global stop OFF by default)
#         CONDITIONAL  -> ENTER_RECOVERY (LIQUIDATE usually unnecessary on repeat/condition)
#         AUTO         -> ENTER_RECOVERY (if a holding arose, follow-up is conditional)
#
# 2) EXIT_UNRESOLVED (liquidation failure/deadlock)
#         all modes  -> ENTER_RECOVERY + SET_EMERGENCY_STOP
#         CONDITIONAL/AUTO -> (conditional/immediate) LIQUIDATE
#
# 3) ORDER_FINAL (order termination including done/cancel)
#    - SELL order is cancel with executed_volume > 0 (remainder left after partial liquidation)
#         MANUAL       -> ENTER_RECOVERY + SET_EMERGENCY_STOP
#         CONDITIONAL  -> ENTER_RECOVERY + SET_EMERGENCY_STOP + (conditional) LIQUIDATE
#         AUTO         -> ENTER_RECOVERY + SET_EMERGENCY_STOP + LIQUIDATE
#    - otherwise: mainly for tuning/audit purposes (no immediate action)
#

@dataclass
class PolicyDecision:
    enter_recovery: bool = False
    set_emergency_stop: bool = False
    attempt_liquidate: bool = False
    reason: str = ""
    meta: Dict[str, Any] = None

def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    if v is None:
        return default
    v = str(v).strip()
    return v if v else default

from app.core.constants import env_float as _env_float, env_int as _env_int

def _now_ts() -> float:
    return time.time()

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        logger.warning(f"_safe_float: failed to convert {x!r}, using default={default}", exc_info=True)
        return float(default)

def _market_currency(market: str) -> str:
    # 'BTC-USDT' -> 'USDT' (market quote/coin parse)
    try:
        return market.split("-", 1)[1].strip().upper()
    except (AttributeError, TypeError, IndexError) as e:
        logger.warning(f"_market_currency: failed to parse {market!r}: {e}", exc_info=True)
        return ""

class RecoveryPolicyEngine:
    """Executes the RECOVERY policy based on ledger events."""

    def __init__(self) -> None:
        # mode
        raw = _env_str("OMA_RECOVERY_POLICY", "MANUAL").upper()
        # compat: HOLD=MANUAL
        if raw in ("HOLD", "MANUAL"):
            self.mode = RecoveryPolicyMode.MANUAL
        elif raw in ("COND", "CONDITIONAL"):
            self.mode = RecoveryPolicyMode.CONDITIONAL
        elif raw in ("AUTO",):
            self.mode = RecoveryPolicyMode.AUTO
        else:
            self.mode = RecoveryPolicyMode.MANUAL

        # conditional/auto liquidation parameters
        # NOTE: env var name OMA_RECOVERY_MIN_VALUE_USDT kept for compatibility
        self.min_value_usdt = _env_float("OMA_RECOVERY_MIN_VALUE_USDT", 10.0)
        self.cond_max_hold_sec = _env_int("OMA_RECOVERY_COND_MAX_HOLD_SEC", 1800)
        self.cond_stoploss_pct = _env_float("OMA_RECOVERY_COND_STOPLOSS_PCT", 3.0)  # loss % threshold (positive)

        # slippage hard-breach threshold (bps) — fallback when the event has no value
        self.hard_slippage_bps = _env_float("OMA_SLIPPAGE_HARD_BPS", 80.0)

        # file-based state (keeps recovery_since across reboots)
        self.state_path = _env_str("OMA_RECOVERY_STATE_PATH", "runtime/recovery_state.json")
        self._state: Dict[str, Dict[str, Any]] = {}
        self._load_state()

        # internal periodic check (conditional)
        self._last_periodic_ts = 0.0
        self.periodic_interval_sec = _env_float("OMA_RECOVERY_PERIODIC_SEC", 1.0)

    # --------------------------------------------------------
    # Persistence
    # --------------------------------------------------------
    def _load_state(self) -> None:
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._state = data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"RecoveryPolicyEngine._load_state: failed to read {self.state_path}: {e}")
            self._state = {}

    def _save_state(self) -> None:
        try:
            from app.core.io_utils import safe_write_json
            safe_write_json(self.state_path, self._state)
        except OSError as e:
            logger.warning(f"RecoveryPolicyEngine._save_state: failed to save {self.state_path}: {e}")

    # --------------------------------------------------------
    # Public entrypoint
    # --------------------------------------------------------
    def on_ledger_event(self, system: Any, record: Dict[str, Any]) -> None:
        """Receive a single ledger record and apply the policy."""
        event = str(record.get("event") or "").strip()
        if not event:
            return

        # only handle events of interest
        if event not in ("ORDER_FINAL", "EXIT_UNRESOLVED", "SLIPPAGE_HARD_BREACH", "ORPHAN_DETECTED"):
            return

        market = str(record.get("market") or "").strip()
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if not market:
            # some ledger implementations may carry market inside data
            market = str((data or {}).get("market") or "").strip()
        if not market:
            return

        decision = self.decide(system=system, event=event, market=market, data=data, record=record)
        if decision.enter_recovery or decision.set_emergency_stop or decision.attempt_liquidate:
            self.execute(system=system, market=market, event=event, decision=decision, record=record)

    def snapshot(self) -> Dict[str, Any]:
        """Current RECOVERY policy/threshold snapshot (READ-ONLY)."""
        markets: List[str] = []
        try:
            for m, st in (self._state or {}).items():
                if isinstance(st, dict) and st.get("status") == "RECOVERY":
                    markets.append(m)
        except (TypeError, AttributeError) as e:
            logger.warning("snapshot: failed to iterate state: %s", e, exc_info=True)
            markets = []

        return {
            "policy": self.mode.value,
            "min_value_usdt": float(self.min_value_usdt),
            "cond_max_hold_sec": int(self.cond_max_hold_sec),
            "cond_stoploss_pct": float(self.cond_stoploss_pct),
            "hard_slippage_bps": float(self.hard_slippage_bps),
            "state_path": self.state_path,
            "markets_in_recovery": sorted(markets),
        }

    def manual_enter_recovery(self, system: Any, market: str, *, reason: str = "manual") -> None:
        """Manually enter RECOVERY (does not auto-liquidate)."""
        self._enter_recovery(system, market, reason=reason, meta={"event": "MANUAL_RECOVERY"})

    def manual_liquidate(self, system: Any, market: str, *, reason: str = "manual_liquidate") -> None:
        """Manually attempt full RECOVERY liquidation."""
        self._enter_recovery(system, market, reason=reason, meta={"event": "MANUAL_LIQUIDATE"})
        self._attempt_liquidate(system, market, reason=reason)

    def periodic(self, system: Any) -> None:
        """Periodically evaluate time/loss conditions under conditional policy."""
        now = _now_ts()
        if (now - self._last_periodic_ts) < self.periodic_interval_sec:
            return
        self._last_periodic_ts = now

        if self.mode == RecoveryPolicyMode.MANUAL:
            return

        # recover markets in RECOVERY when conditions are met
        for market, st in list(self._state.items()):
            if not isinstance(st, dict):
                continue
            if st.get("status") != "RECOVERY":
                continue
            since = _safe_float(st.get("since"), 0.0)
            if since <= 0:
                continue

            # If the OMA market state is no longer RECOVERY (operator manual change / false-positive correction, etc.),
            # auto-clear RecoveryPolicyEngine's local RECOVERY state to prevent log spam / infinite liquidation loops.
            try:
                reg = getattr(system, "oma_registry", None)
                if reg is not None and hasattr(reg, "get_state"):
                    cur = reg.get_state(market)
                    cur_name = cur.name if hasattr(cur, "name") else str(cur)
                    if "RECOVERY" not in str(cur_name).upper():
                        st["status"] = "CLEARED"
                        st["cleared_at"] = now
                        st["updated_at"] = now
                        self._state[market] = st
                        self._save_state()
                        self._ledger_append(system, "RECOVERY_EXIT", market, {
                            "reason": "oma_state_not_recovery",
                            "oma_state": cur_name,
                            "since": since,
                            "now": now,
                        })
                        continue
            except (AttributeError, KeyError, TypeError) as e:
                logger.warning("periodic: failed to check OMA state for %s: %s", market, e, exc_info=True)

            # time condition
            if self.mode in (RecoveryPolicyMode.CONDITIONAL, RecoveryPolicyMode.AUTO):
                if self.cond_max_hold_sec > 0 and (now - since) >= float(self.cond_max_hold_sec):
                    self.execute(
                        system=system,
                        market=market,
                        event="RECOVERY_TICK",
                        decision=PolicyDecision(
                            enter_recovery=True,
                            set_emergency_stop=False,
                            attempt_liquidate=True,
                            reason=f"recovery_hold_timeout>={self.cond_max_hold_sec}s",
                            meta={"since": since, "now": now},
                        ),
                        record={"event": "RECOVERY_TICK", "market": market, "data": {}},
                    )
                    continue

            # loss condition (computed from context entry/current price when possible)
            pnl_pct = self._estimate_pnl_pct(system, market)
            if pnl_pct is not None and pnl_pct <= -abs(self.cond_stoploss_pct):
                self.execute(
                    system=system,
                    market=market,
                    event="RECOVERY_TICK",
                    decision=PolicyDecision(
                        enter_recovery=True,
                        set_emergency_stop=False,
                        attempt_liquidate=True,
                        reason=f"recovery_stoploss_breach pnl={pnl_pct:.2f}%",
                        meta={"pnl_pct": pnl_pct},
                    ),
                    record={"event": "RECOVERY_TICK", "market": market, "data": {}},
                )

    # --------------------------------------------------------
    # Decision
    # --------------------------------------------------------
    def decide(
        self,
        *,
        system: Any,
        event: str,
        market: str,
        data: Dict[str, Any],
        record: Dict[str, Any],
    ) -> PolicyDecision:
        event = event.upper()

        # --- SLIPPAGE_HARD_BREACH ---
        if event == "SLIPPAGE_HARD_BREACH":
            side = str(data.get("side") or "").lower()  # 'ask'|'bid'
            slp = _safe_float(data.get("slippage_bps"), _safe_float(data.get("slippage"), self.hard_slippage_bps))

            if side == "ask":
                # sell-side hard breach: global stop + recovery
                return PolicyDecision(
                    enter_recovery=True,
                    set_emergency_stop=True,
                    attempt_liquidate=(self.mode != RecoveryPolicyMode.MANUAL),
                    reason=f"slippage_hard_breach_sell bps={slp:.1f}",
                    meta={"side": side, "slippage_bps": slp},
                )

            # buy hard breach: block entry via market-level recovery (global stop off by default)
            return PolicyDecision(
                enter_recovery=True,
                set_emergency_stop=False,
                attempt_liquidate=False,
                reason=f"slippage_hard_breach_buy bps={slp:.1f}",
                meta={"side": side, "slippage_bps": slp},
            )

        # --- EXIT_UNRESOLVED ---
        if event == "EXIT_UNRESOLVED":
            # liquidation deadlock is critical: global stop + recovery
            return PolicyDecision(
                enter_recovery=True,
                set_emergency_stop=True,
                attempt_liquidate=(self.mode != RecoveryPolicyMode.MANUAL),
                reason="exit_unresolved",
                meta={"data": data},
            )

        # --- ORPHAN_DETECTED ---
        if event == "ORPHAN_DETECTED":
            # an orphan is subject to recovery and entry is blocked
            # if auto/conditional, attempt auto-liquidation (above min_value)
            return PolicyDecision(
                enter_recovery=True,
                set_emergency_stop=False,
                attempt_liquidate=(self.mode == RecoveryPolicyMode.AUTO),
                reason="orphan_detected",
                meta={"data": data},
            )

        # --- ORDER_FINAL ---
        if event == "ORDER_FINAL":
            # if the order ended in cancel but a sell order was partially filled,
            # there's a high chance a remainder is left -> promote to RECOVERY.
            state = str(data.get("state") or "").lower()
            side = str(data.get("side") or "").lower()
            executed = _safe_float(data.get("executed_volume"), 0.0)

            if side == "ask" and state == "cancel" and executed > 0:
                return PolicyDecision(
                    enter_recovery=True,
                    set_emergency_stop=True,
                    attempt_liquidate=(self.mode != RecoveryPolicyMode.MANUAL),
                    reason="sell_partial_then_cancel",
                    meta={"executed_volume": executed},
                )

            # other ORDER_FINAL is recorded for tuning (no immediate action)
            return PolicyDecision(
                enter_recovery=False,
                set_emergency_stop=False,
                attempt_liquidate=False,
                reason="order_final_no_action",
                meta={},
            )

        # default: do nothing
        return PolicyDecision(reason="ignored")

    # --------------------------------------------------------
    # Execute
    # --------------------------------------------------------
    def execute(
        self,
        *,
        system: Any,
        market: str,
        event: str,
        decision: PolicyDecision,
        record: Dict[str, Any],
    ) -> None:
        meta = decision.meta or {}

        if decision.enter_recovery:
            self._enter_recovery(system, market, reason=decision.reason, meta=meta)

        if decision.set_emergency_stop:
            self._set_emergency_stop(system, True, reason=decision.reason, meta=meta)

        if decision.attempt_liquidate:
            # in conditional mode, act only when the "min value" threshold is met (minimize the barrier)
            if self.mode == RecoveryPolicyMode.CONDITIONAL:
                ok, val = self._estimate_position_value_usdt(system, market)
                if ok and val is not None and val >= float(self.min_value_usdt):
                    self._attempt_liquidate(system, market, reason=decision.reason)
                else:
                    self._ledger_append(system, "RECOVERY_LIQUIDATE_SKIP", market, {
                        "reason": decision.reason,
                        "estimated_value_usdt": val,
                        "min_value_usdt": self.min_value_usdt,
                    })
            else:
                self._attempt_liquidate(system, market, reason=decision.reason)

    # --------------------------------------------------------
    # Actions
    # --------------------------------------------------------
    def _enter_recovery(self, system: Any, market: str, *, reason: str, meta: Dict[str, Any]) -> None:
        now = _now_ts()

        # Skip if already in RECOVERY (avoid log spam)
        st = self._state.get(market) if isinstance(self._state.get(market), dict) else {}
        if st.get("status") == "RECOVERY":
            return

        # OMA state: promote to RECOVERY (fall back to WATCH if it doesn't exist)
        try:
            from app.manager.oma_market_registry import MarketState
            desired_state = getattr(MarketState, "RECOVERY", None)
        except ImportError as e:
            logger.warning("_enter_recovery: failed to import MarketState: %s", e, exc_info=True)
            desired_state = None

        set_ok = False
        if desired_state is not None:
            # prefer system.oma_set_market
            if hasattr(system, "oma_set_market"):
                try:
                    system.oma_set_market(market=market, state=desired_state, reason=[f"RECOVERY:{reason}"])
                    set_ok = True
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    logger.warning("_enter_recovery: oma_set_market failed for %s: %s", market, e)
                    set_ok = False
            if not set_ok:
                # direct registry
                reg = getattr(system, "oma", None) or getattr(system, "oma_registry", None)
                if reg and hasattr(reg, "set_state"):
                    try:
                        reg.set_state(market=market, state=desired_state, reason=[f"RECOVERY:{reason}"])
                        set_ok = True
                    except (KeyError, AttributeError, TypeError, ValueError) as e:
                        logger.warning("_enter_recovery: reg.set_state failed for %s: %s", market, e)
                        set_ok = False

        if not set_ok:
            # fallback: WATCH
            try:
                from app.manager.oma_market_registry import MarketState
                if hasattr(system, "oma_set_market"):
                    system.oma_set_market(market=market, state=MarketState.WATCH, reason=[f"RECOVERY_FALLBACK:{reason}"])
                else:
                    reg = getattr(system, "oma", None) or getattr(system, "oma_registry", None)
                    if reg and hasattr(reg, "set_state"):
                        reg.set_state(market=market, state=MarketState.WATCH, reason=[f"RECOVERY_FALLBACK:{reason}"])
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.warning("_enter_recovery: fallback to WATCH failed for %s: %s", market, e)

        # local persistent recovery state
        st = self._state.get(market) if isinstance(self._state.get(market), dict) else {}
        st = dict(st)
        st.update({
            "status": "RECOVERY",
            "since": st.get("since") or now,
            "reason": reason,
            "last_event": event if isinstance((event := str(meta.get("event") or "")), str) else "",
            "updated_at": now,
        })
        self._state[market] = st
        self._save_state()

        self._ledger_append(system, "RECOVERY_ENTER", market, {"reason": reason, **meta})

    def _set_emergency_stop(self, system: Any, value: bool, *, reason: str, meta: Dict[str, Any]) -> None:
        # prefer the system method
        if hasattr(system, "set_emergency_stop"):
            try:
                system.set_emergency_stop(bool(value), reason=reason)
                self._ledger_append(system, "EMERGENCY_STOP_SET", "", {"value": bool(value), "reason": reason, **meta})
                return
            except (AttributeError, TypeError) as e:
                logger.warning("_set_emergency_stop: system.set_emergency_stop failed: %s", e)

        # fallback: set the attribute directly
        try:
            setattr(system, "emergency_stop", bool(value))
        except (AttributeError, TypeError) as e:
            logger.warning("_set_emergency_stop: failed to set emergency_stop attr: %s", e, exc_info=True)

        self._ledger_append(system, "EMERGENCY_STOP_SET", "", {"value": bool(value), "reason": reason, **meta})

    def _attempt_liquidate(self, system: Any, market: str, *, reason: str) -> None:
        """Attempt a full market sell of the market's coin based on the Bybit balance."""

        # [PROTECTED] LongHold markets are blocked from auto-liquidation (2026-02-01)
        # DO NOT MODIFY: this logic is protected by user instruction
        is_longhold = False
        user_sell_only = False
        try:
            ladder_mgr = getattr(system, "ladder_manager", None)
            if ladder_mgr:
                lh_cfg = ladder_mgr.get_longhold_config(market)
                if lh_cfg and lh_cfg.get("enabled"):
                    is_longhold = True
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[recovery_policy] %s: %s", 'DO NOT MODIFY: this logic is protected by user instruction', exc, exc_info=True)
        
        try:
            coord = getattr(system, "coordinator", None)
            if coord:
                ctx = coord.contexts.get(market)
                if ctx:
                    ctrls = getattr(ctx, "controls", {}) or {}
                    sp = ctrls.get("strategy", {}).get("params", {}) or {}
                    user_sell_only = bool(sp.get("user_sell_only", False))
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[recovery_policy] %s: %s", 'DO NOT MODIFY: this logic is protected by user instruction', exc, exc_info=True)
        
        if is_longhold or user_sell_only:
            self._ledger_append(system, "RECOVERY_LIQUIDATE_BLOCKED", market, {
                "reason": reason,
                "blocked_by": "longhold_protected",
                "is_longhold": is_longhold,
                "user_sell_only": user_sell_only,
            })
            return
        
        trade = self._resolve_trade_client(system)
        if trade is None:
            self._ledger_append(system, "RECOVERY_LIQUIDATE_FAIL", market, {"reason": reason, "error": "no_trade_client"})
            return

        currency = _market_currency(market)
        if not currency:
            self._ledger_append(system, "RECOVERY_LIQUIDATE_FAIL", market, {"reason": reason, "error": "bad_market"})
            return

        qty = self._accounts_qty(trade, currency)
        if qty <= 0:
            self._ledger_append(system, "RECOVERY_LIQUIDATE_SKIP", market, {"reason": reason, "qty": qty})

            # if there's no holding left to liquidate, auto-clear RECOVERY state (prevent log spam / infinite loop)
            try:
                now = time.time()
                st = self._state.get(market)
                if isinstance(st, dict) and str(st.get("status") or "").upper() == "RECOVERY":
                    st["status"] = "CLEARED"
                    st["cleared_at"] = now
                    st["updated_at"] = now
                    self._state[market] = st
                    self._save_state()
                    self._ledger_append(system, "RECOVERY_EXIT", market, {
                        "reason": "no_holding",
                        "qty": qty,
                        "now": now,
                    })
            except (KeyError, TypeError) as e:
                logger.warning("_attempt_liquidate: failed to clear RECOVERY state for %s: %s", market, e)
            return

        ok, value_usdt = self._estimate_position_value_usdt(system, market)
        if ok and value_usdt is not None and value_usdt < float(self.min_value_usdt):
            self._ledger_append(system, "RECOVERY_LIQUIDATE_SKIP", market, {
                "reason": reason,
                "qty": qty,
                "estimated_value_usdt": value_usdt,
                "min_value_usdt": self.min_value_usdt,
            })
            return

        # market sell
        try:
            resp = trade.market_sell_qty(market, qty)
            oid = str(resp.get("uuid") or "")
            self._ledger_append(system, "RECOVERY_LIQUIDATE_SUBMIT", market, {
                "reason": reason,
                "uuid": oid,
                "qty": qty,
            })

            # confirm result (briefly)
            timeout = _env_float("BYBIT_ORDER_WAIT_SEC_SELL", _env_float("BYBIT_ORDER_WAIT_SEC", 3.0))
            poll = _env_float("BYBIT_ORDER_POLL_SEC", 0.2)
            final = trade.wait_order(uuid=oid, timeout_sec=timeout, poll_interval=poll) if oid else resp

            # NOTE: summarize as a dict to record structured fields in the ledger.
            summ = self._summarize_order_for_ledger(final)
            if oid and not summ.get("uuid"):
                summ["uuid"] = oid

            self._ledger_append(system, "RECOVERY_LIQUIDATE_FINAL", market, {
                "reason": reason,
                **summ,
            })

        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[RecoveryPolicy] liquidate failed for %s reason=%s", market, reason, exc_info=True)
            self._ledger_append(system, "RECOVERY_LIQUIDATE_FAIL", market, {"reason": reason, "error": str(exc)})

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------
    def _summarize_order_for_ledger(self, order: Any) -> Dict[str, Any]:
        """Safely summarize an order response (dict) for ledger recording.

        - Exchange responses often deliver numbers as strings.
        - If trades are included, funds/avg_price are computed more accurately.
        - If order is not a dict, record it as a raw string (avoid exceptions).
        """
        if not isinstance(order, dict):
            return {"raw": str(order)}

        def sf(x: Any, default: float = 0.0) -> float:
            try:
                return float(x)
            except (TypeError, ValueError):
                logger.warning("[RecoveryPolicy] sf: conversion failed for %r", x, exc_info=True)
                return float(default)

        uuid = str(order.get("uuid") or "")
        state = str(order.get("state") or "")
        side = str(order.get("side") or "")

        executed_volume = sf(order.get("executed_volume"), 0.0)
        paid_fee = sf(order.get("paid_fee"), 0.0)

        # funds / avg_price best-effort
        funds = 0.0
        avg_price = None

        trades = order.get("trades")
        if isinstance(trades, list) and trades:
            vol_sum = 0.0
            funds_sum = 0.0
            for t in trades:
                if not isinstance(t, dict):
                    continue
                vol_sum += sf(t.get("volume") or t.get("executed_volume"), 0.0)
                funds_sum += sf(t.get("funds"), 0.0)

            if executed_volume <= 0.0 and vol_sum > 0.0:
                executed_volume = vol_sum

            funds = funds_sum
            if executed_volume > 0.0 and funds > 0.0:
                avg_price = funds / executed_volume

        else:
            # fallback: if price is present (limit order), approximate
            px = sf(order.get("price"), 0.0)
            if px > 0.0 and executed_volume > 0.0:
                funds = px * executed_volume
                avg_price = px

        return {
            "uuid": uuid,
            "state": state,
            "side": side,
            "executed_volume": executed_volume,
            "funds": funds,
            "avg_price": avg_price,
            "paid_fee": paid_fee,
        }

    def _resolve_trade_client(self, system: Any) -> Any:
        # common field names
        for name in ("trade_client", "trade"):
            obj = getattr(system, name, None)
            if obj is not None:
                return obj
        # method-based
        for name in ("get_trade_client",):
            fn = getattr(system, name, None)
            if callable(fn):
                try:
                    obj = fn()
                    if obj is not None:
                        return obj
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[recovery_policy] %s: %s", f"_resolve_trade_client: {name}() failed", exc, exc_info=True)
                    continue
        return None

    def _accounts_qty(self, trade: Any, currency: str) -> float:
        try:
            acc = trade.accounts()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("_accounts_qty: trade.accounts() failed for %s: %s", currency, e)
            return 0.0

        qty = 0.0
        for a in acc or []:
            if str(a.get("currency") or "").upper() != currency.upper():
                continue
            qty += _safe_float(a.get("balance"), 0.0)
            qty += _safe_float(a.get("locked"), 0.0)
        return float(qty)

    def _estimate_position_value_usdt(self, system: Any, market: str) -> Tuple[bool, Optional[float]]:
        """Estimate holding value (USDT) from current price when possible."""
        try:
            # use price_store if available
            from app.core.hyper_price_store import price_store
            px = price_store.get_price(market)
        except (ImportError, AttributeError, TypeError):
            logger.warning("[RecoveryPolicy] _estimate_position_value_usdt: price_store import failed", exc_info=True)
            px = None

        if px is None:
            return False, None

        trade = self._resolve_trade_client(system)
        if trade is None:
            return False, None

        currency = _market_currency(market)
        qty = self._accounts_qty(trade, currency)
        return True, float(qty) * float(px)

    def _estimate_pnl_pct(self, system: Any, market: str) -> Optional[float]:
        """Estimate PnL% from the context position (entry) and current price."""
        try:
            from app.core.hyper_price_store import price_store
            px = price_store.get_price(market)
        except (ImportError, AttributeError, TypeError):
            logger.warning("[RecoveryPolicy] _estimate_pnl_pct: price_store import failed for %s", market, exc_info=True)
            return None
        if px is None:
            return None

        # read entry from coordinator/context
        entry = None
        try:
            coord = getattr(system, "coordinator", None)
            if coord and hasattr(coord, "get_context"):
                ctx = coord.get_context(market)
                pos = getattr(ctx, "position", None)
                if isinstance(pos, dict):
                    entry = pos.get("entry")
                elif pos is not None and isinstance(pos, dict):
                    entry = pos.get("entry")
        except (KeyError, AttributeError, TypeError):
            logger.warning("[RecoveryPolicy] _entry_price_from_ctx: context read failed for %s", market, exc_info=True)
            entry = None

        if entry is None:
            return None

        e = _safe_float(entry, 0.0)
        if e <= 0:
            return None

        return (float(px) - e) / e * 100.0

    def _ledger_append(self, system: Any, event: str, market: str, data: Dict[str, Any]) -> None:
        """Use the system's ledger writer if present, otherwise append directly to file."""
        # prefer the system.ledger.append(...) form
        ledger = getattr(system, "ledger", None)
        if ledger and hasattr(ledger, "append"):
            try:
                ledger.append(event=event, market=market or None, data=data)
                return
            except (AttributeError, TypeError) as e:
                logger.warning("_ledger_append: ledger.append failed: %s", e, exc_info=True)

        # fallback: append directly as JSONL
        path = _env_str("OMA_LEDGER_PATH", "runtime/trade_ledger.jsonl")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            rec = {
                "ts": time.time(),
                "event": event,
                "market": market,
                "data": data,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("_ledger_append: failed to write to %s: %s", path, e)
