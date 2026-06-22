# ============================================================
# File: app/ai/labeling.py
# Autocoin OS v3-H — 실제 거래 결과 기반 라벨링
# ============================================================
"""
기존 방식: 5분 후 가격 상승 여부 예측 (노이즈 많음)
개선 방식: 실제 FILL_BUY → FILL_SELL 거래 결과를 라벨로 사용

라벨 정의:
- target=1: 수익 거래 (profit_pct > 0)
- target=0: 손실 거래 (profit_pct <= 0)
"""

from __future__ import annotations

import json
import logging
import os
import glob
import time
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class TradeRecord:
    """매수-매도 쌍을 나타내는 거래 기록"""
    market: str
    buy_ts: float
    buy_price: float
    buy_qty: float
    buy_reason: str = ""
    
    sell_ts: Optional[float] = None
    sell_price: Optional[float] = None
    sell_reason: str = ""
    profit_pct: Optional[float] = None
    profit_usdt: Optional[float] = None
    
    # 매수 시점의 market snapshot (features)
    snapshot: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_complete(self) -> bool:
        return self.sell_ts is not None and self.sell_price is not None
    
    @property
    def is_profitable(self) -> bool:
        if self.profit_pct is not None:
            return self.profit_pct > 0
        if self.sell_price and self.buy_price > 0:
            return self.sell_price > self.buy_price
        return False
    
    @property
    def hold_duration_sec(self) -> Optional[float]:
        if self.sell_ts and self.buy_ts:
            return self.sell_ts - self.buy_ts
        return None

