# ============================================================
# Spot GAZUA — 현물 교차 관제(Cross-Exchange Control) API Router
# ------------------------------------------------------------
# 한 박스 안에서 도는 현물 3거래소(업비트/빗썸/바이비트) GAZUA manager 의
# 차단 관제(near-miss 사후판정 + 게이트 통계)를 단일 응답으로 통합한다.
# 선물 strategy_focus_router /peer-cache 의 현물판 — 단, 옆 "서버"가 아니라
# 같은 서버의 거래소별 manager 를 모은다.
#
# ★ 100% 로컬 — 옆 서버 폴링·CF Access·PEER_BRIEF_TOKEN 같은 새 인증문 0개.
#   거래소 Tick 신성불가침(서버간 Tick 무관). 관측 전용 · 진입 1바이트 불침.
#   각 manager.get_near_miss_enriched() 가 이미 25s kline 캐시 → 여기 통합캐시는 이중방어.
# ============================================================
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy/spot_gazua_cross", tags=["SPOT_GAZUA_CROSS"])

# (system 속성, key, 표시 라벨, 견적통화) — 거래소별 GAZUA manager 매핑.
_EXCHANGES = [
    ("upbit_gazua_manager", "upbit", "업비트", "₩"),
    ("bithumb_gazua_manager", "bithumb", "빗썸", "₩"),
    ("bybit_spot_gazua_manager", "bybit_spot", "바이비트", "USDT"),
    ("binance_spot_gazua_manager", "binance", "바이낸스", "USDT"),  # 2026-06-23 연결 — BinanceSpotGazuaManager. 미설정 서버엔 present:false(흐림), BINANCE_SPOT_FOCUS_ENABLED+키 시 자동 점등.
]

# 통합 응답 캐시 — 3 탭 동시 폴링이 manager near-miss enrich(kline) 를 중복 트리거 않게.
_BOX: Dict[str, Any] = {"ts": 0.0, "data": None}
_TTL = 15.0


def _exchange_brief(um, key: str, label: str, quote: str) -> Dict[str, Any]:
    """단일 거래소 manager → 관제 brief. manager 없거나 일부 실패해도 안전한 부분응답."""
    ex: Dict[str, Any] = {
        "key": key, "label": label, "quote": quote, "present": um is not None,
        "enabled": False, "paper": True, "contrarian_enabled": False,
        "near_miss": [], "gate_stats": None,
    }
    if um is None:
        return ex
    try:
        ex["enabled"] = bool(getattr(um.config, "enabled", False))
        ex["paper"] = bool(getattr(um.config, "paper", True))
        ex["contrarian_enabled"] = bool(getattr(um.config, "contrarian_enabled", False))
    except Exception:  # noqa: BLE001
        pass
    try:
        ex["near_miss"] = um.get_near_miss_enriched()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SPOT_CROSS] %s near-miss skip: %s", key, exc)
    try:
        gl = getattr(um, "_gate_ledger", None)
        if gl is not None and getattr(um.config, "gate_ledger_enabled", False):
            ex["gate_stats"] = gl.snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SPOT_CROSS] %s gate-stats skip: %s", key, exc)
    return ex


@router.get("/control")
def spot_cross_control(request: Request):
    """현물 3거래소 차단 관제 통합 — near-miss 시계열 사후판정 + 게이트별 차단 품질 +
    거래소별 보수성 비교 재료. 관측 전용 · 진입 무관 · 100% 로컬."""
    now = time.time()
    if _BOX.get("data") is not None and (now - float(_BOX.get("ts") or 0.0)) < _TTL:
        return _BOX["data"]

    system = request.app.state.system
    exchanges: List[Dict[str, Any]] = []
    for attr, key, label, quote in _EXCHANGES:
        # getattr 만 — 없으면 None(관제 패널이 manager 를 새로 띄우는 부수효과 방지).
        um = getattr(system, attr, None)
        exchanges.append(_exchange_brief(um, key, label, quote))

    data = {"ok": True, "ts": now, "exchanges": exchanges}
    _BOX["ts"] = now
    _BOX["data"] = data
    return data
