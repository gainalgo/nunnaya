# ============================================================
# File: app/api/triage_router.py
# Autocoin OS v3-H — Triage Mode API Router
# ============================================================

from fastapi import APIRouter, HTTPException, Request
from typing import Dict, Any, Optional
from pydantic import BaseModel

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/triage", tags=["triage"])

# ============================================================
# Request Models
# ============================================================

class EnterTriageRequest(BaseModel):
    reason: str = "manual"
    trigger_pnl_pct: Optional[float] = None       # 임시 오버라이드 (None=ENV 값 사용)
    profit_target_pct: Optional[float] = None
    max_dca_ratio: Optional[float] = None

class SettingsPatchRequest(BaseModel):
    enabled: Optional[bool] = None
    trigger_pnl_pct: Optional[float] = None
    trigger_loss_count: Optional[int] = None
    max_dca_ratio: Optional[float] = None
    profit_target_pct: Optional[float] = None
    max_loss_exclude_pct: Optional[float] = None
    dca_interval_sec: Optional[float] = None
    max_duration_hours: Optional[float] = None
    coin_timeout_hours: Optional[float] = None
    exit_pnl_pct: Optional[float] = None
    notify: Optional[bool] = None
    buy_mode: Optional[str] = None          # "block_all" | "allow_non_loss"
    opportunistic_dca: Optional[bool] = None  # 손실 코인 조건부 즉시 DCA
    market_recovery_exit_enabled: Optional[bool] = None   # 시장 회복 자동 해제
    market_recovery_min_hours: Optional[float] = None     # 자동 해제 최소 경과 시간
    loss_grace_min: Optional[float] = None                # 매수 후 N분 이내 손실 카운트 제외
    max_concurrent_targets: Optional[int] = None          # 병렬 복구 동시 타겟 수
    recovery_target: Optional[str] = None                 # ALL / 0.6 / 3 등
    emergency_exit_enabled: Optional[bool] = None         # 긴급 탈출 모드
    emergency_moderate_avg_loss_pct: Optional[float] = None  # 경고 임계값 (기본 -10%)
    emergency_severe_avg_loss_pct: Optional[float] = None    # 긴급 임계값 (기본 -30%)
    # [2026-06-01] GET settings 엔 있으나 PATCH 미노출이던 누락 연결 (patch_settings 가 tm.settings 키면 적용)
    global_dca_cap_pct: Optional[float] = None               # 전체 DCA 합산 포트폴리오 % 캡
    focus_dca_allow: Optional[bool] = None                   # 포커스 마켓 PRM 우회 허용
    sell_timeout_sec: Optional[float] = None                 # TRIAGE_SELL 타임아웃
    min_position_usdt: Optional[float] = None                # 먼지 제외 기준

class SkipRequest(BaseModel):
    reason: str = "manual skip"
    market: Optional[str] = None   # 특정 타겟 스킵 (None이면 첫 번째)

# ============================================================
# Endpoints
# ============================================================

@router.get("/status")
async def get_triage_status(request: Request) -> Dict[str, Any]:
    """
    트리아지 모드 현재 상태 조회

    Returns:
        - state: 현재 상태 (NORMAL / TRIAGE_INIT / TRIAGE_SCAN / TRIAGE_DCA / TRIAGE_WAIT / TRIAGE_SELL / TRIAGE_EXIT)
        - active: 트리아지 활성 여부
        - current_target: 현재 집중 복구 중인 마켓
        - recovered: 복구 완료 마켓 목록
        - skipped: 건너뛴 마켓 목록
        - trigger_reason: 활성화 사유
        - elapsed_sec: 활성 후 경과 시간
        - settings: 현재 설정값
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)
        if tm is None:
            return {"state": "NORMAL", "active": False, "message": "TriageManager not initialized"}
        return tm.get_status_dict()
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("triage_router.unknown L83: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/enter")
async def enter_triage(request: Request, req: EnterTriageRequest) -> Dict[str, Any]:
    """
    트리아지 모드 수동 진입

    OMA_TRIAGE_ENABLED=0 이어도 수동 진입은 허용됨.
    enabled 설정은 '자동 트리거 비활성'이지 '수동 진입 불가'가 아님.
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)
        if tm is None:
            raise HTTPException(status_code=503, detail="TriageManager not initialized")

        from app.manager.triage_manager import TriageManager
        if tm.state != TriageManager.STATE_NORMAL:
            return {
                "success": False,
                "message": f"Already in triage mode: {tm.state}",
                "state": tm.state
            }

        # 파라미터 임시 오버라이드 적용
        overrides = {}
        if req.trigger_pnl_pct is not None:
            overrides["trigger_pnl_pct"] = req.trigger_pnl_pct
        if req.profit_target_pct is not None:
            overrides["profit_target_pct"] = req.profit_target_pct
        if req.max_dca_ratio is not None:
            overrides["max_dca_ratio"] = req.max_dca_ratio

        if overrides:
            tm.settings.update(overrides)

        tm.enter_triage(system, reason=req.reason)

        return {
            "success": True,
            "message": f"Triage mode activated: {req.reason}",
            "state": tm.state,
            "current_target": tm.current_target,
            "active_targets": len(tm.active_targets),
        }
    except HTTPException:
        logger.warning("triage_router.unknown L130 except", exc_info=True)
        raise
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("triage_router.unknown L132: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/exit")
async def exit_triage(request: Request) -> Dict[str, Any]:
    """
    트리아지 모드 수동 해제

    복구 목표 미달성 상태에서도 강제 종료.
    BUY 차단 해제, 예산 복원, 상태 초기화.
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)
        if tm is None:
            raise HTTPException(status_code=503, detail="TriageManager not initialized")

        from app.manager.triage_manager import TriageManager
        if tm.state == TriageManager.STATE_NORMAL:
            return {"success": False, "message": "Triage mode is not active"}

        tm.exit_triage(system, reason="manual exit by operator")

        return {
            "success": True,
            "message": "Triage mode deactivated",
            "state": tm.state,
            "recovered": tm.recovered,
            "skipped": tm.skipped,
        }
    except HTTPException:
        logger.warning("triage_router.unknown L163 except", exc_info=True)
        raise
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("triage_router.unknown L165: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/skip")
async def skip_current_target(request: Request, req: SkipRequest) -> Dict[str, Any]:
    """
    현재 집중 복구 중인 마켓 건너뛰기

    현재 포커스 마켓을 skipped 목록으로 이동하고 다음 마켓으로 진행.
    TRIAGE_SCAN 상태로 전환하여 다음 타겟 선택.
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)
        if tm is None:
            raise HTTPException(status_code=503, detail="TriageManager not initialized")

        from app.manager.triage_manager import TriageManager
        if tm.state == TriageManager.STATE_NORMAL:
            return {"success": False, "message": "Triage mode is not active"}

        if not tm.active_targets:
            return {"success": False, "message": "No active target to skip"}

        # 특정 마켓 지정 시 해당 타겟 스킵, 미지정 시 첫 번째
        if req.market:
            target = tm._find_target(req.market)
            if not target:
                return {"success": False, "message": f"Market {req.market} not in active targets"}
            tm.skip_target(target, system, reason=req.reason)
            skipped_market = req.market
        else:
            skipped_market = tm.active_targets[0].get("market", "?")
            tm.skip_target(tm.active_targets[0], system, reason=req.reason)

        return {
            "success": True,
            "message": f"Skipped {skipped_market}",
            "skipped_market": skipped_market,
            "state": tm.state,
            "active_targets": len(tm.active_targets),
            "skipped": tm.skipped,
        }
    except HTTPException:
        logger.warning("triage_router.unknown L209 except", exc_info=True)
        raise
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
        logger.warning("triage_router.unknown L211: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/portfolio-loss")
