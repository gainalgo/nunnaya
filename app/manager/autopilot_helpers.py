# ============================================================
# File: app/manager/autopilot_helpers.py
# Autocoin OS v3-H — Autopilot pure utility functions
# Phase 3-A: Extracted from autopilot_manager.py
# ============================================================

from __future__ import annotations
from typing import Any, List


def normalize_strategy_name(raw: Any) -> str:
    """전략 이름 정규화. SNIPER(S)/SNIPERS → SNIPER 등."""
    s = str(raw or "").strip().upper()
    if s in ("SNIPER(S)", "SNIPERS"):
        return "SNIPER"
    return s


def extract_row_strategy(row: Any) -> str:
    """OMA row에서 전략 이름 추출. strategy 필드 우선, 없으면 reason에서 STRATEGY: 접두사 파싱."""
    if not isinstance(row, dict):
        return ""
    st = str(row.get("strategy") or "").strip().upper()
    if st:
        return st
    rs = row.get("reason")
    if isinstance(rs, list):
        for r in rs:
            if isinstance(r, str) and r.upper().startswith("STRATEGY:"):
                return r.split(":", 1)[1].strip().upper()
    return ""


def infer_strategy_from_reason(reasons: List[str]) -> str:
    """reason 리스트에서 전략 추론."""
    for r in reasons:
        if isinstance(r, str) and r.upper().startswith("STRATEGY:"):
            return r.split(":", 1)[1].strip().upper() or "UNKNOWN"
    return "UNKNOWN"
