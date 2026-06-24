"""
Cross Exchange Signal Provider
Central signal service that feeds cross-exchange difference data to strategies
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
from decimal import Decimal

logger = logging.getLogger(__name__)


@dataclass
class CrossExchangeSignal:
    """Cross-exchange signal"""
    coin: str                          # "BTC"

    # Arbitrage info
    arbitrage_pct: float               # Max spread % (0.5 = 0.5%)
    arbitrage_direction: str           # "BYBIT→BITHUMB", "BITHUMB→BYBIT", "NONE"
    arbitrage_profit_estimate: float   # Estimated profit (USDT, per 1M USDT)

    # Kimchi premium
    kimchi_premium_pct: float          # Kimchi premium % (-2.0 = -2%, 3.0 = +3%)
    kimchi_signal: str                 # "OVERHEATED", "NORMAL", "UNDERVALUED"

    # Leading indicator (Binance moves before Bybit)
    leading_signal: Optional[str]      # "UP" (Binance up → Bybit to follow), "DOWN", None
    leading_confidence: float          # Confidence (0.0 ~ 1.0)
    leading_change_pct: float          # Binance change rate (last 5 min)

    # Liquidity
    bybit_volume_24h: float            # Bybit 24h volume (USDT)
    binance_volume_24h: float          # Binance 24h volume (USDT)
    liquidity_score: float             # Liquidity score (0~1, 1=excellent)

    # Meta
    timestamp: float                   # Signal creation time
    data_age_sec: float                # Data age (seconds)


class CrossExchangeSignalProvider:
    """
    Cross-exchange signal provider

    Service that refines Cross Exchange Monitor data so strategies can
    consume it easily
    """

    def __init__(self):
        self._signals: Dict[str, CrossExchangeSignal] = {}
        self._last_update: float = 0.0
        self._cache_ttl_sec: float = 10.0  # 10s cache
    
    def update_from_monitor(self, monitor_data: Dict[str, Any]):
        """
        Update data from Cross Exchange Monitor

        Args:
            monitor_data: {
                "arbitrage": [ArbitrageOpportunity, ...],
                "kimchi": [KimchiPremium, ...],
                "leading": [LeadingIndicatorSignal, ...],
                "prices": {...}
            }
        """
        try:
            now = time.time()

            # Reset existing signals (remove stale ones)
            self._clean_stale_signals(now)

            # Process arbitrage data
            arbitrage_map = self._process_arbitrage(monitor_data.get("arbitrage", []))

            # Process kimchi premium data
            kimchi_map = self._process_kimchi(monitor_data.get("kimchi", []))

            # Process leading indicator data
            leading_map = self._process_leading(monitor_data.get("leading", []))

            # Process price data
            prices = monitor_data.get("prices", {})
            volume_map = self._process_volumes(prices)

            # Build combined signals
            all_coins = set()
            all_coins.update(arbitrage_map.keys())
            all_coins.update(kimchi_map.keys())
            all_coins.update(leading_map.keys())
            all_coins.update(volume_map.keys())
            
            for coin in all_coins:
                signal = CrossExchangeSignal(
                    coin=coin,

                    # Arbitrage
                    arbitrage_pct=arbitrage_map.get(coin, {}).get("pct", 0.0),
                    arbitrage_direction=arbitrage_map.get(coin, {}).get("direction", "NONE"),
                    arbitrage_profit_estimate=arbitrage_map.get(coin, {}).get("profit", 0.0),

                    # Kimchi premium
                    kimchi_premium_pct=kimchi_map.get(coin, {}).get("premium_pct", 0.0),
                    kimchi_signal=kimchi_map.get(coin, {}).get("signal", "NORMAL"),

                    # Leading indicator
                    leading_signal=leading_map.get(coin, {}).get("signal"),
                    leading_confidence=leading_map.get(coin, {}).get("confidence", 0.0),
                    leading_change_pct=leading_map.get(coin, {}).get("change_pct", 0.0),

                    # Liquidity
                    bybit_volume_24h=volume_map.get(coin, {}).get("bybit_volume", 0.0),
                    binance_volume_24h=volume_map.get(coin, {}).get("binance_volume", 0.0),
                    liquidity_score=volume_map.get(coin, {}).get("liquidity_score", 0.5),
                    
                    timestamp=now,
                    data_age_sec=0.0
                )
                
                self._signals[coin] = signal
            
            self._last_update = now
            logger.debug(f"Updated {len(self._signals)} cross-exchange signals")
            
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
            logger.error(f"Failed to update cross-exchange signals: {e}", exc_info=True)
    
    def update_signal(
        self,
        coin: str,
        liquidity_score: float,
        arbitrage_pct: float = 0.0,
        arbitrage_direction: str = "",
        kimchi_premium_pct: float = 0.0,
        leading_signal: str = "",
        leading_confidence: float = 0.0,
        leading_change_pct: float = 0.0
    ):
        """
        Update a single coin's signal (called directly by the Monitor)

        Args:
            coin: Coin symbol (e.g. "BTC")
            liquidity_score: Liquidity score (0~1)
            arbitrage_pct: Arbitrage % (default 0.0)
            arbitrage_direction: Arbitrage direction (default empty string)
            kimchi_premium_pct: Kimchi premium % (default 0.0)
            leading_signal: Leading indicator ("UP", "DOWN", empty string)
            leading_confidence: Leading indicator confidence (0~1)
            leading_change_pct: Leading change rate % (default 0.0)
        """
        try:
            clean_coin = coin.upper().replace("USDT", "").replace("_USDT", "")
            now = time.time()

            # Compute kimchi signal
            kimchi_signal = "NORMAL"
            if kimchi_premium_pct > 5.0:
                kimchi_signal = "OVERHEATED"
            elif kimchi_premium_pct < -1.0:
                kimchi_signal = "UNDERVALUED"
            
            signal = CrossExchangeSignal(
                coin=clean_coin,
                arbitrage_pct=arbitrage_pct,
                arbitrage_direction=arbitrage_direction or "NONE",
                arbitrage_profit_estimate=0.0,  # Not computed by the Monitor
                kimchi_premium_pct=kimchi_premium_pct,
                kimchi_signal=kimchi_signal,
                leading_signal=leading_signal if leading_signal else None,
                leading_confidence=leading_confidence,
                leading_change_pct=leading_change_pct,
                bybit_volume_24h=0.0,  # Simplified
                binance_volume_24h=0.0,  # Simplified
                liquidity_score=liquidity_score,
                timestamp=now,
                data_age_sec=0.0
            )
            
            self._signals[clean_coin] = signal
            self._last_update = now
            
        except (OSError, AttributeError, TypeError, ValueError, OverflowError) as e:
            logger.error(f"Failed to update signal for {coin}: {e}")
    
    def get_signal(self, coin: str) -> Optional[CrossExchangeSignal]:
        """
        Look up the signal for a specific coin

        Args:
            coin: Coin symbol (e.g. "BTC", "ETH")

        Returns:
            CrossExchangeSignal or None
        """
        # Normalize coin name (USDT-BTC → BTC)
        clean_coin = coin.upper().replace("USDT", "").replace("_USDT", "")

        signal = self._signals.get(clean_coin)

        if signal:
            # Update data age
            signal.data_age_sec = time.time() - signal.timestamp

            # Return None if too old
            if signal.data_age_sec > 60.0:  # Over 1 minute
                return None
        
        return signal
    
    def get_all_signals(self) -> Dict[str, CrossExchangeSignal]:
        """Look up all signals (latest only)"""
        now = time.time()
        return {
            coin: signal 
            for coin, signal in self._signals.items() 
            if now - signal.timestamp < 60.0
        }
    
    def _clean_stale_signals(self, now: float):
        """Remove stale signals (over 5 minutes)"""
        stale_coins = [
            coin for coin, signal in self._signals.items()
            if now - signal.timestamp > 300.0
        ]
        for coin in stale_coins:
            del self._signals[coin]
    
    def _process_arbitrage(self, opportunities: List[Any]) -> Dict[str, Dict]:
        """Process arbitrage opportunities"""
        result = {}
        for opp in opportunities:
            coin = opp.coin
            if coin not in result or abs(opp.diff_pct) > abs(result[coin]["pct"]):
                result[coin] = {
                    "pct": opp.diff_pct,
                    "direction": f"{opp.buy_exchange}→{opp.sell_exchange}",
                    "profit": float(opp.profit_estimate)
                }
        return result
    
    def _process_kimchi(self, kimchi_list: List[Any]) -> Dict[str, Dict]:
        """Process kimchi premium"""
        result = {}
        for k in kimchi_list:
            result[k.coin] = {
                "premium_pct": k.premium_pct,
                "signal": k.signal
            }
        return result
    
    def _process_leading(self, leading_list: List[Any]) -> Dict[str, Dict]:
        """Process leading indicators"""
        result = {}
        for lead in leading_list:
            coin = lead.coin
            # Keep only the latest / highest-confidence one
            if coin not in result or lead.confidence > result[coin]["confidence"]:
                result[coin] = {
                    "signal": lead.direction,  # "UP" or "DOWN"
                    "confidence": lead.confidence,
                    "change_pct": lead.leader_change_pct
                }
        return result
    
    def _process_volumes(self, prices: Dict[str, Dict]) -> Dict[str, Dict]:
        """Process volumes"""
        result = {}
        
        for coin in prices.get("BYBIT", {}).keys():
            bybit_ticker = prices.get("BYBIT", {}).get(coin)
            binance_ticker = prices.get("BINANCE", {}).get(coin)
            
            bybit_vol = float(bybit_ticker.volume_24h) if bybit_ticker else 0.0
            binance_vol = float(binance_ticker.volume_24h) if binance_ticker else 0.0
            
            # Liquidity score: higher volume is better
            # Simplified: based on the sum of both exchanges' volumes
            total_vol = bybit_vol + binance_vol
            liquidity_score = min(1.0, total_vol / 1_000_000.0)  # Relative to 1M USDT
            
            result[coin] = {
                "bybit_volume": bybit_vol,
                "binance_volume": binance_vol,
                "liquidity_score": liquidity_score
            }
        
        return result
    
    def get_stats(self) -> Dict[str, Any]:
        """Statistics info"""
        now = time.time()
        active_signals = [s for s in self._signals.values() if now - s.timestamp < 60.0]
        
        return {
            "total_signals": len(active_signals),
            "last_update": self._last_update,
            "data_age_sec": now - self._last_update if self._last_update > 0 else 999.0,
            "coins": list(self._signals.keys())
        }


# Singleton instance
_signal_provider = CrossExchangeSignalProvider()


def get_cross_exchange_signal_provider() -> CrossExchangeSignalProvider:
    """Return the global signal provider"""
    return _signal_provider


def get_signal(coin: str) -> Optional[CrossExchangeSignal]:
    """Convenience function: look up a coin's signal"""
    return _signal_provider.get_signal(coin)
