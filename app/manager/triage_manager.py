# ============================================================
# File: app/manager/triage_manager.py
# Autocoin OS v3-H — portfolio triage mode manager
# ------------------------------------------------------------
# Design doc: docs/TRAIGE MODE PLAN.md
# Bug fixes: ctx.position field names (qty, entry — set during reconcile),
#            should_sell() based on post-DCA new average price,
#            async DCA offload, TRIAGE_SELL completion detection
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================
# Environment variable helpers (same approach as hyper_system)
# ============================================================

def _ef(key: str, default: float) -> float:
    try:
        v = os.getenv(key, "")
        return float(v) if v.strip() else default
    except (TypeError, ValueError):
        logger.warning("_ef suppressed exception", exc_info=True)
        return default

def _eb(key: str, default: bool) -> bool:
    v = str(os.getenv(key, "1" if default else "0")).strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _es(key: str, default: str) -> str:
    return os.getenv(key, default) or default


def _load_settings_from_env() -> Dict[str, Any]:
    return {
        "enabled": _eb("OMA_TRIAGE_ENABLED", False),
        "trigger_pnl_pct": _ef("OMA_TRIAGE_TRIGGER_PNL_PCT", -5.0),
        "trigger_loss_count": int(_ef("OMA_TRIAGE_TRIGGER_LOSS_COUNT", 5)),
        "max_dca_ratio": _ef("OMA_TRIAGE_MAX_DCA_RATIO", 2.0),
        "profit_target_pct": _ef("OMA_TRIAGE_PROFIT_TARGET_PCT", 0.3),
        "fee_pct": 0.15,  # fee margin (buy + sell combined)
        "max_loss_exclude_pct": _ef("OMA_TRIAGE_MAX_LOSS_EXCLUDE_PCT", -30.0),
        "coin_timeout_hours": _ef("OMA_TRIAGE_COIN_TIMEOUT_HOURS", 48.0),
        "max_duration_hours": _ef("OMA_TRIAGE_MAX_DURATION_HOURS", 168.0),
        "dca_interval_sec": _ef("OMA_TRIAGE_DCA_INTERVAL_SEC", 300.0),
        "exit_pnl_pct": _ef("OMA_TRIAGE_EXIT_PNL_PCT", -2.0),
        "recovery_target": _es("OMA_TRIAGE_RECOVERY_TARGET", "ALL"),
        "notify": _eb("OMA_TRIAGE_NOTIFY", True),
        "state_path": _es("OMA_TRIAGE_STATE_PATH", "runtime/triage_state.json"),
        "sell_timeout_sec": 300.0,   # TRIAGE_SELL 5-minute timeout
        "min_position_usdt": 10.0, # dust exclusion threshold (min_order_usdt * 2)
        # Strategies exempt from BUY blocking
        # CONTRARIAN: a falling market is precisely the entry timing (BTC Guard uses same policy)
        # SNIPER: time-sensitive pump signal, opportunity independent of portfolio loss
        # WHALE: follows whale signals, time-sensitive
        "global_dca_cap_pct": _ef("OMA_TRIAGE_GLOBAL_DCA_CAP_PCT", 30.0),  # cap on total DCA as % of portfolio
        "exempt_strategies": ["CONTRARIAN", "SNIPER", "WHALE"],
        "focus_dca_allow": True,     # allow focus market to bypass PRM
        # BUY mode control
        # "block_all"      : legacy behavior — block all BUY except exempt strategies
        # "allow_non_loss" : allow BUY for coins that were not loss coins at triage entry time
        "buy_mode": "block_all",
        # Conditional immediate DCA on loss coins
        # True: if a strategy emits a BUY signal on a loss coin, allow immediate DCA without
        #       waiting for the schedule, after passing the cash buffer (triage_reserved_usdt) check
        "opportunistic_dca": False,
        # Automatic exit on market recovery
        # BTC Guard OFF + PnL improvement + minimum elapsed time → auto-exit triage
        "market_recovery_exit_enabled": _eb("OMA_TRIAGE_MARKET_RECOVERY_EXIT", True),
        "market_recovery_min_hours": _ef("OMA_TRIAGE_MARKET_RECOVERY_MIN_HOURS", 2.0),
        # Coins are not counted as loss coins within this many minutes after a buy
        # Prevents false triggers from fees + spread right after a buy
        "loss_grace_min": _ef("OMA_TRIAGE_LOSS_GRACE_MIN", 30.0),
        # Parallel recovery: max number of coins to DCA-recover concurrently
        "max_concurrent_targets": int(_ef("OMA_TRIAGE_MAX_CONCURRENT", 3)),
        # Emergency exit: dynamically lower the profit target based on market conditions
        "emergency_exit_enabled": _eb("OMA_TRIAGE_EMERGENCY_EXIT", True),
        "emergency_moderate_avg_loss_pct": _ef("OMA_TRIAGE_EMERGENCY_MODERATE_PCT", -10.0),
        "emergency_severe_avg_loss_pct": _ef("OMA_TRIAGE_EMERGENCY_SEVERE_PCT", -30.0),
    }


# ============================================================
# TriageManager
# ============================================================

