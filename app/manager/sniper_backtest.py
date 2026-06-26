"""
SNIPER strategy backtest module.
Validates SNIPER parameter performance using historical candle data.

[FIX L3] 2026-03-05: Added fee/slippage accounting and trailing stop support.
Still differs from the real SNIPER state machine (WATCH->PROBE->CONFIRM),
but PnL calculation now reflects real trading costs.
"""

import logging
from typing import Dict, Any, List
from dataclasses import dataclass
import requests
from app.core.rate_limiter import bybit_get
from app.core.constants import BYBIT_MARKET_KLINE, bybit_v5_rest_category, parse_bybit_list

logger = logging.getLogger(__name__)

# Default real trading costs (Upbit basis)
DEFAULT_FEE_RATE = 0.0005   # 0.05% buy + 0.05% sell = per-side basis
DEFAULT_SLIPPAGE = 0.001    # 0.1% slippage (fill uncertainty)


@dataclass
class BacktestResult:
    """Backtest result."""
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_profit_pct: float
    avg_profit_pct: float
    max_drawdown_pct: float
    trades: List[Dict[str, Any]]


def fetch_candles(market: str, count: int = 200, unit: int = 1) -> List[Dict[str, Any]]:
    """Fetches candle data from Bybit V5."""
    try:
        resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": str(unit), "limit": count}, timeout=10)
        if resp.ok:
            raw = parse_bybit_list(resp.json())
            candles = [{"opening_price": float(k[1]), "high_price": float(k[2]), "low_price": float(k[3]), "trade_price": float(k[4]), "candle_acc_trade_volume": float(k[5]), "timestamp": int(k[0])} for k in raw if isinstance(k, (list, tuple)) and len(k) >= 6]
            return list(reversed(candles))
        return []
    except (requests.RequestException, ValueError):
        logger.warning("[SniperBacktest] _fetch_candles(%s) failed", market, exc_info=True)
        return []


