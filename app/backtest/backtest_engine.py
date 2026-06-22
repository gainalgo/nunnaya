# ============================================================
# File: app/backtest/backtest_engine.py
# Autocoin OS v3-H — Backtest Engine
# ------------------------------------------------------------
# 과거 캔들 데이터로 전략 시뮬레이션
# ============================================================

from __future__ import annotations

import time
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class BacktestPosition:
    """백테스팅 포지션"""
    market: str
    strategy: str
    entry_time: float
    entry_price: float
    quantity: float
    budget_usdt: float
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    exit_time: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    pnl_usdt: float = 0.0
    roi_pct: float = 0.0


@dataclass
class BacktestResult:
    """백테스팅 결과"""
    strategy: str
    market: str
    start_time: float
    end_time: float
    initial_capital: float
    final_capital: float
    
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    
    total_pnl_usdt: float = 0.0
    total_fees_usdt: float = 0.0
    roi_pct: float = 0.0
    
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    
    positions: List[BacktestPosition] = field(default_factory=list)
    equity_curve: List[tuple] = field(default_factory=list)  # (time, equity)


class BacktestEngine:
    """백테스팅 엔진"""
    
    def __init__(
        self,
        *,
        initial_capital: float = 1000.0,
        fee_rate: float = 0.0005,  # Bybit 수수료 0.05%
        slippage_pct: float = 0.1  # 슬리피지 0.1%
    ):
        self.initial_capital = initial_capital
        self.fee_rate = fee_rate
        self.slippage_pct = slippage_pct
        
        # 시뮬레이션 상태
        self.current_capital = initial_capital
        self.cash = initial_capital  # alias for current_capital (for test compatibility)
        self.positions: Dict[str, BacktestPosition] = {}  # market -> position
        self.closed_positions: List[BacktestPosition] = []
        self.equity_curve: List[tuple] = []
    
    def reset(self):
        """시뮬레이션 상태 초기화"""
        self.current_capital = self.initial_capital
        self.cash = self.initial_capital
        self.positions.clear()
        self.closed_positions.clear()
        self.equity_curve.clear()
    
    def can_open_position(self, budget_usdt: float) -> bool:
        """포지션 진입 가능 여부"""
        return self.current_capital >= budget_usdt
    
    def open_position(
        self,
        market: str,
        strategy: str,
        entry_time: float,
        entry_price: float,
        budget_usdt: float,
        tp_pct: float = 5.0,
        sl_pct: float = -3.0
    ) -> Optional[BacktestPosition]:
        """포지션 진입
        
        Args:
            market: 마켓
            strategy: 전략
            entry_time: 진입 시각
            entry_price: 진입 가격
            budget_usdt: 투자 금액
            tp_pct: 익절 %
            sl_pct: 손절 % (음수)
        
        Returns:
            생성된 포지션 또는 None
        """
        if not self.can_open_position(budget_usdt):
            return None
        
        if market in self.positions:
            # 이미 포지션 있음
            return None
        
        # 슬리피지 적용
        actual_entry_price = entry_price * (1 + self.slippage_pct / 100.0)
        
        # 수수료 차감
        fee = budget_usdt * self.fee_rate
        net_budget = budget_usdt - fee
        
        # 수량 계산
        quantity = net_budget / actual_entry_price
        
        # TP/SL 가격 계산
        tp_price = actual_entry_price * (1 + tp_pct / 100.0) if tp_pct > 0 else None
        sl_price = actual_entry_price * (1 + sl_pct / 100.0) if sl_pct < 0 else None
        
        position = BacktestPosition(
            market=market,
            strategy=strategy,
            entry_time=entry_time,
            entry_price=actual_entry_price,
            quantity=quantity,
            budget_usdt=budget_usdt,
            tp_price=tp_price,
            sl_price=sl_price
        )
        
        self.positions[market] = position
        self.current_capital -= budget_usdt
        
        logger.debug(
            f"OPEN {strategy} {market} @ {actual_entry_price:,.0f} "
            f"({budget_usdt:,.0f} USDT, qty={quantity:.8f})"
        )
        
        return position
    
    def close_position(
        self,
        market: str,
        exit_time: float,
        exit_price: float,
        reason: str = "signal"
    ) -> Optional[BacktestPosition]:
        """포지션 청산
        
        Args:
            market: 마켓
            exit_time: 청산 시각
            exit_price: 청산 가격
            reason: 청산 이유
        
        Returns:
            청산된 포지션 또는 None
        """
        position = self.positions.pop(market, None)
        if not position:
            return None
        
        # 슬리피지 적용
        actual_exit_price = exit_price * (1 - self.slippage_pct / 100.0)
        
        # 매도 금액
        sell_value = position.quantity * actual_exit_price
        
        # 수수료 차감
        fee = sell_value * self.fee_rate
        net_proceeds = sell_value - fee
        
        # 손익 계산
        pnl_usdt = net_proceeds - position.budget_usdt
        roi_pct = (pnl_usdt / position.budget_usdt) * 100.0 if position.budget_usdt > 0 else 0.0
        
        # 포지션 업데이트
        position.exit_time = exit_time
        position.exit_price = actual_exit_price
        position.exit_reason = reason
        position.pnl_usdt = pnl_usdt
        position.roi_pct = roi_pct
        
        # 자본 회수
        self.current_capital += net_proceeds
        
        self.closed_positions.append(position)
        
        logger.debug(
            f"CLOSE {position.strategy} {market} @ {actual_exit_price:,.0f} "
            f"({reason}) PnL: {pnl_usdt:+,.0f} USDT ({roi_pct:+.2f}%)"
        )
        
        return position
    
    def check_tp_sl(self, current_time: float, current_price: float, market: str) -> bool:
        """TP/SL 체크 및 청산
        
        Returns:
            청산 여부
        """
        position = self.positions.get(market)
        if not position:
            return False
        
        # TP 체크
        if position.tp_price and current_price >= position.tp_price:
            self.close_position(market, current_time, current_price, "TP")
            return True
        
        # SL 체크
        if position.sl_price and current_price <= position.sl_price:
            self.close_position(market, current_time, current_price, "SL")
            return True
        
        return False
    
    def update_equity_curve(self, current_time: float, market_prices: Dict[str, float]):
        """자산 곡선 업데이트
        
        Args:
            current_time: 현재 시각
            market_prices: 마켓별 현재 가격
        """
        # 현재 보유 포지션 평가
        position_value = sum(
            pos.quantity * market_prices.get(pos.market, pos.entry_price)
            for pos in self.positions.values()
        )
        
        total_equity = self.current_capital + position_value
        self.equity_curve.append((current_time, total_equity))
    
    def get_result(self, strategy: str, market: str, start_time: float, end_time: float) -> BacktestResult:
        """백테스팅 결과 생성"""
        wins = sum(1 for p in self.closed_positions if p.pnl_usdt > 0)
        losses = sum(1 for p in self.closed_positions if p.pnl_usdt < 0)
        total_trades = len(self.closed_positions)
        
        win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
        
        total_pnl = sum(p.pnl_usdt for p in self.closed_positions)
        final_capital = self.current_capital + sum(
            p.quantity * p.entry_price for p in self.positions.values()
        )
        
        roi_pct = ((final_capital - self.initial_capital) / self.initial_capital) * 100.0
        
        # MDD 계산
        max_drawdown_pct = self._calculate_max_drawdown()
        
        # Sharpe Ratio 계산 (간단 버전)
        sharpe_ratio = self._calculate_sharpe_ratio()
        
        return BacktestResult(
            strategy=strategy,
            market=market,
            start_time=start_time,
            end_time=end_time,
            initial_capital=self.initial_capital,
            final_capital=final_capital,
            total_trades=total_trades,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            total_pnl_usdt=total_pnl,
            roi_pct=roi_pct,
            max_drawdown_pct=max_drawdown_pct,
            sharpe_ratio=sharpe_ratio,
            positions=self.closed_positions.copy(),
            equity_curve=self.equity_curve.copy()
        )
    
    def _calculate_max_drawdown(self) -> float:
        """최대 낙폭 계산"""
        if not self.equity_curve:
            return 0.0
        
        peak = self.initial_capital
        max_dd = 0.0
        
        for _, equity in self.equity_curve:
            if equity > peak:
                peak = equity
            dd = ((peak - equity) / peak) * 100.0 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        
        return max_dd
    
    def _calculate_sharpe_ratio(self) -> float:
        """샤프 비율 계산 (간단 버전)"""
        if len(self.closed_positions) < 2:
            return 0.0
        
        returns = [p.roi_pct for p in self.closed_positions]
        
        if not returns:
            return 0.0
        
        avg_return = sum(returns) / len(returns)
        
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
        std_dev = variance ** 0.5
        
        if std_dev == 0:
            return 0.0
        
        # 연율화 (가정: 거래당 1일)
        sharpe = (avg_return / std_dev) * (252 ** 0.5)
        
        return sharpe
