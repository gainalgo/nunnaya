"""Weekly Intelligence Report — a weekly analysis gift from this agent's server to the sibling server.

Auto-generated every Monday 07:00 KST. Aggregates daily snapshots
(focus_daily_snapshots/) to discover patterns + produce actionable recommendations.

Accumulated as runtime/focus_weekly_reports/YYYY-WNN.json.
"""
import json
import os
import time
import statistics
import datetime as _dt
from typing import Dict, Any, List, Optional, Tuple

from app.core.io_utils import safe_write_json, safe_load_json

SNAPSHOT_DIR = os.path.join("runtime", "focus_daily_snapshots")
REPORT_DIR = os.path.join("runtime", "focus_weekly_reports")
os.makedirs(REPORT_DIR, exist_ok=True)

import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Guard against missing/None values in snapshots."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _iso_week_label(date_str: str) -> str:
    """'2026-04-13' → '2026-W16'."""
    try:
        d = _dt.date.fromisoformat(date_str)
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    except (ValueError, TypeError):
        return "unknown"


def _week_dates(year: int, week: int) -> Tuple[_dt.date, _dt.date]:
    """ISO year+week → (Monday, Sunday) range."""
    # Find the Monday of ISO week 1
    jan4 = _dt.date(year, 1, 4)
    start_of_w1 = jan4 - _dt.timedelta(days=jan4.weekday())
    monday = start_of_w1 + _dt.timedelta(weeks=week - 1)
    sunday = monday + _dt.timedelta(days=6)
    return monday, sunday


def _parse_week_label(week_label: str) -> Tuple[int, int]:
    """'2026-W16' → (2026, 16). Returns (0, 0) on failure."""
    try:
        parts = week_label.split("-W")
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return 0, 0


# ---------------------------------------------------------------------------
# 1. build_weekly_report
# ---------------------------------------------------------------------------