def run_sniper_backtest(
    market: str,
    entry_lookback_min: int = 15,
    entry_threshold_pct: float = 0.3,
    exit_lookback_min: int = 15,
    exit_threshold_pct: float = 0.3,
    tp_pct: float = 2.0,
    sl_pct: float = 1.5,
    trail_tp: bool = False,
    trail_dist_pct: float = 0.3,
    fee_rate: float = DEFAULT_FEE_RATE,
    slippage: float = DEFAULT_SLIPPAGE,
    candle_count: int = 200,
    candle_unit: int = 1,
) -> BacktestResult:
    """
    Runs the SNIPER strategy backtest.

    Args:
        market: Market code (e.g. BTCUSDT)
        entry_lookback_min: Entry lookback period (minutes)
        entry_threshold_pct: Entry threshold (%)
        exit_lookback_min: Exit lookback period (minutes)
        exit_threshold_pct: Exit threshold (%)
        tp_pct: Take-profit ratio (%)
        sl_pct: Stop-loss ratio (%)
        trail_tp: Whether to enable trailing stop (uses trail_dist_pct when True)
        trail_dist_pct: Trailing stop distance (%)
        fee_rate: Per-side fee (default 0.05%)
        slippage: Slippage (default 0.1%)
        candle_count: Number of candles
        candle_unit: Candle unit (minutes)

    Returns:
        BacktestResult: Backtest result (with fee/slippage applied)
    """
    candles = fetch_candles(market, candle_count, candle_unit)
    if not candles or len(candles) < max(entry_lookback_min, exit_lookback_min) + 10:
        return BacktestResult(
            total_trades=0, wins=0, losses=0, win_rate=0.0,
            total_profit_pct=0.0, avg_profit_pct=0.0, max_drawdown_pct=0.0, trades=[]
        )
    
    prices = [float(c.get("trade_price", 0)) for c in candles]
    highs = [float(c.get("high_price", 0)) for c in candles]
    lows = [float(c.get("low_price", 0)) for c in candles]
    
    # Sum of fee + slippage (buy + sell round trip)
    round_trip_cost_pct = (fee_rate + slippage) * 2 * 100
    
    trades: List[Dict[str, Any]] = []
    position: Dict[str, Any] | None = None
    equity_curve = [0.0]

    # [FIX N4] Convert lookback minutes -> number of candles (accounting for candle_unit)
    entry_lookback_candles = max(1, entry_lookback_min // candle_unit) if candle_unit > 0 else entry_lookback_min
    exit_lookback_candles = max(1, exit_lookback_min // candle_unit) if candle_unit > 0 else exit_lookback_min

    for i in range(max(entry_lookback_candles, exit_lookback_candles), len(prices)):
        price = prices[i]
        
        if position is None:
            lookback_start = max(0, i - entry_lookback_candles)
            recent_lows = lows[lookback_start:i]
            if recent_lows:
                period_low = min(recent_lows)
                entry_target = period_low * (1 + entry_threshold_pct / 100)
                
                if price <= entry_target:
                    position = {"entry_price": price, "entry_idx": i, "trail_peak": price}
        else:
            entry_price = position["entry_price"]
            gross_profit_pct = (price - entry_price) / entry_price * 100
            # Actual PnL = gross PnL - round trip cost
            profit_pct = gross_profit_pct - round_trip_cost_pct
            
            exit_signal = False
            exit_reason = ""
            
            # Trailing stop update
            if trail_tp and gross_profit_pct >= tp_pct:
                if price > position["trail_peak"]:
                    position["trail_peak"] = price
                trail_stop_price = position["trail_peak"] * (1 - trail_dist_pct / 100)
                if price <= trail_stop_price:
                    exit_signal = True
                    exit_reason = "trail_tp"
            elif gross_profit_pct >= tp_pct:
                exit_signal = True
                exit_reason = "tp"
            elif gross_profit_pct <= -sl_pct:
                exit_signal = True
                exit_reason = "sl"
            else:
                lookback_start = max(0, i - exit_lookback_candles)
                recent_highs = highs[lookback_start:i]
                if recent_highs:
                    period_high = max(recent_highs)
                    exit_target = period_high * (1 - exit_threshold_pct / 100)
                    
                    if price >= exit_target:
                        exit_signal = True
                        exit_reason = "near_high"
            
            if exit_signal:
                trades.append({
                    "entry_idx": position["entry_idx"],
                    "exit_idx": i,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "gross_profit_pct": round(gross_profit_pct, 4),
                    "profit_pct": round(profit_pct, 4),  # fee/slippage applied
                    "cost_pct": round(round_trip_cost_pct, 4),
                    "reason": exit_reason,
                })
                equity_curve.append(equity_curve[-1] + profit_pct)
                position = None
    
    if position:
        final_price = prices[-1]
        gross_profit_pct = (final_price - position["entry_price"]) / position["entry_price"] * 100
        profit_pct = gross_profit_pct - round_trip_cost_pct
        trades.append({
            "entry_idx": position["entry_idx"],
            "exit_idx": len(prices) - 1,
            "entry_price": position["entry_price"],
            "exit_price": final_price,
            "gross_profit_pct": round(gross_profit_pct, 4),
            "profit_pct": round(profit_pct, 4),
            "cost_pct": round(round_trip_cost_pct, 4),
            "reason": "open",
        })
        equity_curve.append(equity_curve[-1] + profit_pct)
    
    total_trades = len(trades)
    wins = len([t for t in trades if t["profit_pct"] > 0])
    losses = len([t for t in trades if t["profit_pct"] < 0])  # [FIX #12] break-even(=0) excluded from loss
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    total_profit_pct = sum(t["profit_pct"] for t in trades)
    avg_profit_pct = (total_profit_pct / total_trades) if total_trades > 0 else 0.0
    
    peak = 0.0
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    
    return BacktestResult(
        total_trades=total_trades,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        total_profit_pct=total_profit_pct,
        avg_profit_pct=avg_profit_pct,
        max_drawdown_pct=max_dd,
        trades=trades,
    )
