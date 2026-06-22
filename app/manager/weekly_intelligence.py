"""Weekly Intelligence Report — 형 서버가 동생 서버에게 주는 주간 분석 선물.

매주 월요일 07:00 KST 자동 생성. 일별 스냅샷(focus_daily_snapshots/)을
집계하여 패턴 발견 + 실행 가능한 추천을 생성.

runtime/focus_weekly_reports/YYYY-WNN.json 형태로 누적.
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
# 유틸
# ---------------------------------------------------------------------------

def _safe_float(val: Any, default: float = 0.0) -> float:
    """스냅샷에서 누락/None 방어."""
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
    """ISO year+week → (월요일, 일요일) 범위."""
    # ISO week 1의 월요일 구하기
    jan4 = _dt.date(year, 1, 4)
    start_of_w1 = jan4 - _dt.timedelta(days=jan4.weekday())
    monday = start_of_w1 + _dt.timedelta(weeks=week - 1)
    sunday = monday + _dt.timedelta(days=6)
    return monday, sunday


def _parse_week_label(week_label: str) -> Tuple[int, int]:
    """'2026-W16' → (2026, 16). 실패 시 (0, 0)."""
    try:
        parts = week_label.split("-W")
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return 0, 0


# ---------------------------------------------------------------------------
# 1. build_weekly_report
# ---------------------------------------------------------------------------

def build_weekly_report(snapshots: List[Dict], week_label: str) -> Dict[str, Any]:
    """일별 스냅샷 리스트 → 주간 리포트 dict.

    빈 snapshots도 허용 (거래 없는 주).
    스냅샷 필드 누락에 안전.
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

    # --- 기본 집계 ---
    daily_pnls: List[float] = []
    total_pnl = 0.0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    profitable_days = 0
    losing_days = 0

    best_day: Dict[str, Any] = {"date": "", "pnl": float("-inf")}
    worst_day: Dict[str, Any] = {"date": "", "pnl": float("inf")}

    # 시간대별 집계 (KST 0~23)
    hourly_agg: Dict[int, float] = {h: 0.0 for h in range(24)}

    # 코인별 집계
    coin_agg: Dict[str, Dict[str, Any]] = {}

    # 방향별 집계
    long_pnl = 0.0
    long_trades = 0
    long_wins = 0
    short_pnl = 0.0
    short_trades = 0
    short_wins = 0

    # DT 비교 집계
    dt_pnl = 0.0
    dt_trades = 0
    no_dt_pnl = 0.0
    no_dt_trades = 0

    # 청산 사유 집계
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
        # pnl == 0: 수익일도 손실일도 아님

        if pnl > best_day["pnl"]:
            best_day = {"date": date, "pnl": round(pnl, 4)}
        if pnl < worst_day["pnl"]:
            worst_day = {"date": date, "pnl": round(pnl, 4)}

        # 시간대별 PnL (hourly_kst: {"0": {"pnl": ..., "trades": ...}, ...})
        hourly = snap.get("hourly_kst", {})
        for h_str, h_data in hourly.items():
            try:
                h = int(h_str)
                hourly_agg[h] = round(hourly_agg.get(h, 0.0) + _safe_float(h_data.get("pnl") if isinstance(h_data, dict) else 0), 4)
            except (ValueError, AttributeError):
                continue

        # 코인별 (by_market: {"BTCUSDT": {"pnl": ..., "trades": ..., "wins": ..., "losses": ...}})
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

        # 방향별
        long_pnl += _safe_float(snap.get("long_pnl"))
        long_trades += _safe_int(snap.get("long_trades"))
        short_pnl += _safe_float(snap.get("short_pnl"))
        short_trades += _safe_int(snap.get("short_trades"))

        # 일별 승수에서 방향별 승 구분은 스냅샷에 없으므로 hourly wins 근사 사용 불가
        # → 코인별 wins에서 LONG/SHORT 재분리 불가 → 전체 승률로 대체
        # (실제 방향별 win 필드가 스냅샷에 없으므로 거래 건수 기반 비율 추정)

        # DT 비교
        dt_pnl += _safe_float(snap.get("dt_pnl"))
        dt_trades += _safe_int(snap.get("dt_trades"))
        no_dt_pnl += _safe_float(snap.get("no_dt_pnl"))
        no_dt_trades += _safe_int(snap.get("no_dt_trades"))

        # 청산 사유 집계
        by_reason = snap.get("by_exit_reason", {})
        for reason, rdata in by_reason.items():
            if isinstance(rdata, dict):
                exit_reasons[reason] = exit_reasons.get(reason, 0) + _safe_int(rdata.get("count"))

    # --- 파생 지표 ---
    days_count = len(snapshots)
    avg_daily_pnl = round(total_pnl / days_count, 4) if days_count > 0 else 0.0
    overall_win_rate = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0.0

    # consistency_score: 수익일 비율(60%) + PnL 안정성(40%)
    consistency_score = _calc_consistency(daily_pnls, profitable_days, days_count)

    # best_day / worst_day 정리 (빈 데이터 방어)
    if best_day["pnl"] == float("-inf"):
        best_day = {"date": "", "pnl": 0.0}
    if worst_day["pnl"] == float("inf"):
        worst_day = {"date": "", "pnl": 0.0}

    # 시간대별 TOP/BOTTOM 3
    sorted_hours = sorted(hourly_agg.items(), key=lambda x: x[1], reverse=True)
    best_hours = [{"hour": h, "pnl": round(p, 4)} for h, p in sorted_hours[:3]]
    worst_hours = [{"hour": h, "pnl": round(p, 4)} for h, p in sorted_hours[-3:]]

    # 코인 순위
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

    # 방향별 승률 추정 (코인별 세부 방향 없으므로 전체 비율로 안분)
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
        "recommendations": [],  # generate_recommendations 로 채움
        "generated_ts": time.time(),
    }

    # 추천 생성 + 삽입
    report["recommendations"] = generate_recommendations(report)
    return report


