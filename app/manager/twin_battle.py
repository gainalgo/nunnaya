# ============================================================
# Twin Battle — server-to-server performance comparison system
# ------------------------------------------------------------
# A gift from one agent to a sibling session.
# Two servers running the same FOCUS strategy export their
# performance in a standard format and compare the results.
#
# READ-ONLY: never interferes with trading behavior.
#
# Usage:
#   from app.manager.twin_battle import twin_battle
#   snap = twin_battle.export_snapshot()
#   result = twin_battle.compare(other_server_snapshot)
# ============================================================
from __future__ import annotations

import json
import logging
import os
import time
import datetime as _dt
from collections import defaultdict
from typing import Any, Dict, List, Optional

from app.core.io_utils import safe_write_json, safe_load_json

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────
JOURNAL_PATH = os.path.join("runtime", "focus_harpoon_journal.jsonl")
CONFIG_PATH = os.path.join("runtime", "focus_config.json")
TWIN_CONFIG_PATH = os.path.join("runtime", "twin_config.json")
TWIN_SNAPSHOT_DIR = os.path.join("runtime", "twin_snapshots")

# ── 07:00 KST = 22:00 UTC reset boundary ────────────────────
_RESET_HOUR_UTC = 22


def _today_reset_ts() -> float:
    """Return today's 07:00 KST (22:00 UTC) reset timestamp."""
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    boundary = now_utc.replace(hour=_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now_utc.hour < _RESET_HOUR_UTC:
        boundary -= _dt.timedelta(days=1)
    return boundary.timestamp()


def _today_date_label() -> str:
    """Today's trading-day label (KST-based YYYY-MM-DD)."""
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    boundary = now_utc.replace(hour=_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now_utc.hour < _RESET_HOUR_UTC:
        boundary -= _dt.timedelta(days=1)
    kst = boundary + _dt.timedelta(hours=9)
    return kst.strftime("%Y-%m-%d")


# ── Helper: journal reading ──────────────────────────────────

def _read_all_exits() -> List[Dict]:
    """Load all EXIT trades from the journal."""
    exits: List[Dict] = []
    if not os.path.exists(JOURNAL_PATH):
        return exits
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("event") == "EXIT":
                        exits.append(rec)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("[TwinBattle] failed to read journal: %s", exc)
    return exits


def _read_today_exits() -> List[Dict]:
    """Filter only EXIT trades after today's 07:00 KST."""
    reset_ts = _today_reset_ts()
    return [t for t in _read_all_exits() if t.get("ts", 0) >= reset_ts]


def _read_today_blocked() -> List[Dict]:
    """Load today's BLOCKED events."""
    reset_ts = _today_reset_ts()
    blocked: List[Dict] = []
    if not os.path.exists(JOURNAL_PATH):
        return blocked
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("event") == "BLOCKED" and rec.get("ts", 0) >= reset_ts:
                        blocked.append(rec)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("[TwinBattle] failed to read BLOCKED: %s", exc)
    return blocked


def _read_active_positions() -> List[Dict]:
    """Read currently held positions from focus_config.json."""
    positions: List[Dict] = []
    data = safe_load_json(CONFIG_PATH, default={})
    # ★ Phase H (2026-04-20 this agent letter#3 A-11): isinstance guard — type may break when edited by external tools
    if not isinstance(data, dict):
        return positions
    state = data.get("state", {})
    if not isinstance(state, dict):
        return positions

    # multi-position list
    raw_positions = state.get("positions", [])
    if not isinstance(raw_positions, list):
        raw_positions = []
    for p in raw_positions:
        if p.get("market") and p.get("entry_price", 0) > 0:
            positions.append({
                "coin": p.get("market", ""),
                "direction": p.get("direction", ""),
                "entry_price": p.get("entry_price", 0.0),
                "current_pnl_pct": 0.0,  # placeholder, no live price
                "hold_min": round((time.time() - p.get("entry_ts", time.time())) / 60, 1),
                "conviction": p.get("conviction_score", 0),
                "leverage": p.get("leverage", 1),
                "qty": p.get("qty", 0.0),
            })

    # Legacy single-position compatibility
    legacy = state.get("position")
    if legacy and legacy.get("market") and legacy.get("entry_price", 0) > 0:
        already = {p["coin"] for p in positions}
        if legacy["market"] not in already:
            positions.append({
                "coin": legacy.get("market", ""),
                "direction": legacy.get("direction", ""),
                "entry_price": legacy.get("entry_price", 0.0),
                "current_pnl_pct": 0.0,
                "hold_min": round((time.time() - legacy.get("entry_ts", time.time())) / 60, 1),
                "conviction": legacy.get("conviction_score", 0),
                "leverage": legacy.get("leverage", 1),
                "qty": legacy.get("qty", 0.0),
            })

    return positions


def _calc_today_coin_summary(exits: List[Dict]) -> Dict[str, Dict]:
    """Per-coin summary of today's performance."""
    by_coin: Dict[str, Dict] = defaultdict(lambda: {
        "trades": 0, "pnl": 0.0, "wins": 0,
    })
    for t in exits:
        coin = t.get("market", "UNKNOWN")
        pnl = t.get("pnl_net", 0) or 0
        by_coin[coin]["trades"] += 1
        by_coin[coin]["pnl"] = round(by_coin[coin]["pnl"] + pnl, 4)
        if pnl > 0:
            by_coin[coin]["wins"] += 1

    result = {}
    for coin, data in sorted(by_coin.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = round(data["wins"] / data["trades"] * 100, 1) if data["trades"] > 0 else 0.0
        result[coin] = {
            "trades": data["trades"],
            "pnl": data["pnl"],
            "win_rate": wr,
        }
    return result


def _calc_blocked_counts(blocked_entries: List[Dict]) -> Dict[str, int]:
    """Count BLOCKED events by reason."""
    counts: Dict[str, int] = defaultdict(int)
    for entry in blocked_entries:
        reason = entry.get("exit_reason", "unknown")
        # Simplify: drop the detail portion
        key = reason.split(":")[0] if ":" in reason else reason
        counts[key] += 1
    return dict(counts)


def _calc_exposure(positions: List[Dict]) -> float:
    """Compute total exposure (notional value)."""
    total = 0.0
    for p in positions:
        price = p.get("entry_price", 0)
        qty = p.get("qty", 0)
        total += price * qty
    return round(total, 2)


# ── Rivalry message generation ───────────────────────────────

_RIVALRY_MESSAGES = {
    "dominant": [
        "{winner} dominates! {loser}, step it up~",
        "{winner} wins big! {loser} owes an apology note today",
        "Today belongs to {winner}. {loser}, go study quietly",
    ],
    "close": [
        "Neck and neck! {winner} is barely ahead",
        "Almost tied! Still anyone's game, {loser} can come back",
        "Fierce battle — {winner} leads by a hair",
    ],
    "tied": [
        "Dead even, like twins! Truly Nunnaya~",
        "The brothers' energy is as one... profits match too",
        "Same DNA, so same results, it seems",
    ],
    "no_data": [
        "No data to compare yet. Let's trade hard!",
        "Once the data piles up, then we duel!",
    ],
}


def _pick_message(category: str, **kwargs) -> str:
    """Pick a (time-based) message from a category."""
    messages = _RIVALRY_MESSAGES.get(category, _RIVALRY_MESSAGES["no_data"])
    # Time-based rotation (deterministic)
    idx = int(time.time() / 3600) % len(messages)
    return messages[idx].format(**kwargs)


def _find_biggest_diff_coin(coin_battles: List[Dict]) -> str:
    """Find the coin with the largest PnL difference in the battles."""
    if not coin_battles:
        return ""
    best = max(coin_battles, key=lambda x: abs(x.get("diff", 0)))
    return best.get("coin", "")


# ── TwinBattle main class ────────────────────────────────────

class TwinBattle:
    """Server-to-server performance comparison manager.

    READ-ONLY system: never interferes with trading.
    Exports standard data via export_snapshot() and
    compares against the other server via compare().
    """

    def __init__(self, server_name: str = "server-a"):
        # Load name from twin_config.json (fallback to default)
        cfg = safe_load_json(TWIN_CONFIG_PATH, default={})
        self._server_name: str = cfg.get("server_name", server_name)
        self._last_export_ts: float = 0.0
        self._boot_ts: float = time.time()
        os.makedirs(TWIN_SNAPSHOT_DIR, exist_ok=True)
        logger.info("[TwinBattle] initialized: server_name=%s", self._server_name)

    # ── Server Name ──────────────────────────────────────────

    def set_server_name(self, name: str):
        """Change and persist the server name."""
        self._server_name = name
        cfg = safe_load_json(TWIN_CONFIG_PATH, default={})
        cfg["server_name"] = name
        safe_write_json(TWIN_CONFIG_PATH, cfg)
        logger.info("[TwinBattle] server name changed: %s", name)

    def get_status(self) -> Dict[str, Any]:
        """Return current status."""
        return {
            "server_name": self._server_name,
            "last_export_ts": self._last_export_ts,
            "last_export_ago_sec": round(time.time() - self._last_export_ts, 1) if self._last_export_ts > 0 else None,
            "uptime_sec": round(time.time() - self._boot_ts, 1),
        }

    # ── Export Snapshot ──────────────────────────────────────

    def export_snapshot(self) -> Dict[str, Any]:
        """Export a standard performance snapshot — core of the twin comparison.

        Returns:
            A standardized performance dict (can be passed to compare() on another server)
        """
        now = time.time()
        all_exits = _read_all_exits()
        today_exits = []
        reset_ts = _today_reset_ts()
        for t in all_exits:
            if t.get("ts", 0) >= reset_ts:
                today_exits.append(t)

        today_blocked = _read_today_blocked()
        active_positions = _read_active_positions()

        # ── Overall Stats ──
        total_pnl = sum(t.get("pnl_net", 0) or 0 for t in all_exits)
        total_wins = sum(1 for t in all_exits if (t.get("pnl_net", 0) or 0) > 0)
        overall_wr = round(total_wins / len(all_exits) * 100, 1) if all_exits else 0.0

        # ── Today Stats ──
        today_pnl = sum(t.get("pnl_net", 0) or 0 for t in today_exits)
        today_wins = sum(1 for t in today_exits if (t.get("pnl_net", 0) or 0) > 0)
        today_wr = round(today_wins / len(today_exits) * 100, 1) if today_exits else 0.0

        # Amount-based win rate (amt_win_rate)
        win_pnl = sum(t.get("pnl_net", 0) or 0 for t in all_exits if (t.get("pnl_net", 0) or 0) > 0)
        loss_pnl = abs(sum(t.get("pnl_net", 0) or 0 for t in all_exits if (t.get("pnl_net", 0) or 0) <= 0))
        amt_wr = round(win_pnl / (win_pnl + loss_pnl) * 100, 1) if (win_pnl + loss_pnl) > 0 else 0.0

        # ── Today Best/Worst ──
        today_best = _find_best_trade(today_exits)
        today_worst = _find_worst_trade(today_exits)

        # ── Coin Summary ──
        coin_summary = _calc_today_coin_summary(today_exits)

        # ── Blocked Counts ──
        blocked_counts = _calc_blocked_counts(today_blocked)

        # ── Daily Plans / SL Count (read from focus_config) ──
        focus_data = safe_load_json(CONFIG_PATH, default={})
        focus_state = focus_data.get("state", {})
        daily_plans_used = focus_state.get("daily_plans_used", 0)
        daily_sl_count = focus_state.get("daily_sl_count", 0)

        # ── Exposure ──
        total_exposure = _calc_exposure(active_positions)

        snapshot = {
            "server_name": self._server_name,
            "export_ts": now,
            "export_date": _today_date_label(),
            # === Overall Stats ===
            "total_pnl": round(total_pnl, 4),
            "today_pnl": round(today_pnl, 4),
            "total_trades": len(all_exits),
            "today_trades": len(today_exits),
            "overall_win_rate": overall_wr,
            "today_win_rate": today_wr,
            "amt_win_rate": amt_wr,
            # === Active Positions ===
            "active_positions": active_positions,
            "total_exposure": total_exposure,
            # === Today's Best/Worst ===
            "today_best_trade": today_best,
            "today_worst_trade": today_worst,
            # === Coin Overlap ===
            "coin_summary_today": coin_summary,
            # === Timing Analysis ===
            "avg_entry_delay_sec": 0.0,  # placeholder — for future cross-server comparison
            # === System Health ===
            "uptime_sec": round(now - self._boot_ts, 1),
            "blocked_today": blocked_counts,
            "daily_plans_used": daily_plans_used,
            "daily_sl_count": daily_sl_count,
        }

        self._last_export_ts = now
        self._save_snapshot(snapshot)
        return snapshot

    # ── Compare ──────────────────────────────────────────────

    def compare(self, other_snapshot: Dict) -> Dict[str, Any]:
        """Compare this server vs the other server's performance.

        Args:
            other_snapshot: the other server's export_snapshot() result

        Returns:
            A battle-result dict — PnL diff, per-coin outcomes, and summary message
        """
        my_snap = self.export_snapshot()
        my_name = my_snap.get("server_name", "server-a")
        other_name = other_snapshot.get("server_name", "server-b")

        # ── PnL comparison ──
        my_today_pnl = my_snap.get("today_pnl", 0)
        other_today_pnl = other_snapshot.get("today_pnl", 0)
        pnl_diff = round(my_today_pnl - other_today_pnl, 4)

        my_total_pnl = my_snap.get("total_pnl", 0)
        other_total_pnl = other_snapshot.get("total_pnl", 0)
        total_diff = round(my_total_pnl - other_total_pnl, 4)

        winner_today = my_name if pnl_diff > 0 else (other_name if pnl_diff < 0 else "tie")
        winner_total = my_name if total_diff > 0 else (other_name if total_diff < 0 else "tie")

        # ── Win-rate comparison ──
        wr_diff = round(
            (my_snap.get("today_win_rate", 0) or 0) - (other_snapshot.get("today_win_rate", 0) or 0),
            1,
        )

        # ── Per-coin battles (coins traded by both) ──
        my_coins = my_snap.get("coin_summary_today", {})
        other_coins = other_snapshot.get("coin_summary_today", {})
        overlap_coins = set(my_coins.keys()) & set(other_coins.keys())

        coin_battles: List[Dict] = []
        my_coin_wins = 0
        other_coin_wins = 0

        for coin in sorted(overlap_coins):
            my_coin_pnl = my_coins[coin].get("pnl", 0)
            other_coin_pnl = other_coins[coin].get("pnl", 0)
            diff = round(my_coin_pnl - other_coin_pnl, 4)

            if my_coin_pnl > other_coin_pnl:
                coin_winner = my_name
                my_coin_wins += 1
            elif other_coin_pnl > my_coin_pnl:
                coin_winner = other_name
                other_coin_wins += 1
            else:
                coin_winner = "tie"

            battle = {
                "coin": coin,
                my_name + "_pnl": round(my_coin_pnl, 4),
                other_name + "_pnl": round(other_coin_pnl, 4),
                "diff": diff,
                "winner": coin_winner,
            }
            coin_battles.append(battle)

        coin_battle_score = f"{my_name} {my_coin_wins} : {other_name} {other_coin_wins}"

        # ── Trade-count comparison ──
        my_today_trades = my_snap.get("today_trades", 0)
        other_today_trades = other_snapshot.get("today_trades", 0)

        # ── Build summary message (sibling rivalry!) ──
        summary = _build_summary_message(
            my_name=my_name,
            other_name=other_name,
            pnl_diff=pnl_diff,
            my_coin_wins=my_coin_wins,
            other_coin_wins=other_coin_wins,
            coin_battles=coin_battles,
            my_today_trades=my_today_trades,
            other_today_trades=other_today_trades,
        )

        label = f"{my_name}_vs_{other_name}"
        return {
            label: {
                "pnl_diff": pnl_diff,
                "total_pnl_diff": total_diff,
                "winner_today": winner_today,
                "winner_total": winner_total,
                "win_rate_diff": wr_diff,
                # Coin battles
                "coin_battles": coin_battles,
                "coin_battle_score": coin_battle_score,
                # Trade counts
                f"{my_name}_trades": my_today_trades,
                f"{other_name}_trades": other_today_trades,
                # Summary
                "summary": summary,
            }
        }

    # ── Private ──────────────────────────────────────────────

    def _save_snapshot(self, snapshot: Dict):
        """Save the snapshot to twin_snapshots/ (for trend comparison)."""
        try:
            date_str = snapshot.get("export_date", "unknown")
            ts_suffix = int(snapshot.get("export_ts", time.time()))
            path = os.path.join(TWIN_SNAPSHOT_DIR, f"{date_str}_{ts_suffix}.json")
            safe_write_json(path, snapshot)
        except Exception as exc:
            logger.warning("[TwinBattle] failed to save snapshot: %s", exc)


# ── Private Helpers (module level) ───────────────────────────

def _find_best_trade(exits: List[Dict]) -> Dict:
    """Find the best trade."""
    if not exits:
        return {}
    best = max(exits, key=lambda t: t.get("pnl_net", 0) or 0)
    pnl = best.get("pnl_net", 0) or 0
    if pnl <= 0:
        return {}
    return {
        "coin": best.get("market", ""),
        "pnl": round(pnl, 4),
        "roe_pct": round(best.get("roe_pct", 0) or 0, 2),
    }


def _find_worst_trade(exits: List[Dict]) -> Dict:
    """Find the worst trade."""
    if not exits:
        return {}
    worst = min(exits, key=lambda t: t.get("pnl_net", 0) or 0)
    pnl = worst.get("pnl_net", 0) or 0
    if pnl >= 0:
        return {}
    return {
        "coin": worst.get("market", ""),
        "pnl": round(pnl, 4),
        "roe_pct": round(worst.get("roe_pct", 0) or 0, 2),
    }


def _build_summary_message(
    *,
    my_name: str,
    other_name: str,
    pnl_diff: float,
    my_coin_wins: int,
    other_coin_wins: int,
    coin_battles: List[Dict],
    my_today_trades: int,
    other_today_trades: int,
) -> str:
    """Build the sibling-battle summary message (fun tone!)."""

    # No data
    if my_today_trades == 0 and other_today_trades == 0:
        return _pick_message("no_data")

    # Other side has no data
    if other_today_trades == 0:
        return f"Only {my_name} is trading (${abs(pnl_diff):+.2f}). {other_name} hasn't started yet!"

    # This side has no data
    if my_today_trades == 0:
        return f"Only {other_name} is trading. {my_name} hasn't started yet!"

    # Decide winner/loser
    abs_diff = abs(pnl_diff)
    if abs_diff < 0.01:
        # Tied
        msg = _pick_message("tied")
    else:
        winner = my_name if pnl_diff > 0 else other_name
        loser = other_name if pnl_diff > 0 else my_name

        if abs_diff > 50:
            msg = _pick_message("dominant", winner=winner, loser=loser)
        else:
            msg = _pick_message("close", winner=winner, loser=loser)

    # Append the coin-battle score
    if coin_battles:
        biggest_coin = _find_biggest_diff_coin(coin_battles)
        if biggest_coin:
            msg += f" Biggest gap on {biggest_coin}."

    # Append PnL figures
    if abs_diff >= 0.01:
        winner = my_name if pnl_diff > 0 else other_name
        msg = f"{winner} leads by +${abs_diff:.2f}. Coin battle {my_coin_wins}:{other_coin_wins}. {msg}"
    else:
        msg = f"PnL tied! Coin battle {my_coin_wins}:{other_coin_wins}. {msg}"

    return msg


# ── Singleton ────────────────────────────────────────────────
twin_battle = TwinBattle()
