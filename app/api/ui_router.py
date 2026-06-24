# ============================================================
# File: app/api/ui_router.py
# Autocoin OS v3-H — UI Router (Final)
# ============================================================

from typing import Dict, Any
from fastapi import APIRouter

from app.core.currency import Q

router = APIRouter(tags=["ui"])

# NOTE: Root redirect ("/") is defined in app/main.py to avoid duplicate operation_id


@router.get("/api/ui/config", summary="UI settings", description="Settings info for the UI (quote currency, etc.)")
def ui_config() -> Dict[str, Any]:
    """Return the settings info needed to initialize the UI.

    Returns:
        quote_currency: current quote currency settings (symbol, decimals, min_order, etc.)
    """
    return {
        "ok": True,
        "quote_currency": Q.to_dict(),
    }