class TriageManager:
    """
    Portfolio triage mode state machine.

    7 states: NORMAL → TRIAGE_INIT → TRIAGE_SCAN → TRIAGE_DCA
              → TRIAGE_WAIT → TRIAGE_SELL → TRIAGE_EXIT → NORMAL

    Core principle:
      - DCA into one loss coin at a time → recover sequentially by selling at breakeven + alpha
      - Block all new BUYs during recovery (hyper_system._triage_entry_blocked)
    """

    STATE_NORMAL = "NORMAL"
    STATE_INIT   = "TRIAGE_INIT"
    STATE_SCAN   = "TRIAGE_SCAN"
    STATE_DCA    = "TRIAGE_DCA"
    STATE_WAIT   = "TRIAGE_WAIT"
    STATE_SELL   = "TRIAGE_SELL"
    STATE_EXIT   = "TRIAGE_EXIT"

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings: Dict[str, Any] = settings or _load_settings_from_env()
        self.state: str = self.STATE_NORMAL
        self.start_ts: float = 0.0
        self.trigger_reason: str = ""
        self.active_targets: List[Dict[str, Any]] = []  # parallel recovery targets (each has a state field)
        self.recovered: List[Dict[str, Any]] = []              # recovery complete
        self.skipped: List[Dict[str, Any]] = []                # skipped
        self.excluded: List[Dict[str, Any]] = []               # excluded
        self.initial_snapshot: Dict[str, Any] = {}
        self._entry_equity_usdt: float = 0.0      # total equity at entry time (performance measurement)

    # ============================================================
    # current_target backward-compat property
    # ============================================================

    @property
    def current_target(self) -> Optional[Dict[str, Any]]:
        """Backward compat: return the first active target."""
        return self.active_targets[0] if self.active_targets else None

    @current_target.setter
    def current_target(self, val: Optional[Dict[str, Any]]) -> None:
        """Backward compat setter (for tests / external code)."""
        if val is None:
            self.active_targets = []
        else:
            val.setdefault("state", "DCA")
            val.setdefault("last_dca_ts", 0.0)
            val.setdefault("sell_submitted_ts", 0.0)
            val.setdefault("dca_confirmed_funds", 0.0)
            if self.active_targets:
                self.active_targets[0] = val
            else:
                self.active_targets = [val]

    # Backward-compat properties: delegate former instance variables into current_target
    @property
    def _dca_confirmed_funds(self) -> float:
        t = self.current_target
        return t.get("dca_confirmed_funds", 0.0) if t else 0.0

    @_dca_confirmed_funds.setter
    def _dca_confirmed_funds(self, val: float) -> None:
        t = self.current_target
        if t:
            t["dca_confirmed_funds"] = val

    @property
    def _last_dca_ts(self) -> float:
        t = self.current_target
        return t.get("last_dca_ts", 0.0) if t else 0.0

    @_last_dca_ts.setter
    def _last_dca_ts(self, val: float) -> None:
        t = self.current_target
        if t:
            t["last_dca_ts"] = val

    @property
    def _sell_submitted_ts(self) -> float:
        t = self.current_target
        return t.get("sell_submitted_ts", 0.0) if t else 0.0

    @_sell_submitted_ts.setter
    def _sell_submitted_ts(self, val: float) -> None:
        t = self.current_target
        if t:
            t["sell_submitted_ts"] = val

    # ============================================================
    # DCA fill confirmation (called from order_fsm buy-fill callback)
    # ============================================================

    def on_dca_fill_confirmed(self, *, market: str, entry_price: float,
                               qty: float, funds: float, fee: float) -> None:
        """order_fsm buy-fill callback → confirm actual DCA fill."""
        target = self._find_target(market)
        if target is None:
            return
        target["dca_confirmed_funds"] = target.get("dca_confirmed_funds", 0.0) + funds
        # Correct dca_invested to the actual fill (ACK-time estimate → actual value)
        target["dca_invested"] = target["dca_confirmed_funds"]
        self.save_state()
        logger.info("[TriageManager] DCA fill CONFIRMED market=%s funds=%.0f total_confirmed=%.0f",
                    market, funds, target["dca_confirmed_funds"])

    # ============================================================
    # Target lookup helper
    # ============================================================

    def _find_target(self, market: str) -> Optional[Dict[str, Any]]:
        """Find a target by market in active_targets."""
        for t in self.active_targets:
            if t.get("market") == market:
                return t
        return None

    # ============================================================
    # Reserved capital calculation
    # ============================================================

    def calc_reserved_capital(self, system: Any) -> float:
        """Sum remaining DCA budget across all active targets → reflect in system._triage_reserved_usdt."""
        if not self.is_active() or not self.active_targets:
            return 0.0
        total = 0.0
        for target in self.active_targets:
            try:
                ctx = system.coordinator.get_context(target["market"])
                if not ctx or not getattr(ctx, "position", None):
                    continue
                avg_buy = float(ctx.position.get("entry", 0.0) or 0.0)
                qty = float(ctx.position.get("qty", 0.0) or 0.0)
                if avg_buy <= 0 or qty <= 0:
                    continue
                current_invested = avg_buy * qty
                max_additional = current_invested * float(self.settings["max_dca_ratio"])
                already = target.get("dca_invested", 0.0)
                remaining = max(0.0, max_additional - already)
                total += remaining
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] calc_reserved_capital: %s", exc, exc_info=True)
                continue
        return total

    # ============================================================
    # Entry conditions
    # ============================================================

    def should_enter(self, system: Any) -> Tuple[bool, str]:
        """Decide whether to enter triage."""
        if self.state != self.STATE_NORMAL:
            return False, "already_in_triage"

        # Total PnL: reused from PRM (already updated every 30s)
        prm = getattr(system, "portfolio_risk_manager", None)
        total_pnl_pct: Optional[float] = None
        if prm and getattr(prm, "daily_status", None):
            total_pnl_pct = prm.daily_status.loss_pct

        # per-market loss coin count (count trigger + scoring inputs)
        loss_coins = self._gather_loss_coins(system)

        trigger_pnl = float(self.settings["trigger_pnl_pct"])   # e.g. -5.0
        trigger_count = int(self.settings["trigger_loss_count"])  # e.g. 5

        # [FIX 2026-03-23] Normalize triage entry conditions
        # Both conditions require at least trigger_loss_count loss coins to fire
        # If only PnL is bad but there are few/no positions, triage has nothing to do
        _n_loss = len(loss_coins)
        if _n_loss < trigger_count:
            return False, f"ok: pnl={total_pnl_pct:.1f}%, loss_coins={_n_loss} < {trigger_count}"

        # loss coins >= trigger_count confirmed → PnL condition OR coin-count condition
        if total_pnl_pct is not None and total_pnl_pct <= trigger_pnl:
            return True, f"total_pnl={total_pnl_pct:.2f}% <= {trigger_pnl}% (loss_coins={_n_loss})"

        return True, f"loss_coins={_n_loss} >= {trigger_count}"

    def _gather_loss_coins(self, system: Any) -> List[Dict[str, Any]]:
        """Collect the list of loss coins from current positions."""
        from app.core.hyper_price_store import price_store

        result = []
        try:
            coordinator = getattr(system, "coordinator", None)
            if not coordinator:
                return result
            # [FIX] lock protection + dict copy to avoid RuntimeError during iteration
            contexts = coordinator.get_contexts() if hasattr(coordinator, "get_contexts") else dict(getattr(coordinator, "contexts", {}))
            for market, ctx in contexts.items():
                pos = getattr(ctx, "position", None)
                if not pos:
                    continue
                qty = float(pos.get("qty", 0.0) or 0.0)
                avg_buy = float(pos.get("entry", 0.0) or 0.0)
                if qty <= 0 or avg_buy <= 0:
                    continue
                # [2026-03-30] Exclude dust coins: investments under 5 USDT are not counted as loss_coins
                if avg_buy * qty < 5.0:
                    continue
                # [FIX 2026-03-22] price_store.get() returns an orderbook dict, so
                # use get_price() to fetch a float.
                # If price is missing (e.g. right after restart), falling back to avg_buy
                # would misclassify as pnl=0%, so skip and wait until price arrives
                # (works with the _handle_scan retry logic)
                current_price = price_store.get_price(market)
                if current_price is None:
                    continue
                invested = avg_buy * qty
                current_val = current_price * qty
                pnl_pct = (current_price - avg_buy) / avg_buy * 100
                # [FIX 2026-03-24] Exclude from loss count within N minutes after a buy
                # Prevents false triage triggers from fees + spread right after a buy
                import time as _time
                _grace_min = float(self.settings.get("loss_grace_min", 30.0))
                _entry_ts = float(getattr(ctx, "_last_buy_fill_ts", 0) or
                                  getattr(ctx, "_entry_ts", 0) or 0)
                if _entry_ts > 0 and (_time.time() - _entry_ts) < _grace_min * 60:
                    continue
                if pnl_pct < 0:
                    result.append({
                        "market": market,
                        "pnl_pct": pnl_pct,
                        "invested": invested,
                        "current_val": current_val,
                        "qty": qty,
                        "avg_buy": avg_buy,
                        "current_price": current_price,
                    })
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            logger.warning("[triage] _gather_loss_coins error: %s", exc)
        return result

    # ============================================================
    # Emergency exit: dynamic profit target calculation
    # ============================================================

    def get_effective_profit_target(self, system: Any) -> Tuple[float, str]:
        """Return a dynamic profit target based on market conditions.

        Returns:
            (profit_target_pct, reason)
            - normal:    (settings.profit_target_pct, "normal")
            - moderate:  (profit_target_pct * 0.5, "moderate")
            - emergency: (0.0, "emergency")  ← exit immediately once fees are covered
        """
        base_target = float(self.settings["profit_target_pct"])

        if not self.settings.get("emergency_exit_enabled", True):
            return base_target, "normal"

        # Compute average loss depth of loss coins
        avg_loss = self._calc_avg_loss_depth(system)
        if avg_loss is None:
            return base_target, "normal"

        moderate_threshold = float(self.settings.get("emergency_moderate_avg_loss_pct", -10.0))
        severe_threshold = float(self.settings.get("emergency_severe_avg_loss_pct", -30.0))

        btc_guard = bool(getattr(system, "btc_guard_mode", False))

        # Emergency: avg_loss at or below severe threshold OR (BTC Guard ON + avg_loss beyond moderate level)
        if avg_loss <= severe_threshold or (btc_guard and avg_loss <= moderate_threshold * 2):
            return 0.0, f"emergency(avg={avg_loss:.1f}%,btc_guard={btc_guard})"

        # Moderate: avg_loss at or below moderate threshold
        if avg_loss <= moderate_threshold:
            reduced = round(base_target * 0.5, 3)
            return reduced, f"moderate(avg={avg_loss:.1f}%)"

        return base_target, "normal"

    def _calc_avg_loss_depth(self, system: Any) -> Optional[float]:
        """Compute the average loss rate (%) across all loss coins."""
        try:
            loss_coins = self._gather_loss_coins(system)
            if not loss_coins:
                return None
            total_pnl = sum(c["pnl_pct"] for c in loss_coins)
            return total_pnl / len(loss_coins)
        except (KeyError, AttributeError, TypeError):
            logger.warning("TriageManager._calc_avg_loss_depth suppressed exception", exc_info=True)
            return None

    # ============================================================
    # Triage entry
    # ============================================================

    def enter_triage(self, system: Any, reason: str) -> None:
        """Enter triage mode. Block BUY + save snapshot."""
        self.state = self.STATE_SCAN
        self.start_ts = time.time()
        self.trigger_reason = reason
        self.recovered = []
        self.skipped = []
        self.excluded = []
        self.active_targets = []

        # Performance measurement: total equity at entry time
        self._entry_equity_usdt = float(getattr(system, "_last_equity_usdt", 0.0) or 0.0)

        # Initial snapshot
        loss_coins = self._gather_loss_coins(system)
        prm = getattr(system, "portfolio_risk_manager", None)
        total_pnl = (prm.daily_status.loss_pct if prm and prm.daily_status else None)
        # Snapshot for market-recovery exit: BTC Guard state + total PnL
        _btc_guard_at_entry = bool(getattr(system, "btc_guard_mode", False))

        self.initial_snapshot = {
            "total_pnl_pct": total_pnl,
            "loss_coin_count": len(loss_coins),
            "loss_coins": [c["market"] for c in loss_coins],
            "entry_equity_usdt": self._entry_equity_usdt,
            "ts": self.start_ts,
            "btc_guard_at_entry": _btc_guard_at_entry,
        }

        system._triage_entry_blocked = True
        self.save_state()

        # Ledger record
        try:
            system.ledger.append(
                "TRIAGE_ENTERED",
                reason=reason,
                total_pnl_pct=total_pnl,
                loss_coin_count=len(loss_coins),
            )
        except (AttributeError, TypeError) as exc:
            logger.warning("[TRIAGE] ledger record: %s", exc, exc_info=True)

        # Telegram
        if self.settings.get("notify"):
            try:
                system._send_telegram_safe(
                    f"🏥 [TRIAGE] entered\nreason: {reason}\n"
                    f"loss coins: {len(loss_coins)}\nnew buys blocked"
                )
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] telegram: %s", exc, exc_info=True)

        logger.info("[TriageManager] ENTERED reason=%s loss_coins=%d", reason, len(loss_coins))

    # ============================================================
    # Recovery target selection
    # ============================================================

    def select_recovery_target(self, system: Any) -> Optional[Dict[str, Any]]:
        """Select the single coin with the highest recovery likelihood based on score."""
        from app.core.hyper_price_store import price_store
        from app.strategy import indicators

        loss_coins = self._gather_loss_coins(system)
        exclude_pct = float(self.settings["max_loss_exclude_pct"])  # e.g. -30.0
        min_val = float(self.settings["min_position_usdt"])

        done_markets = set(
            [r["market"] for r in self.recovered] +
            [s["market"] for s in self.skipped] +
            [t["market"] for t in self.active_targets]
        )

        scored = []
        new_excluded = []

        for coin in loss_coins:
            market = coin["market"]
            if market in done_markets:
                continue

            pnl_pct = coin["pnl_pct"]
            invested = coin["invested"]
            current_val = coin["current_val"]

            # Exclusion conditions
            ctx = None
            try:
                ctx = system.coordinator.get_context(market)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[TRIAGE] exclusion conditions: %s", exc, exc_info=True)

            if getattr(ctx, "longhold", False) or getattr(ctx, "is_longhold", False):
                new_excluded.append({"market": market, "reason": "longhold"})
                continue
            if pnl_pct < exclude_pct:
                new_excluded.append({"market": market, "reason": f"too_deep:{pnl_pct:.1f}%"})
                continue
            if current_val < min_val:
                new_excluded.append({"market": market, "reason": "dust"})
                continue

            # Scoring
            closeness = max(0.0, 100.0 / max(1.0, abs(pnl_pct))) * 3.0
            capital = min(50.0, invested / 100_000.0)

            # RSI (the more oversold, the higher the bounce expectation)
            rsi_score = 0.0
            try:
                tick_prices = getattr(ctx, "_tick_prices", None) if ctx else None
                if tick_prices and len(tick_prices) >= 15:
                    rsi_val = indicators.rsi(tick_prices, 14)
                    if rsi_val is not None and rsi_val < 40:
                        rsi_score = (40.0 - rsi_val) * 2.5
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[TRIAGE] RSI (oversold bounce expectation): %s", exc, exc_info=True)

            # Volume (24h — approximated by tick_prices volatility)
            vol_score = 0.0
            try:
                if tick_prices and len(tick_prices) >= 5:
                    # Approximate liquidity from the recent price range
                    hi = max(tick_prices[-20:]) if len(tick_prices) >= 20 else max(tick_prices)
                    lo = min(tick_prices[-20:]) if len(tick_prices) >= 20 else min(tick_prices)
                    if lo > 0:
                        range_pct = (hi - lo) / lo * 100
                        vol_score = min(50.0, range_pct * 2.0) * 1.5
            except (TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] approximate liquidity from recent price range: %s", exc, exc_info=True)

            total_score = closeness + capital + rsi_score + vol_score
            scored.append({**coin, "score": round(total_score, 1), "rsi_score": rsi_score})

        # Update newly excluded (without duplicates)
        ex_markets = {e["market"] for e in self.excluded}
        for e in new_excluded:
            if e["market"] not in ex_markets:
                self.excluded.append(e)
                ex_markets.add(e["market"])

        if not scored:
            return None

        scored.sort(key=lambda x: x["score"], reverse=True)
        best = scored[0]
        logger.info("[TriageManager] TARGET selected: %s score=%.1f pnl=%.2f%%",
                    best["market"], best["score"], best["pnl_pct"])
        return best

    # ============================================================
    # DCA start
    # ============================================================

    def start_recovery(self, target: Dict[str, Any], system: Any = None) -> None:
        """Build a DCA recovery plan for the selected coin → add to active_targets."""
        new_target = {
            "market": target["market"],
            "state": "DCA",
            "started_ts": time.time(),
            "original_avg_buy": target["avg_buy"],
            "original_qty": target["qty"],
            "original_pnl_pct": target["pnl_pct"],
            "dca_splits_total": self._calc_splits(
                target["pnl_pct"],
                ctx=system.coordinator.get_context(target["market"]) if system else None,
                system=system,
            ),
            "dca_splits_done": 0,
            "dca_invested": 0.0,
            "dca_confirmed_funds": 0.0,
            "last_dca_ts": 0.0,
            "sell_submitted_ts": 0.0,
            "score": target.get("score", 0.0),
        }
        self.active_targets.append(new_target)
        self.state = self.STATE_DCA
        self.save_state()

        try:
            if system:
                system.ledger.append(
                    "TRIAGE_TARGET_SELECTED",
                    market=target["market"],
                    score=target.get("score", 0),
                    pnl_pct=target["pnl_pct"],
                    concurrent=len(self.active_targets),
                )
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[TRIAGE] start_recovery fallback: %s", exc, exc_info=True)
        logger.info("[TriageManager] START_RECOVERY market=%s splits=%d concurrent=%d",
                    target["market"], new_target["dca_splits_total"], len(self.active_targets))

    def _calc_splits(self, pnl_pct: float, ctx: Any = None, system: Any = None) -> int:
        """Determine the number of DCA splits based on loss depth + market conditions.

        Base: loss depth → 2/3/4
        Dynamic adjustment: RSI oversold → +1, high volatility → +1, BTC downtrend → +1
        Range: 2~6 (more splits = more conservative)
        """
        # Base splits
        if pnl_pct > -5:
            base = 2
        elif pnl_pct > -10:
            base = 3
        else:
            base = 4

        adjust = 0
        try:
            from app.strategy import indicators
            tick_prices = getattr(ctx, "_tick_prices", None) if ctx else None
            if tick_prices and len(tick_prices) >= 15:
                rsi_val = indicators.rsi(tick_prices, 14)
                vol = indicators.volatility(tick_prices, 14)

                # RSI oversold (< 30): high bounce expectation → fewer splits to front-load the position
                if rsi_val is not None and rsi_val < 30:
                    adjust -= 1

                # High volatility (> 8%): further downside possible → more splits, more conservative
                if vol is not None and vol > 8.0:
                    adjust += 1

            # BTC downtrend: overall market weakness → more splits, more conservative
            if system and getattr(system, "btc_guard_mode", False):
                adjust += 1
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[TRIAGE] BTC downtrend / market weakness conservative split: %s", exc, exc_info=True)

        return max(2, min(6, base + adjust))

    # ============================================================
    # DCA execution (offloaded to bg_executor by system)
    # ============================================================

    def execute_dca_step(self, target_or_system, system=None) -> None:
        """Execute one DCA split-buy step (called from executor).

        Duplicate prevention is handled by order_fsm's ctx.order_state check.
        Backward compat: execute_dca_step(system) → uses current_target
        """
        if system is None:
            system = target_or_system
            target = self.current_target
            if target is None:
                return
        else:
            target = target_or_system
        now = time.time()
        interval = float(self.settings["dca_interval_sec"])
        if (now - target.get("last_dca_ts", 0.0)) < interval:
            return

        market = target["market"]
        splits_total = target["dca_splits_total"]
        splits_done = target["dca_splits_done"]

        if splits_done >= splits_total:
            # DCA complete → transition to WAIT state
            target["state"] = "WAIT"
            self._update_display_state()
            self.save_state()
            logger.info("[TriageManager] DCA complete → WAIT market=%s", market)
            return

        # Budget calculation
        try:
            from app.core.hyper_price_store import price_store
            ctx = system.coordinator.get_context(market)
            if not ctx or not getattr(ctx, "position", None):
                return

            qty = float(ctx.position.get("qty", 0.0) or 0.0)
            avg_buy = float(ctx.position.get("entry", 0.0) or 0.0)
            _raw_price = price_store.get_price(market)
            if _raw_price is None or float(_raw_price or 0) <= 0:
                logger.debug("[TriageManager] DCA skip: price unavailable for %s", market)
                return
            current_price = float(_raw_price)
            if avg_buy <= 0 or qty <= 0:
                return

            current_invested = avg_buy * qty
            max_additional = current_invested * float(self.settings["max_dca_ratio"])
            already_invested = target["dca_invested"]
            remaining_budget = max(0.0, max_additional - already_invested)

            # ★ Global DCA cap: allow DCA up to only N% of the total portfolio (Upbit sync 2026-04-05)
            global_cap_pct = float(self.settings.get("global_dca_cap_pct", 30.0))
            total_equity = float(getattr(system, "_last_equity_usdt", 0.0) or getattr(system, "_last_cash_usdt", 0.0) or 0.0)
            if total_equity > 0:
                global_cap = total_equity * (global_cap_pct / 100.0)
                total_dca_all = sum(t.get("dca_invested", 0.0) for t in self.active_targets)
                if total_dca_all >= global_cap:
                    logger.info("[TriageManager] DCA GLOBAL CAP reached: total_dca=%.0f >= cap=%.0f (%.0f%%) market=%s",
                                total_dca_all, global_cap, global_cap_pct, market)
                    return
                remaining_budget = min(remaining_budget, global_cap - total_dca_all)

            remaining_splits = splits_total - splits_done
            per_split = remaining_budget / max(1, remaining_splits)

            # Dynamic budget adjustment: increase weight when RSI oversold, decrease on BTC downtrend
            try:
                from app.strategy import indicators
                tick_prices = getattr(ctx, "_tick_prices", None)
                if tick_prices and len(tick_prices) >= 15:
                    rsi_val = indicators.rsi(tick_prices, 14)
                    if rsi_val is not None:
                        if rsi_val < 30:       # strong oversold → +20%
                            per_split *= 1.2
                        elif rsi_val > 60:     # bounce in progress → -15%
                            per_split *= 0.85
                if getattr(system, "btc_guard_mode", False):
                    per_split *= 0.7           # BTC downtrend → -30%
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[TRIAGE] dynamic budget adjustment (RSI oversold up, BTC down down): %s", exc, exc_info=True)

            # Minimum order check
            min_order = float(getattr(system, "min_order_usdt", 5.0))
            if per_split < min_order:
                logger.info("[TriageManager] DCA skip: per_split=%.0f < min_order=%.0f market=%s",
                            per_split, min_order, market)
                # Insufficient capital → transition to WAIT
                target["state"] = "WAIT"
                self._update_display_state()
                self.save_state()
                return

            # Available cash check
            avail_usdt = float(getattr(system, "_last_cash_usdt", 0.0) or 0.0)
            per_split = min(per_split, avail_usdt * 0.95)
            if per_split < min_order:
                logger.info("[TriageManager] DCA skip: insufficient cash avail=%.0f market=%s", avail_usdt, market)
                return

            target["last_dca_ts"] = now

            ok, msg = system.order_fsm.submit_market_buy(
                ctx=ctx,
                market=market,
                usdt_amount=per_split,
                expected_price=current_price,
                reason="triage:dca",
            )

            if ok:
                target["dca_splits_done"] += 1
                target["dca_invested"] += per_split
                self.save_state()
                try:
                    system.ledger.append(
                        "TRIAGE_DCA_STEP",
                        market=market,
                        split=target["dca_splits_done"],
                        of=splits_total,
                        usdt=round(per_split),
                        current_price=round(current_price, 2),
                        note="ack_not_fill",
                    )
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[TRIAGE] ledger append (DCA step): %s", exc, exc_info=True)
                logger.info("[TriageManager] DCA step %d/%d market=%s usdt=%.0f (ACK, fill pending)",
                            target["dca_splits_done"], splits_total, market, per_split)
                try:
                    reserved = self.calc_reserved_capital(system)
                    system._triage_reserved_usdt = reserved
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[TRIAGE] reserved capital update: %s", exc, exc_info=True)
            else:
                logger.warning("[TriageManager] DCA submit failed market=%s: %s", market, msg)

        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.error("[TriageManager] execute_dca_step error market=%s: %s", market, exc)

    # ============================================================
    # Sell condition check
    # ============================================================

    def check_sell_condition(self, target_or_system, system=None) -> Tuple[bool, str]:
        """Check whether breakeven + alpha is reached based on the post-DCA new average price."""
        if system is None:
            system = target_or_system
            target = self.current_target
            if target is None:
                return False, "no_target"
        else:
            target = target_or_system
        market = target["market"]
        try:
            from app.core.hyper_price_store import price_store
            ctx = system.coordinator.get_context(market)
            if not ctx or not getattr(ctx, "position", None):
                return False, "no_position"

            # Post-DCA new average price (updated by exchange reconcile)
            avg_buy = float(ctx.position.get("entry", 0.0) or 0.0)
            current_price = price_store.get_price(market)
            if current_price is None:
                return False, "price_not_ready"
            if avg_buy <= 0 or current_price <= 0:
                return False, "invalid_price"

            # Target: (price - post-DCA avg) / post-DCA avg >= profit_target + fee
            # Emergency exit: dynamically lower profit_target based on market conditions
            effective_target, severity = self.get_effective_profit_target(system)
            self._last_emergency_info = {"target_pct": effective_target, "severity": severity}
            target_pct = effective_target + float(self.settings["fee_pct"])
            pnl_pct = (current_price - avg_buy) / avg_buy * 100

            if pnl_pct >= target_pct:
                return True, f"target_reached: {pnl_pct:.2f}% >= {target_pct:.2f}% ({severity})"
            return False, f"waiting: {pnl_pct:.2f}% < {target_pct:.2f}% ({severity})"

        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("TriageManager.check_sell_condition except: %s", exc, exc_info=True)
            return False, f"error: {exc}"

    def is_coin_timeout(self, target=None) -> bool:
        """Whether the recovery attempt for this coin has timed out."""
        if target is None:
            target = self.current_target
            if target is None:
                return False
        elapsed = time.time() - target.get("started_ts", time.time())
        timeout_sec = float(self.settings["coin_timeout_hours"]) * 3600
        return elapsed > timeout_sec

    # ============================================================
    # Sell execution
    # ============================================================

    def execute_sell(self, target_or_system, system=None) -> None:
        """Market-sell the entire target position."""
        if system is None:
            system = target_or_system
            target = self.current_target
            if target is None:
                return
        else:
            target = target_or_system
        market = target["market"]
        try:
            from app.core.hyper_price_store import price_store
            ctx = system.coordinator.get_context(market)
            if not ctx or not getattr(ctx, "position", None):
                logger.warning("[TriageManager] execute_sell: no position market=%s", market)
                return

            qty = float(ctx.position.get("qty", 0.0) or 0.0)
            expected_price = price_store.get_price(market) or float(
                ctx.position.get("entry", 0.0) or 0.0
            )
            if qty <= 0:
                logger.warning("[TriageManager] execute_sell: qty=0 market=%s", market)
                self._remove_target(target, system, recovered=False)
                return

            avg_buy = float(ctx.position.get("entry", 0.0) or 0.0)
            target["sell_snapshot_qty"] = qty
            target["sell_snapshot_avg"] = avg_buy

            ok, msg = system.order_fsm.submit_market_sell(
                ctx=ctx,
                market=market,
                qty=qty,
                expected_price=expected_price,
                reason="triage:tp_hit",
            )

            if ok:
                target["state"] = "SELL"
                target["sell_submitted_ts"] = time.time()
                self._update_display_state()
                self.save_state()
                logger.info("[TriageManager] SELL submitted market=%s qty=%.6f avg=%.2f", market, qty, avg_buy)
            else:
                logger.warning("[TriageManager] SELL failed market=%s: %s", market, msg)
                try:
                    system.ledger.append("TRIAGE_SELL_FAILED", market=market, error=str(msg))
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[TRIAGE] ledger append (sell failed): %s", exc, exc_info=True)
                target["state"] = "WAIT"
                self._update_display_state()
                self.save_state()
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            logger.error("[TriageManager] execute_sell error market=%s: %s", market, exc)
            target["state"] = "WAIT"
            self._update_display_state()
            self.save_state()

    # ============================================================
    # Sell completion detection
    # ============================================================

    def is_sell_complete(self, target_or_system, system=None) -> bool:
        """Sell completion: position qty ≈ 0 or 5-minute timeout."""
        if system is None:
            system = target_or_system
            target = self.current_target
            if target is None:
                return False
        else:
            target = target_or_system
        market = target["market"]

        # Timeout check (prevents infinite wait on partial fills, etc.)
        sell_timeout = float(self.settings.get("sell_timeout_sec", 300.0))
        if (time.time() - target.get("sell_submitted_ts", 0.0)) > sell_timeout:
            # Check remaining qty then retry sell (once only)
            try:
                ctx = system.coordinator.get_context(market)
                remaining_qty = float(ctx.position.get("qty", 0.0) or 0.0) if ctx and getattr(ctx, "position", None) else 0.0
                if remaining_qty > 1e-8 and not target.get("_sell_retry_done", False):
                    target["_sell_retry_done"] = True
                    logger.warning("[TriageManager] SELL timeout market=%s remaining=%.6f — retrying sell", market, remaining_qty)
                    try:
                        from app.core.hyper_price_store import price_store
                        expected_price = price_store.get_price(market) or float(ctx.position.get("entry", 0.0) or 0.0)
                        system.order_fsm.submit_market_sell(
                            ctx=ctx, market=market, qty=remaining_qty,
                            expected_price=expected_price, reason="triage:tp_hit_retry",
                        )
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[TRIAGE] retry sell after checking remaining qty (once): %s", exc, exc_info=True)
                    target["sell_submitted_ts"] = time.time()
                    return False
            except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                logger.warning("[TRIAGE] retry sell after checking remaining qty (once): %s", exc, exc_info=True)
            logger.warning("[TriageManager] SELL timeout market=%s — forcing advance", market)
            target["_sell_retry_done"] = False
            return True

        # position qty check
        try:
            ctx = system.coordinator.get_context(market)
            if not ctx or not getattr(ctx, "position", None):
                return True
            qty = float(ctx.position.get("qty", 0.0) or 0.0)
            return qty < 1e-8
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("TriageManager.is_sell_complete suppressed exception", exc_info=True)
            return False

    # ============================================================
    # Recovery completion handling
    # ============================================================

    def on_recovery_complete(self, target_or_system, system=None) -> None:
        """Market recovery complete → add to recovered, remove from active_targets."""
        if system is None:
            system = target_or_system
            target = self.current_target
            if target is None:
                return
        else:
            target = target_or_system
        market = target["market"]

        # Profit calc: prefer the execute_sell() snapshot, then fallback order
        profit_usdt = 0.0
        try:
            from app.core.hyper_price_store import price_store
            sell_price = price_store.get_price(market) or 0.0

            actual_avg = float(target.get("sell_snapshot_avg", 0.0) or 0.0)
            actual_qty = float(target.get("sell_snapshot_qty", 0.0) or 0.0)

            if actual_avg <= 0 or actual_qty <= 0:
                ctx = system.coordinator.get_context(market) if system else None
                if ctx and getattr(ctx, "position", None):
                    actual_avg = float(ctx.position.get("entry", 0.0) or 0.0)
                    actual_qty = float(ctx.position.get("qty", 0.0) or 0.0)

            if actual_avg <= 0 or actual_qty <= 0:
                actual_avg = target.get("original_avg_buy", 0.0)
                actual_qty = target.get("original_qty", 0.0)

            if sell_price > 0 and actual_avg > 0 and actual_qty > 0:
                total_cost = actual_avg * actual_qty
                total_revenue = sell_price * actual_qty
                fee = (total_cost + total_revenue) * 0.001
                profit_usdt = total_revenue - total_cost - fee
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[TRIAGE] profit calc (prefer execute_sell snapshot, then fallback): %s", exc, exc_info=True)

        self.recovered.append({
            "market": market,
            "recovered_ts": time.time(),
            "profit_usdt": round(profit_usdt, 2),
            "dca_invested": target.get("dca_invested", 0.0),
        })
        self._remove_target_from_list(target)
        self._update_display_state()
        self.save_state()

        try:
            system.ledger.append(
                "TRIAGE_RECOVERED",
                market=market,
                profit_usdt=round(profit_usdt, 2),
                total_recovered=len(self.recovered),
                remaining_targets=len(self.active_targets),
            )
        except (TypeError, ValueError) as exc:
            logger.warning("[TRIAGE] ledger append (recovered): %s", exc, exc_info=True)

        if self.settings.get("notify"):
            try:
                system._send_telegram_safe(
                    f"✅ [TRIAGE] {market} recovered!\n"
                    f"recovery profit (est.): {profit_usdt:+,.2f} USDT\n"
                    f"done: {len(self.recovered)} / in progress: {len(self.active_targets)}"
                )
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] telegram notify (recovered): %s", exc, exc_info=True)

        logger.info("[TriageManager] RECOVERED market=%s profit≈%.0f remaining=%d",
                    market, profit_usdt, len(self.active_targets))

    # ============================================================
    # Skip
    # ============================================================

    def skip_target(self, target: Dict[str, Any], system: Any, reason: str = "manual") -> None:
        """Skip a recovery target → remove from active_targets."""
        market = target["market"]
        self.skipped.append({"market": market, "reason": reason, "ts": time.time()})
        self._remove_target_from_list(target)
        self._update_display_state()
        self.save_state()

        try:
            system.ledger.append("TRIAGE_SKIPPED", market=market, reason=reason,
                                 remaining_targets=len(self.active_targets))
        except (AttributeError, TypeError) as exc:
            logger.warning("[TRIAGE] skip_target fallback: %s", exc, exc_info=True)
        logger.info("[TriageManager] SKIPPED market=%s reason=%s remaining=%d",
                    market, reason, len(self.active_targets))

    def skip_current_target(self, system: Any, reason: str = "manual") -> None:
        """Backward compat: skip the first active target."""
        if self.active_targets:
            self.skip_target(self.active_targets[0], system, reason)

    def _remove_target_from_list(self, target: Dict[str, Any]) -> None:
        """Remove a target from active_targets (identity comparison)."""
        self.active_targets = [t for t in self.active_targets if t is not target]

    def _remove_target(self, target: Dict[str, Any], system: Any, recovered: bool) -> None:
        if recovered:
            self.on_recovery_complete(target, system)
        else:
            self._remove_target_from_list(target)
            self._update_display_state()
            self.save_state()

    def _update_display_state(self) -> None:
        """Update the global display state from active_targets states."""
        if not self.active_targets:
            if self.is_active():
                self.state = self.STATE_SCAN
            return
        # Priority: SELL > WAIT > DCA
        states = {t.get("state", "DCA") for t in self.active_targets}
        if "SELL" in states:
            self.state = self.STATE_SELL
        elif "WAIT" in states:
            self.state = self.STATE_WAIT
        else:
            self.state = self.STATE_DCA

    # ============================================================
    # Exit conditions
    # ============================================================

    def should_exit_triage(self, system: Optional[Any] = None) -> Tuple[bool, str]:
        """Decide whether to exit triage (4 conditions).

        [FIX 2026-03-22] Fixed 3 immediate-exit bugs (2nd review):
          1) min_duration_guard: cannot exit before 60 seconds
             - Before: could exit immediately on the first poll() call 5s after entry
          2) ALL condition: skip if initial_count=0
             - Before: right after entry, price unavailable → loss_coin_count=0 →
                       _count_remaining_loss_coins()=0 → returned "all_recovered" immediately
          3) PnL condition: only check when initial_pnl < exit_pnl
             - Before: prm.daily_status.loss_pct(0.0) >= exit_pnl(-2.0) →
                       returned "pnl_recovered" immediately even with no loss at entry
        """
        elapsed = time.time() - self.start_ts

        # [FIX] Minimum 60s guard — prevent misjudgment right after entry
        if elapsed < 60:
            return False, f"min_duration_guard: {elapsed:.0f}s < 60s"

        # Condition 1: recovery target reached
        target_str = str(self.settings.get("recovery_target", "ALL")).strip()
        initial_count = self.initial_snapshot.get("loss_coin_count", 0)
        recovered_count = len(self.recovered)

        if target_str == "ALL":
            # [FIX] Skip if initial_count=0 — snapshot not yet finalized
            # (corrected in _handle_scan when the first target is found)
            if initial_count > 0:
                loss_coins_remaining = self._count_remaining_loss_coins(system)
                if loss_coins_remaining == 0:
                    return True, f"all_recovered: {recovered_count} recovered"
        elif target_str.replace(".", "").isdigit():
            # [FIX 2026-03-24] contains "." → ratio (0.0~1.0), integer → count
            # "0.8" → recover 80%, "3" → recover 3, "1.0" → recover 100%, "1" → recover 1
            if "." in target_str:
                val = max(0.0, min(1.0, float(target_str)))
                if initial_count > 0 and recovered_count / initial_count >= val:
                    return True, f"ratio_target: {recovered_count}/{initial_count} >= {val:.0%}"
            else:
                val = int(float(target_str))
                if recovered_count >= val:
                    return True, f"count_target: {recovered_count}>={val}"

        # Condition 2: total PnL recovered
        # [FIX] Only check when PnL at entry was worse than exit_pnl
        # (fixes the bug where 0.0% at entry made 0.0% >= -2.0% immediately true)
        if system:
            prm = getattr(system, "portfolio_risk_manager", None)
            if prm and getattr(prm, "daily_status", None):
                exit_pnl = float(self.settings["exit_pnl_pct"])
                initial_pnl = self.initial_snapshot.get("total_pnl_pct", 0.0)
                current_pnl = prm.daily_status.loss_pct
                if initial_pnl < exit_pnl and current_pnl >= exit_pnl:
                    return True, f"pnl_recovered: {current_pnl:.2f}% >= {exit_pnl}%"

        # Condition 3: automatic exit on market recovery
        # BTC Guard OFF (recovered) + total PnL improved vs entry + minimum elapsed time
        # Whole market fell together → entered triage → on market recovery, auto-exit and resume normal trading
        if self.settings.get("market_recovery_exit_enabled", True) and system:
            _min_h = float(self.settings.get("market_recovery_min_hours", 2.0))
            if elapsed >= _min_h * 3600:
                _btc_guard_now = bool(getattr(system, "btc_guard_mode", False))
                if not _btc_guard_now:
                    # BTC Guard OFF confirmed — also confirm PnL improvement
                    _prm = getattr(system, "portfolio_risk_manager", None)
                    _cur_pnl = (_prm.daily_status.loss_pct
                                if _prm and getattr(_prm, "daily_status", None) else None)
                    _entry_pnl = self.initial_snapshot.get("total_pnl_pct")
                    if _cur_pnl is not None and _entry_pnl is not None:
                        if _cur_pnl > _entry_pnl:
                            return True, (
                                f"market_recovery: btc_guard=OFF, "
                                f"pnl={_cur_pnl:.2f}% > entry={_entry_pnl:.2f}%, "
                                f"elapsed={elapsed/3600:.1f}h"
                            )

        # Condition 4: max duration exceeded
        max_hours = float(self.settings["max_duration_hours"])
        if elapsed > max_hours * 3600:
            return True, f"max_duration: {elapsed/3600:.1f}h > {max_hours}h"

        return False, f"continuing: recovered={recovered_count}, initial={initial_count}"

    def _count_remaining_loss_coins(self, system: Optional[Any]) -> int:
        """Current number of loss coins (excluding recovered + skipped)."""
        if not system:
            return 1  # if unknown, continue
        done = {r["market"] for r in self.recovered} | {s["market"] for s in self.skipped}
        loss_coins = self._gather_loss_coins(system)
        return sum(1 for c in loss_coins if c["market"] not in done)

    # ============================================================
    # Triage exit
    # ============================================================

    def exit_triage(self, system: Any, reason: str) -> None:
        """Exit triage mode → return to normal."""
        elapsed_hours = (time.time() - self.start_ts) / 3600

        # Performance measurement
        exit_equity = float(getattr(system, "_last_equity_usdt", 0.0) or 0.0)
        entry_equity = self._entry_equity_usdt or self.initial_snapshot.get("entry_equity_usdt", 0.0)
        equity_change = exit_equity - entry_equity if entry_equity > 0 else 0.0
        equity_change_pct = (equity_change / entry_equity * 100) if entry_equity > 0 else 0.0
        total_profit = sum(r.get("profit_usdt", 0) for r in self.recovered)
        total_dca_invested = sum(r.get("dca_invested", 0) for r in self.recovered)

        self.state = self.STATE_NORMAL
        self.active_targets = []  # clear all in-progress targets
        system._triage_entry_blocked = False
        system._triage_reserved_usdt = 0.0  # release capital reservation
        self.save_state()

        # Record session history (runtime/triage_history.jsonl)
        self._save_session_history(
            reason=reason,
            elapsed_hours=elapsed_hours,
            entry_equity=entry_equity,
            exit_equity=exit_equity,
            equity_change=equity_change,
            equity_change_pct=equity_change_pct,
            total_profit=total_profit,
            total_dca_invested=total_dca_invested,
        )

        try:
            system.ledger.append(
                "TRIAGE_EXITED",
                reason=reason,
                recovered_count=len(self.recovered),
                skipped_count=len(self.skipped),
                elapsed_hours=round(elapsed_hours, 2),
                entry_equity=round(entry_equity),
                exit_equity=round(exit_equity),
                equity_change=round(equity_change),
                equity_change_pct=round(equity_change_pct, 2),
            )
        except (TypeError, ValueError) as exc:
            logger.warning("[TRIAGE] session history record: %s", exc, exc_info=True)

        if self.settings.get("notify"):
            try:
                system._send_telegram_safe(
                    f"🎉 [TRIAGE] back to normal mode\n"
                    f"reason: {reason}\n"
                    f"recovered: {len(self.recovered)} / skipped: {len(self.skipped)}\n"
                    f"est. profit: {total_profit:+,.2f} USDT\n"
                    f"equity change: {equity_change:+,.2f} USDT ({equity_change_pct:+.2f}%)\n"
                    f"DCA invested: {total_dca_invested:,.0f}\n"
                    f"elapsed: {elapsed_hours:.1f}h"
                )
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] telegram notify on exit: %s", exc, exc_info=True)

        logger.info("[TriageManager] EXITED reason=%s recovered=%d equity_change=%.0f(%.2f%%) elapsed=%.1fh",
                    reason, len(self.recovered), equity_change, equity_change_pct, elapsed_hours)

    # ============================================================
    # Status queries
    # ============================================================

    def is_active(self) -> bool:
        """Whether triage is active (any state other than NORMAL)."""
        return self.state != self.STATE_NORMAL

    def get_status_dict(self) -> Dict[str, Any]:
        """Status dictionary for API responses."""
        elapsed_sec = (time.time() - self.start_ts) if self.start_ts else 0.0
        elapsed_hours = elapsed_sec / 3600
        return {
            "enabled": bool(self.settings.get("enabled", False)),
            "state": self.state,
            "active": self.is_active(),
            "started_at": self.start_ts or None,
            "elapsed_sec": round(elapsed_sec, 1),
            "elapsed_hours": round(elapsed_hours, 2),
            "trigger_reason": self.trigger_reason,
            "initial_snapshot": self.initial_snapshot,
            "current_target": self.current_target,  # backward compat (first target)
            "active_targets": self.active_targets,   # full list of parallel recovery targets
            "recovered": self.recovered,
            "skipped": self.skipped,
            "excluded": self.excluded,
            "recovered_count": len(self.recovered),
            "skipped_count": len(self.skipped),
            "active_target_count": len(self.active_targets),
            "emergency_exit": getattr(self, "_last_emergency_info", None),
            "settings": {k: v for k, v in self.settings.items() if k != "fee_pct"},
        }

    # ============================================================
    # Main state-machine poll (called from tick_loop via bg_executor)
    # ============================================================

    def poll(self, system: Any) -> None:
        """
        State-machine dispatcher called periodically from tick_loop.

        Parallel recovery: processes each active_targets state (DCA/WAIT/SELL),
        and assigns additional targets via SCAN when there are empty slots.
        """
        try:
            # Auto-entry (NORMAL state + enabled + not yet entered)
            if self.state == self.STATE_NORMAL:
                if self.settings.get("enabled"):
                    ok, reason = self.should_enter(system)
                    if ok:
                        self.enter_triage(system, reason)
                return

            # Exit condition check (in all active states)
            should_exit, exit_reason = self.should_exit_triage(system)
            if should_exit:
                self.exit_triage(system, reason=exit_reason)
                return

            # Refresh capital reservation (every poll cycle)
            try:
                system._triage_reserved_usdt = self.calc_reserved_capital(system)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[TRIAGE] reserved capital refresh: %s", exc, exc_info=True)

            # Process each active target (iterate over a copy — may be removed during processing)
            for target in list(self.active_targets):
                tstate = target.get("state", "DCA")
                if tstate == "DCA":
                    self._handle_target_dca(target, system)
                elif tstate == "WAIT":
                    self._handle_target_wait(target, system)
                elif tstate == "SELL":
                    self._handle_target_sell(target, system)

            # Fill empty slots (SCAN)
            max_targets = int(self.settings.get("max_concurrent_targets", 3))
            self._fill_target_slots(max_targets, system)

            # If active_targets is empty and there are no more targets, check for exit
            if not self.active_targets:
                # handled via all_recovered etc. in should_exit_triage — exits on next poll
                pass

        except Exception as exc:
            now = time.time()
            if (now - getattr(self, "_last_poll_error_ts", 0.0)) >= 60.0:
                self._last_poll_error_ts = now
                logger.error("[TriageManager] poll error state=%s: %s", self.state, exc, exc_info=True)
                try:
                    system.ledger.append("TRIAGE_POLL_ERROR", state=self.state, error=str(exc))
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[TRIAGE] poll error ledger append: %s", exc, exc_info=True)

    def _fill_target_slots(self, max_targets: int, system: Any) -> None:
        """Fill empty slots via SCAN."""
        while len(self.active_targets) < max_targets:
            prev_count = len(self.active_targets)
            target = self.select_recovery_target(system)
            if target is None:
                # no target
                if not self.active_targets:
                    no_target_count = getattr(self, "_no_target_count", 0) + 1
                    self._no_target_count = no_target_count
                    elapsed = time.time() - self.start_ts
                    if no_target_count >= 24 or elapsed > float(self.settings.get("max_duration_hours", 168)) * 3600:
                        logger.info("[TriageManager] SCAN: no eligible target after %d retries → exiting", no_target_count)
                        self.exit_triage(system, reason="no_eligible_target")
                    else:
                        logger.debug("[TriageManager] SCAN: no eligible target, retry %d/24", no_target_count)
                break
            else:
                self._no_target_count = 0
                # Correct the snapshot when the first target is found
                if self.initial_snapshot.get("loss_coin_count", 0) == 0:
                    loss_coins = self._gather_loss_coins(system)
                    self.initial_snapshot["loss_coin_count"] = len(loss_coins)
                    self.initial_snapshot["loss_coins"] = [c["market"] for c in loss_coins]
                    logger.info("[TriageManager] initial_snapshot corrected: loss_coin_count=%d", len(loss_coins))
                    self.save_state()
                self.start_recovery(target, system=system)
                logger.info("[TriageManager] SCAN → DCA: %s (concurrent=%d)",
                            target["market"], len(self.active_targets))
                # Safety: prevent infinite loop if start_recovery did not actually add a target
                if len(self.active_targets) <= prev_count:
                    break

    def _handle_target_dca(self, target: Dict[str, Any], system: Any) -> None:
        """Execute a DCA step."""
        splits_total = target.get("dca_splits_total", 1)
        splits_done = target.get("dca_splits_done", 0)
        if splits_done < splits_total:
            self.execute_dca_step(target, system)
        else:
            target["state"] = "WAIT"
            self._update_display_state()
            self.save_state()

    def _handle_target_wait(self, target: Dict[str, Any], system: Any) -> None:
        """Detect reaching the target profit rate."""
        market = target.get("market", "?")

        # Auto-skip if the position was cleared externally
        try:
            ctx = system.coordinator.get_context(market)
            if ctx is not None and not getattr(ctx, "position", None):
                logger.info("[TriageManager] WAIT → SKIP: %s position externally cleared", market)
                self.skip_target(target, system, reason="position_externally_cleared")
                return
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[TRIAGE] position externally cleared check: %s", exc, exc_info=True)

        reached, reason = self.check_sell_condition(target, system)
        if reached:
            logger.info("[TriageManager] WAIT → SELL: %s (%s)", market, reason)
            self.execute_sell(target, system)
        elif self.is_coin_timeout(target):
            dca_invested = target.get("dca_invested", 0.0)
            logger.warning("[TriageManager] WAIT timeout → hold(skip): %s dca_invested=%.0f",
                           market, dca_invested)
            self.skip_target(target, system, reason=f"coin_timeout_hold(dca={dca_invested:.0f})")

    def _handle_target_sell(self, target: Dict[str, Any], system: Any) -> None:
        """Detect sell completion."""
        if self.is_sell_complete(target, system):
            market = target.get("market", "?")
            logger.info("[TriageManager] SELL complete → RECOVERED: %s", market)
            self.on_recovery_complete(target, system)

    # ============================================================
    # Backward compat: former _handle_* methods (for tests and external code)
    # ============================================================

    def _handle_scan(self, system: Any) -> None:
        """Backward compat: _fill_target_slots + handle first target."""
        max_targets = int(self.settings.get("max_concurrent_targets", 3))
        self._fill_target_slots(max_targets, system)

    def _handle_dca(self, system: Any) -> None:
        """Backward compat: handle DCA for current_target."""
        target = self.current_target
        if target is None:
            self.state = self.STATE_SCAN
            return
        self._handle_target_dca(target, system)

    def _handle_wait(self, system: Any) -> None:
        """Backward compat: handle WAIT for current_target."""
        target = self.current_target
        if target is None:
            self.state = self.STATE_SCAN
            return
        self._handle_target_wait(target, system)

    def _handle_sell(self, system: Any) -> None:
        """Backward compat: handle SELL for current_target."""
        target = self.current_target
        if target is None:
            self.state = self.STATE_SCAN
            return
        self._handle_target_sell(target, system)

    # ============================================================
    # Session history (performance measurement)
    # ============================================================

    def _save_session_history(self, **kwargs) -> None:
        """Append the triage session result to runtime/triage_history.jsonl."""
        try:
            history_path = Path("runtime/triage_history.jsonl")
            history_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": time.time(),
                "start_ts": self.start_ts,
                "trigger_reason": self.trigger_reason,
                "recovered_count": len(self.recovered),
                "skipped_count": len(self.skipped),
                "excluded_count": len(self.excluded),
                "recovered_markets": [r["market"] for r in self.recovered],
                "skipped_markets": [s["market"] for s in self.skipped],
                **kwargs,
            }
            with open(history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            # File size limit (drop the front if it exceeds 1MB)
            try:
                if history_path.stat().st_size > 1_000_000:
                    lines = history_path.read_text(encoding="utf-8").strip().split("\n")
                    history_path.write_text("\n".join(lines[-100:]) + "\n", encoding="utf-8")
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] history file truncation: %s", exc, exc_info=True)
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[TriageManager] _save_session_history error: %s", exc)

    # ============================================================
    # Persistence
    # ============================================================

    def save_state(self) -> None:
        """Atomically save to runtime/triage_state.json."""
        try:
            path = Path(self.settings.get("state_path", "runtime/triage_state.json"))
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "state": self.state,
                "start_ts": self.start_ts,
                "trigger_reason": self.trigger_reason,
                "active_targets": self.active_targets,
                "recovered": self.recovered,
                "skipped": self.skipped,
                "excluded": self.excluded,
                "initial_snapshot": self.initial_snapshot,
                "settings": self.settings,
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.error("[TriageManager] save_state failed: %s", exc)

    def load_state(self) -> None:
        """Restore triage_state.json (persist state across restarts)."""
        path = Path(self.settings.get("state_path", "runtime/triage_state.json"))
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.state = data.get("state", self.STATE_NORMAL)
            self.start_ts = float(data.get("start_ts", 0.0))
            self.trigger_reason = data.get("trigger_reason", "")

            # Backward compat: convert old-format current_target → active_targets
            if "active_targets" in data:
                self.active_targets = data["active_targets"] or []
            elif data.get("current_target"):
                old_target = data["current_target"]
                # Old format → new format: add the state field
                if "state" not in old_target:
                    # Infer per-target state from the global state
                    _gs = data.get("state", "")
                    if "SELL" in _gs:
                        old_target["state"] = "SELL"
                    elif "WAIT" in _gs:
                        old_target["state"] = "WAIT"
                    else:
                        old_target["state"] = "DCA"
                    # Add per-target timing fields
                    old_target.setdefault("last_dca_ts", 0.0)
                    old_target.setdefault("sell_submitted_ts", 0.0)
                    old_target.setdefault("dca_confirmed_funds", 0.0)
                self.active_targets = [old_target]
                logger.info("[TriageManager] migrated current_target → active_targets[0]")
            else:
                self.active_targets = []

            self.recovered = data.get("recovered", [])
            self.skipped = data.get("skipped", [])
            self.excluded = data.get("excluded", [])
            self.initial_snapshot = data.get("initial_snapshot", {})
            # Restore settings: PATCH changes saved in the state file take priority, ENV is the fallback
            saved_settings = data.get("settings")
            if saved_settings and isinstance(saved_settings, dict):
                for k, v in saved_settings.items():
                    if k in self.settings and v is not None:
                        self.settings[k] = v
            logger.info("[TriageManager] state loaded: state=%s active_targets=%d recovered=%d",
                        self.state, len(self.active_targets), len(self.recovered))
        except (OSError, json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            logger.error("[TriageManager] load_state failed: %s", exc)