class TradeLabeler:
    """
    실제 거래(FILL_BUY → FILL_SELL)를 추적하여 라벨 생성.
    
    장점:
    1. 실제 수익/손실 기반 라벨 → 노이즈 감소
    2. 슬리피지, 수수료 반영된 현실적 라벨
    3. 전략별(reason) 성능 분석 가능
    """
    
    def __init__(self, ledger_dir: str = "runtime"):
        self.ledger_dir = ledger_dir
        self._open_positions: Dict[str, TradeRecord] = {}  # market -> open position
        self._completed_trades: List[TradeRecord] = []
    
    def extract_trades(self, days: float = 14.0) -> List[TradeRecord]:
        """
        trade_ledger에서 FILL_BUY/FILL_SELL 쌍을 추출하여 완료된 거래 목록 생성.
        """
        self._open_positions.clear()
        self._completed_trades.clear()
        
        pattern = os.path.join(self.ledger_dir, "trade_ledger.jsonl*")
        files = sorted(glob.glob(pattern))
        
        cutoff_ts = time.time() - (float(days) * 86400.0)
        
        for file in files:
            try:
                with open(file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            ts = float(rec.get("ts") or 0.0)
                            if ts < cutoff_ts:
                                continue
                            
                            event = rec.get("event", "")
                            market = rec.get("market", "")
                            data = rec.get("data", {})
                            
                            if event == "FILL_BUY" and market:
                                self._handle_fill_buy(ts, market, data)
                            elif event == "FILL_SELL" and market:
                                self._handle_fill_sell(ts, market, data)
                                
                        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                            logger.warning("[Labeling] ledger line parse failed", exc_info=True)
                            continue
            except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[labeling] %s: %s", 'labeling.extract_trades fallback', exc, exc_info=True)
        
        return self._completed_trades
    
    def _handle_fill_buy(self, ts: float, market: str, data: Dict[str, Any]):
        """매수 체결 처리"""
        avg_price = float(data.get("avg_price") or 0.0)
        qty = float(data.get("qty") or 0.0)
        reason = str(data.get("reason") or "")
        
        if avg_price <= 0 or qty <= 0:
            return
        
        # 기존 포지션이 있으면 평균 단가 업데이트 (분할 매수)
        if market in self._open_positions:
            pos = self._open_positions[market]
            total_qty = pos.buy_qty + qty
            pos.buy_price = ((pos.buy_price * pos.buy_qty) + (avg_price * qty)) / total_qty
            pos.buy_qty = total_qty
        else:
            self._open_positions[market] = TradeRecord(
                market=market,
                buy_ts=ts,
                buy_price=avg_price,
                buy_qty=qty,
                buy_reason=reason,
            )
    
    def _handle_fill_sell(self, ts: float, market: str, data: Dict[str, Any]):
        """매도 체결 처리"""
        avg_price = float(data.get("avg_price") or 0.0)
        qty = float(data.get("qty") or 0.0)
        reason = str(data.get("reason") or "")
        profit_pct = data.get("profit_pct")
        profit_usdt = data.get("profit_usdt")
        
        if avg_price <= 0:
            return
        
        # 매칭되는 매수 포지션 찾기
        if market not in self._open_positions:
            return  # 매수 없이 매도 (orphan 등)
        
        pos = self._open_positions[market]
        
        # 거래 완료 처리
        pos.sell_ts = ts
        pos.sell_price = avg_price
        pos.sell_reason = reason
        
        if profit_pct is not None:
            pos.profit_pct = float(profit_pct)
        else:
            # 직접 계산
            if pos.buy_price > 0:
                pos.profit_pct = ((avg_price - pos.buy_price) / pos.buy_price) * 100.0
        
        if profit_usdt is not None:
            pos.profit_usdt = float(profit_usdt)
        
        self._completed_trades.append(pos)
        
        # 부분 매도 처리
        remaining_qty = pos.buy_qty - qty
        if remaining_qty > 0.01:  # 의미있는 잔량
            self._open_positions[market] = TradeRecord(
                market=market,
                buy_ts=pos.buy_ts,
                buy_price=pos.buy_price,
                buy_qty=remaining_qty,
                buy_reason=pos.buy_reason,
            )
        else:
            del self._open_positions[market]
    
    def get_trade_stats(self) -> Dict[str, Any]:
        """거래 통계 요약"""
        if not self._completed_trades:
            return {"total": 0}
        
        total = len(self._completed_trades)
        wins = sum(1 for t in self._completed_trades if t.is_profitable)
        losses = total - wins
        
        profits = [t.profit_pct for t in self._completed_trades if t.profit_pct is not None]
        
        avg_profit = sum(profits) / len(profits) if profits else 0.0
        avg_win = sum(p for p in profits if p > 0) / max(1, wins)
        avg_loss = sum(p for p in profits if p <= 0) / max(1, losses)
        
        # Hold duration 분석
        durations = [t.hold_duration_sec for t in self._completed_trades if t.hold_duration_sec]
        avg_duration_min = (sum(durations) / len(durations) / 60.0) if durations else 0.0
        
        # 전략별 통계
        strategy_stats: Dict[str, Dict[str, Any]] = {}
        for t in self._completed_trades:
            strategy = t.buy_reason or "unknown"
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {"count": 0, "wins": 0, "profit_sum": 0.0}
            strategy_stats[strategy]["count"] += 1
            if t.is_profitable:
                strategy_stats[strategy]["wins"] += 1
            if t.profit_pct is not None:
                strategy_stats[strategy]["profit_sum"] += t.profit_pct
        
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total if total > 0 else 0.0,
            "avg_profit_pct": avg_profit,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "avg_hold_duration_min": avg_duration_min,
            "by_strategy": strategy_stats,
        }

def match_snapshot_to_trade(
    trade: TradeRecord,
    snapshots: List[Dict[str, Any]],
    window_sec: float = 300.0
) -> Optional[Dict[str, Any]]:
    """
    거래의 매수 시점과 가장 가까운 snapshot을 매칭.
    
    Args:
        trade: 거래 기록
        snapshots: AUTOLOOP_SNAPSHOT 등의 리스트 (ts, market 포함)
        window_sec: 매칭 허용 시간 윈도우 (초)
    
    Returns:
        가장 가까운 snapshot 또는 None
    """
    candidates = [
        s for s in snapshots
        if s.get("market") == trade.market
        and abs(float(s.get("ts") or 0) - trade.buy_ts) <= window_sec
    ]
    
    if not candidates:
        return None
    
    # 가장 가까운 snapshot 선택
    return min(candidates, key=lambda s: abs(float(s.get("ts") or 0) - trade.buy_ts))
