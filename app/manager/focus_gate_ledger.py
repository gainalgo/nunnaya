"""GateLedger — entry-gate pass/reject tally ledger (solution B, master blueprint §9.2-B / F3).

The owner's North Star "bot = sets the table, human = harvests" (INV-6) hinges on
*candidate visibility*, but right now only "what was set out (near-miss)" is visible
and "why it couldn't be set out (gate bottleneck)" is not. This ledger fills that gap —
it *only counts* the reason an entry gate throws on reject, as a coin×gate matrix
(zero new judgments). It takes the reason already produced by _record_skip /
_record_near_miss, accumulates it in an in-memory counter, and periodically persists
to runtime/focus_gate_stats.json.

★ It does not touch a single byte of entry logic (observation only). Every exception in
  record() is swallowed so it never propagates into the entry flow (INV-2, unbroken as
  before). default OFF — enabled only via FOCUS_GATE_LEDGER_ENABLED.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from typing import Dict


def _atomic_write_json(path: str, data: dict) -> None:
    """Write to tmp then swap via os.replace = atomic (same pattern as app.core.io_utils.safe_write_json).
    ★ Self-contained — does not depend on the heavy app.core import chain (fastapi etc.) so an observation tool can't break the core."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# Defensive caps — the code's reason/coin sets are finite, but cap cardinality to guard against anomalous input
_MAX_GATES = 300
_MAX_MARKETS_PER_GATE = 50


class GateLedger:
    """Per-tick in-memory counter + periodic flush. Since record() has a lazy flush built in,
    the caller only needs a single record() line at the wiring point (no separate tick hook needed)."""

    def __init__(self, flush_path: str = "runtime/focus_gate_stats.json",
                 flush_sec: float = 60.0) -> None:
        self.flush_path = flush_path
        self.flush_sec = max(5.0, float(flush_sec or 60.0))
        self._date: str = self._today()
        # {gate: {"pass": int, "reject": int, "markets": Counter}}
        self._gates: Dict[str, Dict] = {}
        self._total_scanned: int = 0
        self._last_flush: float = 0.0

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _roll_if_new_day(self) -> None:
        today = self._today()
        if today != self._date:
            self._date = today
            self._gates = {}
            self._total_scanned = 0

    def record(self, market: str, gate: str, passed: bool, direction: str = "-",
               detail: str = "") -> None:
        """Tally one entry-gate pass/reject. Call as a single line at the existing reject/entry point.
        Exceptions must never propagate — observation must not break the entry flow."""
        try:
            self._roll_if_new_day()
            g = str(gate or "-").strip()[:80] or "-"
            mkt = str(market or "-").strip().upper()[:20] or "-"
            slot = self._gates.get(g)
            if slot is None:
                if len(self._gates) >= _MAX_GATES:
                    return  # cardinality-blowup guard — ignore new gates (existing tallies continue)
                slot = {"pass": 0, "reject": 0, "markets": Counter()}
                self._gates[g] = slot
            if passed:
                slot["pass"] += 1
            else:
                slot["reject"] += 1
            mc: Counter = slot["markets"]
            if mkt in mc or len(mc) < _MAX_MARKETS_PER_GATE:
                mc[mkt] += 1
            self._total_scanned += 1
            self.maybe_flush(time.time())
        except Exception:  # noqa: BLE001 — observation failure is harmless, entry flow unbroken
            pass

    def snapshot(self) -> dict:
        """For peer_brief / UI exposure. {"date", "gates": {<gate>: {pass, reject, top_markets}}, "total_scanned"}."""
        try:
            gates_out: Dict[str, Dict] = {}
            for g, slot in self._gates.items():
                mc: Counter = slot["markets"]
                gates_out[g] = {
                    "pass": int(slot["pass"]),
                    "reject": int(slot["reject"]),
                    "top_markets": [m for m, _ in mc.most_common(5)],
                }
            return {"date": self._date, "gates": gates_out, "total_scanned": int(self._total_scanned)}
        except Exception:  # noqa: BLE001
            return {"date": self._date, "gates": {}, "total_scanned": 0}

    def maybe_flush(self, now: float) -> None:
        """Persist via atomic write once flush_sec has elapsed. Harmless even on failure."""
        try:
            if (now - self._last_flush) < self.flush_sec:
                return
            self._last_flush = now
            _atomic_write_json(self.flush_path, self.snapshot())
        except Exception:  # noqa: BLE001
            pass