def build_weekly_report(snapshots: List[Dict], week_label: str) -> Dict[str, Any]:
    """List of daily snapshots → weekly report dict.

    Empty snapshots are allowed (a week with no trades).
    Safe against missing snapshot fields.
    """
    year, week = _parse_week_label(week_label)
    if year and week:
        mon, sun = _week_dates(year, week)
        period_start = mon.isoformat()
        period_end = sun.isoformat()
    else:
        period_start = ""
        period_end = ""

    if not snapshots:
        return _empty_report(week_label, period_start, period_end)

    # --- Base aggregation ---
    daily_pnls: List[float] = []
    total_pnl = 0.0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    profitable_days = 0
    losing_days = 0

    best_day: Dict[str, Any] = {"date": "", "pnl": float("-inf")}
    worst_day: Dict[str, Any] = {"date": "", "pnl": float("inf")}

    # Per-hour aggregation (KST 0~23)
    hourly_agg: Dict[int, float] = {h: 0.0 for h in range(24)}

    # Per-coin aggregation
    coin_agg: Dict[str, Dict[str, Any]] = {}

    # Per-direction aggregation
    long_pnl = 0.0
    long_trades = 0
    long_wins = 0
    short_pnl = 0.0
    short_trades = 0
    short_wins = 0

    # DT comparison aggregation
    dt_pnl = 0.0
    dt_trades = 0
    no_dt_pnl = 0.0
    no_dt_trades = 0

    # Exit-reason aggregation
    exit_reasons: Dict[str, int] = {}

    for snap in snapshots:
        pnl = _safe_float(snap.get("total_pnl"))
        trades = _safe_int(snap.get("total_trades"))
        wins = _safe_int(snap.get("wins"))
        losses = _safe_int(snap.get("losses"))
        date = snap.get("date", "?")

        daily_pnls.append(pnl)
        total_pnl += pnl
        total_trades += trades
        total_wins += wins
        total_losses += losses

        if pnl > 0:
            profitable_days += 1
        elif pnl < 0:
            losing_days += 1
        # pnl == 0: neither a profitable nor a losing day

        if pnl > best_day["pnl"]:
            best_day = {"date": date, "pnl": round(pnl, 4)}
        if pnl < worst_day["pnl"]:
            worst_day = {"date": date, "pnl": round(pnl, 4)}

        # Per-hour PnL (hourly_kst: {"0": {"pnl": ..., "trades": ...}, ...})
        hourly = snap.get("hourly_kst", {})
        for h_str, h_data in hourly.items():
            try:
                h = int(h_str)
                hourly_agg[h] = round(hourly_agg.get(h, 0.0) + _safe_float(h_data.get("pnl") if isinstance(h_data, dict) else 0), 4)
            except (ValueError, AttributeError):
                continue

        # Per-coin (by_market: {"BTCUSDT": {"pnl": ..., "trades": ..., "wins": ..., "losses": ...}})
        by_market = snap.get("by_market", {})
        for coin, cdata in by_market.items():
            if not isinstance(cdata, dict):
                continue
            if coin not in coin_agg:
                coin_agg[coin] = {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}
            coin_agg[coin]["pnl"] = round(coin_agg[coin]["pnl"] + _safe_float(cdata.get("pnl")), 4)
            coin_agg[coin]["trades"] += _safe_int(cdata.get("trades"))
            coin_agg[coin]["wins"] += _safe_int(cdata.get("wins"))
            coin_agg[coin]["losses"] += _safe_int(cdata.get("losses"))

        # Per-direction
        long_pnl += _safe_float(snap.get("long_pnl"))
        long_trades += _safe_int(snap.get("long_trades"))
        short_pnl += _safe_float(snap.get("short_pnl"))
        short_trades += _safe_int(snap.get("short_trades"))

        # Snapshots lack per-direction win counts, so hourly-wins approximation is unusable
        # → cannot re-split LONG/SHORT from per-coin wins → fall back to overall win rate
        # (no actual per-direction win field in snapshots, so estimate a ratio from trade counts)

        # DT comparison
        dt_pnl += _safe_float(snap.get("dt_pnl"))
        dt_trades += _safe_int(snap.get("dt_trades"))
        no_dt_pnl += _safe_float(snap.get("no_dt_pnl"))
        no_dt_trades += _safe_int(snap.get("no_dt_trades"))

        # Exit-reason aggregation
        by_reason = snap.get("by_exit_reason", {})
        for reason, rdata in by_reason.items():
            if isinstance(rdata, dict):
                exit_reasons[reason] = exit_reasons.get(reason, 0) + _safe_int(rdata.get("count"))

    # --- Derived metrics ---
    days_count = len(snapshots)
    avg_daily_pnl = round(total_pnl / days_count, 4) if days_count > 0 else 0.0
    overall_win_rate = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0.0

    # consistency_score: profitable-day ratio (60%) + PnL stability (40%)
    consistency_score = _calc_consistency(daily_pnls, profitable_days, days_count)

    # Clean up best_day / worst_day (guard against empty data)
    if best_day["pnl"] == float("-inf"):
        best_day = {"date": "", "pnl": 0.0}
    if worst_day["pnl"] == float("inf"):
        worst_day = {"date": "", "pnl": 0.0}

    # TOP/BOTTOM 3 hours
    sorted_hours = sorted(hourly_agg.items(), key=lambda x: x[1], reverse=True)
    best_hours = [{"hour": h, "pnl": round(p, 4)} for h, p in sorted_hours[:3]]
    worst_hours = [{"hour": h, "pnl": round(p, 4)} for h, p in sorted_hours[-3:]]

    # Coin ranking
    sorted_coins = sorted(coin_agg.items(), key=lambda x: x[1]["pnl"], reverse=True)
    best_coins = [
        {
            "coin": c,
            "pnl": round(d["pnl"], 4),
            "trades": d["trades"],
            "win_rate": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] > 0 else 0.0,
        }
        for c, d in sorted_coins[:5]
    ]
    worst_coins = [
        {
            "coin": c,
            "pnl": round(d["pnl"], 4),
            "trades": d["trades"],
            "win_rate": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] > 0 else 0.0,
        }
        for c, d in sorted_coins[-3:]
    ] if len(sorted_coins) >= 3 else [
        {
            "coin": c,
            "pnl": round(d["pnl"], 4),
            "trades": d["trades"],
            "win_rate": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] > 0 else 0.0,
        }
        for c, d in sorted_coins
    ]

    # Estimate per-direction win rate (no per-coin direction detail, so apportion by overall ratio)
    long_win_rate = _estimate_direction_wr(long_trades, total_trades, overall_win_rate)
    short_win_rate = _estimate_direction_wr(short_trades, total_trades, overall_win_rate)

    report: Dict[str, Any] = {
        "week": week_label,
        "period": {"start": period_start, "end": period_end},
        "days_count": days_count,
        "summary": {
            "total_pnl": round(total_pnl, 4),
            "total_trades": total_trades,
            "win_count": total_wins,
            "loss_count": total_losses,
            "overall_win_rate": overall_win_rate,
            "avg_daily_pnl": avg_daily_pnl,
            "best_day": best_day,
            "worst_day": worst_day,
            "profitable_days": profitable_days,
            "losing_days": losing_days,
            "consistency_score": round(consistency_score, 1),
        },
        "patterns": {
            "best_hours_kst": best_hours,
            "worst_hours_kst": worst_hours,
            "best_coins": best_coins,
            "worst_coins": worst_coins,
            "long_stats": {
                "pnl": round(long_pnl, 4),
                "trades": long_trades,
                "win_rate": long_win_rate,
            },
            "short_stats": {
                "pnl": round(short_pnl, 4),
                "trades": short_trades,
                "win_rate": short_win_rate,
            },
            "dt_stats": {
                "dt_pnl": round(dt_pnl, 4),
                "dt_trades": dt_trades,
                "no_dt_pnl": round(no_dt_pnl, 4),
                "no_dt_trades": no_dt_trades,
            },
            "exit_reasons": exit_reasons,
        },
        "recommendations": [],  # filled in by generate_recommendations
        "generated_ts": time.time(),
    }

    # Generate + insert recommendations
    report["recommendations"] = generate_recommendations(report)
    return report


