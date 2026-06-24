"""FOCUS Daily Performance Snapshot — daily performance record based on the 07:00 KST reset.

On each daily reset, automatically saves the previous day's performance + config + hourly distribution.
Accumulated in runtime/focus_daily_snapshots/ as YYYY-MM-DD.json files.
"""
import json
import os
import time
import datetime as _dt
from typing import Dict, Any, List, Optional

from app.core.io_utils import safe_write_json

SNAPSHOT_DIR = os.path.join("runtime", "focus_daily_snapshots")
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

import logging
logger = logging.getLogger(__name__)


def _reset_boundary(dt_utc: _dt.datetime) -> _dt.datetime:
    """Compute the 07:00 KST (22:00 UTC) reset boundary for the given moment."""
    boundary = dt_utc.replace(hour=22, minute=0, second=0, microsecond=0)
    if dt_utc.hour < 22:
        boundary -= _dt.timedelta(days=1)
    return boundary


def _date_label(boundary: _dt.datetime) -> str:
    """The 'trading day' label (KST date) derived from the reset boundary.
    22:00 UTC = 07:00 KST next day → the trading day is the date of boundary + 9h."""
    kst = boundary + _dt.timedelta(hours=9)
    return kst.strftime("%Y-%m-%d")


def build_snapshot(
    trades: List[Dict],
    reset_ts: float,
    next_reset_ts: float,
    config: Optional[Dict] = None,
    equity_start: float = 0.0,
) -> Dict[str, Any]:
    """Build a snapshot from the EXIT trades within the given period.

    Args:
        trades: full list of journal EXIT trades
        reset_ts: start timestamp (07:00 KST)
        next_reset_ts: end timestamp (next 07:00 KST)
        config: FOCUS config snapshot (optional)
        equity_start: exchange balance at the start of the day (for ROI% calculation)
    """
    period_exits = [
        t for t in trades
        if t.get("event") == "EXIT"
        and reset_ts <= t.get("ts", 0) < next_reset_ts
    ]

    if not period_exits:
        return _empty_snapshot(reset_ts, config)

    wins = [t for t in period_exits if (t.get("pnl_net", 0) or 0) > 0]
    losses = [t for t in period_exits if (t.get("pnl_net", 0) or 0) <= 0]
    total_pnl = sum(t.get("pnl_net", 0) or 0 for t in period_exits)
    total_fee = sum(t.get("fee", 0) or 0 for t in period_exits)
    win_pnl = sum(t.get("pnl_net", 0) or 0 for t in wins)
    loss_pnl = sum(t.get("pnl_net", 0) or 0 for t in losses)

    # Dynamic Trailing comparison
    dt_exits = [t for t in period_exits if t.get("dynamic_trailing")]
    no_dt_exits = [t for t in period_exits if not t.get("dynamic_trailing")]

    # Hourly PnL (KST, hours 0~23)
    hourly: Dict[int, Dict] = {}
    for h in range(24):
        hourly[h] = {"pnl": 0.0, "trades": 0, "wins": 0}
    for t in period_exits:
        ts = t.get("ts", 0)
        kst_hour = (_dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc) + _dt.timedelta(hours=9)).hour
        pnl = t.get("pnl_net", 0) or 0
        hourly[kst_hour]["pnl"] = round(hourly[kst_hour]["pnl"] + pnl, 4)
        hourly[kst_hour]["trades"] += 1
        if pnl > 0:
            hourly[kst_hour]["wins"] += 1

    # PnL by coin/market
    by_market: Dict[str, Dict] = {}
    for t in period_exits:
        m = t.get("market", "?")
        if m not in by_market:
            by_market[m] = {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}
        pnl = t.get("pnl_net", 0) or 0
        by_market[m]["pnl"] = round(by_market[m]["pnl"] + pnl, 4)
        by_market[m]["trades"] += 1
        if pnl > 0:
            by_market[m]["wins"] += 1
        else:
            by_market[m]["losses"] += 1

    # PnL by direction
    long_exits = [t for t in period_exits if t.get("direction") == "LONG"]
    short_exits = [t for t in period_exits if t.get("direction") == "SHORT"]

    # Stats by exit reason
    by_reason: Dict[str, Dict] = {}
    for t in period_exits:
        reason = t.get("exit_reason", "unknown")
        # Simplify: SERVER_SL, SERVER_TP, trend_reversal, etc.
        key = reason.split(":")[0] if ":" in reason else reason
        if key not in by_reason:
            by_reason[key] = {"pnl": 0.0, "count": 0}
        by_reason[key]["pnl"] = round(by_reason[key]["pnl"] + (t.get("pnl_net", 0) or 0), 4)
        by_reason[key]["count"] += 1

    # Hold time stats
    hold_secs = [t.get("hold_sec", 0) or 0 for t in period_exits]
    avg_hold = sum(hold_secs) / len(hold_secs) if hold_secs else 0

    # Weekday (KST)
    boundary_dt = _dt.datetime.fromtimestamp(reset_ts, tz=_dt.timezone.utc)
    date_label = _date_label(boundary_dt)
    kst_date = boundary_dt + _dt.timedelta(hours=9)
    weekday = kst_date.strftime("%A")  # Monday, Tuesday, ...

    return {
        "date": date_label,
        "weekday": weekday,
        "reset_ts": reset_ts,
        # Key metrics
        "total_pnl": round(total_pnl, 4),
        "total_trades": len(period_exits),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(period_exits) * 100, 1),
        "total_fee": round(total_fee, 4),
        "win_pnl": round(win_pnl, 4),
        "loss_pnl": round(loss_pnl, 4),
        "amount_wr": round(win_pnl / (win_pnl + abs(loss_pnl)) * 100, 1) if (win_pnl + abs(loss_pnl)) > 0 else 0,
        "avg_pnl": round(total_pnl / len(period_exits), 4),
        "best_trade": round(max((t.get("pnl_net", 0) or 0 for t in period_exits), default=0), 4),
        "worst_trade": round(min((t.get("pnl_net", 0) or 0 for t in period_exits), default=0), 4),
        "avg_hold_min": round(avg_hold / 60, 1),
        # By direction
        "long_pnl": round(sum(t.get("pnl_net", 0) or 0 for t in long_exits), 4),
        "long_trades": len(long_exits),
        "short_pnl": round(sum(t.get("pnl_net", 0) or 0 for t in short_exits), 4),
        "short_trades": len(short_exits),
        # DT comparison
        "dt_pnl": round(sum(t.get("pnl_net", 0) or 0 for t in dt_exits), 4),
        "dt_trades": len(dt_exits),
        "no_dt_pnl": round(sum(t.get("pnl_net", 0) or 0 for t in no_dt_exits), 4),
        "no_dt_trades": len(no_dt_exits),
        # Distributions
        "hourly_kst": {str(h): hourly[h] for h in range(24)},
        "by_market": by_market,
        "by_exit_reason": by_reason,
        # Config snapshot
        "config": config or {},
        # Equity tracking (for pure ROI calculation)
        "equity_start": round(equity_start, 2),
        "roi_pct": round(total_pnl / equity_start * 100, 2) if equity_start > 0 else 0,
        # Meta
        "saved_ts": time.time(),
    }


