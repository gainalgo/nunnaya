"""Performance Analytics for Strategy Evaluation.

File: app/manager/performance_analyzer.py

Provides comprehensive performance metrics for trading strategies:
- Win rate, average return, max drawdown
- Strategy-specific performance tracking
- Time-period aggregation (daily, weekly, monthly)
- Real-time performance snapshots

Data Source: Trade Ledger (FILL_BUY / FILL_SELL events)
"""

from __future__ import annotations
import logging

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.manager.ledger_pnl import aggregate_fill_pnl, MarketFillAgg
logger = logging.getLogger(__name__)

@dataclass
class StrategyPerformance:
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
    markets: List[str] = field(default_factory=list)
    first_trade_ts: float = 0.0
    last_trade_ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "avg_return_pct": round(self.avg_return_pct, 4),
            "total_pnl_usdt": round(self.total_pnl_usdt, 2),
            "total_invested_usdt": round(self.total_invested_usdt, 2),
            "roi_pct": round(self.roi_pct, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "markets": sorted(self.markets),
            "first_trade_ts": self.first_trade_ts,
            "last_trade_ts": self.last_trade_ts,
        }

@dataclass
class PeriodPerformance:
    """Performance metrics for a specific time period."""
    period_name: str
    start_ts: float
    end_ts: float
    total_trades: int = 0
    total_pnl_usdt: float = 0.0
    total_invested_usdt: float = 0.0
    roi_pct: float = 0.0
    strategies: Dict[str, StrategyPerformance] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "period_name": self.period_name,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "total_trades": self.total_trades,
            "total_pnl_usdt": round(self.total_pnl_usdt, 2),
            "total_invested_usdt": round(self.total_invested_usdt, 2),
            "roi_pct": round(self.roi_pct, 4),
            "strategies": {k: v.to_dict() for k, v in self.strategies.items()},
        }