def _calc_consistency(daily_pnls: List[float], profitable_days: int, days_count: int) -> float:
    """Consistency score (0~100).

    Composition:
    - Profitable-day ratio 60% (profitable days / total days × 60)
    - PnL stability 40% (based on inverse coefficient of variation; lower is better)
    """
    if days_count == 0:
        return 0.0

    # Profitable-day score (max 60 points)
    profit_ratio_score = (profitable_days / days_count) * 60.0

    # PnL stability score (max 40 points)
    if len(daily_pnls) < 2:
        stability_score = 20.0  # mid value when data is insufficient
    else:
        mean_pnl = statistics.mean(daily_pnls)
        stdev_pnl = statistics.stdev(daily_pnls)

        if abs(mean_pnl) < 0.001:
            # When mean is near zero, judge by stdev alone
            stability_score = max(0, 40.0 - stdev_pnl * 2)
        else:
            # Coefficient of variation (CV = stdev / |mean|): lower is more stable
            cv = stdev_pnl / abs(mean_pnl) if abs(mean_pnl) > 0 else 10.0
            # CV 0 → 40 points, CV 2+ → 0 points (linear decline)
            stability_score = max(0, min(40.0, (1 - cv / 2.0) * 40.0))

    return min(100.0, profit_ratio_score + stability_score)


def _estimate_direction_wr(dir_trades: int, total_trades: int, overall_wr: float) -> float:
    """Estimate per-direction win rate (when snapshots lack a per-direction win_count).

    Assume that if a direction's trade share is 60%+ of the total, its win rate
    is close to the overall win rate. Without actual data, return the overall win rate as-is.
    """
    if dir_trades == 0 or total_trades == 0:
        return 0.0
    return round(overall_wr, 1)


