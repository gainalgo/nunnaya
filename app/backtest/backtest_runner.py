# ============================================================
# File: app/backtest/backtest_runner.py
# Autocoin OS v3-H — Backtest Runner
# ------------------------------------------------------------
# Per-strategy backtest execution and result management
# ============================================================

from __future__ import annotations

import time
import logging
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.backtest.candle_loader import CandleLoader
from app.backtest.backtest_engine import BacktestEngine, BacktestResult
from app.backtest.strategy_simulator import StrategySimulator

logger = logging.getLogger(__name__)


class BacktestRunner:
    """Backtest execution manager"""
    
    def __init__(self):
        self.candle_loader = CandleLoader()
        self.results_cache: Dict[str, BacktestResult] = {}  # key: strategy_market_days
    
    def run_backtest(
        self,
        strategy: str,
        market: str,
        days: int = 30,
        initial_capital: float = 1000.0,
        budget_per_trade: float = 200.0
    ) -> BacktestResult:
        """Run a backtest for a single strategy-market pair

        Args:
            strategy: strategy name
            market: market code
            days: backtest period (days)
            initial_capital: initial capital
            budget_per_trade: budget per trade

        Returns:
            backtest result
        """
        logger.info(f"Starting backtest: {strategy} on {market} ({days} days)")
        
        # Load candle data
        candles = self.candle_loader.load_candles(market, days=days, interval_minutes=60)

        if not candles:
            logger.warning(f"No candle data for {market}")
            return self._create_empty_result(strategy, market)

        # Initialize backtest engine
        engine = BacktestEngine(initial_capital=initial_capital)

        # Strategy simulator
        simulator = StrategySimulator(strategy)

        # Run simulation
        start_time = self._parse_candle_time(candles[0])
        end_time = self._parse_candle_time(candles[-1])
        
        for idx, candle in enumerate(candles):
            current_time = self._parse_candle_time(candle)
            current_price = candle["trade_price"]
            
            # Check TP/SL on existing position
            if market in engine.positions:
                engine.check_tp_sl(current_time, current_price, market)

            # Check for new entry signal (only when no position is open)
            if market not in engine.positions:
                signal = simulator.generate_entry_signal(candles, idx)
                
                if signal:
                    engine.open_position(
                        market=market,
                        strategy=strategy,
                        entry_time=current_time,
                        entry_price=signal["entry_price"],
                        budget_usdt=budget_per_trade,
                        tp_pct=signal["tp_pct"],
                        sl_pct=signal["sl_pct"]
                    )
            
            # Update equity curve
            if idx % 24 == 0:  # once per day
                engine.update_equity_curve(current_time, {market: current_price})

        # Close remaining positions
        for market_key in list(engine.positions.keys()):
            last_candle = candles[-1]
            engine.close_position(
                market_key,
                end_time,
                last_candle["trade_price"],
                "end_of_backtest"
            )
        
        # Build result
        result = engine.get_result(strategy, market, start_time, end_time)

        # Save to cache
        cache_key = f"{strategy}_{market}_{days}"
        self.results_cache[cache_key] = result
        
        logger.info(
            f"Backtest complete: {strategy} {market} | "
            f"Trades: {result.total_trades}, Win rate: {result.win_rate:.1f}%, "
            f"ROI: {result.roi_pct:+.2f}%"
        )
        
        return result
    
    def run_multiple_backtests(
        self,
        strategies: List[str],
        markets: List[str],
        days: int = 30,
        parallel: bool = True
    ) -> Dict[str, Dict[str, BacktestResult]]:
        """Run backtests for multiple strategy-market pairs

        Args:
            strategies: list of strategies
            markets: list of markets
            days: backtest period
            parallel: whether to run in parallel

        Returns:
            {strategy: {market: BacktestResult}}
        """
        results = {}
        
        tasks = [
            (strategy, market)
            for strategy in strategies
            for market in markets
        ]
        
        if parallel:
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(self.run_backtest, strategy, market, days): (strategy, market)
                    for strategy, market in tasks
                }
                
                for future in as_completed(futures):
                    strategy, market = futures[future]
                    try:
                        result = future.result()
                        if strategy not in results:
                            results[strategy] = {}
                        results[strategy][market] = result
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                        logger.error(f"Backtest failed: {strategy} {market} - {e}")
        else:
            for strategy, market in tasks:
                try:
                    result = self.run_backtest(strategy, market, days)
                    if strategy not in results:
                        results[strategy] = {}
                    results[strategy][market] = result
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                    logger.error(f"Backtest failed: {strategy} {market} - {e}")
        
        return results
    
    def get_strategy_summary(
        self,
        results: Dict[str, Dict[str, BacktestResult]]
    ) -> Dict[str, Dict[str, Any]]:
        """Aggregate performance per strategy

        Returns:
            {strategy: {total_trades, avg_win_rate, avg_roi, ...}}
        """
        summary = {}
        
        for strategy, market_results in results.items():
            if not market_results:
                continue
            
            total_trades = sum(r.total_trades for r in market_results.values())
            total_wins = sum(r.wins for r in market_results.values())
            total_losses = sum(r.losses for r in market_results.values())
            
            win_rate = (total_wins / (total_wins + total_losses) * 100.0) if (total_wins + total_losses) > 0 else 0.0
            
            avg_roi = sum(r.roi_pct for r in market_results.values()) / len(market_results)
            max_roi = max(r.roi_pct for r in market_results.values())
            min_roi = min(r.roi_pct for r in market_results.values())
            
            avg_mdd = sum(r.max_drawdown_pct for r in market_results.values()) / len(market_results)
            
            summary[strategy] = {
                "total_trades": total_trades,
                "wins": total_wins,
                "losses": total_losses,
                "win_rate": win_rate,
                "avg_roi_pct": avg_roi,
                "max_roi_pct": max_roi,
                "min_roi_pct": min_roi,
                "avg_max_drawdown_pct": avg_mdd,
                "markets_tested": len(market_results)
            }
        
        return summary
    
    def _parse_candle_time(self, candle: Dict) -> float:
        """Convert candle time to a Unix timestamp"""
        from datetime import datetime
        time_str = candle.get("candle_date_time_kst", "")
        try:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except (OSError, TypeError, ValueError):
            logger.warning("[BacktestRunner] candle time parse failed: %s", time_str, exc_info=True)
            return time.time()
    
    def _create_empty_result(self, strategy: str, market: str) -> BacktestResult:
        """Create an empty result"""
        return BacktestResult(
            strategy=strategy,
            market=market,
            start_time=time.time(),
            end_time=time.time(),
            initial_capital=0.0,
            final_capital=0.0
        )
