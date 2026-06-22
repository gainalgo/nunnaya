# ============================================================
# File: app/backtest/backtest_runner.py
# Autocoin OS v3-H — Backtest Runner
# ------------------------------------------------------------
# 전략별 백테스팅 실행 및 결과 관리
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
    """백테스팅 실행 관리자"""
    
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
        """단일 전략-마켓 백테스팅 실행
        
        Args:
            strategy: 전략 이름
            market: 마켓 코드
            days: 백테스팅 기간 (일)
            initial_capital: 초기 자본
            budget_per_trade: 거래당 예산
        
        Returns:
            백테스팅 결과
        """
        logger.info(f"Starting backtest: {strategy} on {market} ({days} days)")
        
        # 캔들 데이터 로드
        candles = self.candle_loader.load_candles(market, days=days, interval_minutes=60)
        
        if not candles:
            logger.warning(f"No candle data for {market}")
            return self._create_empty_result(strategy, market)
        
        # 백테스팅 엔진 초기화
        engine = BacktestEngine(initial_capital=initial_capital)
        
        # 전략 시뮬레이터
        simulator = StrategySimulator(strategy)
        
        # 시뮬레이션 실행
        start_time = self._parse_candle_time(candles[0])
        end_time = self._parse_candle_time(candles[-1])
        
        for idx, candle in enumerate(candles):
            current_time = self._parse_candle_time(candle)
            current_price = candle["trade_price"]
            
            # 기존 포지션 TP/SL 체크
            if market in engine.positions:
                engine.check_tp_sl(current_time, current_price, market)
            
            # 새 진입 시그널 체크 (포지션 없을 때만)
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
            
            # 자산 곡선 업데이트
            if idx % 24 == 0:  # 하루마다
                engine.update_equity_curve(current_time, {market: current_price})
        
        # 남은 포지션 청산
        for market_key in list(engine.positions.keys()):
            last_candle = candles[-1]
            engine.close_position(
                market_key,
                end_time,
                last_candle["trade_price"],
                "end_of_backtest"
            )
        
        # 결과 생성
        result = engine.get_result(strategy, market, start_time, end_time)
        
        # 캐시 저장
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
        """여러 전략-마켓 백테스팅 실행
        
        Args:
            strategies: 전략 리스트
            markets: 마켓 리스트
            days: 백테스팅 기간
            parallel: 병렬 실행 여부
        
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
        """전략별 종합 성과
        
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
        """캔들 시간을 Unix timestamp로 변환"""
        from datetime import datetime
        time_str = candle.get("candle_date_time_kst", "")
        try:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except (OSError, TypeError, ValueError):
            logger.warning("[BacktestRunner] candle time parse failed: %s", time_str, exc_info=True)
            return time.time()
    
    def _create_empty_result(self, strategy: str, market: str) -> BacktestResult:
        """빈 결과 생성"""
        return BacktestResult(
            strategy=strategy,
            market=market,
            start_time=time.time(),
            end_time=time.time(),
            initial_capital=0.0,
            final_capital=0.0
        )
