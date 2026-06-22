# ============================================================
# File: app/strategy/strategy_api.py
# ------------------------------------------------------------
# 전략 엔진 외부 접근 API (FastAPI Router)
# 분석, 정책 조회/수정, 신호 테스트용 엔드포인트 제공
# ============================================================

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Dict, Any, List

from .strategy_initializer import StrategyPipeline
from .strategy_store import strategy_store
from app.core.hyper_price_store import price_store
from app.core.currency import Q

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy", tags=["strategy"])

# 단일 글로벌 전략 파이프라인
pipeline = StrategyPipeline()


# ------------------------------------------------------------
# 1) 정책 조회
# ------------------------------------------------------------
@router.get("/policy/{market}")
def get_policy(market: str) -> Dict[str, Any]:
    policy = strategy_store.get_policy(market)
    return policy.to_dict()


# ------------------------------------------------------------
# 2) 정책 업데이트
# ------------------------------------------------------------
@router.post("/policy/{market}")
def update_policy(market: str, updates: Dict[str, Any]):
    strategy_store.update_policy(market, updates)
    return {"ok": True, "updated": updates}


# ------------------------------------------------------------
# 3) Brain 분석 결과 조회
# ------------------------------------------------------------
@router.get("/brain/{market}/{price}")
def analyze_brain(market: str, price: float) -> Dict[str, Any]:
    policy = strategy_store.get_policy(market)
    brain_out = pipeline.brain.analyze(market, price, price_history=None, policy=policy)
    return brain_out.to_dict()


# ------------------------------------------------------------
# 4) 최종 전략 시그널 테스트
# ------------------------------------------------------------
@router.get("/signal/{market}/{price}")
def get_signal(market: str, price: float) -> Dict[str, Any]:
    signal = pipeline.run(market, price)
    return {"market": market, "signal": signal.signal}


# ------------------------------------------------------------
# 5) Ladder 전략 계산기 (Preview/Simulation)
# ------------------------------------------------------------
def _calculate_ladder_steps(alloc: float, step_pct: float, max_steps: int, martingale: float, min_order_usdt: float, base_price: float = 0.0) -> Dict[str, Any]:
    """Ladder 단계별 금액 계산 로직 (공통)."""
    steps = []
    total_weight = 0.0
    
    # 총 가중치 계산 (Entry(0) + N steps)
    for i in range(max_steps + 1):
        total_weight += pow(martingale, i)
    
    base_fraction = 1.0 / total_weight if total_weight > 0 else 1.0
    total_planned = 0.0
    
    for i in range(max_steps + 1):
        fraction = base_fraction * pow(martingale, i)
        amount_raw = alloc * fraction
        
        # USDT 0.01 단위 절사
        amount = int(amount_raw * 100) / 100
        
        drop_pct = step_pct * i
        step_price = base_price * (1.0 - drop_pct / 100.0) if base_price > 0 else 0.0
        
        steps.append({
            "step": i,
            "type": "ENTRY" if i == 0 else f"ADD_{i}",
            "drop_pct": drop_pct,
            "price": step_price,
            "fraction": round(fraction, 6),
            "amount_usdt": amount,
            "raw_usdt": round(amount_raw, 2)
        })
        total_planned += amount
        
    return {
        "allocated_capital": alloc,
        "params": {
            "step_pct": step_pct,
            "max_steps": max_steps,
            "martingale": martingale,
            "min_order_usdt": min_order_usdt
        },
        "base_price": base_price,
        "total_weight": round(total_weight, 4),
        "steps": steps,
        "total_planned_usdt": total_planned,
        "utilization_pct": round((total_planned / alloc * 100.0) if alloc > 0 else 0.0, 2)
    }

class LadderCalcRequest(BaseModel):
    allocated_capital: float
    step_pct: float
    max_steps: int
    martingale: float
    min_order_usdt: float = 10.0
    base_price: float = 0.0

@router.post("/ladder/calculate")
def calculate_ladder(req: LadderCalcRequest) -> Dict[str, Any]:
    """임의 파라미터로 Ladder 시뮬레이션."""
    return _calculate_ladder_steps(req.allocated_capital, req.step_pct, req.max_steps, req.martingale, req.min_order_usdt, req.base_price)