def _calc_consistency(daily_pnls: List[float], profitable_days: int, days_count: int) -> float:
    """일관성 점수 (0~100).

    구성:
    - 수익일 비율 60% (전체일 대비 수익일 비율 × 60)
    - PnL 안정성 40% (변동계수 역수 기반, 낮을수록 좋음)
    """
    if days_count == 0:
        return 0.0

    # 수익일 점수 (60점 만점)
    profit_ratio_score = (profitable_days / days_count) * 60.0

    # PnL 안정성 점수 (40점 만점)
    if len(daily_pnls) < 2:
        stability_score = 20.0  # 데이터 부족 시 중간값
    else:
        mean_pnl = statistics.mean(daily_pnls)
        stdev_pnl = statistics.stdev(daily_pnls)

        if abs(mean_pnl) < 0.001:
            # 평균 0에 가까우면 stdev만으로 판단
            stability_score = max(0, 40.0 - stdev_pnl * 2)
        else:
            # 변동계수 (CV = stdev / |mean|): 낮을수록 안정적
            cv = stdev_pnl / abs(mean_pnl) if abs(mean_pnl) > 0 else 10.0
            # CV 0 → 40점, CV 2+ → 0점 (선형 감소)
            stability_score = max(0, min(40.0, (1 - cv / 2.0) * 40.0))

    return min(100.0, profit_ratio_score + stability_score)


def _estimate_direction_wr(dir_trades: int, total_trades: int, overall_wr: float) -> float:
    """방향별 승률 추정 (스냅샷에 방향별 win_count 미포함 시).

    방향별 거래 비중이 전체의 60% 이상이면 전체 승률에 가깝다고 가정.
    실제 데이터 없으면 전체 승률을 그대로 반환.
    """
    if dir_trades == 0 or total_trades == 0:
        return 0.0
    return round(overall_wr, 1)


def _empty_report(week_label: str, period_start: str, period_end: str) -> Dict[str, Any]:
    """거래 없는 주간 빈 리포트."""
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
        "recommendations": ["📭 이번 주 거래 데이터가 없습니다."],
        "generated_ts": time.time(),
    }


# ---------------------------------------------------------------------------
# 2. save_weekly_report
# ---------------------------------------------------------------------------

