# ============================================================
# File: app/api/am_performance_router.py
# Autocoin OS v3-H — Auto/Manual Performance Report API
# ============================================================

from __future__ import annotations

from fastapi import APIRouter, Request, Query

from app.manager.am_performance import build_auto_manual_report

import logging
logger = logging.getLogger(__name__)



router = APIRouter(prefix="/api/report", tags=["report"])


@router.get("/auto-manual")
def auto_manual_report(
    request: Request,
    since_hours: float = Query(168.0, description="Hours to look back"),
    tail_lines: int = Query(50000, description="Max ledger lines to scan"),
):
    system = request.app.state.system
    try:
        return build_auto_manual_report(system, since_hours=since_hours, tail_lines=tail_lines)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("am_performance_router.auto_manual_report L29: %s", exc)
        return {"ok": False, "error": str(exc)}