def _empty_report(week_label: str, period_start: str, period_end: str) -> Dict[str, Any]:
    """Empty report for a week with no trades."""
    return {
        "week": week_label,
        "period": {"start": period_start, "end": period_end},
        "days_count": 0,
        "summary": {
            "total_pnl": 0.0,
            "total_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "overall_win_rate": 0.0,
            "avg_daily_pnl": 0.0,
            "best_day": {"date": "", "pnl": 0.0},
            "worst_day": {"date": "", "pnl": 0.0},
            "profitable_days": 0,
            "losing_days": 0,
            "consistency_score": 0.0,
        },
        "patterns": {
            "best_hours_kst": [],
            "worst_hours_kst": [],
            "best_coins": [],
            "worst_coins": [],
            "long_stats": {"pnl": 0.0, "trades": 0, "win_rate": 0.0},
            "short_stats": {"pnl": 0.0, "trades": 0, "win_rate": 0.0},
            "dt_stats": {"dt_pnl": 0.0, "dt_trades": 0, "no_dt_pnl": 0.0, "no_dt_trades": 0},
            "exit_reasons": {},
        },
        "recommendations": ["📭 No trade data for this week."],
        "generated_ts": time.time(),
    }


# ---------------------------------------------------------------------------
# 2. save_weekly_report
# ---------------------------------------------------------------------------

def save_weekly_report(report: Dict[str, Any], week_label: str) -> str:
    """Save the weekly report. Returns: file path."""
    path = os.path.join(REPORT_DIR, f"{week_label}.json")
    safe_write_json(path, report)
    pnl = report.get("summary", {}).get("total_pnl", 0)
    trades = report.get("summary", {}).get("total_trades", 0)
    logger.info("[WeeklyIntel] Saved: %s (PnL=$%.2f, %d trades)", week_label, pnl, trades)
    return path


# ---------------------------------------------------------------------------
# 3. load_weekly_report
# ---------------------------------------------------------------------------

def load_weekly_report(week_label: str) -> Optional[Dict]:
    """Load a specific weekly report."""
    path = os.path.join(REPORT_DIR, f"{week_label}.json")
    data = safe_load_json(path, default=None)
    return data


# ---------------------------------------------------------------------------
# 4. get_all_weekly_reports
# ---------------------------------------------------------------------------

