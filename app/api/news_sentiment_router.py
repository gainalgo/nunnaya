"""
News Sentiment API Router

Endpoints:
    GET  /api/news-sentiment/status     - 전체 상태 + 감성 + 헤드라인
    GET  /api/news-sentiment/coin/{c}   - 특정 코인 감성
    POST /api/news-sentiment/config     - ON/OFF 토글 + 설정 변경
    POST /api/news-sentiment/refresh    - 캐시 무시 강제 갱신
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news-sentiment", tags=["NEWS_SENTIMENT"])


def _get_ns():
    """Get NewsSentiment singleton."""
    from app.core.news_sentiment import get_news_sentiment
    return get_news_sentiment()


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------

@router.get("/status")
def news_sentiment_status():
    """Current sentiment status + headlines + config."""
    ns = _get_ns()
    return {"ok": True, **ns.get_status()}


# ------------------------------------------------------------------
# Coin-specific
# ------------------------------------------------------------------

@router.get("/coin/{coin}")
def news_sentiment_coin(coin: str):
    """Get sentiment for a specific coin (e.g., BTC, ETH, gold)."""
    ns = _get_ns()
    result = ns.get_sentiment(coin=coin.upper())
    return {"ok": True, **result.to_dict()}


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

@router.post("/config")
def news_sentiment_config(
    focus_enabled: Optional[bool] = Query(None, description="FOCUS conviction 연동 ON/OFF"),
    nunnaya_enabled: Optional[bool] = Query(None, description="Nunnaya budget 연동 ON/OFF"),
    cache_sec: Optional[int] = Query(None, ge=60, le=7200, description="Cache duration (sec)"),
    api_key: Optional[str] = Body(None, description="CryptoPanic API key (POST body only)"),
):
    """Update news sentiment config. api_key는 POST body로만 전달 (URL 노출 방지)."""
    ns = _get_ns()
    patch = {}
    for k, v in {
        "focus_enabled": focus_enabled,
        "nunnaya_enabled": nunnaya_enabled,
        "api_key": api_key,
        "cache_sec": cache_sec,
    }.items():
        if v is not None:
            patch[k] = v

    if not patch:
        return {"ok": True, "config": ns.config, "message": "no changes"}

    result = ns.update_config(patch)
    # 민감 정보 마스킹 후 로그
    _safe = {k: ("****" if k == "api_key" else v) for k, v in patch.items()}
    logger.info("[NEWS] Config updated: %s", _safe)
    return {"ok": True, "config": result}


# ------------------------------------------------------------------
# Refresh
# ------------------------------------------------------------------

@router.post("/refresh")
def news_sentiment_refresh(
    coin: Optional[str] = Query("ALL", description="Coin to refresh (ALL for everything)"),
):
    """Force refresh sentiment data (bypass cache)."""
    ns = _get_ns()
    ns.clear_cache()
    result = ns.get_sentiment(coin=coin.upper() if coin else "ALL")
    logger.info("[NEWS] Force refresh: %s → score=%.3f", coin, result.score)
    return {"ok": True, **result.to_dict()}
