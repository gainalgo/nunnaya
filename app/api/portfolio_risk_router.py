# ============================================================
# File: app/api/portfolio_risk_router.py
# Autocoin OS v3-H — Portfolio Risk API Router
# ============================================================

from fastapi import APIRouter, HTTPException, Request
from typing import Dict, Any, Optional
from pydantic import BaseModel

import logging
logger = logging.getLogger(__name__)


router = APIRouter(prefix="/portfolio-risk", tags=["portfolio-risk"])


# ============================================================
# Request Models
# ============================================================

class ManualPauseRequest(BaseModel):
    reason: str = "Manual pause by operator"


class ResetDailyStatusRequest(BaseModel):
    new_capital: float


class UpdateCorrelationGroupsRequest(BaseModel):
    market_sectors: Dict[str, str]  # {market: sector}


# ============================================================
# Endpoints
# ============================================================

@router.get("/status")
async def get_portfolio_risk_status(request: Request) -> Dict[str, Any]:
    """
    포트폴리오 리스크 관리 상태 조회
    
    Returns:
        - enabled: 리스크 관리 활성화 여부
        - can_enter_new_position: 신규 진입 가능 여부
        - entry_block_reason: 진입 차단 사유 (차단 시)
        - daily_status: 일일 리스크 상태 (손익, 손실률, 일시정지 등)
        - circuit_breaker: Circuit Breaker 상태
        - correlation_guard: 상관관계 가드 상태
        - thresholds: 설정된 임계값
    """
    try:
        system = request.app.state.system
        prm = system.portfolio_risk_manager
        return prm.get_status_summary()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("portfolio_risk_router.unknown L55: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/manual-pause")
async def manual_pause(request: Request, req: ManualPauseRequest) -> Dict[str, Any]:
    """
    수동으로 신규 진입 일시정지
    
    Args:
        reason: 일시정지 사유
    """
    try:
        system = request.app.state.system
        prm = system.portfolio_risk_manager
        prm.manual_pause(reason=req.reason)
        return {"success": True, "message": f"New entries paused: {req.reason}"}
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("portfolio_risk_router.unknown L72: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/manual-unpause")
async def manual_unpause(request: Request) -> Dict[str, Any]:
    """수동으로 일시정지 해제"""
    try:
        system = request.app.state.system
        prm = system.portfolio_risk_manager
        prm.manual_unpause()
        return {"success": True, "message": "New entries resumed"}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("portfolio_risk_router.unknown L84: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/circuit-breaker/resume")
async def resume_circuit_breaker(request: Request) -> Dict[str, Any]:
    """Circuit Breaker 수동 재개"""
    try:
        system = request.app.state.system
        prm = system.portfolio_risk_manager
        if not prm.circuit_breaker.active:
            return {"success": False, "message": "Circuit breaker is not active"}
        
        prm.manual_resume()
        return {"success": True, "message": "Circuit breaker manually resumed"}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("portfolio_risk_router.unknown L99: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reset-daily-status")
async def reset_daily_status(request: Request, req: ResetDailyStatusRequest) -> Dict[str, Any]:
    """
    일일 상태 강제 리셋 (운영자 전용)
    
    Args:
        new_capital: 새로운 시작 자본
    """
    try:
        system = request.app.state.system
        prm = system.portfolio_risk_manager
        prm.reset_daily_status(new_capital=req.new_capital)
        return {
            "success": True,
            "message": f"Daily status reset with capital: {req.new_capital:,.0f}"
        }
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("portfolio_risk_router.unknown L119: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update-correlation-groups")
async def update_correlation_groups(request: Request, req: UpdateCorrelationGroupsRequest) -> Dict[str, Any]:
    """
    코인 섹터 정보 업데이트 (상관관계 그룹)
    
    Args:
        market_sectors: {market: sector} 예) {"BTCUSDT": "L1", "ETHUSDT": "L1"}
    """
    try:
        system = request.app.state.system
        prm = system.portfolio_risk_manager
        prm.update_correlation_groups(req.market_sectors)
        return {
            "success": True,
            "message": f"Updated {len(req.market_sectors)} market sectors",
            "groups": prm.correlation_guard.correlated_groups
        }
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("portfolio_risk_router.unknown L140: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sector-exposure")
async def get_sector_exposure(request: Request) -> Dict[str, Any]:
    """
    섹터별 익스포저 조회
    
    Returns:
        {sector: total_exposure_usdt}
    """
    try:
        system = request.app.state.system
        prm = system.portfolio_risk_manager
        
        # 현재 활성 포지션 수집
        active_positions = {}
        active_markets = system.oma_registry.list_active()
        
        for market in active_markets:
            ctx = system.coordinator.get_context(market)
            if not ctx:
                continue
            
            budget = getattr(ctx, "budget_usdt", 0.0) or 0.0
            # 섹터 정보는 strategy에서 가져오거나 기본값 사용
            sector = "UNKNOWN"
            strategy = str(getattr(ctx, "strategy", "") or "").upper()
            
            # 간단한 섹터 매핑 (나중에 개선 가능)
            if "BTC" in market or "ETH" in market:
                sector = "L1"
            elif "USDT" in market or "USDC" in market:
                sector = "STABLECOIN"
            else:
                sector = "ALTCOIN"
            
            active_positions[market] = {
                "budget": budget,
                "sector": sector,
                "strategy": strategy
            }
        
        exposure = prm.get_sector_exposure(active_positions)
        
        return {
            "sector_exposure": exposure,
            "total_exposure": sum(exposure.values()),
            "active_positions_count": len(active_positions)
        }
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("portfolio_risk_router.unknown L191: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_risk_history(days: int = 7) -> Dict[str, Any]:
    """
    리스크 관리 히스토리 (향후 구현)
    
    Args:
        days: 조회 일수
    """
    # TODO: 일일 손익 히스토리 저장 및 조회 기능 추가
    return {
        "message": "History feature not implemented yet",
        "days": days
    }
