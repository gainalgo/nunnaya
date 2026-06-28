# ============================================================
# File: app/manager/autopilot_tracker.py
# Autocoin OS v3-H — Autopilot Performance Feedback Loop
# ------------------------------------------------------------
# Tracks autopilot promotion/demotion decisions and correlates
# them with trade outcomes (FILL_SELL) for performance analysis.
# ============================================================

from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from app.manager.ledger_pnl import aggregate_fill_pnl


DEFAULT_DECISIONS_PATH = os.getenv(
    "OMA_AUTOPILOT_DECISIONS_PATH",
    "data/autopilot_decisions.jsonl",
)
DEFAULT_LEDGER_PATH = os.getenv(
    "OMA_LEDGER_PATH",
    "runtime/trade_ledger.jsonl",
)


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        logger.warning("[AutopilotTracker] _f() conversion failed for %r", x, exc_info=True)
        return default


def _s(x: Any, default: str = "") -> str:
    try:
        if x is None:
            return default
        return str(x)
    except (AttributeError, TypeError, ValueError):
        logger.warning("[AutopilotTracker] _s() conversion failed for %r", x, exc_info=True)
        return default


class AutopilotTracker:
    """Tracks autopilot decisions and correlates with trade outcomes."""

    def __init__(
        self,
        *,
        decisions_path: Optional[str] = None,
        ledger_path: Optional[str] = None,
    ) -> None:
        self._decisions_path = str(decisions_path or DEFAULT_DECISIONS_PATH)
        self._ledger_path = str(ledger_path or DEFAULT_LEDGER_PATH)
        d = os.path.dirname(self._decisions_path)
        if d:
            os.makedirs(d, exist_ok=True)

    def record_decision(
        self,
        market: str,
        from_state: str,
        to_state: str,
        strategy: str,
        reason: str,
    ) -> None:
        """Log an autopilot decision (promotion or demotion)."""
        market = _s(market, "").strip().upper()
        if not market:
            return
        rec = {
            "ts": time.time(),
            "market": market,
            "from_state": _s(from_state, ""),
            "to_state": _s(to_state, ""),
            "strategy": _s(strategy, "").strip().upper() or "UNKNOWN",
            "reason": _s(reason, ""),
        }
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        try:
            with open(self._decisions_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[AP_TRACKER] record_decision write: %s", exc, exc_info=True)

    def get_decision_history(
        self,
        hours: float = 168.0,
    ) -> List[Dict[str, Any]]:
        """Return recent decisions within the last `hours` hours."""
        if not os.path.exists(self._decisions_path):
            return []
        since_ts = time.time() - (hours * 3600.0)
        out: List[Dict[str, Any]] = []
        try:
            with open(self._decisions_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = _f(rec.get("ts"), 0.0)
                        if ts >= since_ts:
                            out.append(rec)
                    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[AP_TRACKER] decision history line parse: %s", exc, exc_info=True)
                        continue
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[AP_TRACKER] decision history read: %s", exc, exc_info=True)
        return out

    def get_strategy_promotion_stats(
        self,
        hours: float = 168.0,
        pnl_window_hours: float = 168.0,
        ledger_path: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """For each strategy, return promotion stats: total, successful, failed, avg_pnl.

        - total_promotions: count of promotions (to_state=ACTIVE)
        - successful: promotions that led to net profit (FILL_SELL PnL > 0)
        - failed: promotions that led to net loss (PnL < 0)
        - avg_pnl_after_promotion: average net_cash_usdt from FILL events after promotion
        """
        decisions = self.get_decision_history(hours=hours)
        now = time.time()
        since_global = now - (hours * 3600.0)

        # Collect promotions: (ts, market, strategy)
        promotions: List[Dict[str, Any]] = []
        for rec in decisions:
            to_state = _s(rec.get("to_state"), "").upper()
            if to_state != "ACTIVE":
                continue
            promotions.append({
                "ts": _f(rec.get("ts"), 0.0),
                "market": _s(rec.get("market"), "").strip().upper(),
                "strategy": _s(rec.get("strategy"), "").strip().upper() or "UNKNOWN",
            })

        # Load ledger for PnL (use TradeLedger.tail_records when available)
        ledger_path = ledger_path or self._ledger_path
        records: List[Dict[str, Any]] = []
        try:
            from app.manager.trade_ledger import TradeLedger
            ledger = TradeLedger(path=ledger_path)
            records = ledger.tail_records(since_ts=since_global, tail_lines=50000)
        except (ImportError, AttributeError, TypeError):
            logger.warning("[AutopilotTracker] TradeLedger import/load failed, falling back to raw read", exc_info=True)
            if os.path.exists(ledger_path):
                try:
                    with open(ledger_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                                ts = _f(rec.get("ts"), 0.0)
                                if ts >= since_global:
                                    records.append(rec)
                            except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[AP_TRACKER] ledger line parse: %s", exc, exc_info=True)
                                continue
                except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[AP_TRACKER] ledger file read: %s", exc, exc_info=True)

        # Per-strategy stats
        stats: Dict[str, Dict[str, Any]] = {}

        for prom in promotions:
            strat = prom["strategy"]
            if strat not in stats:
                stats[strat] = {
                    "total_promotions": 0,
                    "successful": 0,
                    "failed": 0,
                    "unknown": 0,
                    "pnl_sum": 0.0,
                    "pnl_count": 0,
                }

            stats[strat]["total_promotions"] += 1

            prom_ts = prom["ts"]
            market = prom["market"]
            until_ts = min(now, prom_ts + (pnl_window_hours * 3600.0))

            # Aggregate FILL events for this market in [prom_ts, until_ts]
            market_records = [
                r for r in records
                if _s(r.get("market") or (r.get("data") or {}).get("market")).strip().upper() == market
                and _f(r.get("ts"), 0.0) >= prom_ts
                and _f(r.get("ts"), 0.0) <= until_ts
            ]

            market_agg = aggregate_fill_pnl(
                market_records,
                since_ts=prom_ts,
                until_ts=until_ts,
                markets=[market],
            )
            agg = market_agg.get(market)
            net_cash = agg.net_cash_usdt if agg else 0.0

            stats[strat]["pnl_sum"] += net_cash
            stats[strat]["pnl_count"] += 1

            if net_cash > 0:
                stats[strat]["successful"] += 1
            elif net_cash < 0:
                stats[strat]["failed"] += 1
            else:
                stats[strat]["unknown"] += 1

        # Build final output
        result: Dict[str, Dict[str, Any]] = {}
        for strat, s in stats.items():
            cnt = s["pnl_count"]
            avg_pnl = s["pnl_sum"] / cnt if cnt > 0 else 0.0
            result[strat] = {
                "total_promotions": s["total_promotions"],
                "successful": s["successful"],
                "failed": s["failed"],
                "unknown": s["unknown"],
                "avg_pnl_after_promotion": round(avg_pnl, 2),
            }
        return result


# Singleton for use across the app
autopilot_tracker = AutopilotTracker()
