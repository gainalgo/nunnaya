# ============================================================
# File: app/api/strategy_weight_router.py
# Autocoin OS v3-H — Strategy Weight API
# ------------------------------------------------------------
# Strategy weight adjustment API endpoints
# ============================================================

from __future__ import annotations

from typing import Dict, List
from fastapi import APIRouter, Request, HTTPException

from app.manager.strategy_weight_adjuster import get_strategy_weight_adjuster

router = APIRouter(prefix="/api/strategy-weight", tags=["strategy-weight"])


@router.get("/status")
def get_strategy_weight_status(request: Request) -> Dict:
    """Get strategy weight adjuster status"""
    
    system = request.app.state.system
    if not hasattr(system, "strategy_weight_adjuster") or not system.strategy_weight_adjuster:
        return {"enabled": False, "error": "Strategy weight adjuster not initialized"}
    
    return {
        "enabled": True,
        **system.strategy_weight_adjuster.get_status()
    }


@router.get("/weights")
def get_strategy_weights(request: Request) -> Dict:
    """Get per-strategy weights"""
    
    system = request.app.state.system
    if not hasattr(system, "strategy_weight_adjuster") or not system.strategy_weight_adjuster:
        raise HTTPException(status_code=503, detail="Strategy weight adjuster not available")
    
    adjuster = system.strategy_weight_adjuster
    
    weights = {}
    for strategy, weight in adjuster.strategy_weights.items():
        weights[strategy] = {
            "final_weight": weight.final_weight,
            "performance_weight": weight.performance_weight,
            "reason": weight.reason,
            "win_rate": weight.win_rate,
            "roi_pct": weight.roi_pct,
            "total_trades": weight.total_trades,
            "consecutive_losses": weight.consecutive_losses
        }
    
    return {"weights": weights}


@router.post("/recalculate")
def recalculate_weights(request: Request) -> Dict:
    """Recalculate weights immediately"""
    
    system = request.app.state.system
    if not hasattr(system, "strategy_weight_adjuster") or not system.strategy_weight_adjuster:
        raise HTTPException(status_code=503, detail="Strategy weight adjuster not available")
    
    adjuster = system.strategy_weight_adjuster
    ledger_records = system.ledger.tail(5000)
    
    strategies = ["PINGPONG", "AUTOLOOP", "LADDER", "LIGHTNING", "GAZUA", "CONTRARIAN", "SNIPER"]
    weights = adjuster.calculate_weights(ledger_records, strategies)
    
    return {
        "success": True,
        "weights": {
            strategy: {
                "final_weight": weight.final_weight,
                "reason": weight.reason,
                "win_rate": weight.win_rate,
                "roi_pct": weight.roi_pct,
                "total_trades": weight.total_trades
            }
            for strategy, weight in weights.items()
        }
    }


@router.get("/recommendations")
def get_recommendations(request: Request) -> Dict:
    """Strategy adjustment recommendations"""
    
    system = request.app.state.system
    if not hasattr(system, "strategy_weight_adjuster") or not system.strategy_weight_adjuster:
        raise HTTPException(status_code=503, detail="Strategy weight adjuster not available")
    
    adjuster = system.strategy_weight_adjuster
    recommendations = adjuster.get_recommendations()
    
    return {
        "recommendations": recommendations,
        "count": len(recommendations)
    }


@router.get("/multiplier/{strategy}")
def get_budget_multiplier(request: Request, strategy: str) -> Dict:
    """Get budget multiplier for a specific strategy"""
    
    system = request.app.state.system
    if not hasattr(system, "strategy_weight_adjuster") or not system.strategy_weight_adjuster:
        raise HTTPException(status_code=503, detail="Strategy weight adjuster not available")
    
    adjuster = system.strategy_weight_adjuster
    multiplier = adjuster.get_budget_multiplier(strategy)
    
    weight = adjuster.strategy_weights.get(strategy)
    
    return {
        "strategy": strategy,
        "multiplier": multiplier,
        "weight_info": {
            "final_weight": weight.final_weight if weight else 1.0,
            "reason": weight.reason if weight else "No data",
            "win_rate": weight.win_rate if weight else 0.0,
            "roi_pct": weight.roi_pct if weight else 0.0
        } if weight else None
    }
