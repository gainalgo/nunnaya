"""Drawdown Shield -- equity high watermark tracking + automatic risk reduction on losing streaks.

A gift sent from this agent's server to the sibling server.
Detects sharp drawdowns in the FOCUS strategy (e.g. 10% evaporated vs equity peak),
and raises the conviction penalty as it deepens so the Scanner naturally becomes more selective.

Does not block trades directly — provides advisory information only.
focus_manager calls get_protection_level() to receive the penalty and apply it to conviction.

[2026-04-18 refactor — sibling server]
  Old: cumulative PnL-based DD% = (peak_pnl - current_pnl) / peak_pnl * 100
        → when current_pnl drops negative, DD% diverges past 100% (anomalous values like 213%)
  New: Equity(USDT)-based DD% = (peak_equity - current_equity) / peak_equity * 100
        → equity is always > 0 on leveraged perpetuals, so it naturally stays in the 0~100% range
        → peak persists across sessions (high watermark); DD only when current_equity < peak_equity
        → hitting a new high automatically resets DD to 0

Files:
  - runtime/drawdown_shield.json — watermark + history state
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from typing import Any, Dict, List

from app.core.io_utils import safe_load_json, safe_write_json

logger = logging.getLogger(__name__)

STATE_PATH = os.path.join("runtime", "drawdown_shield.json")

# ── Cumulative drawdown tiers (% vs peak equity) ──
# [2026-05-17 100-pt scale ×10] conviction_penalty old -3/-2/-1 → -30/-20/-10
_CUMUL_TIERS = [
    # (threshold_pct, level, label, conviction_penalty)
    (20.0, 3, "CRISIS",  -30),
    (10.0, 2, "DEFEND",  -20),
    (5.0,  1, "CAUTION", -10),
    (0.0,  0, "NORMAL",    0),
]

# ── Daily drawdown tiers (absolute amount vs today's peak PnL) ──
# [2026-05-17 100-pt scale ×10]
_DAILY_TIERS = [
    # (threshold_usd, level, label, conviction_penalty)
    (100.0, 3, "CRISIS",  -30),
    (60.0,  2, "DEFEND",  -20),
    (30.0,  1, "CAUTION", -10),
    (0.0,   0, "NORMAL",    0),
]

# ── 💰 [2026-06-12 owner urgent] capital inflow/outflow detection threshold ──
#   If equity change between one tick exceeds this ratio (12%), treat it as a deposit/withdrawal
#   rather than trading PnL → readjust peak.
#   Rationale: position = a fraction of capital (slot 20%) · SL a few % → single-tick trading PnL
#   stays within a few % of capital. 12%+ = deposit/withdrawal.
_CAPITAL_FLOW_STEP_PCT = 0.12

# ── Display message templates ──
_MESSAGES = {
    0: "Normal -- no drawdown",
    1: "Caution -- {pct:.1f}% below peak (${amt:.1f})",
    2: "Defend -- {pct:.1f}% below peak, hold off on new entries",
    3: "Crisis -- {pct:.1f}% below peak, entries advised to stop",
}


class DrawdownShield:
    """Drawdown defense system based on equity high watermark."""

    def __init__(self):
        self._state = self._load_state()
        logger.info(
            "[DrawdownShield] init — peak_equity=$%.2f, current=$%.2f, dd=%.2f%% (max=%.2f%%)",
            self._state.get("peak_equity", 0.0),
            self._state.get("current_equity", 0.0),
            self._state.get("drawdown_pct", 0.0),
            self._state.get("max_drawdown_pct", 0.0),
        )

    # ================================================================
    #  Public API
    # ================================================================

    def update(self, current_equity: float, realized_pnl_today: float) -> Dict[str, Any]:
        """Called every tick (or periodically) — refresh watermark + return current protection state.

        Args:
            current_equity: current account equity (USDT) — includes cash + unrealized PnL, always > 0
            realized_pnl_today: today's (07:00 KST~) realized PnL — used for daily drawdown calc

        Warmup:
            If current_equity is 0 or below, treat reconcile as incomplete and skip the update.
        """
        s = self._state
        now = time.time()

        # ── Warmup guard: skip while reconcile is incomplete ──
        try:
            eq = float(current_equity)
        except (TypeError, ValueError):
            eq = 0.0
        if eq <= 0.0:
            logger.debug("[DrawdownShield] warmup — equity=%.2f invalid, skipping update", eq)
            return self.get_protection_level()

        # ── 💰 [2026-06-12 owner urgent] detect capital inflow/outflow (deposit/withdrawal) → readjust watermark ──
        #   Bug: a withdrawal drops equity but peak stays put → fake DD → CRISIS → bot stalls. Deposits/withdrawals aren't PnL.
        #   A large equity jump (≥12%) between one tick is impossible from trading PnL (position = a fraction of capital · SL a few %),
        #   so treat it as a deposit/withdrawal and shift peak by Δ as well (withdrawal=peak↓/deposit=peak↑). Does not fire on gradual trading moves.
        _last_eq = s.get("last_equity_for_flow", 0.0)
        if _last_eq > 0 and s["peak_equity"] > 0:
            _step = eq - _last_eq
            if abs(_step) >= _last_eq * _CAPITAL_FLOW_STEP_PCT:
                _old_peak = s["peak_equity"]
                # Withdrawal: peak+step(=peak−withdrawn). Deposit: peak+step(upward). But clamp to eq if peak<eq (DD 0).
                s["peak_equity"] = round(max(eq, s["peak_equity"] + _step), 4)
                logger.warning(
                    "[DrawdownShield] 💰 capital flow detected ($%.2f→$%.2f Δ$%+.2f) → peak $%.2f→$%.2f readjusted "
                    "(deposit/withdrawal=not PnL, prevents fake DD)",
                    _last_eq, eq, _step, _old_peak, s["peak_equity"],
                )
        s["last_equity_for_flow"] = round(eq, 4)

        # ── Update cumulative equity watermark ──
        if eq > s["peak_equity"]:
            s["peak_equity"] = round(eq, 4)
            s["peak_equity_ts"] = now

        s["current_equity"] = round(eq, 4)

        # Cumulative drawdown calc — equity-based, so naturally 0~100%
        dd_amt = max(s["peak_equity"] - eq, 0.0)
        dd_pct = (dd_amt / s["peak_equity"] * 100.0) if s["peak_equity"] > 0 else 0.0
        # Safety: cap on numerical anomaly (with peak > 0 it's theoretically 0~100, but defensive)
        if dd_pct > 100.0:
            dd_pct = 100.0

        s["drawdown_amount"] = round(dd_amt, 4)
        s["drawdown_pct"] = round(dd_pct, 2)

        # Update worst-case records
        if dd_amt > s["max_drawdown_amount"]:
            s["max_drawdown_amount"] = round(dd_amt, 4)
        if dd_pct > s["max_drawdown_pct"]:
            s["max_drawdown_pct"] = round(dd_pct, 2)

        # ── Update daily watermark (realized PnL amount basis — kept as-is) ──
        try:
            rp = float(realized_pnl_today)
        except (TypeError, ValueError):
            rp = 0.0
        if rp > s["daily_peak_pnl"]:
            s["daily_peak_pnl"] = round(rp, 4)

        s["daily_current_pnl"] = round(rp, 4)
        s["daily_drawdown"] = round(max(s["daily_peak_pnl"] - rp, 0.0), 4)

        s["last_update_ts"] = now

        # Persist state
        self._save_state()

        return self.get_protection_level()

    def get_protection_level(self) -> Dict[str, Any]:
        """Return the protection level based on current drawdown depth.

        Adopts the worse of the cumulative level and the daily level.
        """
        s = self._state

        # Cumulative level (equity basis)
        cumul_level, cumul_label, cumul_penalty = self._resolve_cumul_tier(s["drawdown_pct"])

        # Daily level (realized PnL amount basis)
        daily_level, daily_label, daily_penalty = self._resolve_daily_tier(s["daily_drawdown"])

        # Adopt the worse one
        if daily_level > cumul_level:
            level = daily_level
            label = daily_label
            penalty = daily_penalty
            source = "daily"
        else:
            level = cumul_level
            label = cumul_label
            penalty = cumul_penalty
            source = "cumulative"

        dd_pct = s["drawdown_pct"]
        dd_amt = s["drawdown_amount"]
        msg = _MESSAGES.get(level, "").format(pct=dd_pct, amt=dd_amt)

        return {
            "protection_level": level,
            "protection_label": label,
            "conviction_penalty": penalty,
            "message": msg,
            "source": source,
            "cumulative": {"level": cumul_level, "label": cumul_label, "penalty": cumul_penalty},
            "daily": {"level": daily_level, "label": daily_label, "penalty": daily_penalty},
        }

    def get_status(self) -> Dict[str, Any]:
        """Full state for API/dashboard display."""
        s = self._state
        prot = self.get_protection_level()

        return {
            "enabled": True,
            # New equity-based keys
            "peak_equity": s["peak_equity"],
            "current_equity": s["current_equity"],
            "drawdown_amount": s["drawdown_amount"],
            "drawdown_pct": s["drawdown_pct"],
            "max_drawdown_amount": s["max_drawdown_amount"],
            "max_drawdown_pct": s["max_drawdown_pct"],
            "peak_equity_ts": s.get("peak_equity_ts", 0.0),
            # Daily (kept as-is)
            "daily_peak_pnl": s["daily_peak_pnl"],
            "daily_current_pnl": s["daily_current_pnl"],
            "daily_drawdown": s["daily_drawdown"],
            # Protection level
            "protection_level": prot["protection_level"],
            "protection_label": prot["protection_label"],
            "conviction_penalty": prot["conviction_penalty"],
            "message": prot["message"],
            "last_update_ts": s["last_update_ts"],
        }

    def reset_daily(self) -> None:
        """Called on 07:00 KST reset — archive the previous day's daily drawdown record, then reset."""
        s = self._state

        # Append previous day's record to history
        today_label = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        record = {
            "date": today_label,
            "daily_peak_pnl": s["daily_peak_pnl"],
            "daily_current_pnl": s["daily_current_pnl"],
            "daily_drawdown": s["daily_drawdown"],
            "peak_equity": s["peak_equity"],
            "current_equity": s["current_equity"],
            "drawdown_pct": s["drawdown_pct"],
            "max_drawdown_pct": s["max_drawdown_pct"],
            "ts": time.time(),
        }
        s.setdefault("history", []).append(record)

        # Keep 90 days of history
        if len(s["history"]) > 90:
            s["history"] = s["history"][-90:]

        logger.info(
            "[DrawdownShield] daily reset — prev day peak=$%.2f, dd=$%.2f",
            s["daily_peak_pnl"], s["daily_drawdown"],
        )

        # Reset daily figures
        s["daily_peak_pnl"] = 0.0
        s["daily_current_pnl"] = 0.0
        s["daily_drawdown"] = 0.0

        self._save_state()

    def reset_cumulative(self) -> None:
        """Admin use: reset the cumulative watermark.

        Call manually after a long engine stop/restart or on capital changes (deposit/withdrawal).
        Also resets max_drawdown_pct/amount for a clean fresh start.
        """
        s = self._state
        old_peak = s["peak_equity"]
        old_dd = s["drawdown_pct"]
        s["peak_equity"] = 0.0
        s["current_equity"] = 0.0
        s["drawdown_amount"] = 0.0
        s["drawdown_pct"] = 0.0
        s["max_drawdown_amount"] = 0.0
        s["max_drawdown_pct"] = 0.0
        s["peak_equity_ts"] = 0.0
        logger.info(
            "[DrawdownShield] cumulative reset — prev peak=$%.2f, dd=%.2f%% → reset to 0",
            old_peak, old_dd,
        )
        self._save_state()

    def get_history(self) -> List[Dict]:
        """Return daily drawdown history (newest first)."""
        history = list(self._state.get("history", []))
        history.reverse()
        return history

    # ================================================================
    #  Persistence
    # ================================================================

    def _save_state(self) -> None:
        """Save state to runtime/drawdown_shield.json."""
        try:
            safe_write_json(STATE_PATH, self._state)
        except Exception as exc:
            logger.error("[DrawdownShield] state save failed: %s", exc)

    def _load_state(self) -> Dict[str, Any]:
        """Load state from file. Use defaults if missing or corrupted.

        [2026-04-18] Auto-migrate on detecting old version (peak_equity_pnl based):
          - Discard the old PnL-based metrics (different meaning)
          - Start new peak_equity at 0.0 → on first update() the current equity becomes peak
          - Preserve history
        """
        defaults = {
            # New equity-based keys
            "peak_equity": 0.0,
            "current_equity": 0.0,
            "drawdown_amount": 0.0,
            "drawdown_pct": 0.0,
            "max_drawdown_amount": 0.0,
            "max_drawdown_pct": 0.0,
            "peak_equity_ts": 0.0,
            # Daily (kept as-is)
            "daily_peak_pnl": 0.0,
            "daily_current_pnl": 0.0,
            "daily_drawdown": 0.0,
            # Meta
            "last_update_ts": 0.0,
            "history": [],
        }

        loaded = safe_load_json(STATE_PATH, default=None)
        if loaded is None or not isinstance(loaded, dict):
            logger.info("[DrawdownShield] state file missing/corrupted — initializing with defaults")
            return defaults

        # ── Migration: on detecting old version key (peak_equity_pnl) ──
        if "peak_equity_pnl" in loaded and "peak_equity" not in loaded:
            logger.warning(
                "[DrawdownShield] old version state file detected (peak_equity_pnl=$%.2f, dd=%.2f%%) "
                "— migrating PnL-based → Equity-based refactor. Resetting cumulative metrics.",
                float(loaded.get("peak_equity_pnl", 0.0)),
                float(loaded.get("drawdown_pct", 0.0)),
            )
            # Discard all old PnL-based cumulative metrics, initialize with new schema
            migrated = dict(defaults)
            # Carry over daily + history
            for k in ("daily_peak_pnl", "daily_current_pnl", "daily_drawdown",
                      "last_update_ts", "history"):
                if k in loaded:
                    migrated[k] = loaded[k]
            return self._coerce_types(migrated, defaults)

        # Fill in missing fields (version-up handling)
        for key, val in defaults.items():
            if key not in loaded:
                loaded[key] = val

        return self._coerce_types(loaded, defaults)

    @staticmethod
    def _coerce_types(data: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
        """Type coercion — defensive in case strings were stored."""
        for key in defaults:
            if key == "history":
                if not isinstance(data.get(key), list):
                    data[key] = []
                else:
                    # ★ Phase H (2026-04-20 this agent letter#3 A-10): recursive dict type check inside history
                    _clean_history = []
                    for _h in data[key]:
                        if isinstance(_h, dict):
                            _clean_history.append(_h)
                    data[key] = _clean_history
            else:
                try:
                    data[key] = float(data.get(key, defaults[key]))
                except (TypeError, ValueError):
                    data[key] = defaults[key]
        return data

    # ================================================================
    #  Internal
    # ================================================================

    def set_tiers(self, cumul=None, daily=None):
        """[2026-06-09 owner] focus_manager injects tiers from config values (room to tune).
        Overrides the hardcoded _CUMUL_TIERS/_DAILY_TIERS with runtime config (None keeps existing).
        Each tier = (threshold, level, label, penalty) list, threshold in descending order."""
        if cumul:
            self._cumul_tiers = list(cumul)
        if daily:
            self._daily_tiers = list(daily)

    def _resolve_cumul_tier(self, dd_pct: float):
        """Return (level, label, penalty) for cumulative drawdown % (config-injected tiers take priority)."""
        for threshold, level, label, penalty in getattr(self, "_cumul_tiers", _CUMUL_TIERS):
            if dd_pct >= threshold:
                return level, label, penalty
        return 0, "NORMAL", 0

    def _resolve_daily_tier(self, dd_usd: float):
        """Return (level, label, penalty) for daily drawdown $ (config-injected tiers take priority)."""
        for threshold, level, label, penalty in getattr(self, "_daily_tiers", _DAILY_TIERS):
            if dd_usd >= threshold:
                return level, label, penalty
        return 0, "NORMAL", 0


# ── Singleton ──
drawdown_shield = DrawdownShield()
