# ============================================================
# File: app/backtest/strategy_simulator.py
# Autocoin OS v3-H — Strategy Simulator for Backtesting
# ------------------------------------------------------------
# Generate trade signals per strategy (simplified logic)
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class StrategySimulator:
    """Strategy simulator (for backtesting) - simplified standalone logic"""
    
    def __init__(self, strategy_name: str):
        self.strategy_name = strategy_name.upper()
        self.strategy = self.strategy_name  # alias for backwards compat

        supported = ["PINGPONG", "AUTOLOOP", "LADDER", "LIGHTNING", "GAZUA", "CONTRARIAN", "SNIPER"]
        if self.strategy_name not in supported:
            raise ValueError(f"Unsupported strategy: {strategy_name}. Supported: {supported}")
    
    def generate_entry_signal(
        self,
        candles: List[Dict[str, Any]],
        current_idx: int
    ) -> Optional[Dict[str, Any]]:
        """Generate entry signal

        Args:
            candles: candle data (sorted oldest to newest)
            current_idx: current candle index

        Returns:
            Entry signal info or None
            {
                "entry_price": float,
                "tp_pct": float,
                "sl_pct": float,
                "reason": str
            }
        """
        if current_idx < 20:  # need at least 20 candles (for indicator calc)
            return None

        # Build context from candles up to now
        recent_candles = candles[max(0, current_idx - 100):current_idx + 1]
        current_candle = candles[current_idx]

        try:
            # Price and indicator calculation
            close_prices = [c["trade_price"] for c in recent_candles]
            volumes = [c["candle_acc_trade_volume"] for c in recent_candles]

            current_price = current_candle["trade_price"]

            # Check entry conditions per strategy (simple version)
            if self.strategy_name == "PINGPONG":
                return self._check_pingpong_entry(close_prices, current_price)
            elif self.strategy_name == "AUTOLOOP":
                return self._check_autoloop_entry(close_prices, current_price)
            elif self.strategy_name == "LADDER":
                return self._check_ladder_entry(close_prices, current_price)
            elif self.strategy_name == "LIGHTNING":
                return self._check_lightning_entry(close_prices, volumes, current_price)
            elif self.strategy_name == "GAZUA":
                return self._check_gazua_entry(close_prices, current_price)
            elif self.strategy_name == "CONTRARIAN":
                return self._check_contrarian_entry(close_prices, current_price)
            elif self.strategy_name == "SNIPER":
                return self._check_sniper_entry(close_prices, current_price)
            
        except (KeyError, AttributeError, TypeError) as e:
            logger.error(f"Error generating entry signal: {e}")
        
        return None
    
    def _check_pingpong_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """PINGPONG entry condition (simple version)"""
        if len(prices) < 20:
            return None

        # 20-period average
        ma20 = sum(prices[-20:]) / 20

        # Price 2% below average → enter
        if current_price < ma20 * 0.98:
            return {
                "entry_price": current_price,
                "tp_pct": 3.0,
                "sl_pct": -5.0,
                "reason": "below_ma20"
            }
        
        return None
    
    def _check_autoloop_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """AUTOLOOP entry condition"""
        if len(prices) < 30:
            return None

        # Volatility check
        recent_high = max(prices[-30:])
        recent_low = min(prices[-30:])
        volatility = (recent_high - recent_low) / recent_low * 100

        # Volatility >= 10% + mid price range
        mid_price = (recent_high + recent_low) / 2
        
        if volatility >= 10 and abs(current_price - mid_price) / mid_price < 0.05:
            return {
                "entry_price": current_price,
                "tp_pct": 5.0,
                "sl_pct": -7.0,
                "reason": "high_volatility"
            }
        
        return None
    
    def _check_ladder_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """LADDER entry condition (downtrend)"""
        if len(prices) < 20:
            return None

        # Recent 10-period downtrend
        ma10_old = sum(prices[-20:-10]) / 10
        ma10_new = sum(prices[-10:]) / 10

        if ma10_new < ma10_old * 0.95:  # 5% drop
            return {
                "entry_price": current_price,
                "tp_pct": 10.0,
                "sl_pct": -15.0,
                "reason": "downtrend"
            }
        
        return None
    
    def _check_lightning_entry(self, prices: List[float], volumes: List[float], current_price: float) -> Optional[Dict]:
        """LIGHTNING entry condition (surge)"""
        if len(prices) < 10:
            return None

        # Check recent 5-bar surge (>= 2%)
        price_change = (prices[-1] - prices[-5]) / prices[-5] * 100

        # Volume spike
        avg_volume = sum(volumes[-20:-5]) / 15 if len(volumes) >= 20 else sum(volumes) / len(volumes)
        current_volume = volumes[-1]
        
        if price_change >= 2.0 and current_volume > avg_volume * 2:
            return {
                "entry_price": current_price,
                "tp_pct": 5.0,
                "sl_pct": -10.0,
                "reason": "surge"
            }
        
        return None
    
    def _check_gazua_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """GAZUA entry condition (strong uptrend)"""
        if len(prices) < 30:
            return None

        # Recent 20-period uptrend
        ma20 = sum(prices[-20:]) / 20

        if current_price > ma20 * 1.1:  # 10% above average
            return {
                "entry_price": current_price,
                "tp_pct": 25.0,
                "sl_pct": -25.0,
                "reason": "strong_uptrend"
            }
        
        return None
    
    def _check_contrarian_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """CONTRARIAN entry condition (oversold)"""
        if len(prices) < 30:
            return None

        # RSI calculation (simple version)
        changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [max(c, 0) for c in changes[-14:]]
        losses = [abs(min(c, 0)) for c in changes[-14:]]
        
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        
        if avg_loss == 0:
            return None
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        # RSI < 30 (oversold)
        if rsi < 30:
            return {
                "entry_price": current_price,
                "tp_pct": 15.0,
                "sl_pct": -70.0,  # extreme SL
                "reason": "oversold"
            }
        
        return None
    
    def _check_sniper_entry(self, prices: List[float], current_price: float) -> Optional[Dict]:
        """SNIPER entry condition (early surge)"""
        if len(prices) < 20:
            return None

        # Recent 3-bar surge (>= 1.5%)
        price_change = (prices[-1] - prices[-3]) / prices[-3] * 100

        # Above EMA
        ema20 = self._calc_ema(prices, 20)
        
        if price_change >= 1.5 and current_price > ema20:
            return {
                "entry_price": current_price,
                "tp_pct": 3.0,
                "sl_pct": -5.0,
                "reason": "early_surge"
            }
        
        return None
    
    def _calc_ema(self, prices: List[float], period: int) -> float:
        """EMA calculation"""
        if len(prices) < period:
            return sum(prices) / len(prices)
        
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