def save_weekly_report(report: Dict[str, Any], week_label: str) -> str:
    """주간 리포트 저장. 반환: 파일 경로."""
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
    """특정 주간 리포트 로드."""
    path = os.path.join(REPORT_DIR, f"{week_label}.json")
    data = safe_load_json(path, default=None)
    return data


# ---------------------------------------------------------------------------
# 4. get_all_weekly_reports
# ---------------------------------------------------------------------------

def get_all_weekly_reports() -> List[Dict]:
    """전체 주간 리포트 (날짜순 오름차순)."""
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
    """현재 ISO 주의 일별 스냅샷을 읽어 리포트 생성.

    오늘이 화요일이면 월~화 스냅샷만 포함 (진행중인 주).
    """
    today = _dt.date.today()
    iso = today.isocalendar()
    week_label = f"{iso[0]}-W{iso[1]:02d}"
    monday, sunday = _week_dates(iso[0], iso[1])

    snapshots = _load_snapshots_range(monday, sunday)
    report = build_weekly_report(snapshots, week_label)
    return report


def _load_snapshots_range(start: _dt.date, end: _dt.date) -> List[Dict]:
    """날짜 범위의 일별 스냅샷 로드."""
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
    """리포트 기반 한국어 실행 추천 생성.

    매주 핵심 인사이트를 즉시 활용 가능한 형태로 제공.
    """
    recs: List[str] = []
    summary = report.get("summary", {})
    patterns = report.get("patterns", {})
    total_trades = summary.get("total_trades", 0)

    if total_trades == 0:
        return ["📭 이번 주 거래 데이터가 없습니다."]

    # --- MVP 코인 ---
    best_coins = patterns.get("best_coins", [])
    if best_coins:
        mvp = best_coins[0]
        coin = mvp.get("coin", "?")
        pnl = mvp.get("pnl", 0)
        wr = mvp.get("win_rate", 0)
        recs.append(
            f"🏆 금주 MVP: {coin} (PnL ${pnl:+.2f}, 승률 {wr:.0f}%) — 다음 주에도 주력 감시 추천"
        )

    # --- 최악 코인 경고 ---
    worst_coins = patterns.get("worst_coins", [])
    if worst_coins:
        villain = worst_coins[-1] if len(worst_coins) > 0 else None
        if villain and villain.get("pnl", 0) < 0:
            recs.append(
                f"⚠️ 주의 코인: {villain['coin']} (PnL ${villain['pnl']:+.2f}, "
                f"승률 {villain.get('win_rate', 0):.0f}%) — conviction 기준 강화 고려"
            )

    # --- 최적 시간대 (KST) ---
    best_hours = patterns.get("best_hours_kst", [])
    if best_hours:
        hour_strs = [f"{h['hour']:02d}시(${h['pnl']:+.2f})" for h in best_hours if h.get("pnl", 0) > 0]
        if hour_strs:
            recs.append(f"⏰ 최고 수익 시간대(KST): {', '.join(hour_strs)} — 이 시간대 적극 진입 추천")

    # --- 최악 시간대 (KST) ---
    worst_hours = patterns.get("worst_hours_kst", [])
    if worst_hours:
        bad_strs = [f"{h['hour']:02d}시(${h['pnl']:+.2f})" for h in worst_hours if h.get("pnl", 0) < 0]
        if bad_strs:
            recs.append(f"🚫 손실 집중 시간대(KST): {', '.join(bad_strs)} — SL 타이트닝 또는 진입 자제 권장")

    # --- LONG vs SHORT 편향 ---
    long_stats = patterns.get("long_stats", {})
    short_stats = patterns.get("short_stats", {})
    l_pnl = long_stats.get("pnl", 0)
    s_pnl = short_stats.get("pnl", 0)
    l_trades = long_stats.get("trades", 0)
    s_trades = short_stats.get("trades", 0)

    if l_trades > 0 and s_trades > 0:
        if l_pnl > 0 and s_pnl < 0:
            recs.append(
                f"📈 LONG 우세 주간 (L: ${l_pnl:+.2f}/{l_trades}건, S: ${s_pnl:+.2f}/{s_trades}건) "
                f"— SHORT 진입 기준 상향 고려"
            )
        elif s_pnl > 0 and l_pnl < 0:
            recs.append(
                f"📉 SHORT 우세 주간 (S: ${s_pnl:+.2f}/{s_trades}건, L: ${l_pnl:+.2f}/{l_trades}건) "
                f"— LONG 진입 기준 상향 고려"
            )
        else:
            recs.append(
                f"↔️ 방향 균형: LONG ${l_pnl:+.2f}/{l_trades}건, SHORT ${s_pnl:+.2f}/{s_trades}건"
            )
    elif l_trades > 0:
        recs.append(f"📈 LONG 전용 주간: ${l_pnl:+.2f}/{l_trades}건 — SHORT 기회 탐색 필요")
    elif s_trades > 0:
        recs.append(f"📉 SHORT 전용 주간: ${s_pnl:+.2f}/{s_trades}건 — LONG 기회 탐색 필요")

    # --- DT(Dynamic Trailing) 효과 ---
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
                f"🎯 DT 효과 긍정적: DT 평균 ${dt_avg:+.4f} vs 일반 ${no_dt_avg:+.4f} — DT 유지 권장"
            )
        else:
            recs.append(
                f"⚡ DT 효과 미미/부정적: DT 평균 ${dt_avg:+.4f} vs 일반 ${no_dt_avg:+.4f} — DT 설정 재검토"
            )
    elif dt_trades > 0:
        recs.append(f"🎯 DT 전용 운영: {dt_trades}건 ${dt_pnl:+.2f} — 비교 대조군 부재")

    # --- 청산 사유 인사이트 ---
    exit_reasons = patterns.get("exit_reasons", {})
    if exit_reasons:
        total_exits = sum(exit_reasons.values())
        # 가장 빈번한 사유
        top_reason = max(exit_reasons.items(), key=lambda x: x[1])
        pct = round(top_reason[1] / total_exits * 100, 0) if total_exits > 0 else 0
        recs.append(f"🔍 주요 청산 사유: {top_reason[0]} ({top_reason[1]}건, {pct:.0f}%)")

    # --- 일관성 등급 ---
    score = summary.get("consistency_score", 0)
    if score >= 80:
        grade = "A (매우 안정)"
    elif score >= 60:
        grade = "B (양호)"
    elif score >= 40:
        grade = "C (보통)"
    elif score >= 20:
        grade = "D (불안정)"
    else:
        grade = "F (위험)"
    total_pnl = summary.get("total_pnl", 0)
    profitable_days = summary.get("profitable_days", 0)
    days_count = report.get("days_count", 0)
    recs.append(
        f"📊 일관성 등급: {grade} (점수 {score:.0f}/100, "
        f"수익일 {profitable_days}/{days_count}일, 총 PnL ${total_pnl:+.2f})"
    )

    return recs


