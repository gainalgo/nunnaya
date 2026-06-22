# ============================================================
# File: app/api/backtest_router.py
# Autocoin OS v3-H — Backtest API Router
# ------------------------------------------------------------
# 백테스팅 실행 및 결과 조회 API
# ============================================================

from __future__ import annotations

import logging
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from dataclasses import asdict, is_dataclass

from app.backtest.backtest_runner import BacktestRunner

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backtest", tags=["backtest"])

# 지원되는 전략 목록
ALL_STRATEGIES = [
    "PINGPONG",
    "AUTOLOOP",
    "LADDER",
    "LIGHTNING",
    "GAZUA",
    "CONTRARIAN",
    "SNIPER"
]

# 전역 백테스트 러너 (싱글톤)
_backtest_runner: Optional[BacktestRunner] = None


def get_backtest_runner() -> BacktestRunner:
    """백테스트 러너 싱글톤 가져오기"""
    global _backtest_runner
    if _backtest_runner is None:
        _backtest_runner = BacktestRunner()
    return _backtest_runner


# ============================================================
# Request/Response Models
# ============================================================

class BacktestRequest(BaseModel):
    """백테스트 실행 요청"""
    strategy: str = Field(..., description="전략 이름 (PINGPONG, AUTOLOOP, ...)")
    market: str = Field(..., description="마켓 코드 (BTCUSDT, ...)")
    days: int = Field(default=30, ge=1, le=365, description="백테스팅 기간 (일)")
    initial_capital: float = Field(default=1000.0, description="초기 자본 (USDT)")
    budget_per_trade: float = Field(default=200.0, description="거래당 예산 (USDT)")


class BulkBacktestRequest(BaseModel):
    """대량 백테스트 실행 요청"""
    strategies: List[str] = Field(..., description="전략 리스트")
    markets: List[str] = Field(..., description="마켓 리스트")
    days: int = Field(default=30, ge=1, le=365, description="백테스팅 기간 (일)")
    parallel: bool = Field(default=True, description="병렬 실행 여부")


class BacktestResultResponse(BaseModel):
    """백테스트 결과 응답"""
    strategy: str
    market: str
    start_time: float
    end_time: float
    initial_capital: float
    final_capital: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    roi_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    avg_trade_duration_hours: float
    closed_positions: List[Dict[str, Any]]
    equity_curve: List[Dict[str, float]]


class StrategySummaryResponse(BaseModel):
    """전략별 종합 성과 응답"""
    strategy: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_roi_pct: float
    max_roi_pct: float
    min_roi_pct: float
    avg_max_drawdown_pct: float
    markets_tested: int


def _serialize_closed_positions(result: Any) -> tuple[List[Dict[str, Any]], float]:
    """BacktestResult.positions -> API response shape + avg duration(hours)."""
    out: List[Dict[str, Any]] = []
    durations_h: List[float] = []

    for pos in list(getattr(result, "positions", []) or []):
        if is_dataclass(pos):
            row = asdict(pos)
        elif isinstance(pos, dict):
            row = dict(pos)
        else:
            row = {}
            for k in (
                "market",
                "strategy",
                "entry_time",
                "entry_price",
                "quantity",
                "budget_usdt",
                "tp_price",
                "sl_price",
                "exit_time",
                "exit_price",
                "exit_reason",
                "pnl_usdt",
                "roi_pct",
            ):
                row[k] = getattr(pos, k, None)

        ent = float(row.get("entry_time") or 0.0)
        ext = float(row.get("exit_time") or 0.0)
        if ent > 0 and ext > ent:
            durations_h.append((ext - ent) / 3600.0)

        out.append(row)

    avg_h = (sum(durations_h) / len(durations_h)) if durations_h else 0.0
    return out, avg_h


def _serialize_equity_curve(result: Any) -> List[Dict[str, float]]:
    """BacktestResult.equity_curve(tuple) -> [{time,equity}]"""
    out: List[Dict[str, float]] = []
    for item in list(getattr(result, "equity_curve", []) or []):
        if isinstance(item, dict):
            t = float(item.get("time") or item.get("ts") or 0.0)
            e = float(item.get("equity") or item.get("value") or 0.0)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            t = float(item[0] or 0.0)
            e = float(item[1] or 0.0)
        else:
            continue
        out.append({"time": t, "equity": e})
    return out


def _to_response(result: Any) -> BacktestResultResponse:
    closed_positions, avg_trade_duration_hours = _serialize_closed_positions(result)
    equity_curve = _serialize_equity_curve(result)
    return BacktestResultResponse(
        strategy=result.strategy,
        market=result.market,
        start_time=result.start_time,
        end_time=result.end_time,
        initial_capital=result.initial_capital,
        final_capital=result.final_capital,
        total_trades=result.total_trades,
        wins=result.wins,
        losses=result.losses,
        win_rate=result.win_rate,
        roi_pct=result.roi_pct,
        max_drawdown_pct=result.max_drawdown_pct,
        sharpe_ratio=result.sharpe_ratio,
        avg_trade_duration_hours=avg_trade_duration_hours,
        closed_positions=closed_positions,
        equity_curve=equity_curve,
    )


# ============================================================
# API Endpoints
# ============================================================