def _empty_snapshot(reset_ts: float, config: Optional[Dict] = None) -> Dict[str, Any]:
    boundary_dt = _dt.datetime.fromtimestamp(reset_ts, tz=_dt.timezone.utc)
    date_label = _date_label(boundary_dt)
    kst_date = boundary_dt + _dt.timedelta(hours=9)
    return {
        "date": date_label,
        "weekday": kst_date.strftime("%A"),
        "reset_ts": reset_ts,
        "total_pnl": 0, "total_trades": 0, "wins": 0, "losses": 0,
        "win_rate": 0, "total_fee": 0, "win_pnl": 0, "loss_pnl": 0,
        "amount_wr": 0, "avg_pnl": 0, "best_trade": 0, "worst_trade": 0,
        "avg_hold_min": 0,
        "long_pnl": 0, "long_trades": 0, "short_pnl": 0, "short_trades": 0,
        "dt_pnl": 0, "dt_trades": 0, "no_dt_pnl": 0, "no_dt_trades": 0,
        "hourly_kst": {str(h): {"pnl": 0, "trades": 0, "wins": 0} for h in range(24)},
        "by_market": {}, "by_exit_reason": {},
        "config": config or {},
        "equity_start": 0, "roi_pct": 0,
        "saved_ts": time.time(),
    }


