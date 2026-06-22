# ============================================================
# File: app/api/peer_brief_router.py
# Autocoin OS — Peer Brief Router (옆 서버 공유 endpoint)
# ------------------------------------------------------------
# GET  /peer/brief     — 옆 서버가 polling 하는 자기 brief
# GET  /peer/cache     — 현재 캐시 상태 (디버그/대시보드)
# GET  /peer/settings  — UI 가 표시할 현재 설정 (runtime + env)
# POST /peer/settings  — UI 가 저장. runtime store + polling 즉시 재시작.
# ============================================================
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/peer", tags=["PEER"])


@router.get("/brief")
async def get_brief(request: Request):
    """자기 서버의 brief — 최근 손실/보유/health."""
    from app.core.peer_brief import build_my_brief, get_peer_token

    # 옵션 token 인증 (양쪽 서버에 같은 PEER_BRIEF_TOKEN 설정 시)
    tok = get_peer_token()
    if tok:
        provided = (request.headers.get("x-peer-token") or "").strip()
        if provided != tok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    system = getattr(request.app.state, "system", None)
    brief = build_my_brief(system=system)
    return brief.as_dict()


@router.get("/cache")
async def get_cache(request: Request):  # noqa: ARG001 — request unused, FastAPI 의존
    """현재 peer 캐시 상태 (디버그)."""
    from app.core.peer_brief import get_cache_snapshot
    return get_cache_snapshot()


@router.get("/settings")
async def get_settings(request: Request):  # noqa: ARG001
    """UI 표시용 — runtime override 값 + env defaults."""
    from app.core.peer_brief import (
        is_enabled, is_paper, get_peer_urls, get_server_id,
        get_sl_window_min, get_poll_interval_sec, get_timeout_sec,
        get_peer_token, get_cache_snapshot, get_state,
    )
    from app.core.peer_brief import get_peer_win_window_min, get_peer_win_bonus
    from app.core.peer_brief import (
        get_peer_sl_penalty, get_peer_struggle_penalty,
        get_peer_struggle_age_min, get_peer_struggle_peak_pct,
    )
    from app.core.peer_brief import (
        get_peer_conflict_penalty, get_peer_crowding_penalty, get_peer_crowding_cap,
    )
    from app.core.peer_brief import (
        get_peer_fleet_dirfail_enabled, get_peer_fleet_dirfail_max,
        get_peer_fleet_dirfail_window_min,
    )
    return {
        "enabled": is_enabled(),
        "paper": is_paper(),
        "urls": get_peer_urls(),
        "server_id": get_server_id(),
        "sl_window_min": get_sl_window_min(),
        "poll_interval_sec": get_poll_interval_sec(),
        "timeout_sec": get_timeout_sec(),
        "peer_win_window_min": get_peer_win_window_min(),
        "peer_win_bonus": get_peer_win_bonus(),
        "peer_sl_penalty": get_peer_sl_penalty(),
        "peer_struggle_penalty": get_peer_struggle_penalty(),
        "peer_struggle_age_min": get_peer_struggle_age_min(),
        "peer_struggle_peak_pct": get_peer_struggle_peak_pct(),
        "peer_conflict_penalty": get_peer_conflict_penalty(),
        # 🌊 함대 몰림(crowding) — 옆 같은 코인+같은 방향 보유 수 × 감점 — 2026-06-15
        "peer_crowding_penalty": get_peer_crowding_penalty(),
        "peer_crowding_cap": get_peer_crowding_cap(),
        # 🛡️ 함대 dir_fail (같은 코인·방향 누적 차단) — 2026-06-11
        "fleet_dirfail_enabled": get_peer_fleet_dirfail_enabled(),
        "fleet_dirfail_max": get_peer_fleet_dirfail_max(),
        "fleet_dirfail_window_min": get_peer_fleet_dirfail_window_min(),
        "token_set": bool(get_peer_token()),
        # 정보용 — UI 가 "runtime override 적용 중" vs "env 사용 중" 구분 위해
        "state_keys": sorted(get_state().keys()),
        "env_urls_raw": (os.getenv("PEER_SERVER_URLS", "") or ""),
        "env_server_id": (os.getenv("PEER_SERVER_ID", "") or ""),
        # 현재 polling 상태 (한 호출로 같이 받기)
        "cache": get_cache_snapshot(),
    }


