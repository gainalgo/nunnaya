"""Performance Analytics API Router.

File: app/api/performance_router.py

Provides endpoints for strategy performance analysis:
- GET /api/performance/strategy - Strategy-specific metrics
- GET /api/performance/period - Time-period aggregated metrics
- GET /api/performance/top - Top performing strategies
- GET /api/performance/summary - Overall performance summary
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.hyper_system import HyperSystem
from app.manager.ledger_pnl import aggregate_fill_pnl
from app.manager.performance_analyzer import PerformanceAnalyzer

logger = logging.getLogger(__name__)



router = APIRouter(prefix="/api/performance", tags=["performance"])


class PerformanceSummaryResponse(BaseModel):
    """Overall performance summary."""
    total_trades: int = 0
    total_pnl_usdt: float = 0.0
    total_invested_usdt: float = 0.0
    total_fees_usdt: float = 0.0
    roi_pct: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    strategies: Dict[str, Any] = Field(default_factory=dict)
    period_start_ts: float = 0.0
    period_end_ts: float = 0.0


class StrategyPerformanceResponse(BaseModel):
    """Strategy performance metrics."""
    strategy: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_pnl_usdt: float = 0.0
    total_invested_usdt: float = 0.0
    roi_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    markets: List[str] = Field(default_factory=list)
    first_trade_ts: float = 0.0
    last_trade_ts: float = 0.0


class PeriodPerformanceResponse(BaseModel):
    """Period performance metrics."""
    period_name: str
    start_ts: float
    end_ts: float
    total_trades: int = 0
    total_pnl_usdt: float = 0.0
    total_invested_usdt: float = 0.0
    roi_pct: float = 0.0
    strategies: Dict[str, Any] = Field(default_factory=dict)


class TopPerformerResponse(BaseModel):
    """Top performer metrics."""
    strategy: str
    metric_name: str
    metric_value: float


@router.get("/summary")
async def get_performance_summary(
    request: Request,
    since_hours: float = Query(24.0, description="Hours to look back"),
) -> PerformanceSummaryResponse:
    """Get overall performance summary.

    Args:
        request: FastAPI request
        since_hours: Hours to look back (default: 24)

    Returns:
        Overall performance metrics
    """
    system = request.app.state.system
    analyzer = PerformanceAnalyzer()

    since_ts = time.time() - (since_hours * 3600.0)
    records = system.ledger.tail_records(since_ts=since_ts, tail_lines=50000)

    # Strategy performance
    strat_perf = analyzer.analyze_strategy_performance(records, since_ts=since_ts)

    # Fee aggregation
    aggs = aggregate_fill_pnl(records, since_ts=since_ts, until_ts=time.time())
    total_fees = sum(a.fees_usdt for a in aggs.values())

    # Overall metrics
    total_trades = sum(sp.total_trades for sp in strat_perf.values())
    total_pnl = sum(sp.total_pnl_usdt for sp in strat_perf.values())
    total_invested = sum(sp.total_invested_usdt for sp in strat_perf.values())
    roi = (total_pnl / total_invested * 100.0) if total_invested > 0 else 0.0

    # Sharpe and MDD (cache periods to avoid redundant computation)
    _periods = analyzer.analyze_period_performance(records, period_hours=24.0)
    sharpe = analyzer.calculate_sharpe_ratio(records, since_ts=since_ts, _cached_periods=_periods)
    mdd = analyzer.calculate_max_drawdown(records, since_ts=since_ts, _cached_periods=_periods)

    return PerformanceSummaryResponse(
        total_trades=total_trades,
        total_pnl_usdt=round(total_pnl, 2),
        total_invested_usdt=round(total_invested, 2),
        total_fees_usdt=round(total_fees, 2),
        roi_pct=round(roi, 4),
        sharpe_ratio=round(sharpe, 4),
        max_drawdown_pct=round(mdd, 4),
        strategies={k: v.to_dict() for k, v in strat_perf.items()},
        period_start_ts=since_ts,
        period_end_ts=time.time(),
    )


@router.get("/strategy")
async def get_strategy_performance(
    request: Request,
    strategy: Optional[str] = Query(None, description="Strategy name filter"),
    since_hours: float = Query(24.0, description="Hours to look back"),
) -> Dict[str, StrategyPerformanceResponse]:
    """Get strategy-specific performance metrics.

    Args:
        request: FastAPI request
        strategy: Strategy name filter (None = all strategies)
        since_hours: Hours to look back (default: 24)

    Returns:
        Dict mapping strategy name to performance metrics
    """
    system = request.app.state.system
    analyzer = PerformanceAnalyzer()

    since_ts = time.time() - (since_hours * 3600.0)
    records = system.ledger.tail_records(since_ts=since_ts, tail_lines=50000)

    strat_perf = analyzer.analyze_strategy_performance(records, since_ts=since_ts)

    # Filter by strategy if specified
    if strategy:
        strategy_upper = strategy.upper()
        strat_perf = {k: v for k, v in strat_perf.items() if k == strategy_upper}

    # Calculate additional metrics for each strategy (cache periods once)
    _periods = analyzer.analyze_period_performance(records, period_hours=24.0)
    result = {}
    for strat_name, sp in strat_perf.items():
        mdd = analyzer.calculate_max_drawdown(records, strategy=strat_name, since_ts=since_ts, _cached_periods=_periods)
        sharpe = analyzer.calculate_sharpe_ratio(records, strategy=strat_name, since_ts=since_ts, _cached_periods=_periods)

        result[strat_name] = StrategyPerformanceResponse(
            strategy=sp.strategy,
            total_trades=sp.total_trades,
            wins=sp.wins,
            losses=sp.losses,
            win_rate=round(sp.win_rate, 4),
            avg_return_pct=round(sp.avg_return_pct, 4),
            total_pnl_usdt=round(sp.total_pnl_usdt, 2),
            total_invested_usdt=round(sp.total_invested_usdt, 2),
            roi_pct=round(sp.roi_pct, 4),
            max_drawdown_pct=round(mdd, 4),
            sharpe_ratio=round(sharpe, 4),
            markets=sp.markets,
            first_trade_ts=sp.first_trade_ts,
            last_trade_ts=sp.last_trade_ts,
        )

    return result


@router.get("/period")
async def get_period_performance(
    request: Request,
    period_hours: float = Query(24.0, description="Period length in hours"),
    limit: int = Query(7, description="Number of periods to return"),
) -> List[PeriodPerformanceResponse]:
    """Get time-period aggregated performance.

    Args:
        request: FastAPI request
        period_hours: Period length in hours (default: 24 = daily)
        limit: Number of periods to return (default: 7)

    Returns:
        List of period performance metrics (newest first)
    """
    system = request.app.state.system
    analyzer = PerformanceAnalyzer()

    # Look back enough to cover requested periods
    since_ts = time.time() - (period_hours * limit * 3600.0)
    records = system.ledger.tail_records(since_ts=since_ts, tail_lines=50000)

    periods = analyzer.analyze_period_performance(records, period_hours=period_hours)

    # Convert to response format
    result = []
    for pp in periods[:limit]:
        result.append(PeriodPerformanceResponse(
            period_name=pp.period_name,
            start_ts=pp.start_ts,
            end_ts=pp.end_ts,
            total_trades=pp.total_trades,
            total_pnl_usdt=round(pp.total_pnl_usdt, 2),
            total_invested_usdt=round(pp.total_invested_usdt, 2),
            roi_pct=round(pp.roi_pct, 4),
            strategies={k: v.to_dict() for k, v in pp.strategies.items()},
        ))

    return result


@router.get("/top")
async def get_top_performers(
    request: Request,
    limit: int = Query(5, description="Number of top performers"),
    metric: str = Query("roi_pct", description="Metric to rank by"),
    since_hours: float = Query(24.0, description="Hours to look back"),
) -> List[TopPerformerResponse]:
    """Get top performing strategies.

    Args:
        request: FastAPI request
        limit: Number of top performers (default: 5)
        metric: Metric to rank by ("roi_pct", "win_rate", "total_pnl_usdt")
        since_hours: Hours to look back (default: 24)

    Returns:
        List of top performing strategies
    """
    system = request.app.state.system
    analyzer = PerformanceAnalyzer()

    since_ts = time.time() - (since_hours * 3600.0)
    records = system.ledger.tail_records(since_ts=since_ts, tail_lines=50000)

    top = analyzer.get_top_performers(records, since_ts=since_ts, limit=limit, metric=metric)

    return [
        TopPerformerResponse(
            strategy=strategy,
            metric_name=metric,
            metric_value=round(value, 4),
        )
        for strategy, value in top
    ]


@router.get("/markets/{market}")
async def get_market_performance(
    request: Request,
    market: str,
    since_hours: float = Query(24.0, description="Hours to look back"),
) -> Dict[str, Any]:
    """Get performance metrics for a specific market.

    Args:
        request: FastAPI request
        market: Market symbol (e.g., BTCUSDT)
        since_hours: Hours to look back (default: 24)

    Returns:
        Market performance metrics
    """
    system = request.app.state.system
    analyzer = PerformanceAnalyzer()

    since_ts = time.time() - (since_hours * 3600.0)
    records = system.ledger.tail_records(since_ts=since_ts, tail_lines=50000)

    # Filter records for this market
    market_records = [r for r in records if r.get("market") == market]

    if not market_records:
        return {
            "market": market,
            "error": "No trade records found for this market",
        }

    # Get aggregated fills
    from app.manager.ledger_pnl import aggregate_fill_pnl
    aggs = aggregate_fill_pnl(market_records, since_ts=since_ts, until_ts=time.time())
    agg = aggs.get(market)

    if not agg:
        return {
            "market": market,
            "error": "No fill records found for this market",
        }

    # Find strategy
    strategy = "UNKNOWN"
    for rec in market_records:
        if rec.get("event") == "OMA_ENTRY":
            data = rec.get("data") or {}
            strategy = data.get("strategy", "UNKNOWN").upper()
            break

    return {
        "market": market,
        "strategy": strategy,
        "total_trades": agg.trade_n,
        "buy_count": agg.buy_n,
        "sell_count": agg.sell_n,
        "buy_funds_usdt": round(agg.buy_funds_usdt, 2),
        "sell_funds_usdt": round(agg.sell_funds_usdt, 2),
        "fees_usdt": round(agg.fees_usdt, 2),
        "net_pnl_usdt": round(agg.net_cash_usdt, 2),
        "roi_pct": round((agg.net_cash_usdt / agg.buy_funds_usdt * 100.0) if agg.buy_funds_usdt > 0 else 0.0, 4),
        "first_trade_ts": agg.first_ts,
        "last_trade_ts": agg.last_ts,
    }


@router.get("/slot-recommendations")
async def get_slot_recommendations(
    request: Request,
    total_slots: int = Query(20, description="Total slots to distribute"),
    since_hours: float = Query(168.0, description="Hours to look back (default: 7 days)"),
    min_trades_per_strategy: int = Query(3, description="Minimum trades required for scoring"),
    use_backtest: bool = Query(True, description="Include backtest data when real trades are insufficient"),
) -> Dict[str, Any]:
    """Get recommended slot distribution based on recent performance.
    
    [2026-02-04] When real-trade data is insufficient, blend backtest results in by weight.
    The weight is configurable per-strategy in Reserved Settings (0%=real trades only, 100%=backtest only)

    Args:
        request: FastAPI request object (for accessing app.state.system)
        total_slots: Total number of slots to distribute across strategies
        since_hours: Hours to look back for performance analysis (default: 7 days)
        min_trades_per_strategy: Minimum trades required for a strategy to be scored
        use_backtest: Include backtest data when real trades are insufficient

    Returns:
        Recommended slots per strategy with performance metrics
    """
    all_strategies = ["PINGPONG", "AUTOLOOP", "LADDER", "LIGHTNING", "GAZUA", "CONTRARIAN", "SNIPER"]

    def _equal_fallback(reason: str, error: str = "", ok: bool = True):
        equal_slots = total_slots // 7
        remainder = total_slots % 7
        recs = {}
        for i, s in enumerate(all_strategies):
            extra = 1 if i < remainder else 0
            recs[s] = {"recommended_slots": equal_slots + extra, "win_rate": 0.0, "roi_pct": 0.0,
                        "total_trades": 0, "total_pnl_usdt": 0.0, "reason": reason, "score": 1.0}
        result = {"ok": ok, "total_slots": total_slots, "analysis_period_hours": since_hours,
                  "has_data": False, "recommendations": recs,
                  "summary": {"total_allocated": total_slots, "top_strategy": "PINGPONG", "analyzed_strategies": 0}}
        if error:
            result["error"] = error
        return result

    try:
        system = request.app.state.system
        analyzer = PerformanceAnalyzer()
        since_ts = time.time() - (since_hours * 3600.0)

        try:
            records = system.ledger.tail(50000)
        except AttributeError:
            logger.warning("performance_router._equal_fallback L381 except", exc_info=True)
            records = system.ledger.tail_records(since_ts=since_ts, tail_lines=50000)

        if not records:
            return _equal_fallback("No performance data")
    except (OSError, TypeError, ValueError, OverflowError) as e:
        logger.warning("performance_router._equal_fallback L386: %s", e)
        return _equal_fallback("Error loading data", error=str(e), ok=False)

    # Analyze strategy performance
    strategy_perf = analyzer.analyze_strategy_performance(records, since_ts=since_ts)

    # [2026-02-04] Load backtest weights (per-strategy)
    backtest_weights = {
        "PINGPONG": getattr(system, "backtest_weight_pingpong", 0.10),
        "AUTOLOOP": getattr(system, "backtest_weight_autoloop", 0.15),
        "LADDER": getattr(system, "backtest_weight_ladder", 0.30),
        "LIGHTNING": getattr(system, "backtest_weight_lightning", 0.15),
        "GAZUA": getattr(system, "backtest_weight_gazua", 0.35),
        "CONTRARIAN": getattr(system, "backtest_weight_contrarian", 0.20),
        "SNIPER": getattr(system, "backtest_weight_sniper", 0.30),
    }
    
    # [2026-02-04] Load backtest data (when use_backtest=True)
    backtest_scores = {}
    if use_backtest:
        try:
            from app.backtest.backtest_runner import BacktestRunner
            runner = BacktestRunner()
            
            # A simple market sample (in practice, roughly the top 10 popular markets)
            sample_markets = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]

            # Check cache
            for strategy in all_strategies:
                cache_key = f"{strategy}_SAMPLE_30"  # 30-day backtest
                if cache_key in runner.results_cache:
                    result = runner.results_cache[cache_key]
                    backtest_scores[strategy] = {
                        "win_rate": result.win_rate,
                        "roi_pct": result.roi_pct,
                        "total_trades": result.total_trades
                    }
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            # On backtest failure, just log and keep going
            import logging
            logging.warning(f"Backtest data load failed: {e}")

    # Calculate performance scores
    strategy_scores = {}
    for strategy in all_strategies:
        perf = strategy_perf.get(strategy)
        real_trade_count = perf.total_trades if perf else 0
        
        # [2026-02-04] Compute real-trade data score
        real_score = 1.0  # default
        real_reason = "No data"

        if perf and perf.total_trades >= min_trades_per_strategy:
            # Sufficient real-trade data available
            win_rate_score = perf.win_rate / 100.0
            roi_score = max(0.0, min(1.0, (perf.roi_pct + 50.0) / 100.0))
            trade_bonus = min(0.2, perf.total_trades / 100.0)
            composite = (win_rate_score * 0.5 + roi_score * 0.5) + trade_bonus
            real_score = max(0.5, min(2.0, composite * 1.5))
            real_reason = "Real trades"
        
        # [2026-02-04] Compute backtest data score
        backtest_score = 1.0
        backtest_reason = "No backtest"
        
        if use_backtest and strategy in backtest_scores:
            bt = backtest_scores[strategy]
            if bt["total_trades"] > 0:
                # Clip extreme values (25-75% win rate, -30%~+50% ROI)
                bt_win_rate = max(25.0, min(75.0, bt["win_rate"]))
                bt_roi = max(-30.0, min(50.0, bt["roi_pct"]))
                
                bt_win_score = bt_win_rate / 100.0
                bt_roi_score = max(0.0, min(1.0, (bt_roi + 50.0) / 100.0))
                bt_composite = (bt_win_score * 0.5 + bt_roi_score * 0.5)
                backtest_score = max(0.5, min(2.0, bt_composite * 1.5))
                backtest_reason = f"Backtest ({bt['total_trades']} trades)"
        
        # [2026-02-04] Weighted blend (raise backtest share when real trades are scarce)
        weight = backtest_weights.get(strategy, 0.15)

        # Adjust weight based on the amount of real-trade data
        if real_trade_count >= 30:
            # Plenty of real trades → reduce backtest share
            effective_weight = weight * 0.3
        elif real_trade_count >= 15:
            # Moderate → half backtest share
            effective_weight = weight * 0.5
        elif real_trade_count >= min_trades_per_strategy:
            # Minimum met → default backtest share
            effective_weight = weight
        else:
            # Real trades scarce → prioritize backtest (max 75%)
            effective_weight = min(0.75, weight * 1.5)

        # Final score = real × (1-w) + backtest × w
        final_score = (real_score * (1.0 - effective_weight)) + (backtest_score * effective_weight)

        # Final reason
        if real_trade_count < min_trades_per_strategy and use_backtest and strategy in backtest_scores:
            reason = f"Backtest-weighted ({int(effective_weight*100)}%)"
        elif real_trade_count >= min_trades_per_strategy:
            reason = f"Real data ({real_trade_count} trades)"
        else:
            reason = "Insufficient data"
        
        strategy_scores[strategy] = {
            "score": final_score,
            "win_rate": perf.win_rate if perf else (backtest_scores.get(strategy, {}).get("win_rate", 0.0)),
            "roi_pct": perf.roi_pct if perf else (backtest_scores.get(strategy, {}).get("roi_pct", 0.0)),
            "total_trades": real_trade_count,
            "total_pnl_usdt": perf.total_pnl_usdt if perf else 0.0,
            "reason": reason,
            "backtest_weight": effective_weight,
        }

    # Distribute slots proportionally
    total_score = sum(s["score"] for s in strategy_scores.values())
    
    recommendations = {}
    allocated_slots = 0
    
    # First pass: proportional allocation
    for strategy in all_strategies:
        score = strategy_scores[strategy]["score"]
        proportion = score / total_score if total_score > 0 else (1.0 / len(all_strategies))
        slots = max(0, int(round(proportion * total_slots)))
        
        recommendations[strategy] = {
            "recommended_slots": slots,
            "win_rate": strategy_scores[strategy]["win_rate"],
            "roi_pct": strategy_scores[strategy]["roi_pct"],
            "total_trades": strategy_scores[strategy]["total_trades"],
            "total_pnl_usdt": strategy_scores[strategy].get("total_pnl_usdt", 0.0),
            "reason": strategy_scores[strategy]["reason"],
            "score": strategy_scores[strategy]["score"],
            "backtest_weight": strategy_scores[strategy].get("backtest_weight", 0.0)
        }
        allocated_slots += slots

    # Second pass: distribute remaining slots to top performers
    remaining = total_slots - allocated_slots
    if remaining > 0:
        sorted_strategies = sorted(
            all_strategies,
            key=lambda s: strategy_scores[s]["score"],
            reverse=True
        )
        for i in range(remaining):
            strategy = sorted_strategies[i % len(sorted_strategies)]
            recommendations[strategy]["recommended_slots"] += 1

    return {
        "ok": True,
        "total_slots": total_slots,
        "analysis_period_hours": since_hours,
        "recommendations": recommendations,
        "summary": {
            "total_allocated": sum(r["recommended_slots"] for r in recommendations.values()),
            "top_strategy": max(recommendations.items(), key=lambda x: x[1]["recommended_slots"])[0],
            "analyzed_strategies": len([r for r in recommendations.values() if r["total_trades"] >= min_trades_per_strategy])
        }
    }


# =========================================================================
# Fee-to-Profit Analysis (added 2026-03-19)
# =========================================================================

class FeeAnalysisResponse(BaseModel):
    """Fee impact analysis summary."""
    total_trades: int = 0
    total_gross_pnl: float = 0.0
    total_fees: float = 0.0
    total_net_pnl: float = 0.0
    fee_pct_of_gross: float = 0.0
    avg_fee_per_trade: float = 0.0
    by_strategy: Dict[str, Any] = Field(default_factory=dict)


@router.get("/fee-analysis")
async def get_fee_analysis(
    request: Request,
    since_hours: float = Query(168.0, description="Hours to look back (default: 7 days)"),
    group_by_strategy: bool = Query(True, description="Group results by strategy"),
) -> FeeAnalysisResponse:
    """Analyse fee impact on profitability.

    Reads FILL_BUY / FILL_SELL events from the ledger, pairs them
    per-market to form completed trades (buy -> sell), and reports
    gross profit, fees paid, net profit and the fee-to-gross ratio.

    Exchange charges 0.05 % each way; if ``paid_fee`` is recorded in the
    ledger it is used directly, otherwise the 0.05 % rate is applied.

    Args:
        request: FastAPI request
        since_hours: Hours to look back (default: 168 = 7 days)
        group_by_strategy: Include per-strategy breakdown

    Returns:
        Fee analysis summary with optional strategy breakdown
    """
    EXCHANGE_FEE_RATE = 0.0005

    system: HyperSystem = request.app.state.system
    since_ts = time.time() - (since_hours * 3600.0)
    records = system.ledger.tail_records(since_ts=since_ts, tail_lines=50000)

    # Collect per-market buy / sell fills
    buys: Dict[str, List[Dict[str, Any]]] = {}   # market -> [fill, ...]
    sells: Dict[str, List[Dict[str, Any]]] = {}

    for rec in records:
        ev = str(rec.get("event") or "")
        if ev not in ("FILL_BUY", "FILL_SELL", "FILL_SYNC_BUY", "FILL_SYNC_SELL"):
            continue
        market = str(rec.get("market") or "")
        if not market:
            data = rec.get("data") or {}
            market = str(data.get("market") or "")
        if not market:
            continue

        data = rec.get("data") or {}
        entry = {
            "ts": float(rec.get("ts") or 0),
            "funds": float(data.get("funds") or 0),
            "paid_fee": float(data.get("paid_fee") or 0),
            "qty": float(data.get("qty") or 0),
            "avg_price": float(data.get("avg_price") or 0),
            "strategy": str(data.get("strategy") or "UNKNOWN").upper(),
        }

        if ev in ("FILL_BUY", "FILL_SYNC_BUY"):
            buys.setdefault(market, []).append(entry)
        else:
            sells.setdefault(market, []).append(entry)

    # Pair buys with sells (FIFO per market)
    total_gross = 0.0
    total_fees = 0.0
    total_trades = 0
    strategy_agg: Dict[str, Dict[str, float]] = {}

    for market, buy_list in buys.items():
        sell_list = sells.get(market, [])
        buy_list.sort(key=lambda x: x["ts"])
        sell_list.sort(key=lambda x: x["ts"])

        pairs = min(len(buy_list), len(sell_list))
        for i in range(pairs):
            b = buy_list[i]
            s = sell_list[i]

            buy_amount = b["funds"] if b["funds"] > 0 else (b["qty"] * b["avg_price"])
            sell_amount = s["funds"] if s["funds"] > 0 else (s["qty"] * s["avg_price"])

            buy_fee = b["paid_fee"] if b["paid_fee"] > 0 else buy_amount * EXCHANGE_FEE_RATE
            sell_fee = s["paid_fee"] if s["paid_fee"] > 0 else sell_amount * EXCHANGE_FEE_RATE
            fee_total = buy_fee + sell_fee

            gross = sell_amount - buy_amount
            total_gross += gross
            total_fees += fee_total
            total_trades += 1

            strat = s["strategy"] if s["strategy"] != "UNKNOWN" else b["strategy"]
            if group_by_strategy:
                sa = strategy_agg.setdefault(strat, {
                    "trades": 0, "gross": 0.0, "fees": 0.0,
                })
                sa["trades"] += 1
                sa["gross"] += gross
                sa["fees"] += fee_total

    total_net = total_gross - total_fees
    fee_pct = (total_fees / abs(total_gross) * 100.0) if total_gross != 0 else 0.0
    avg_fee = (total_fees / total_trades) if total_trades > 0 else 0.0

    by_strategy: Dict[str, Any] = {}
    if group_by_strategy:
        for strat, sa in strategy_agg.items():
            s_gross = sa["gross"]
            s_fees = sa["fees"]
            s_net = s_gross - s_fees
            s_trades = sa["trades"]
            by_strategy[strat] = {
                "total_trades": s_trades,
                "total_gross_pnl": round(s_gross, 2),
                "total_fees": round(s_fees, 2),
                "total_net_pnl": round(s_net, 2),
                "fee_pct_of_gross": round(
                    (s_fees / abs(s_gross) * 100.0) if s_gross != 0 else 0.0, 4
                ),
                "avg_fee_per_trade": round(s_fees / s_trades, 2) if s_trades > 0 else 0.0,
            }

    return FeeAnalysisResponse(
        total_trades=total_trades,
        total_gross_pnl=round(total_gross, 2),
        total_fees=round(total_fees, 2),
        total_net_pnl=round(total_net, 2),
        fee_pct_of_gross=round(fee_pct, 4),
        avg_fee_per_trade=round(avg_fee, 2),
        by_strategy=by_strategy,
    )