def save_snapshot(snapshot: Dict[str, Any], snap_dir: Optional[str] = None) -> str:
    """Save the snapshot to a file. Returns: file path. (snap_dir unset = default directory, no behavior change)"""
    _dir = snap_dir or SNAPSHOT_DIR
    os.makedirs(_dir, exist_ok=True)
    date_str = snapshot.get("date", "unknown")
    path = os.path.join(_dir, f"{date_str}.json")
    safe_write_json(path, snapshot)
    logger.info("[DailySnapshot] Saved: %s (PnL=$%.2f, %d trades)", date_str, snapshot.get("total_pnl", 0), snapshot.get("total_trades", 0))
    return path


def load_snapshot(date_str: str, snap_dir: Optional[str] = None) -> Optional[Dict]:
    """Load the snapshot for a specific date."""
    path = os.path.join(snap_dir or SNAPSHOT_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def list_snapshots(snap_dir: Optional[str] = None) -> List[str]:
    """List of saved snapshot dates (newest first)."""
    _dir = snap_dir or SNAPSHOT_DIR
    if not os.path.isdir(_dir):
        return []
    dates = []
    for fn in os.listdir(_dir):
        if fn.endswith(".json"):
            dates.append(fn.replace(".json", ""))
    return sorted(dates, reverse=True)


def get_all_snapshots(snap_dir: Optional[str] = None) -> List[Dict]:
    """Load all snapshots (chronological order)."""
    snapshots = []
    for date_str in sorted(list_snapshots(snap_dir)):
        snap = load_snapshot(date_str, snap_dir)
        if snap:
            snapshots.append(snap)
    return snapshots


def backfill_from_journal(config: Optional[Dict] = None, journal_path: Optional[str] = None,
                          snap_dir: Optional[str] = None) -> int:
    """Bulk-generate past daily snapshots from the journal (missing dates only).
    Returns: number of snapshots created. (journal_path/snap_dir unset = global default, no behavior change)"""
    from app.manager.trade_journal import JOURNAL_PATH
    _jp = journal_path or JOURNAL_PATH

    if not os.path.exists(_jp):
        return 0

    # Load all EXIT trades
    exits = []
    with open(_jp, "r", encoding="utf-8") as f:
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

    if not exits:
        return 0

    # Classify by date, starting from the oldest trade
    min_ts = min(t.get("ts", 0) for t in exits)
    max_ts = max(t.get("ts", 0) for t in exits)

    # Find the first reset boundary
    first_dt = _dt.datetime.fromtimestamp(min_ts, tz=_dt.timezone.utc)
    boundary = _reset_boundary(first_dt)

    existing = set(list_snapshots(snap_dir))
    count = 0
    now_ts = time.time()

    while boundary.timestamp() < now_ts:
        next_boundary = boundary + _dt.timedelta(days=1)
        date_label = _date_label(boundary)

        # Skip today since it is still in progress
        if next_boundary.timestamp() > now_ts:
            break

        if date_label not in existing:
            snap = build_snapshot(exits, boundary.timestamp(), next_boundary.timestamp(), config)
            if snap.get("total_trades", 0) > 0:
                save_snapshot(snap, snap_dir)
                count += 1

        boundary = next_boundary

    return count
