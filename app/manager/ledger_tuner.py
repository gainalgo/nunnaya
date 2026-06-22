# ============================================================
# File: app/manager/ledger_tuner.py
# Autocoin OS v3-H — Ledger Tuning Analyzer (empirical tuning)
# ------------------------------------------------------------
# - JSONL 원장(trade_ledger.jsonl) 기반으로 실측 통계를 산출한다.
# - 안전장치가 '진입의 방벽'이 되지 않도록,
#   과도한 false-positive를 줄이는 방향(보수적/완만한 추천)을 기본으로 한다.
# - 환경변수 자체를 자동 변경하지 않는다(권장값만 산출).
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

def _is_number(x: Any) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        logger.warning("[LedgerTuner] _is_number: conversion failed for %r", x, exc_info=True)
        return False

def _percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    p = float(p)
    if p <= 0:
        return float(sorted_vals[0])
    if p >= 100:
        return float(sorted_vals[-1])

    # linear interpolation between closest ranks
    k = (n - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return float(sorted_vals[f])
    d0 = float(sorted_vals[f]) * (c - k)
    d1 = float(sorted_vals[c]) * (k - f)
    return d0 + d1

def _summarize(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {
            "n": 0,
            "min": None,
            "mean": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "max": None,
        }

    vals = [float(v) for v in values]
    vals.sort()

    n = len(vals)
    s = sum(vals)
    mean = s / float(n) if n else None

    return {
        "n": n,
        "min": float(vals[0]),
        "mean": float(mean) if mean is not None else None,
        "p50": _percentile(vals, 50.0),
        "p90": _percentile(vals, 90.0),
        "p95": _percentile(vals, 95.0),
        "p99": _percentile(vals, 99.0),
        "max": float(vals[-1]),
    }

def _safe_float_env(key: str, default: Optional[float] = None) -> Optional[float]:
    v = os.getenv(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        logger.warning("[LedgerTuner] _safe_float_env(%s) parse failed for %r", key, v, exc_info=True)
        return default

def _safe_int_env(key: str, default: Optional[int] = None) -> Optional[int]:
    v = os.getenv(key)
    if v is None:
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        logger.warning("[LedgerTuner] _safe_int_env(%s) parse failed for %r", key, v, exc_info=True)
        return default

def _collect_ledger_files(path: str) -> List[str]:
    """현재 ledger + rotation backup(.bak)를 포함한 파일 리스트."""
    path = str(path)
    out: List[str] = []

    # backups: <path>.<timestamp>.bak
    d = os.path.dirname(path) or "."
    base = os.path.basename(path)

    try:
        for fn in os.listdir(d):
            if fn.startswith(base + ".") and fn.endswith(".bak"):
                out.append(os.path.join(d, fn))
    except (OSError, TypeError, ValueError):
        logger.warning("[LedgerTuner] _collect_ledger_files: listdir failed for %s", d, exc_info=True)
        out = []

    # sort backups by mtime ascending
    try:
        out.sort(key=lambda p: os.path.getmtime(p))
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("[ledger_tuner] %s: %s", 'sort backups by mtime ascending', exc, exc_info=True)

    # finally current file
    out.append(path)

    # unique + exists only
    uniq: List[str] = []
    seen = set()
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        if os.path.exists(p):
            uniq.append(p)
    return uniq

def _iter_records(paths: List[str]) -> Iterable[Dict[str, Any]]:
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if isinstance(rec, dict):
                            yield rec
                    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                        logger.warning("[ledger_tuner] %s: %s", 'ledger_tuner._iter_records except-> continue', exc, exc_info=True)
                        continue
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("[ledger_tuner] %s: %s", 'ledger_tuner._iter_records except-> continue', exc, exc_info=True)
            continue

@dataclass
class TuningInput:
    ledger_path: str
    window_hours: float = 24.0
    min_samples: int = 50

def build_tuning_report(inp: TuningInput) -> Dict[str, Any]:
    now = time.time()
    window_hours = float(inp.window_hours)
    since_ts = 0.0
    if window_hours > 0:
        since_ts = now - (window_hours * 3600.0)

    files = _collect_ledger_files(inp.ledger_path)

    # data buckets
    slip_buy: List[float] = []
    slip_sell: List[float] = []

    age_buy: List[float] = []
    age_sell: List[float] = []

    # counts
    cnt: Dict[str, int] = {
        "ack": 0,
        "final": 0,
        "fill_buy": 0,
        "fill_sell": 0,
        "timeout": 0,
        "retry": 0,
        "entry_unresolved": 0,
        "exit_unresolved": 0,
        "slip_soft": 0,
        "slip_hard": 0,
        "poll_error": 0,
        "fsm_fatal": 0,
    }

    # uuid→ack_ts for fallback latency
    ack_ts: Dict[str, float] = {}

    # iterate
    for rec in _iter_records(files):
        ts = rec.get("ts")
        if not _is_number(ts):
            continue
        tsf = float(ts)
        if since_ts and tsf < since_ts:
            continue

        event = str(rec.get("event") or "")
        data = rec.get("data")
        if not isinstance(data, dict):
            data = {}

        # ack
        if event == "ORDER_ACK":
            uuid = str(data.get("uuid") or "")
            if uuid:
                ack_ts.setdefault(uuid, tsf)
            cnt["ack"] += 1
            continue

        # final (preferred for latency)
        if event == "ORDER_FINAL":
            cnt["final"] += 1
            side = str(data.get("side") or "").lower()

            age = data.get("age_sec")
            if _is_number(age):
                if side == "bid":
                    age_buy.append(float(age))
                elif side == "ask":
                    age_sell.append(float(age))

            slip = data.get("slippage_bps")
            if _is_number(slip):
                if side == "bid":
                    slip_buy.append(float(slip))
                elif side == "ask":
                    slip_sell.append(float(slip))
            continue

        # fills (fallback latency + slippage)
        if event == "FILL_BUY":
            cnt["fill_buy"] += 1
            slip = data.get("slippage_bps")
            if _is_number(slip):
                slip_buy.append(float(slip))
            uuid = str(data.get("uuid") or "")
            if uuid and uuid in ack_ts:
                age_buy.append(max(0.0, tsf - float(ack_ts[uuid])))
            continue

        if event == "FILL_SELL":
            cnt["fill_sell"] += 1
            slip = data.get("slippage_bps")
            if _is_number(slip):
                slip_sell.append(float(slip))
            uuid = str(data.get("uuid") or "")
            if uuid and uuid in ack_ts:
                age_sell.append(max(0.0, tsf - float(ack_ts[uuid])))
            continue

        # other counters
        if event == "ORDER_TIMEOUT":
            cnt["timeout"] += 1
            continue
        if event == "ORDER_RETRY":
            cnt["retry"] += 1
            continue
        if event == "ENTRY_UNRESOLVED":
            cnt["entry_unresolved"] += 1
            continue
        if event == "EXIT_UNRESOLVED":
            cnt["exit_unresolved"] += 1
            continue
        if event == "SLIPPAGE_SOFT_BREACH":
            cnt["slip_soft"] += 1
            continue
        if event == "SLIPPAGE_HARD_BREACH":
            cnt["slip_hard"] += 1
            continue
        if event == "ORDER_POLL_ERROR":
            cnt["poll_error"] += 1
            continue
        if event in ("ORDER_FSM_FATAL", "TICK_LOOP_FATAL"):
            cnt["fsm_fatal"] += 1
            continue

    # summarize
    slip_combined = slip_buy + slip_sell
    age_combined = age_buy + age_sell

    slip_buy_s = _summarize(slip_buy)
    slip_sell_s = _summarize(slip_sell)
    slip_all_s = _summarize(slip_combined)

    age_buy_s = _summarize(age_buy)
    age_sell_s = _summarize(age_sell)
    age_all_s = _summarize(age_combined)

    # --------------------------------------------------------
    # Recommendations (conservative / low-barrier defaults)
    # --------------------------------------------------------
    min_samples = max(1, int(inp.min_samples))

    # current env snapshot
    current_env = {
        "OMA_SLIPPAGE_SOFT_BPS": _safe_float_env("OMA_SLIPPAGE_SOFT_BPS"),
        "OMA_SLIPPAGE_HARD_BPS": _safe_float_env("OMA_SLIPPAGE_HARD_BPS"),
        "OMA_ORDER_TIMEOUT_SEC": _safe_float_env("OMA_ORDER_TIMEOUT_SEC"),
        "OMA_ORDER_TIMEOUT_SEC_BUY": _safe_float_env("OMA_ORDER_TIMEOUT_SEC_BUY"),
        "OMA_ORDER_TIMEOUT_SEC_SELL": _safe_float_env("OMA_ORDER_TIMEOUT_SEC_SELL"),
        "OMA_ORDER_MAX_RETRIES": _safe_int_env("OMA_ORDER_MAX_RETRIES"),
        "OMA_ORDER_MAX_RETRIES_BUY": _safe_int_env("OMA_ORDER_MAX_RETRIES_BUY"),
        "OMA_ORDER_MAX_RETRIES_SELL": _safe_int_env("OMA_ORDER_MAX_RETRIES_SELL"),
    }

    recommend: Dict[str, Any] = {}
    notes: List[str] = []

    # slippage thresholds
    if slip_all_s["n"] >= min_samples and slip_all_s["p95"] is not None and slip_all_s["p99"] is not None:
        p95 = float(slip_all_s["p95"])
        p99 = float(slip_all_s["p99"])

        # soft: p95 + small buffer
        soft = max(50.0, p95 + 10.0)
        # hard: p99 + buffer, and always above soft by margin
        hard = max(soft + 50.0, p99 + 20.0)

        recommend["OMA_SLIPPAGE_SOFT_BPS"] = int(round(soft))
        recommend["OMA_SLIPPAGE_HARD_BPS"] = int(round(hard))
    else:
        notes.append("slippage samples insufficient: keep current OMA_SLIPPAGE_* or defaults")

    # order timeout
    # prefer per-side values if enough samples
    def _timeout_from_summary(s: Dict[str, Any], *, floor: float = 6.0) -> Optional[float]:
        if s.get("n", 0) < min_samples:
            return None
        p99v = s.get("p99")
        if p99v is None:
            return None
        # p99 + 1s buffer (reduces false timeouts)
        return max(float(floor), float(p99v) + 1.0)

    t_buy = _timeout_from_summary(age_buy_s)
    t_sell = _timeout_from_summary(age_sell_s)
    t_all = _timeout_from_summary(age_all_s)

    if t_buy is not None:
        recommend["OMA_ORDER_TIMEOUT_SEC_BUY"] = round(float(t_buy), 2)
    if t_sell is not None:
        recommend["OMA_ORDER_TIMEOUT_SEC_SELL"] = round(float(t_sell), 2)

    if t_buy is None and t_sell is None and t_all is not None:
        recommend["OMA_ORDER_TIMEOUT_SEC"] = round(float(t_all), 2)
    elif t_buy is None and t_sell is None:
        notes.append("latency samples insufficient: keep current OMA_ORDER_TIMEOUT_* or defaults")

    # retries
    # 기본 철학:
    # - buy(entry): 과도한 재시도는 '진입 방벽'보다는 '중복 위험'을 키울 수 있어 2를 기본
    # - sell(exit): 미청산은 치명적이므로 필요하면 3까지(다만 무한은 금지)
    exit_unresolved = int(cnt.get("exit_unresolved") or 0)
    timeout_cnt = int(cnt.get("timeout") or 0)
    final_cnt = int(cnt.get("final") or (cnt.get("fill_buy", 0) + cnt.get("fill_sell", 0)))

    # retry rate rough
    retry_cnt = int(cnt.get("retry") or 0)
    retry_rate = (float(retry_cnt) / float(max(1, final_cnt)))

    # buy retries
    buy_retries = 2
    # if retries are exploding, keep buy retries low (not a barrier; avoids repeated entry churn)
    if retry_rate > 0.25:
        buy_retries = 1
        notes.append("high retry rate detected: suggest reducing BUY retries")

    # sell retries
    sell_retries = 2
    if exit_unresolved > 0:
        sell_retries = 3
        notes.append("EXIT_UNRESOLVED detected: suggest increasing SELL retries to 3")

    recommend["OMA_ORDER_MAX_RETRIES_BUY"] = int(buy_retries)
    recommend["OMA_ORDER_MAX_RETRIES_SELL"] = int(sell_retries)

    # safety flags
    danger: List[str] = []
    if cnt.get("slip_hard", 0) > 0:
        danger.append("hard_slippage_breach")
    if cnt.get("exit_unresolved", 0) > 0:
        danger.append("exit_unresolved")
    if cnt.get("fsm_fatal", 0) > 0:
        danger.append("fsm_fatal")

    report: Dict[str, Any] = {
        "ok": True,
        "ledger": {
            "path": inp.ledger_path,
            "files": files,
        },
        "window": {
            "hours": window_hours,
            "since_ts": since_ts,
            "until_ts": now,
        },
        "counts": cnt,
        "slippage_bps": {
            "buy": slip_buy_s,
            "sell": slip_sell_s,
            "combined": slip_all_s,
        },
        "latency_sec": {
            "buy": age_buy_s,
            "sell": age_sell_s,
            "combined": age_all_s,
        },
        "current_env": current_env,
        "recommend_env": recommend,
        "danger_flags": danger,
        "notes": notes,
    }

    return report

def export_recommended_env(report: Dict[str, Any]) -> str:
    rec = report.get("recommend_env")
    if not isinstance(rec, dict) or not rec:
        return "# no recommendations (insufficient samples)\n"

    lines: List[str] = []
    lines.append("# ------------------------------------------------------------")
    lines.append("# Autocoin OS v3-H — empirical tuning (suggested)")
    lines.append("# Generated from trade_ledger.jsonl")
    lines.append("# NOTE: Do NOT blindly trust. Apply gradually and monitor.")
    lines.append("# ------------------------------------------------------------")

    # stable ordering
    keys = [
        "OMA_SLIPPAGE_SOFT_BPS",
        "OMA_SLIPPAGE_HARD_BPS",
        "OMA_ORDER_TIMEOUT_SEC",
        "OMA_ORDER_TIMEOUT_SEC_BUY",
        "OMA_ORDER_TIMEOUT_SEC_SELL",
        "OMA_ORDER_MAX_RETRIES",
        "OMA_ORDER_MAX_RETRIES_BUY",
        "OMA_ORDER_MAX_RETRIES_SELL",
    ]

    for k in keys:
        if k in rec and rec[k] is not None:
            lines.append(f"{k}={rec[k]}")

    # add comments about flags
    danger = report.get("danger_flags")
    if isinstance(danger, list) and danger:
        lines.append("")
        lines.append("# Danger flags observed:")
        for d in danger:
            lines.append(f"# - {d}")

    notes = report.get("notes")
    if isinstance(notes, list) and notes:
        lines.append("")
        lines.append("# Notes:")
        for n in notes:
            lines.append(f"# - {n}")

    return "\n".join(lines) + "\n"
