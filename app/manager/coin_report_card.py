# ============================================================
# Coin Report Card — per-coin trading report card
# ------------------------------------------------------------
# Analyzes EXIT records from the journal (focus_harpoon_journal.jsonl)
# to assign a per-coin grade (S/A/B/C/D/F) and compute a score.
#
# Scanner can reference priority via get_coin_scores().
#
# Usage:
#   from app.manager.coin_report_card import coin_report_card
#   coin_report_card.refresh()
#   grade = coin_report_card.get_coin_grade("ETHUSDT")
#   scores = coin_report_card.get_coin_scores()
# ============================================================
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from app.core.io_utils import safe_write_json

logger = logging.getLogger(__name__)

JOURNAL_PATH = os.path.join("runtime", "focus_harpoon_journal.jsonl")
REPORT_PATH = os.path.join("runtime", "coin_report_card.json")

# ── Grade thresholds ──
_GRADE_TABLE: List[Tuple[float, str, str]] = [
    (85.0, "S", "Ace \U0001f3c6"),
    (70.0, "A", "Excellent \u2b50"),
    (55.0, "B", "Good \U0001f44d"),
    (40.0, "C", "Average \U0001f610"),
    (25.0, "D", "Poor \U0001f44e"),
    (0.0,  "F", "Risky \U0001f6ab"),
]

# ── Score weights ──
W_WINRATE = 0.30
W_PNL = 0.30
W_CONSISTENCY = 0.20
W_PF = 0.10
W_VOLUME = 0.10


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Guard against missing/wrong-typed journal fields."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _grade_from_score(score: float) -> Tuple[str, str]:
    """Convert score -> (grade, label)."""
    for threshold, grade, label in _GRADE_TABLE:
        if score >= threshold:
            return grade, label
    return "F", "Risky \U0001f6ab"


