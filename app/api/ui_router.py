# ============================================================
# File: app/api/ui_router.py
# Autocoin OS v3-H — UI Router (Final)
# ============================================================

from typing import Dict, Any
from fastapi import APIRouter

from app.core.currency import Q

router = APIRouter(tags=["ui"])

# NOTE: Root redirect ("/") is defined in app/main.py to avoid duplicate operation_id


@router.get("/api/ui/config", summary="UI 설정", description="UI에서 사용할 설정 정보 (기축통화 등)")
def ui_config() -> Dict[str, Any]:
    """UI 초기화에 필요한 설정 정보를 반환합니다.
    
    Returns:
        quote_currency: 현재 기축통화 설정 (symbol, decimals, min_order 등)
    """
    return {
        "ok": True,
        "quote_currency": Q.to_dict(),
    }
