"""
Market Signals API Router
Real-time market signals (Volume Spike, Time Volatility, BTC Leading)
"""

import logging
from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/market-signals",
    tags=["market-signals"],
)


@router.get("/summary")
def get_market_signals_summary() -> Dict[str, Any]:
    """
    Real-time market signals summary

    Returns:
        {
            "volume_spike": {
                "detected_count": 3,
                "signals": [{"market": "DOGEUSDT", "spike_ratio": 3.5, ...}],
            },
            "time_volatility": {
                "current_hour": 3,
                "volatility_multiplier": 1.5,
                "is_high_volatility": true,
                ...
            },
            "btc_leading": {
                "direction": "UP",
                "btc_change_5m": 3.2,
                "strength": 0.8,
                ...
            },
            "timestamp": 1234567890.0
        }
    """
    import time
    
    result = {
        "volume_spike": {
            "detected_count": 0,
            "signals": [],
            "enabled": False,
        },
        "time_volatility": {
            "current_hour": 0,
            "volatility_multiplier": 1.0,
            "is_high_volatility": False,
            "is_low_volatility": False,
            "enabled": True,
        },
        "btc_leading": {
            "direction": "NEUTRAL",
            "btc_change_5m": 0.0,
            "btc_change_15m": 0.0,
            "strength": 0.0,
            "confidence": 0.0,
            "enabled": False,
        },
        "timestamp": time.time(),
    }
    
    # Volume Spike
    try:
        from app.monitor.volume_spike_detector import get_volume_spike_detector
        detector = get_volume_spike_detector()
        if detector:
            signals = detector.detect_spikes()
            result["volume_spike"]["enabled"] = True
            result["volume_spike"]["detected_count"] = len(signals)
            result["volume_spike"]["signals"] = [
                {
                    "market": s.market,
                    "spike_ratio": round(s.spike_ratio, 2),
                    "volume_24h": round(s.volume_24h, 0),
                    "avg_volume_7d": round(s.avg_volume_7d, 0),
                    "price_change_24h": round(s.price_change_24h, 2),
                    "direction": s.direction,
                    "confidence": round(s.confidence, 2),
                }
                for s in signals[:10]  # top 10 only
            ]
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
        logger.warning(f"Volume Spike summary failed: {e}")
    
    # Time Volatility
    try:
        from app.monitor.time_volatility_adjuster import get_time_volatility_adjuster
        adjuster = get_time_volatility_adjuster()
        if adjuster:
            ctx = adjuster.get_time_context()
            result["time_volatility"] = {
                "current_hour": ctx["hour"],
                "weekday": ctx["weekday"],
                "is_weekend": ctx["is_weekend"],
                "volatility_multiplier": round(ctx["volatility_multiplier"], 2),
                "is_high_volatility": ctx["is_high_volatility"],
                "is_low_volatility": ctx["is_low_volatility"],
                "enabled": True,
            }
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning(f"Time Volatility summary failed: {e}")
    
    # BTC Leading
    try:
        from app.monitor.btc_leading_signal import get_btc_leading_detector
        detector = get_btc_leading_detector()
        if detector:
            signal = detector.detect_signal()
            if signal:
                result["btc_leading"] = {
                    "direction": signal.direction,
                    "btc_change_5m": round(signal.btc_change_5m, 2),
                    "btc_change_15m": round(signal.btc_change_15m, 2),
                    "strength": round(signal.strength, 2),
                    "confidence": round(signal.confidence, 2),
                    "follow_altcoins": signal.follow_altcoins,
                    "enabled": True,
                }
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning(f"BTC Leading summary failed: {e}")
    
    return result


@router.get("/volume-spike/detail")
def get_volume_spike_detail() -> Dict[str, Any]:
    """
    Volume Spike detail info
    """
    try:
        from app.monitor.volume_spike_detector import get_volume_spike_detector
        detector = get_volume_spike_detector()
        if not detector:
            return {"enabled": False, "signals": []}
        
        signals = detector.detect_spikes()
        return {
            "enabled": True,
            "spike_threshold": detector.spike_threshold,
            "medium_threshold": detector.medium_threshold,
            "signal_count": len(signals),
            "signals": [
                {
                    "market": s.market,
                    "spike_ratio": round(s.spike_ratio, 2),
                    "volume_24h": round(s.volume_24h, 0),
                    "avg_volume_7d": round(s.avg_volume_7d, 0),
                    "price_change_24h": round(s.price_change_24h, 2),
                    "direction": s.direction,
                    "confidence": round(s.confidence, 2),
                    "timestamp": s.timestamp,
                }
                for s in signals
            ],
        }
    except (AttributeError, TypeError, ValueError) as e:
        logger.error(f"Volume Spike detail failed: {e}")
        return {"enabled": False, "error": str(e)}


@router.get("/btc-leading/detail")
def get_btc_leading_detail() -> Dict[str, Any]:
    """
    BTC Leading Signal detail info
    """
    try:
        from app.monitor.btc_leading_signal import get_btc_leading_detector
        detector = get_btc_leading_detector()
        if not detector:
            return {"enabled": False}
        
        signal = detector.detect_signal()
        if not signal:
            return {"enabled": True, "signal": None}
        
        should_delay, delay_sec = detector.should_delay_entry()
        
        return {
            "enabled": True,
            "signal": {
                "direction": signal.direction,
                "btc_change_5m": round(signal.btc_change_5m, 2),
                "btc_change_15m": round(signal.btc_change_15m, 2),
                "strength": round(signal.strength, 2),
                "confidence": round(signal.confidence, 2),
                "follow_altcoins": signal.follow_altcoins,
                "timestamp": signal.timestamp,
            },
            "entry_delay": {
                "should_delay": should_delay,
                "delay_sec": round(delay_sec, 0),
            },
        }
    except (TypeError, ValueError) as e:
        logger.error(f"BTC Leading detail failed: {e}")
        return {"enabled": False, "error": str(e)}