def get_all_weekly_reports() -> List[Dict]:
    """All weekly reports (ascending by date)."""
    if not os.path.isdir(REPORT_DIR):
        return []

    reports: List[Dict] = []
    try:
        for fn in sorted(os.listdir(REPORT_DIR)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(REPORT_DIR, fn)
            data = safe_load_json(path, default=None)
            if data and isinstance(data, dict):
                reports.append(data)
    except OSError as exc:
        logger.warning("[WeeklyIntel] Failed to list reports: %s", exc)

    return reports


# ---------------------------------------------------------------------------
# 5. generate_current_week_report
# ---------------------------------------------------------------------------

def generate_current_week_report() -> Dict[str, Any]:
    """Read the current ISO week's daily snapshots and generate a report.

    If today is Tuesday, only Mon~Tue snapshots are included (week in progress).
    """
    today = _dt.date.today()
    iso = today.isocalendar()
    week_label = f"{iso[0]}-W{iso[1]:02d}"
    monday, sunday = _week_dates(iso[0], iso[1])

    snapshots = _load_snapshots_range(monday, sunday)
    report = build_weekly_report(snapshots, week_label)
    return report


def _load_snapshots_range(start: _dt.date, end: _dt.date) -> List[Dict]:
    """Load daily snapshots for a date range."""
    snapshots: List[Dict] = []
    current = start
    while current <= end:
        date_str = current.isoformat()
        path = os.path.join(SNAPSHOT_DIR, f"{date_str}.json")
        snap = safe_load_json(path, default=None)
        if snap and isinstance(snap, dict):
            snapshots.append(snap)
        current += _dt.timedelta(days=1)
    return snapshots


# ---------------------------------------------------------------------------
# 6. generate_recommendations
# ---------------------------------------------------------------------------

def generate_recommendations(report: Dict[str, Any]) -> List[str]:
    """Generate actionable recommendations based on the report.

    Provides each week's key insights in an immediately usable form.
    """
    recs: List[str] = []
    summary = report.get("summary", {})
    patterns = report.get("patterns", {})
    total_trades = summary.get("total_trades", 0)

    if total_trades == 0:
        return ["📭 No trade data for this week."]

    # --- MVP coin ---
    best_coins = patterns.get("best_coins", [])
    if best_coins:
        mvp = best_coins[0]
        coin = mvp.get("coin", "?")
        pnl = mvp.get("pnl", 0)
        wr = mvp.get("win_rate", 0)
        recs.append(
            f"🏆 This week's MVP: {coin} (PnL ${pnl:+.2f}, win rate {wr:.0f}%) — recommend keeping it on the main watchlist next week"
        )

    # --- Worst coin warning ---
    worst_coins = patterns.get("worst_coins", [])
    if worst_coins:
        villain = worst_coins[-1] if len(worst_coins) > 0 else None
        if villain and villain.get("pnl", 0) < 0:
            recs.append(
                f"⚠️ Coin to watch: {villain['coin']} (PnL ${villain['pnl']:+.2f}, "
                f"win rate {villain.get('win_rate', 0):.0f}%) — consider tightening the conviction threshold"
            )

    # --- Best hours (KST) ---
    best_hours = patterns.get("best_hours_kst", [])
    if best_hours:
        hour_strs = [f"{h['hour']:02d}:00 (${h['pnl']:+.2f})" for h in best_hours if h.get("pnl", 0) > 0]
        if hour_strs:
            recs.append(f"⏰ Most profitable hours (KST): {', '.join(hour_strs)} — recommend entering aggressively in these windows")

    # --- Worst hours (KST) ---
    worst_hours = patterns.get("worst_hours_kst", [])
    if worst_hours:
        bad_strs = [f"{h['hour']:02d}:00 (${h['pnl']:+.2f})" for h in worst_hours if h.get("pnl", 0) < 0]
        if bad_strs:
            recs.append(f"🚫 Loss-concentrated hours (KST): {', '.join(bad_strs)} — recommend tightening SL or holding off on entries")

    # --- LONG vs SHORT bias ---
    long_stats = patterns.get("long_stats", {})
    short_stats = patterns.get("short_stats", {})
    l_pnl = long_stats.get("pnl", 0)
    s_pnl = short_stats.get("pnl", 0)
    l_trades = long_stats.get("trades", 0)
    s_trades = short_stats.get("trades", 0)

    if l_trades > 0 and s_trades > 0:
        if l_pnl > 0 and s_pnl < 0:
            recs.append(
                f"📈 LONG-favored week (L: ${l_pnl:+.2f}/{l_trades} trades, S: ${s_pnl:+.2f}/{s_trades} trades) "
                f"— consider raising the SHORT entry threshold"
            )
        elif s_pnl > 0 and l_pnl < 0:
            recs.append(
                f"📉 SHORT-favored week (S: ${s_pnl:+.2f}/{s_trades} trades, L: ${l_pnl:+.2f}/{l_trades} trades) "
                f"— consider raising the LONG entry threshold"
            )
        else:
            recs.append(
                f"↔️ Directional balance: LONG ${l_pnl:+.2f}/{l_trades} trades, SHORT ${s_pnl:+.2f}/{s_trades} trades"
            )
    elif l_trades > 0:
        recs.append(f"📈 LONG-only week: ${l_pnl:+.2f}/{l_trades} trades — need to scout for SHORT opportunities")
    elif s_trades > 0:
        recs.append(f"📉 SHORT-only week: ${s_pnl:+.2f}/{s_trades} trades — need to scout for LONG opportunities")

    # --- DT (Dynamic Trailing) effect ---
    dt = patterns.get("dt_stats", {})
    dt_pnl = dt.get("dt_pnl", 0)
    dt_trades = dt.get("dt_trades", 0)
    no_dt_pnl = dt.get("no_dt_pnl", 0)
    no_dt_trades = dt.get("no_dt_trades", 0)

    if dt_trades > 0 and no_dt_trades > 0:
        dt_avg = dt_pnl / dt_trades
        no_dt_avg = no_dt_pnl / no_dt_trades
        if dt_avg > no_dt_avg:
            recs.append(
                f"🎯 DT effect positive: DT avg ${dt_avg:+.4f} vs normal ${no_dt_avg:+.4f} — recommend keeping DT"
            )
        else:
            recs.append(
                f"⚡ DT effect negligible/negative: DT avg ${dt_avg:+.4f} vs normal ${no_dt_avg:+.4f} — review DT settings"
            )
    elif dt_trades > 0:
        recs.append(f"🎯 DT-only operation: {dt_trades} trades ${dt_pnl:+.2f} — no comparison control group")

    # --- Exit-reason insight ---
    exit_reasons = patterns.get("exit_reasons", {})
    if exit_reasons:
        total_exits = sum(exit_reasons.values())
        # Most frequent reason
        top_reason = max(exit_reasons.items(), key=lambda x: x[1])
        pct = round(top_reason[1] / total_exits * 100, 0) if total_exits > 0 else 0
        recs.append(f"🔍 Top exit reason: {top_reason[0]} ({top_reason[1]} trades, {pct:.0f}%)")

    # --- Consistency grade ---
    score = summary.get("consistency_score", 0)
    if score >= 80:
        grade = "A (very stable)"
    elif score >= 60:
        grade = "B (good)"
    elif score >= 40:
        grade = "C (fair)"
    elif score >= 20:
        grade = "D (unstable)"
    else:
        grade = "F (risky)"
    total_pnl = summary.get("total_pnl", 0)
    profitable_days = summary.get("profitable_days", 0)
    days_count = report.get("days_count", 0)
    recs.append(
        f"📊 Consistency grade: {grade} (score {score:.0f}/100, "
        f"profitable days {profitable_days}/{days_count}, total PnL ${total_pnl:+.2f})"
    )

    return recs


# ---------------------------------------------------------------------------
# 7. auto_generate_weekly
# ---------------------------------------------------------------------------

def auto_generate_weekly(force: bool = False) -> Optional[Dict]:
    """Called Monday 07:00 KST — auto-generate the previous week's report.

    force=True: regenerate even if one already exists.
    Returns: the generated report dict, or None (when skipped).
    """
    # Previous week = current ISO week - 1
    today = _dt.date.today()
    iso = today.isocalendar()

    # Compute the previous week (handles year-end/year-start boundary)
    last_week_date = today - _dt.timedelta(days=7)
    last_iso = last_week_date.isocalendar()
    week_label = f"{last_iso[0]}-W{last_iso[1]:02d}"

    # Check if it already exists
    if not force:
        existing = load_weekly_report(week_label)
        if existing:
            logger.info("[WeeklyIntel] %s already exists, skipping (force=False)", week_label)
            return None

    # Load snapshots for the previous week's range
    monday, sunday = _week_dates(last_iso[0], last_iso[1])
    snapshots = _load_snapshots_range(monday, sunday)

    if not snapshots:
        logger.info("[WeeklyIntel] %s no snapshots — generating empty report", week_label)

    report = build_weekly_report(snapshots, week_label)
    path = save_weekly_report(report, week_label)
    logger.info("[WeeklyIntel] Auto-generated: %s → %s", week_label, path)
    return report