async def get_portfolio_loss(request: Request) -> Dict[str, Any]:
    """
    현재 포트폴리오 손실 현황

    Returns:
        - total_loss_pct: 전체 미실현 손실 %
        - markets: 마켓별 손실 상세 (loss_pct, loss_usdt, val_usdt, qty, avg_buy_price, current_price)
        - loss_coin_count: 손실 중인 코인 수
        - triage_trigger_threshold: 트리아지 발동 임계값
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)

        # PRM에서 전체 손실률 가져오기
        prm = getattr(system, "portfolio_risk_manager", None)
        total_loss_pct = 0.0
        if prm and prm.daily_status:
            total_loss_pct = prm.daily_status.loss_pct

        # 마켓별 손실 상세 계산
        markets_detail = {}
        try:
            from app.core.hyper_price_store import price_store
            active_markets = system.oma_registry.list_active()
            for m in active_markets:
                ctx = system.coordinator.get_context(m)
                if not ctx or not getattr(ctx, "position", None):
                    continue
                qty = float(ctx.position.get("qty", 0.0) or 0.0)
                avg_buy = float(ctx.position.get("entry", 0.0) or 0.0)
                if avg_buy <= 0 or qty <= 0:
                    continue
                current = price_store.get_price(m) or avg_buy
                val_usdt = qty * current
                upnl = (current - avg_buy) * qty
                upnl_pct = (current - avg_buy) / avg_buy * 100
                if upnl_pct < 0:
                    markets_detail[m] = {
                        "loss_pct": round(upnl_pct, 2),
                        "loss_usdt": round(upnl, 0),
                        "val_usdt": round(val_usdt, 0),
                        "qty": qty,
                        "avg_buy_price": avg_buy,
                        "current_price": current,
                    }
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[triage_router] %s: %s", '마켓별 손실 상세 계산', exc, exc_info=True)

        triage_trigger = tm.settings.get("trigger_pnl_pct", -5.0) if tm else -5.0

        return {
            "total_loss_pct": round(total_loss_pct, 2),
            "triage_trigger_threshold": triage_trigger,
            "loss_coin_count": len(markets_detail),
            "markets": dict(sorted(markets_detail.items(), key=lambda x: x[1]["loss_pct"])),
        }
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
        logger.warning("triage_router.unknown L273: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/settings")
async def patch_settings(request: Request, req: SettingsPatchRequest) -> Dict[str, Any]:
    """
    트리아지 설정 런타임 수정

    트리아지 활성 중에도 즉시 적용됨.
    변경된 값만 전달 (None인 항목은 현재 값 유지).
    """
    try:
        system = request.app.state.system
        tm = getattr(system, "triage_manager", None)
        if tm is None:
            raise HTTPException(status_code=503, detail="TriageManager not initialized")

        changes = {}
        patch_data = req.model_dump(exclude_none=True)
        for key, val in patch_data.items():
            if key in tm.settings:
                tm.settings[key] = val
                changes[key] = val

        if not changes:
            return {"success": False, "message": "No valid settings to update"}

        # state 파일 갱신 (settings 스냅샷 포함, 재시작 시에는 ENV 우선)
        tm.save_state()

        return {
            "success": True,
            "message": f"Updated {len(changes)} setting(s)",
            "changes": changes,
            "current_settings": {k: v for k, v in tm.settings.items() if k != "fee_pct"},
        }
    except HTTPException:
        logger.warning("triage_router.unknown L310 except", exc_info=True)
        raise
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("triage_router.unknown L312: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