class PerformanceAnalyzer:
    """Analyzes trading performance from ledger records."""

    def __init__(self):
        pass

    def analyze_strategy_performance(
        self,
        records: List[Dict[str, Any]],
        since_ts: float = 0.0,
        until_ts: Optional[float] = None,
    ) -> Dict[str, StrategyPerformance]:
        """Analyze performance by strategy.

        Args:
            records: Ledger records (FILL_BUY, FILL_SELL, OMA_ENTRY, OMA_EXIT, etc.)
            since_ts: Start timestamp (epoch seconds)
            until_ts: End timestamp (epoch seconds, default: now)

        Returns:
            Dict mapping strategy name to StrategyPerformance
        """
        if until_ts is None:
            until_ts = time.time()

        # 1. Extract strategy assignments from ledger events
        #    - Primary: OMA_ENTRY
        #    - Fallback: ENGINE_CONTROLS_SET (market controls patch),
        #                STRATEGY_REASON_SYNCED / ORPHAN_DEFAULT_STRATEGY / RECOVERY_STRATEGY_FIXED
        market_strategy: Dict[str, str] = {}  # market -> strategy
        for rec in records:
            try:
                ts = float(rec.get("ts", 0.0))
                if ts < since_ts or ts > until_ts:
                    continue

                event = rec.get("event", "")
                market = rec.get("market", "")
                data = rec.get("data") or {}
                strategy = ""

                if event == "OMA_ENTRY":
                    strategy = str(data.get("strategy", "") or "")
                elif event in ("FILL_BUY", "FILL_SELL"):
                    strategy = str(data.get("strategy", "") or "")
                elif event == "ENGINE_CONTROLS_SET":
                    patch = data.get("patch") or {}
                    if isinstance(patch, dict):
                        s = patch.get("strategy") or {}
                        if isinstance(s, dict):
                            strategy = str(s.get("mode", "") or "")
                elif event in ("STRATEGY_REASON_SYNCED", "ORPHAN_DEFAULT_STRATEGY", "RECOVERY_STRATEGY_FIXED"):
                    strategy = str(data.get("strategy") or data.get("new") or "")

                if market and strategy:
                    market_strategy[str(market)] = strategy.upper()
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[performance_analyzer] %s: %s", 'STRATEGY_REASON_SYNCED / ORPHAN_DEFAULT_STRATEGY / RECOVERY_STRATEGY_F except-> continue', exc, exc_info=True)
                continue

        # 2. Aggregate fills by market
        aggs = aggregate_fill_pnl(records, since_ts=since_ts, until_ts=until_ts)

        # 3. Group by strategy
        strat_perf: Dict[str, StrategyPerformance] = {}
        for market, agg in aggs.items():
            strategy = market_strategy.get(market, "UNKNOWN")

            if strategy not in strat_perf:
                strat_perf[strategy] = StrategyPerformance(strategy=strategy)

            sp = strat_perf[strategy]
            sp.total_trades += agg.trade_n
            sp.total_pnl_usdt += agg.net_cash_usdt
            sp.total_invested_usdt += agg.buy_funds_usdt

            if agg.sell_n > 0:
                if agg.net_cash_usdt > 0:
                    sp.wins += agg.sell_n
                else:
                    sp.losses += agg.sell_n

            if market not in sp.markets:
                sp.markets.append(market)

            if sp.first_trade_ts == 0 or agg.first_ts < sp.first_trade_ts:
                sp.first_trade_ts = agg.first_ts
            if agg.last_ts > sp.last_trade_ts:
                sp.last_trade_ts = agg.last_ts

        # 4. Calculate derived metrics
        for sp in strat_perf.values():
            if sp.wins + sp.losses > 0:
                sp.win_rate = sp.wins / (sp.wins + sp.losses)
            if sp.total_invested_usdt > 0:
                sp.roi_pct = (sp.total_pnl_usdt / sp.total_invested_usdt) * 100.0
            if sp.wins + sp.losses > 0:
                sp.avg_return_pct = sp.roi_pct / (sp.wins + sp.losses)

        return strat_perf

    def analyze_period_performance(
        self,
        records: List[Dict[str, Any]],
        period_hours: float = 24.0,
    ) -> List[PeriodPerformance]:
        """Analyze performance by time periods.

        Args:
            records: Ledger records
            period_hours: Period length in hours (default: 24 = daily)

        Returns:
            List of PeriodPerformance objects (newest first)
        """
        if not records:
            return []

        # Find time range
        min_ts = min(float(r.get("ts", time.time())) for r in records)
        max_ts = max(float(r.get("ts", time.time())) for r in records)

        period_sec = period_hours * 3600.0
        periods: List[PeriodPerformance] = []

        # Generate periods (newest first)
        current_end = max_ts
        while current_end > min_ts:
            current_start = max(min_ts, current_end - period_sec)

            # Analyze this period
            strat_perf = self.analyze_strategy_performance(
                records,
                since_ts=current_start,
                until_ts=current_end,
            )

            total_trades = sum(sp.total_trades for sp in strat_perf.values())
            total_pnl = sum(sp.total_pnl_usdt for sp in strat_perf.values())
            total_invested = sum(sp.total_invested_usdt for sp in strat_perf.values())
            roi = (total_pnl / total_invested * 100.0) if total_invested > 0 else 0.0

            period_name = time.strftime("%Y-%m-%d %H:%M", time.localtime(current_start))
            pp = PeriodPerformance(
                period_name=period_name,
                start_ts=current_start,
                end_ts=current_end,
                total_trades=total_trades,
                total_pnl_usdt=total_pnl,
                total_invested_usdt=total_invested,
                roi_pct=roi,
                strategies=strat_perf,
            )
            periods.append(pp)

            current_end = current_start

        return periods

    def get_top_performers(
        self,
        records: List[Dict[str, Any]],
        since_ts: float = 0.0,
        limit: int = 10,
        metric: str = "roi_pct",
    ) -> List[Tuple[str, float]]:
        """Get top performing strategies by metric.

        Args:
            records: Ledger records
            since_ts: Start timestamp
            limit: Maximum number of results
            metric: Metric to rank by ("roi_pct", "win_rate", "total_pnl_usdt")

        Returns:
            List of (strategy_name, metric_value) tuples
        """
        strat_perf = self.analyze_strategy_performance(records, since_ts=since_ts)

        ranked = []
        for strategy, sp in strat_perf.items():
            value = getattr(sp, metric, 0.0)
            ranked.append((strategy, value))

        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[:limit]

    def calculate_sharpe_ratio(
        self,
        records: List[Dict[str, Any]],
        strategy: Optional[str] = None,
        since_ts: float = 0.0,
        _cached_periods: Optional[List["PeriodPerformance"]] = None,
    ) -> float:
        """Calculate Sharpe ratio (simplified, using daily returns)."""
        periods = _cached_periods or self.analyze_period_performance(records, period_hours=24.0)

        returns = []
        for pp in periods:
            if pp.start_ts < since_ts:
                continue
            if strategy:
                sp = pp.strategies.get(strategy)
                if sp and sp.total_invested_usdt > 0:
                    returns.append(sp.roi_pct)
            else:
                if pp.total_invested_usdt > 0:
                    returns.append(pp.roi_pct)

        if len(returns) < 2:
            return 0.0

        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        std_dev = variance ** 0.5

        if std_dev == 0:
            return 0.0

        return mean_return / std_dev

    def calculate_max_drawdown(
        self,
        records: List[Dict[str, Any]],
        strategy: Optional[str] = None,
        since_ts: float = 0.0,
        _cached_periods: Optional[List["PeriodPerformance"]] = None,
    ) -> float:
        """Calculate maximum drawdown percentage."""
        periods = _cached_periods or self.analyze_period_performance(records, period_hours=24.0)

        equity = 0.0
        peak = 0.0
        max_dd = 0.0

        for pp in sorted(periods, key=lambda x: x.start_ts):
            if pp.start_ts < since_ts:
                continue
            if strategy:
                sp = pp.strategies.get(strategy)
                if sp:
                    equity += sp.total_pnl_usdt
            else:
                equity += pp.total_pnl_usdt

            if equity > peak:
                peak = equity
            if peak > 0:
                drawdown = ((equity - peak) / peak) * 100.0
                if drawdown < max_dd:
                    max_dd = drawdown

        return max_dd