@router.post("/run", response_model=BacktestResultResponse)
async def run_backtest(request: BacktestRequest):
    """단일 백테스트 실행
    
    Args:
        request: 백테스트 실행 요청
    
    Returns:
        백테스트 결과
    
    Example:
        ```json
        POST /api/backtest/run
        {
            "strategy": "PINGPONG",
            "market": "BTCUSDT",
            "days": 30,
            "initial_capital": 1000,
            "budget_per_trade": 200
        }
        ```
    """
    try:
        runner = get_backtest_runner()
        
        result = runner.run_backtest(
            strategy=request.strategy,
            market=request.market,
            days=request.days,
            initial_capital=request.initial_capital,
            budget_per_trade=request.budget_per_trade
        )
        
        return _to_response(result)
    
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.error(f"Backtest failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Backtest failed: {str(e)}")


@router.post("/bulk", response_model=Dict[str, Dict[str, BacktestResultResponse]])
async def run_bulk_backtest(request: BulkBacktestRequest):
    """대량 백테스트 실행 (여러 전략 × 여러 마켓)
    
    Args:
        request: 대량 백테스트 요청
    
    Returns:
        {strategy: {market: BacktestResult}}
    
    Example:
        ```json
        POST /api/backtest/bulk
        {
            "strategies": ["PINGPONG", "AUTOLOOP", "SNIPER"],
            "markets": ["BTCUSDT", "ETHUSDT", "XRPUSDT"],
            "days": 30,
            "parallel": true
        }
        ```
    """
    try:
        runner = get_backtest_runner()
        
        results = runner.run_multiple_backtests(
            strategies=request.strategies,
            markets=request.markets,
            days=request.days,
            parallel=request.parallel
        )
        
        # 응답 변환
        response = {}
        for strategy, market_results in results.items():
            response[strategy] = {}
            for market, result in market_results.items():
                response[strategy][market] = _to_response(result)
        
        return response
    
    except (KeyError, AttributeError, TypeError) as e:
        logger.error(f"Bulk backtest failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Bulk backtest failed: {str(e)}")


@router.get("/summary", response_model=Dict[str, StrategySummaryResponse])
async def get_strategy_summary(
    strategies: List[str] = Query(default=None, description="전략 리스트 (미지정 시 전체)"),
    markets: List[str] = Query(default=None, description="마켓 리스트"),
    days: int = Query(default=30, ge=1, le=365, description="백테스팅 기간")
):
    """전략별 종합 성과 조회
    
    Args:
        strategies: 전략 리스트 (기본값: 전체 전략)
        markets: 마켓 리스트 (필수)
        days: 백테스팅 기간
    
    Returns:
        {strategy: StrategySummary}
    
    Example:
        ```
        GET /api/backtest/summary?strategies=PINGPONG&strategies=SNIPER&markets=BTCUSDT&markets=ETHUSDT&days=30
        ```
    """
    try:
        if not markets:
            raise HTTPException(status_code=400, detail="markets parameter is required")
        
        runner = get_backtest_runner()
        
        # 기본값: 전체 전략
        if not strategies:
            strategies = ALL_STRATEGIES
        
        # 대량 백테스트 실행
        results = runner.run_multiple_backtests(
            strategies=strategies,
            markets=markets,
            days=days,
            parallel=True
        )
        
        # 전략별 종합 성과 계산
        summary_data = runner.get_strategy_summary(results)
        
        # 응답 변환
        response = {}
        for strategy, data in summary_data.items():
            response[strategy] = StrategySummaryResponse(
                strategy=strategy,
                total_trades=data["total_trades"],
                wins=data["wins"],
                losses=data["losses"],
                win_rate=data["win_rate"],
                avg_roi_pct=data["avg_roi_pct"],
                max_roi_pct=data["max_roi_pct"],
                min_roi_pct=data["min_roi_pct"],
                avg_max_drawdown_pct=data["avg_max_drawdown_pct"],
                markets_tested=data["markets_tested"]
            )
        
        return response
    
    except HTTPException:
        logger.warning("backtest_router._to_response L327 except", exc_info=True)
        raise
    except (KeyError, AttributeError, TypeError) as e:
        logger.error(f"Strategy summary failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Strategy summary failed: {str(e)}")


@router.get("/cache")
async def get_cached_results():
    """캐시된 백테스트 결과 조회
    
    Returns:
        캐시된 결과 목록 (strategy_market_days: result_summary)
    
    Example:
        ```
        GET /api/backtest/cache
        ```
    """
    try:
        runner = get_backtest_runner()
        
        cache_summary = {}
        for key, result in runner.results_cache.items():
            cache_summary[key] = {
                "strategy": result.strategy,
                "market": result.market,
                "total_trades": result.total_trades,
                "win_rate": result.win_rate,
                "roi_pct": result.roi_pct,
                "max_drawdown_pct": result.max_drawdown_pct
            }
        
        return {
            "cached_count": len(cache_summary),
            "results": cache_summary
        }
    
    except (KeyError, AttributeError, TypeError) as e:
        logger.error(f"Cache retrieval failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Cache retrieval failed: {str(e)}")


@router.delete("/cache")
async def clear_cache():
    """백테스트 캐시 클리어
    
    Returns:
        삭제된 항목 수
    
    Example:
        ```
        DELETE /api/backtest/cache
        ```
    """
    try:
        runner = get_backtest_runner()
        count = len(runner.results_cache)
        runner.results_cache.clear()
        
        return {
            "message": "Cache cleared successfully",
            "deleted_count": count
        }
    
    except (AttributeError, TypeError) as e:
        logger.error(f"Cache clear failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Cache clear failed: {str(e)}")
