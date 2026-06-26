"""
BTC Leading Signal
Altcoin leading trade signal based on BTC movement
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class BTCLeadingSignal:
    """BTC leading signal"""
    direction: str  # 'UP' | 'DOWN' | 'NEUTRAL'
    btc_change_5m: float  # 5-minute change rate
    btc_change_15m: float  # 15-minute change rate
    strength: float  # signal strength (0.0 ~ 1.0)
    confidence: float  # confidence (0.0 ~ 1.0)
    follow_altcoins: bool  # whether altcoins are expected to follow
    timestamp: float

class BTCLeadingSignalDetector:
    """
    BTC leading signal detector

    Principle:
    - BTC 5m candle breaks +2% → altcoins follow (30s~2min lag)
    - BTC 15m candle breaks +5% → strong uptrend → altcoins rally in earnest
    - BTC -2% drop → altcoin crash (preemptive response)
    """
    
    def __init__(
        self,
        trade_client: Any,
        btc_market: str = "BTCUSDT",
        threshold_5m: float = 2.0,    # 5-minute threshold (%)
        threshold_15m: float = 5.0,   # 15-minute threshold (%)
        history_size: int = 100,
    ):
        self.trade_client = trade_client
        self.btc_market = btc_market
        self.threshold_5m = threshold_5m
        self.threshold_15m = threshold_15m
        
        # BTC price history
        self.price_history: deque = deque(maxlen=history_size)

        # Over-call prevention: minimum interval for price collection / signal detection
        try:
            self.price_update_min_sec = max(
                0.2, float(os.getenv("OMA_BTC_SIGNAL_PRICE_MIN_SEC", "1.0") or 1.0)
            )
        except (TypeError, ValueError):
            logger.warning("[BTCLeading] failed to parse price_update_min_sec", exc_info=True)
            self.price_update_min_sec = 1.0
        try:
            self.detect_cache_sec = max(
                0.2, float(os.getenv("OMA_BTC_SIGNAL_CACHE_SEC", "1.0") or 1.0)
            )
        except (TypeError, ValueError):
            logger.warning("[BTCLeading] failed to parse detect_cache_sec", exc_info=True)
            self.detect_cache_sec = 1.0
        self._last_price_fetch_ts: float = 0.0
        self._last_detect_ts: float = 0.0
        self._last_detect_signal: Optional[BTCLeadingSignal] = None
        
        # Most recent signal
        self.last_signal: Optional[BTCLeadingSignal] = None
        self.signal_cooldown_sec = 120  # 2min (shortened 300→120, for sharp reversals)
        
        logger.info(
            f"BTCLeading: threshold_5m={threshold_5m}%, "
            f"threshold_15m={threshold_15m}% "
            f"(price_min={self.price_update_min_sec:.2f}s, cache={self.detect_cache_sec:.2f}s)"
        )
    
    def update_btc_price(self, *, force: bool = False) -> bool:
        """
        Update BTC price
        """
        now = time.time()
        if not force and (now - self._last_price_fetch_ts) < self.price_update_min_sec:
            return False

        try:
            ticker = self.trade_client.get_ticker(self.btc_market)
            self._last_price_fetch_ts = now
            if ticker:
                price = float(
                    ticker.get("trade_price")
                    or ticker.get("lastPrice")
                    or ticker.get("last_price")
                    or 0
                )
                if price > 0:
                    self.price_history.append({
                        "timestamp": now,
                        "price": price,
                    })
                    return True
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error(f"BTCLeading: update error - {e}")
        return False
    
    def get_price_change(self, minutes: int) -> Optional[float]:
        """
        Calculate price change rate over N minutes

        Args:
            minutes: time range (minutes)

        Returns:
            change rate (%) or None
        """
        if len(self.price_history) < 2:
            return None
        
        now = time.time()
        target_time = now - (minutes * 60)
        
        # Latest price
        latest = self.price_history[-1]

        # Find the price from N minutes ago
        past_price = None
        for entry in reversed(self.price_history):
            if entry["timestamp"] <= target_time:
                past_price = entry["price"]
                break
        
        if past_price is None or past_price <= 0:
            return None
        
        change_pct = (latest["price"] / past_price - 1.0) * 100.0
        return change_pct
    
    def detect_signal(self, *, force_refresh: bool = False) -> Optional[BTCLeadingSignal]:
        """
        Detect BTC leading signal
        """
        now = time.time()

        # Short cache: consolidate multiple calls at the same instant (strategy/guard/scoring) into one computation
        if (
            not force_refresh
            and self._last_detect_signal is not None
            and (now - self._last_detect_ts) < self.detect_cache_sec
        ):
            return self._last_detect_signal
        
        # Cooldown check (ignore cooldown when a reversal is detected)
        if not force_refresh and self.last_signal:
            elapsed = now - self.last_signal.timestamp
            if elapsed < self.signal_cooldown_sec:
                _quick_chg = self.get_price_change(5)
                _prev_dir = self.last_signal.direction
                _reversed = (
                    _quick_chg is not None
                    and (
                        (_prev_dir == "UP" and _quick_chg < -self.threshold_5m * 0.5)
                        or (_prev_dir == "DOWN" and _quick_chg > self.threshold_5m * 0.5)
                    )
                )
                if not _reversed:
                    self._last_detect_signal = self.last_signal
                    self._last_detect_ts = now
                    return self.last_signal
        
        try:
            # Update BTC price
            self.update_btc_price(force=force_refresh)

            # 5-minute / 15-minute change rates
            change_5m = self.get_price_change(5)
            change_15m = self.get_price_change(15)

            if change_5m is None:
                # While data is still accumulating, briefly reuse the previous value to ease call bursts
                if (
                    self._last_detect_signal is not None
                    and (now - self._last_detect_ts) < max(self.detect_cache_sec, 2.0)
                ):
                    return self._last_detect_signal
                return None
            if change_15m is None:
                change_15m = 0.0
            
            # Signal determination
            direction = "NEUTRAL"
            strength = 0.0
            confidence = 0.5
            follow_altcoins = False

            # Up signal
            if change_5m >= self.threshold_5m:
                direction = "UP"
                strength = min(1.0, change_5m / self.threshold_5m / 2.0)
                confidence = 0.7
                follow_altcoins = True
                
                # 15m candle also strongly up
                if change_15m >= self.threshold_15m:
                    strength = min(1.0, strength + 0.3)
                    confidence = 0.9
            
            # Down signal
            elif change_5m <= -self.threshold_5m:
                direction = "DOWN"
                strength = min(1.0, abs(change_5m) / self.threshold_5m / 2.0)
                confidence = 0.8  # downside propagates faster
                follow_altcoins = True
                
                # 15m candle also strongly down
                if change_15m <= -self.threshold_15m:
                    strength = min(1.0, strength + 0.3)
                    confidence = 0.95
            
            # Weak up (1~2%)
            elif 1.0 <= change_5m < self.threshold_5m:
                direction = "UP"
                strength = 0.3
                confidence = 0.5
                follow_altcoins = True
            
            # Weak down (-1~-2%)
            elif -self.threshold_5m < change_5m <= -1.0:
                direction = "DOWN"
                strength = 0.3
                confidence = 0.6
                follow_altcoins = True
            
            signal = BTCLeadingSignal(
                direction=direction,
                btc_change_5m=change_5m,
                btc_change_15m=change_15m,
                strength=strength,
                confidence=confidence,
                follow_altcoins=follow_altcoins,
                timestamp=now,
            )
            
            # Store only meaningful signals
            if direction != "NEUTRAL":
                self.last_signal = signal
                logger.info(
                    f"BTCLeading: {direction} - "
                    f"5m={change_5m:+.2f}%, 15m={change_15m:+.2f}%, "
                    f"Strength={strength:.2f}, Confidence={confidence:.2f}"
                )

            self._last_detect_signal = signal
            self._last_detect_ts = now
            
            return signal
        
        except (AttributeError, TypeError, ValueError) as e:
            logger.error(f"BTCLeading: detect error - {e}")
            return None
    
    def adjust_score_for_btc_signal(
        self,
        base_score: float,
        strategy: str,
    ) -> float:
        """
        Adjust score based on BTC leading signal

        Args:
            base_score: base score
            strategy: strategy name

        Returns:
            adjusted score
        """
        signal = self.detect_signal()
        if not signal or not signal.follow_altcoins:
            return base_score
        
        # Per-strategy BTC sensitivity
        btc_sensitivity = {
            "PINGPONG": 1.5,   # fast rotation → leverages BTC following
            "AUTOLOOP": 1.4,   # medium rotation → leverages BTC following
            "LIGHTNING": 1.6,  # volatility strategy → BTC following is core
            "SNIPER": 1.3,     # sniper strategy → references BTC following
            "LADDER": 0.9,     # DCA → independent of BTC
            "GAZUA": 0.8,      # long-term → independent of BTC
            "CONTRARIAN": 0.7, # contrarian → goes against BTC
        }
        
        sensitivity = btc_sensitivity.get(strategy, 1.0)
        
        # Directional bonus
        bonus = 1.0
        if signal.direction == "UP":
            bonus = 1.0 + (signal.strength * signal.confidence * 0.3 * sensitivity)
        elif signal.direction == "DOWN":
            # Down signal → avoid entry
            bonus = 1.0 - (signal.strength * signal.confidence * 0.4 * sensitivity)
        
        adjusted = base_score * bonus
        
        logger.debug(
            f"BTCLeading: {strategy} Score {base_score:.2f} "
            f"→ {adjusted:.2f} (bonus={bonus:.2f})"
        )
        
        return adjusted
    
    def should_delay_entry(self) -> Tuple[bool, float]:
        """
        Whether to delay entry

        Returns:
            (whether to delay, delay duration in seconds)
        """
        signal = self.detect_signal()
        if not signal:
            return False, 0.0
        
        # BTC surging → wait 30s~2min (wait for altcoins to follow)
        if signal.direction == "UP" and signal.strength > 0.7:
            delay_sec = 30.0 + (signal.strength * 90.0)  # 30~120s
            return True, delay_sec

        # BTC crashing → avoid entry
        if signal.direction == "DOWN" and signal.strength > 0.7:
            return True, 300.0  # wait 5min
        
        return False, 0.0

    @property
    def drift_mode(self) -> bool:
        """Detect a cumulative drop of -1.5% or more over 1 hour (Guard-inactive zone).

        A zone where BTC is not crashing but slowly sliding.
        Guard uses a 5min -2% threshold, so it misses this zone.
        """
        try:
            c1h = self.get_price_change(60)
            if c1h is None:
                return False
            return c1h <= -1.5
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("[BTCLeading] should_delay_entry failed", exc_info=True)
            return False

    def get_regime_for_lightning(self) -> str:
        """Regime determination dedicated to Lightning v2.

        Returns:
            "SHOCK"    — crash at Guard-triggering level
            "DRIFT"    — slow decline (Guard not triggered)
            "RECOVERY" — start of a rebound
            "TREND"    — normal conditions
        """
        try:
            signal = self.detect_signal()
            if signal and signal.direction == "DOWN" and signal.strength > 0.7:
                return "SHOCK"
            if self.drift_mode:
                return "DRIFT"
            if signal and signal.direction == "UP" and signal.strength > 0.5:
                return "RECOVERY"
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[btc_leading_signal] %s: %s", 'btc_leading_signal.get_regime_for_lightning fallback', exc, exc_info=True)
        return "TREND"

    # ── BTC regime → per-strategy action mapping table ──
    # Defines what action each strategy should take depending on the BTC regime
    _REGIME_STRATEGY_ACTIONS: Dict[str, Dict[str, Dict[str, Any]]] = {
        "SHOCK": {
            "LIGHTNING": {"entry": "halt", "size_mult": 0.5, "trailing_mult": 0.5},
            "GAZUA":     {"entry": "normal", "trailing_mult": 0.5, "dca_enabled": True},
            "SNIPER":    {"entry": "tighten", "threshold_mult": 1.5, "size_mult": 0.7},
            "PINGPONG":  {"entry": "halt", "size_mult": 0.5},
            "AUTOLOOP":  {"entry": "halt", "size_mult": 0.6},
            "CONTRARIAN": {"entry": "aggressive", "size_mult": 1.3},
            "LADDER":    {"entry": "normal"},
        },
        "DRIFT": {
            "LIGHTNING": {"entry": "cautious", "threshold_mult": 1.5, "size_mult": 0.7},
            "GAZUA":     {"entry": "normal", "trailing_mult": 0.7},
            "SNIPER":    {"entry": "cautious", "threshold_mult": 1.2},
            "PINGPONG":  {"entry": "cautious", "size_mult": 0.8},
            "AUTOLOOP":  {"entry": "cautious", "size_mult": 0.8},
            "CONTRARIAN": {"entry": "normal"},
            "LADDER":    {"entry": "normal"},
        },
        "RECOVERY": {
            "LIGHTNING": {"entry": "aggressive", "size_mult": 1.2},
            "GAZUA":     {"entry": "normal", "trailing_mult": 1.0},
            "SNIPER":    {"entry": "aggressive", "threshold_mult": 0.8, "size_mult": 1.1},
            "PINGPONG":  {"entry": "aggressive", "size_mult": 1.1},
            "AUTOLOOP":  {"entry": "aggressive", "size_mult": 1.1},
            "CONTRARIAN": {"entry": "cautious", "size_mult": 0.7},
            "LADDER":    {"entry": "normal"},
        },
        "TREND": {
            "LIGHTNING": {"entry": "normal"},
            "GAZUA":     {"entry": "normal"},
            "SNIPER":    {"entry": "normal"},
            "PINGPONG":  {"entry": "normal"},
            "AUTOLOOP":  {"entry": "normal"},
            "CONTRARIAN": {"entry": "normal"},
            "LADDER":    {"entry": "normal"},
        },
    }

    def get_strategy_action(self, strategy: str) -> Dict[str, Any]:
        """Return recommended per-strategy action based on the current BTC regime.

        Returns:
            dict with keys like "entry", "size_mult", "trailing_mult", "threshold_mult"
            - entry: "normal" | "halt" | "cautious" | "aggressive" | "tighten"
            - size_mult: entry size multiplier (1.0 = 100%)
            - trailing_mult: trailing stop callback multiplier (0.5 = 50% tightening)
            - threshold_mult: entry threshold multiplier (1.5 = 50% higher)
        """
        regime = self.get_regime_for_lightning()
        strat = str(strategy).upper()
        regime_map = self._REGIME_STRATEGY_ACTIONS.get(regime, self._REGIME_STRATEGY_ACTIONS["TREND"])
        action = dict(regime_map.get(strat, {"entry": "normal"}))
        action["regime"] = regime
        return action

# Singleton instance
_DETECTOR_INSTANCE: Optional[BTCLeadingSignalDetector] = None

def get_btc_leading_detector() -> Optional[BTCLeadingSignalDetector]:
    """
    Return the BTC Leading Signal Detector singleton instance
    """
    global _DETECTOR_INSTANCE
    return _DETECTOR_INSTANCE

def initialize_btc_leading_detector(trade_client: Any) -> BTCLeadingSignalDetector:
    """
    Initialize the BTC Leading Signal Detector
    """
    global _DETECTOR_INSTANCE
    if _DETECTOR_INSTANCE is None:
        try:
            thr_5m = float(os.getenv("OMA_BTC_SIGNAL_THRESHOLD_5M", "2.0") or 2.0)
        except (TypeError, ValueError):
            logger.warning("[BTCLeading] failed to parse threshold_5m", exc_info=True)
            thr_5m = 2.0
        try:
            thr_15m = float(os.getenv("OMA_BTC_SIGNAL_THRESHOLD_15M", "5.0") or 5.0)
        except (TypeError, ValueError):
            logger.warning("[BTCLeading] failed to parse threshold_15m", exc_info=True)
            thr_15m = 5.0
        _DETECTOR_INSTANCE = BTCLeadingSignalDetector(
            trade_client,
            threshold_5m=max(0.5, abs(thr_5m)),
            threshold_15m=max(1.0, abs(thr_15m)),
        )
        try:
            cooldown = int(float(os.getenv("OMA_BTC_SIGNAL_COOLDOWN_SEC", "120") or 120))
            _DETECTOR_INSTANCE.signal_cooldown_sec = max(10, cooldown)
        except (TypeError, ValueError) as exc:
            logger.warning("[btc_leading_signal] %s: %s", 'btc_leading_signal.initialize_btc_leading_detector fallback', exc, exc_info=True)
        logger.info("BTC Leading Signal Detector initialized")
    return _DETECTOR_INSTANCE
