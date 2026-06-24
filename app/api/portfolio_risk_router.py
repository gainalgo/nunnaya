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
    Get portfolio risk management status

    Returns:
        - enabled: whether risk management is active
        - can_enter_new_position: whether new entries are allowed
        - entry_block_reason: reason entries are blocked (when blocked)
        - daily_status: daily risk status (PnL, loss rate, pause, etc.)
        - circuit_breaker: Circuit Breaker status
        - correlation_guard: correlation guard status
        - thresholds: configured thresholds
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
    Manually pause new entries

    Args:
        reason: pause reason
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
    """Manually clear pause"""
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
    """Manually resume Circuit Breaker"""
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
    Force-reset daily status (operator only)

    Args:
        new_capital: new starting capital
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
    Update coin sector info (correlation groups)

    Args:
        market_sectors: {market: sector} e.g. {"BTCUSDT": "L1", "ETHUSDT": "L1"}
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
    Get exposure by sector

    Returns:
        {sector: total_exposure_usdt}
    """
    try:
        system = request.app.state.system
        prm = system.portfolio_risk_manager
        
        # Collect currently active positions
        active_positions = {}
        active_markets = system.oma_registry.list_active()
        
        for market in active_markets:
            ctx = system.coordinator.get_context(market)
            if not ctx:
                continue
            
            budget = getattr(ctx, "budget_usdt", 0.0) or 0.0
            # Get sector from strategy or use default
            sector = "UNKNOWN"
            strategy = str(getattr(ctx, "strategy", "") or "").upper()
            
            # Simple sector mapping (can be improved later)
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
    Risk management history (to be implemented)

    Args:
        days: number of days to query
    """
    # TODO: add daily PnL history persistence and query
    return {
        "message": "History feature not implemented yet",
        "days": days
    }