# ---------------------------------------------------------------------------
# 7. auto_generate_weekly
# ---------------------------------------------------------------------------

def auto_generate_weekly(force: bool = False) -> Optional[Dict]:
    """월요일 07:00 KST에 호출 — 지난 주 리포트 자동 생성.

    force=True: 이미 존재해도 재생성.
    반환: 생성된 리포트 dict 또는 None(스킵 시).
    """
    # 지난 주 = 현재 ISO 주 - 1
    today = _dt.date.today()
    iso = today.isocalendar()

    # 지난 주 계산 (연말/연초 경계 처리)
    last_week_date = today - _dt.timedelta(days=7)
    last_iso = last_week_date.isocalendar()
    week_label = f"{last_iso[0]}-W{last_iso[1]:02d}"

    # 이미 존재 체크
    if not force:
        existing = load_weekly_report(week_label)
        if existing:
            logger.info("[WeeklyIntel] %s 이미 존재, 스킵 (force=False)", week_label)
            return None

    # 지난 주 범위 스냅샷 로드
    monday, sunday = _week_dates(last_iso[0], last_iso[1])
    snapshots = _load_snapshots_range(monday, sunday)

    if not snapshots:
        logger.info("[WeeklyIntel] %s 스냅샷 없음 — 빈 리포트 생성", week_label)

    report = build_weekly_report(snapshots, week_label)
    path = save_weekly_report(report, week_label)
    logger.info("[WeeklyIntel] Auto-generated: %s → %s", week_label, path)
    return report
