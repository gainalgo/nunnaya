"""Capital Tracker — deposit/withdraw tracking + pure trading performance.

Module for measuring pure trading skill independent of deposits/withdrawals.
Corrects for capital changes using the TWR (Time-Weighted Return) method.

Usage:
  - Deposit: POST /api/strategy/focus/capital/deposit {amount: 500}
  - Withdraw: POST /api/strategy/focus/capital/withdraw {amount: 200}
  - Performance: GET /api/strategy/focus/capital/performance

Files:
  - runtime/capital_events.jsonl — deposit/withdraw event log
  - runtime/capital_baseline.json — initial capital + current baseline
"""
import json
import os
import time
import logging
from typing import Dict, Any, List, Optional

from app.core.io_utils import safe_write_json

logger = logging.getLogger(__name__)

EVENTS_PATH = os.path.join("runtime", "capital_events.jsonl")
BASELINE_PATH = os.path.join("runtime", "capital_baseline.json")


class CapitalTracker:
    """Deposit/withdraw tracking + pure trading ROI calculation."""

    def __init__(self, events_path: str = None, baseline_path: str = None):
        # ★ [2026-06-23] path-aware — isolate capital tracking per exchange (default=global Bybit, no behavior change).
        self._events_path = events_path or EVENTS_PATH
        self._baseline_path = baseline_path or BASELINE_PATH
        self._baseline = self._load_baseline()

    def _load_baseline(self) -> Dict:
        """Load the baseline. Returns an empty state if none exists."""
        if os.path.exists(self._baseline_path):
            try:
                with open(self._baseline_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "initial_capital": 0.0,     # set on first deposit
            "total_deposited": 0.0,     # cumulative deposits
            "total_withdrawn": 0.0,     # cumulative withdrawals
            "net_invested": 0.0,        # net invested (deposits - withdrawals)
            "created_ts": 0.0,
        }

    def _save_baseline(self):
        safe_write_json(self._baseline_path, self._baseline)

    def _append_event(self, event: Dict):
        """Append an event to the JSONL log."""
        os.makedirs(os.path.dirname(self._events_path), exist_ok=True)
        with open(self._events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def get_events(self) -> List[Dict]:
        """Return all capital events."""
        events = []
        if not os.path.exists(self._events_path):
            return events
        try:
            with open(self._events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return events

    # ── Deposit / Withdraw ──

    def deposit(self, amount: float, memo: str = "") -> Dict:
        """Record a deposit."""
        if amount <= 0:
            return {"ok": False, "error": "Amount must be positive"}

        event = {
            "type": "DEPOSIT",
            "amount": round(amount, 2),
            "memo": memo,
            "ts": time.time(),
        }
        self._append_event(event)

        if self._baseline["initial_capital"] == 0:
            self._baseline["initial_capital"] = amount
            self._baseline["created_ts"] = time.time()

        self._baseline["total_deposited"] = round(
            self._baseline["total_deposited"] + amount, 2
        )
        self._baseline["net_invested"] = round(
            self._baseline["total_deposited"] - self._baseline["total_withdrawn"], 2
        )
        self._save_baseline()

        logger.info("[Capital] DEPOSIT $%.2f (memo: %s) → net_invested=$%.2f",
                     amount, memo, self._baseline["net_invested"])
        return {"ok": True, "event": event, "baseline": self._baseline}

    def withdraw(self, amount: float, memo: str = "") -> Dict:
        """Record a withdrawal."""
        if amount <= 0:
            return {"ok": False, "error": "Amount must be positive"}

        event = {
            "type": "WITHDRAW",
            "amount": round(amount, 2),
            "memo": memo,
            "ts": time.time(),
        }
        self._append_event(event)

        self._baseline["total_withdrawn"] = round(
            self._baseline["total_withdrawn"] + amount, 2
        )
        self._baseline["net_invested"] = round(
            self._baseline["total_deposited"] - self._baseline["total_withdrawn"], 2
        )
        self._save_baseline()

        logger.info("[Capital] WITHDRAW $%.2f (memo: %s) → net_invested=$%.2f",
                     amount, memo, self._baseline["net_invested"])
        return {"ok": True, "event": event, "baseline": self._baseline}

    def set_initial(self, amount: float) -> Dict:
        """Set the initial capital (on first start)."""
        self._baseline["initial_capital"] = round(amount, 2)
        if self._baseline["total_deposited"] == 0:
            self._baseline["total_deposited"] = round(amount, 2)
            self._baseline["net_invested"] = round(amount, 2)
        if self._baseline["created_ts"] == 0:
            self._baseline["created_ts"] = time.time()
        self._save_baseline()

        event = {
            "type": "INITIAL",
            "amount": round(amount, 2),
            "memo": "Initial capital set",
            "ts": time.time(),
        }
        self._append_event(event)

        logger.info("[Capital] Initial capital set to $%.2f", amount)
        return {"ok": True, "baseline": self._baseline}

    # ── Performance calculation ──

    def get_performance(self, current_equity: float, trading_pnl: float) -> Dict[str, Any]:
        """Calculate pure trading performance.

        Args:
            current_equity: current Bybit wallet balance (USDT)
            trading_pnl: total realized PnL computed from the journal

        Returns:
            pure performance metrics
        """
        net_invested = self._baseline.get("net_invested", 0)
        initial = self._baseline.get("initial_capital", 0)

        # 1. Absolute profit = current balance - net invested
        absolute_profit = round(current_equity - net_invested, 2) if net_invested > 0 else 0

        # 2. Pure trading ROI = realized PnL / net invested × 100
        #    Independent of deposits/withdrawals — uses only the sum of journal pnl_net
        trading_roi_pct = round(trading_pnl / net_invested * 100, 2) if net_invested > 0 else 0

        # 3. Total ROI = (current balance - net invested) / net invested × 100
        total_roi_pct = round(absolute_profit / net_invested * 100, 2) if net_invested > 0 else 0

        # 4. Compute TWR from daily snapshots
        daily_snapshots = self._get_daily_roi_series()

        # 5. TWR (Time-Weighted Return) — corrects for deposits/withdrawals
        twr = 1.0
        for d in daily_snapshots:
            twr *= (1 + d.get("roi_pct", 0) / 100)
        twr_pct = round((twr - 1) * 100, 2)

        # 6. Operating period
        created = self._baseline.get("created_ts", 0)
        days_active = round((time.time() - created) / 86400, 1) if created > 0 else 0

        # 7. Average daily ROI
        daily_avg_roi = round(twr_pct / max(days_active, 1), 2)

        return {
            "baseline": self._baseline,
            "current_equity": round(current_equity, 2),
            "net_invested": net_invested,
            # absolute figures
            "absolute_profit": absolute_profit,
            "trading_pnl": round(trading_pnl, 2),
            # ROI
            "trading_roi_pct": trading_roi_pct,
            "total_roi_pct": total_roi_pct,
            "twr_pct": twr_pct,
            # daily stats
            "days_active": days_active,
            "daily_avg_roi_pct": daily_avg_roi,
            "daily_snapshots_count": len(daily_snapshots),
        }

    def _get_daily_roi_series(self) -> List[Dict]:
        """Extract the ROI% series from daily snapshots."""
        from app.manager.focus_daily_snapshot import get_all_snapshots
        snapshots = get_all_snapshots()
        series = []
        for snap in snapshots:
            # If the snapshot has equity_start, compute a precise ROI
            equity_start = snap.get("equity_start", 0)
            pnl = snap.get("total_pnl", 0)
            if equity_start > 0:
                roi = pnl / equity_start * 100
            elif self._baseline.get("net_invested", 0) > 0:
                roi = pnl / self._baseline["net_invested"] * 100
            else:
                roi = 0
            series.append({
                "date": snap.get("date", ""),
                "pnl": round(pnl, 2),
                "roi_pct": round(roi, 2),
            })
        return series

    def get_status(self) -> Dict:
        """Current capital tracking state."""
        return {
            "baseline": self._baseline,
            "events_count": len(self.get_events()),
        }


# Singleton (global Bybit/Harpoon — no behavior change)
capital_tracker = CapitalTracker()

# Per-path registry (isolate capital tracking per exchange). get_capital_tracker(None)=global singleton.
import threading as _threading
_TRACKERS = {None: capital_tracker}
_TRACKERS_LOCK = _threading.Lock()


def get_capital_tracker(events_path: str = None, baseline_path: str = None) -> "CapitalTracker":
    key = baseline_path or None
    t = _TRACKERS.get(key)
    if t is None:
        with _TRACKERS_LOCK:
            t = _TRACKERS.get(key)
            if t is None:
                t = CapitalTracker(events_path=events_path, baseline_path=baseline_path)
                _TRACKERS[key] = t
    return t
