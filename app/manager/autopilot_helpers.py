# ============================================================
# File: app/manager/autopilot_helpers.py
# Autocoin OS v3-H — Autopilot pure utility functions
# Phase 3-A: Extracted from autopilot_manager.py
# ============================================================

from __future__ import annotations
from typing import Any, List


def normalize_strategy_name(raw: Any) -> str:
    """Normalize strategy name. e.g. SNIPER(S)/SNIPERS → SNIPER."""
    s = str(raw or "").strip().upper()
    if s in ("SNIPER(S)", "SNIPERS"):
        return "SNIPER"
    return s


def extract_row_strategy(row: Any) -> str:
    """Extract strategy name from an OMA row. Prefer the strategy field; otherwise parse the STRATEGY: prefix from reason."""
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
    """Infer strategy from a list of reason strings."""
    for r in reasons:
        if isinstance(r, str) and r.upper().startswith("STRATEGY:"):
            return r.split(":", 1)[1].strip().upper() or "UNKNOWN"
    return "UNKNOWN"