@router.get("/ladder/preview/{market}")
def preview_ladder_market(market: str, request: Request) -> Dict[str, Any]:
    """특정 마켓의 현재 설정(배정금+파라미터) 기반 Ladder 시뮬레이션."""
    system = request.app.state.system
    ctx = system.coordinator.get_context(market)
    
    # 1. Get allocated capital
    alloc = float(getattr(ctx, "allocated_capital", 0.0) or 0.0)
    if alloc <= 0:
        alloc = 100.0  # Fallback for preview if 0
        
    # 2. Get strategy params (Runtime controls > StrategyStore)
    params = {}
    try:
        ctrls = getattr(ctx, "controls", {}) or {}
        if isinstance(ctrls, dict):
            params = dict((ctrls.get("strategy") or {}).get("params") or {})
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[strategy_api] %s: %s", '2. Get strategy params (Runtime controls > StrategyStore)', exc, exc_info=True)
        
    if not params:
        pol = strategy_store.get_policy(market)
        params = pol.get("params", {})

    step_pct = float(params.get("step_pct", 1.0))
    max_steps = int(params.get("max_steps", 10))
    martingale = float(params.get("martingale", 1.0))
    min_order_usdt = float(params.get("min_order_usdt", 10.0))
    
    current_price = price_store.get_price(market) or 0.0
    
    result = _calculate_ladder_steps(alloc, step_pct, max_steps, martingale, min_order_usdt, float(current_price))
    result["market"] = market
    return result


# ------------------------------------------------------------
# 6) Autoloop 전략 계산기 (Preview/Simulation)
# ------------------------------------------------------------
def _calculate_autoloop_steps(alloc: float, buy_splits: List[float], add_buy_drop_pcts: List[float], base_price: float = 0.0, martingale: float = 1.0) -> Dict[str, Any]:
    """Autoloop 분할 매수 계획 계산."""
    steps = []
    total_planned = 0.0
    
    # Ensure we have at least one split
    splits = buy_splits if buy_splits else [1.0]
    
    # If martingale is requested in simulation, override splits
    if martingale > 1.0:
        count = max(len(splits), 3) # default to 3 stages if splits empty
        w = [pow(martingale, i) for i in range(count)]
        total_w = sum(w)
        splits = [x / total_w for x in w]
    
    for i, frac in enumerate(splits):
        amount_raw = alloc * frac
        amount = int(amount_raw * 100) / 100  # USDT 0.01 단위
        
        drop_pct = 0.0
        if i > 0:
            # stage 1 (index 1) uses add_buy_drop_pcts[0]
            idx = i - 1
            if idx < len(add_buy_drop_pcts):
                drop_pct = add_buy_drop_pcts[idx]
        
        step_price = base_price * (1.0 + drop_pct / 100.0) if base_price > 0 else 0.0
        
        steps.append({
            "step": i,
            "type": "ENTRY" if i == 0 else f"ADD_{i}",
            "drop_pct": drop_pct,
            "price": step_price,
            "fraction": round(frac, 4),
            "amount_usdt": amount,
            "raw_usdt": round(amount_raw, 2)
        })
        total_planned += amount
        
    return {
        "allocated_capital": alloc,
        "params": {
            "buy_splits": splits,
            "add_buy_drop_pcts": add_buy_drop_pcts,
        },
        "base_price": base_price,
        "steps": steps,
        "total_planned_usdt": total_planned,
        "utilization_pct": round((total_planned / alloc * 100.0) if alloc > 0 else 0.0, 2)
    }

class AutoloopCalcRequest(BaseModel):
    allocated_capital: float
    buy_splits: List[float]
    add_buy_drop_pcts: List[float]
    base_price: float = 0.0
    martingale: float = 1.0

@router.post("/autoloop/calculate")
def calculate_autoloop(req: AutoloopCalcRequest) -> Dict[str, Any]:
    """임의 파라미터로 Autoloop 시뮬레이션."""
    return _calculate_autoloop_steps(req.allocated_capital, req.buy_splits, req.add_buy_drop_pcts, req.base_price, req.martingale)

@router.get("/autoloop/preview/{market}")
def preview_autoloop_market(market: str, request: Request) -> Dict[str, Any]:
    """특정 마켓의 현재 설정(배정금+파라미터) 기반 Autoloop 시뮬레이션."""
    system = request.app.state.system
    ctx = system.coordinator.get_context(market)
    
    alloc = float(getattr(ctx, "allocated_capital", 0.0) or 0.0)
    if alloc <= 0:
        alloc = 100.0
        
    params = {}
    try:
        ctrls = getattr(ctx, "controls", {}) or {}
        if isinstance(ctrls, dict):
            params = dict((ctrls.get("strategy") or {}).get("params") or {})
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[strategy_api] %s: %s", 'strategy_api.preview_autoloop_market fallback', exc, exc_info=True)
        
    if not params:
        pol = strategy_store.get_policy(market)
        params = pol.get("params", {})
        
    buy_splits = params.get("buy_splits")
    # Default fallback if not tuned yet
    if not buy_splits:
        buy_splits = [1.0]
    elif not isinstance(buy_splits, list):
        buy_splits = [1.0]
        
    add_buy_drop_pcts = params.get("add_buy_drop_pcts") or []
    if not isinstance(add_buy_drop_pcts, list):
        add_buy_drop_pcts = []
    
    current_price = price_store.get_price(market) or 0.0
    
    martingale = float(params.get("martingale", 1.0))
    
    result = _calculate_autoloop_steps(alloc, [float(x) for x in buy_splits], [float(x) for x in add_buy_drop_pcts], float(current_price), martingale)
    result["market"] = market
    return result