@router.post("/settings")
async def post_settings(request: Request):
    """UI 저장 → runtime/peer_brief_state.json + polling 즉시 재시작."""
    from app.core.peer_brief import (
        update_state, start_poll_loop, stop_poll_loop, get_cache_snapshot,
    )
    try:
        body = await request.json()
    except (ValueError, RuntimeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    patch = {}
    if "enabled" in body:
        patch["enabled"] = bool(body.get("enabled"))
    if "paper" in body:
        patch["paper"] = bool(body.get("paper"))
    if "server_id" in body:
        patch["server_id"] = str(body.get("server_id") or "").strip()
    if "urls" in body:
        raw = body.get("urls")
        if isinstance(raw, list):
            urls = [str(u).strip().rstrip("/") for u in raw if str(u).strip()]
        else:
            urls = [u.strip().rstrip("/") for u in str(raw or "").split(",") if u.strip()]
        patch["urls"] = urls
    if "poll_interval_sec" in body:
        try:
            v = float(body.get("poll_interval_sec"))
            if v >= 2 and v <= 3600:
                patch["poll_interval_sec"] = v
        except (TypeError, ValueError):
            pass
    if "sl_window_min" in body:
        try:
            v = int(float(body.get("sl_window_min")))
            if v >= 1 and v <= 1440:
                patch["sl_window_min"] = v
        except (TypeError, ValueError):
            pass
    if "peer_win_window_min" in body:
        try:
            v = int(float(body.get("peer_win_window_min")))
            if v >= 1 and v <= 1440:
                patch["peer_win_window_min"] = v
        except (TypeError, ValueError):
            pass
    if "peer_win_bonus" in body:
        try:
            v = float(body.get("peer_win_bonus"))
            if v >= 0 and v <= 50:
                patch["peer_win_bonus"] = v
        except (TypeError, ValueError):
            pass
    if "peer_sl_penalty" in body:
        try:
            v = float(body.get("peer_sl_penalty"))
            if 0 <= v <= 50:
                patch["peer_sl_penalty"] = v
        except (TypeError, ValueError):
            pass
    if "peer_struggle_penalty" in body:
        try:
            v = float(body.get("peer_struggle_penalty"))
            if 0 <= v <= 50:
                patch["peer_struggle_penalty"] = v
        except (TypeError, ValueError):
            pass
    if "peer_conflict_penalty" in body:
        try:
            v = float(body.get("peer_conflict_penalty"))
            if 0 <= v <= 50:
                patch["peer_conflict_penalty"] = v
        except (TypeError, ValueError):
            pass
    if "peer_struggle_age_min" in body:
        try:
            v = float(body.get("peer_struggle_age_min"))
            if 1 <= v <= 120:
                patch["peer_struggle_age_min"] = v
        except (TypeError, ValueError):
            pass
    if "peer_struggle_peak_pct" in body:
        try:
            v = float(body.get("peer_struggle_peak_pct"))
            if 0 <= v <= 5:
                patch["peer_struggle_peak_pct"] = v
        except (TypeError, ValueError):
            pass
    # 🌊 함대 몰림(crowding) — 2026-06-15
    if "peer_crowding_penalty" in body:
        try:
            v = float(body.get("peer_crowding_penalty"))
            if 0 <= v <= 50:
                patch["peer_crowding_penalty"] = v
        except (TypeError, ValueError):
            pass
    if "peer_crowding_cap" in body:
        try:
            v = float(body.get("peer_crowding_cap"))
            if 1 <= v <= 50:
                patch["peer_crowding_cap"] = v
        except (TypeError, ValueError):
            pass
    # 🛡️ 함대 dir_fail (같은 코인·방향 누적 차단) — 2026-06-11
    if "fleet_dirfail_enabled" in body:
        patch["fleet_dirfail_enabled"] = bool(body.get("fleet_dirfail_enabled"))
    if "fleet_dirfail_max" in body:
        try:
            v = int(float(body.get("fleet_dirfail_max")))
            if 1 <= v <= 10:
                patch["fleet_dirfail_max"] = v
        except (TypeError, ValueError):
            pass
    if "fleet_dirfail_window_min" in body:
        try:
            v = int(float(body.get("fleet_dirfail_window_min")))
            if 1 <= v <= 1440:
                patch["fleet_dirfail_window_min"] = v
        except (TypeError, ValueError):
            pass

    if not patch:
        return JSONResponse({"ok": False, "error": "no_fields_provided"}, status_code=400)

    new_state = update_state(patch)

    # polling 즉시 재시작 (새 URLs / enabled 즉시 반영)
    try:
        stop_poll_loop()
        start_poll_loop()
    except Exception as exc:
        logger.warning("[PEER] poll restart after settings save failed: %s", exc)

    return {
        "ok": True,
        "saved": new_state,
        "cache": get_cache_snapshot(),
    }