class CoinReportCard:
    """Per-coin trading report card, based on journal EXIT records."""

    def __init__(
        self,
        journal_path: str = JOURNAL_PATH,
        lookback_days: int = 7,
    ):
        self._journal_path = journal_path
        self._lookback_days = lookback_days
        self._lock = threading.Lock()
        self._report: Optional[Dict[str, Any]] = None

    # ──────────────────────────────────────────────
    # Journal reading
    # ──────────────────────────────────────────────

    def _read_exits(self) -> List[Dict[str, Any]]:
        """Extract only EXIT records within the lookback window."""
        cutoff = time.time() - self._lookback_days * 86400
        exits: List[Dict[str, Any]] = []

        if not os.path.exists(self._journal_path):
            logger.warning("[REPORT_CARD] journal file not found: %s", self._journal_path)
            return exits

        try:
            with open(self._journal_path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("[REPORT_CARD] parse failed line %d", lineno)
                        continue

                    if rec.get("event") != "EXIT":
                        continue

                    ts = _safe_float(rec.get("ts"))
                    if ts < cutoff:
                        continue

                    exits.append(rec)
        except OSError as exc:
            logger.error("[REPORT_CARD] journal read failed: %s", exc)

        return exits

    # ──────────────────────────────────────────────
    # Per-coin stats computation
    # ──────────────────────────────────────────────

    @staticmethod
    def _calc_coin_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """A single coin's EXIT trade list -> stats dict."""
        count = len(trades)
        if count == 0:
            return {}

        pnl_list: List[float] = []
        roe_list: List[float] = []
        hold_list: List[float] = []
        fee_total = 0.0
        gross_wins = 0.0
        gross_losses = 0.0
        wins = 0
        losses = 0
        max_win = -float("inf")
        max_loss = float("inf")

        for t in trades:
            pnl = _safe_float(t.get("pnl_net"))
            roe = _safe_float(t.get("roe_pct"))
            hold = _safe_float(t.get("hold_sec"))
            fee = _safe_float(t.get("fee"))

            pnl_list.append(pnl)
            roe_list.append(roe)
            hold_list.append(hold)
            fee_total += fee

            if pnl >= 0:
                wins += 1
                gross_wins += pnl
            else:
                losses += 1
                gross_losses += pnl  # negative

            if pnl > max_win:
                max_win = pnl
            if pnl < max_loss:
                max_loss = pnl

        total_pnl = sum(pnl_list)
        avg_pnl = total_pnl / count
        avg_hold_min = (sum(hold_list) / count) / 60.0
        avg_roe = sum(roe_list) / count
        win_rate = (wins / count) * 100.0

        # profit_factor: gross profit / |gross loss| (use 999.9 instead of inf if no loss)
        if gross_losses < 0:
            profit_factor = gross_wins / abs(gross_losses)
        else:
            profit_factor = 999.9 if gross_wins > 0 else 0.0

        # PnL standard deviation (for consistency calculation)
        if count > 1:
            mean_pnl = avg_pnl
            variance = sum((p - mean_pnl) ** 2 for p in pnl_list) / (count - 1)
            pnl_std = variance ** 0.5
        else:
            pnl_std = 0.0

        # max_win / max_loss edge case: all wins or all losses
        if max_win == -float("inf"):
            max_win = 0.0
        if max_loss == float("inf"):
            max_loss = 0.0

        return {
            "trades": count,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 4),
            "avg_pnl": round(avg_pnl, 4),
            "max_win": round(max_win, 4),
            "max_loss": round(max_loss, 4),
            "avg_hold_min": round(avg_hold_min, 1),
            "avg_roe_pct": round(avg_roe, 2),
            "profit_factor": round(min(profit_factor, 999.9), 2),
            "total_fees": round(fee_total, 4),
            # internal-only (stripped from final output)
            "_pnl_std": pnl_std,
            "_gross_wins": gross_wins,
            "_gross_losses": gross_losses,
        }

    # ──────────────────────────────────────────────
    # Scoring (0-100)
    # ──────────────────────────────────────────────

    @staticmethod
    def _score_coins(coin_stats: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
        """All-coin stats -> per-coin score (0-100). Includes relative-evaluation factors."""
        if not coin_stats:
            return {}

        # overall average PnL across coins (relative comparison baseline)
        all_avg_pnl = []
        all_pnl_std = []
        for stats in coin_stats.values():
            all_avg_pnl.append(stats.get("avg_pnl", 0.0))
            all_pnl_std.append(stats.get("_pnl_std", 0.0))

        global_avg_pnl = sum(all_avg_pnl) / len(all_avg_pnl) if all_avg_pnl else 0.0

        # std normalization basis: max std across all (0 means everyone perfectly consistent)
        max_std = max(all_pnl_std) if all_pnl_std else 1.0
        if max_std == 0:
            max_std = 1.0  # div-by-zero guard

        scores: Dict[str, float] = {}

        for coin, stats in coin_stats.items():
            # 1) win_rate_norm (0-100)
            win_rate_norm = min(stats.get("win_rate", 0.0), 100.0)

            # 2) pnl_norm — avg_pnl relative comparison
            #    ratio vs global_avg_pnl. median 50, good -> 100, bad -> 0
            avg_pnl = stats.get("avg_pnl", 0.0)
            if global_avg_pnl != 0:
                pnl_ratio = avg_pnl / abs(global_avg_pnl)
            elif avg_pnl > 0:
                pnl_ratio = 2.0  # overall average is 0 but this coin is positive
            elif avg_pnl < 0:
                pnl_ratio = -1.0
            else:
                pnl_ratio = 0.0
            # ratio -> 0~100 mapping: ratio 0 -> 50, ratio 2+ -> 100, ratio -2 -> 0
            pnl_norm = max(0.0, min(100.0, 50.0 + pnl_ratio * 25.0))

            # 3) consistency_norm — inverse of PnL std (lower is more consistent)
            pnl_std = stats.get("_pnl_std", 0.0)
            consistency_norm = (1.0 - pnl_std / max_std) * 100.0
            consistency_norm = max(0.0, min(100.0, consistency_norm))

            # 4) profit_factor_norm — PF 1.0 → 50, PF 3.0+ → 100, PF 0 → 0
            pf = stats.get("profit_factor", 0.0)
            pf_capped = min(pf, 5.0)  # treat >=5 identically
            profit_factor_norm = min(100.0, (pf_capped / 5.0) * 100.0)

            # 5) volume_bonus — data reliability based on trade count
            trades = stats.get("trades", 0)
            volume_bonus = min(trades / 10.0, 1.0) * 100.0

            # composite score
            score = (
                win_rate_norm * W_WINRATE
                + pnl_norm * W_PNL
                + consistency_norm * W_CONSISTENCY
                + profit_factor_norm * W_PF
                + volume_bonus * W_VOLUME
            )
            scores[coin] = round(max(0.0, min(100.0, score)), 1)

        return scores

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    def build_report(self) -> Dict[str, Any]:
        """Journal -> full report build. Internal fields are stripped before returning."""
        exits = self._read_exits()

        # group by coin
        by_coin: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for rec in exits:
            market = rec.get("market", "")
            if not market:
                continue
            by_coin[market].append(rec)

        if not by_coin:
            logger.info("[REPORT_CARD] no EXIT trades within lookback %dd", self._lookback_days)
            return {
                "generated_ts": time.time(),
                "lookback_days": self._lookback_days,
                "total_coins": 0,
                "coins": {},
                "rankings": [],
                "grade_distribution": {"S": 0, "A": 0, "B": 0, "C": 0, "D": 0, "F": 0},
            }

        # per-coin stats
        coin_stats: Dict[str, Dict[str, Any]] = {}
        for coin, trades in by_coin.items():
            stats = self._calc_coin_stats(trades)
            if stats:
                coin_stats[coin] = stats

        # scoring
        scores = self._score_coins(coin_stats)

        # assign grades + assemble final coin dict
        coins_out: Dict[str, Dict[str, Any]] = {}
        grade_dist: Dict[str, int] = {"S": 0, "A": 0, "B": 0, "C": 0, "D": 0, "F": 0}

        for coin, stats in coin_stats.items():
            score = scores.get(coin, 0.0)
            grade, grade_label = _grade_from_score(score)
            grade_dist[grade] = grade_dist.get(grade, 0) + 1

            # strip internal-only fields
            clean = {k: v for k, v in stats.items() if not k.startswith("_")}
            clean["grade"] = grade
            clean["grade_label"] = grade_label
            clean["score"] = score
            coins_out[coin] = clean

        # rankings (descending by score)
        sorted_coins = sorted(coins_out.items(), key=lambda x: x[1]["score"], reverse=True)
        rankings = [
            {
                "rank": i + 1,
                "coin": coin,
                "grade": data["grade"],
                "score": data["score"],
                "pnl": data["total_pnl"],
            }
            for i, (coin, data) in enumerate(sorted_coins)
        ]

        report = {
            "generated_ts": time.time(),
            "lookback_days": self._lookback_days,
            "total_coins": len(coins_out),
            "coins": coins_out,
            "rankings": rankings,
            "grade_distribution": grade_dist,
        }

        with self._lock:
            self._report = report

        return report

    def get_coin_grade(self, coin: str) -> str:
        """Return a specific coin's grade. Auto-builds if the report is not yet generated."""
        if self._report is None:
            self.build_report()
        report = self._report or {}
        coin_data = report.get("coins", {}).get(coin)
        if coin_data is None:
            return "F"  # coins with no data get the lowest grade
        return coin_data.get("grade", "F")

    def get_coin_scores(self) -> Dict[str, float]:
        """All-coin score dict. Used for scanner priority."""
        if self._report is None:
            self.build_report()
        report = self._report or {}
        return {
            coin: data.get("score", 0.0)
            for coin, data in report.get("coins", {}).items()
        }

    def get_full_report(self) -> Dict[str, Any]:
        """Return the full report. Auto-builds if not yet generated."""
        if self._report is None:
            self.build_report()
        return self._report or {}

    def refresh(self) -> Dict[str, Any]:
        """Rebuild report + save to file + return."""
        report = self.build_report()

        try:
            safe_write_json(REPORT_PATH, report)
            logger.info(
                "[REPORT_CARD] saved — %d coins, grade distribution: %s",
                report.get("total_coins", 0),
                report.get("grade_distribution", {}),
            )
        except Exception as exc:
            logger.error("[REPORT_CARD] save failed: %s", exc)

        return report


# ── Singleton ──
coin_report_card = CoinReportCard()
