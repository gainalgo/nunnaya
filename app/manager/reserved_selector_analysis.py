# ============================================================
# File: app/manager/reserved_selector_analysis.py
# Autocoin OS — AI feature extraction and technical analysis
# functions extracted from reserved_selector.py
# ============================================================

from __future__ import annotations

import math
import logging
from typing import Any, Dict, List

from app.manager.reserved_selector_utils import _sf

_logger = logging.getLogger(__name__)


def _extract_ai_features_from_candles(candles: List[Dict[str, Any]]) -> Dict[str, float]:
    """Extract AI features from candle data for strategy classification.

    Returns:
        trend: trend direction (-1 ~ +1, negative=down, positive=up)
        momentum: short-term momentum (%)
        volatility: volatility (%)
        volume_surge: volume surge ratio
    """
    _FEATURES_INVALID = {"trend": 0.0, "momentum": 0.0, "volatility": 0.0, "volume_surge": 0.0, "data_valid": False}
    if not candles or len(candles) < 5:
        return dict(_FEATURES_INVALID)

    try:
        prices = [float(c.get("trade_price") or 0.0) for c in candles if c.get("trade_price")]
        volumes = [float(c.get("candle_acc_trade_volume") or 0.0) for c in candles if c.get("candle_acc_trade_volume")]

        if len(prices) < 5:
            return dict(_FEATURES_INVALID)

        prices = list(reversed(prices))
        volumes = list(reversed(volumes)) if volumes else []

        first_price = prices[0] if prices[0] > 0 else 1.0
        last_price = prices[-1] if prices[-1] > 0 else first_price

        trend = (last_price - first_price) / first_price * 100.0
        trend = max(-10.0, min(10.0, trend)) / 10.0

        if len(prices) >= 3:
            recent_change = (prices[-1] - prices[-3]) / prices[-3] * 100.0 if prices[-3] > 0 else 0.0
        else:
            recent_change = 0.0
        momentum = max(-5.0, min(5.0, recent_change))

        if len(prices) >= 10:
            avg_price = sum(prices[-10:]) / 10.0
            variance = sum((p - avg_price) ** 2 for p in prices[-10:]) / 10.0
            volatility = (variance ** 0.5) / avg_price * 100.0 if avg_price > 0 else 0.0
        else:
            volatility = 0.0

        volume_surge = 0.0
        if volumes and len(volumes) >= 10:
            recent_vol = sum(volumes[-3:]) / 3.0 if len(volumes) >= 3 else volumes[-1]
            prev_vol = sum(volumes[-10:-3]) / 7.0 if len(volumes) >= 10 else sum(volumes[:-3]) / max(1, len(volumes) - 3)
            if prev_vol > 0:
                volume_surge = (recent_vol - prev_vol) / prev_vol

        return {
            "trend": float(trend),
            "momentum": float(momentum),
            "volatility": float(volatility),
            "volume_surge": float(volume_surge),
            "data_valid": True,
        }
    except (KeyError, IndexError, AttributeError, TypeError, ValueError):
        _logger.warning("[Analysis] _extract_ai_features_from_candles failed", exc_info=True)
        return {"trend": 0.0, "momentum": 0.0, "volatility": 0.0, "volume_surge": 0.0, "data_valid": False}


def _calc_ema_simple(prices: List[float], period: int) -> float | None:
    """Simple EMA calculation (uses the most recent period*2 data points)"""
    if not prices or len(prices) < period:
        return None

    arr = prices[-period*2:] if len(prices) > period*2 else prices
    k = 2.0 / (period + 1.0)
    ema_val = arr[0]
    for p in arr[1:]:
        ema_val = p * k + ema_val * (1.0 - k)
    return ema_val


def _check_ema_cross(candles: List[Dict[str, Any]], fast: int = 12, slow: int = 26) -> tuple[bool, float, float]:
    """Check EMA cross.

    Returns:
        (is_golden_cross, ema_fast, ema_slow)
        - is_golden_cross: ema_fast > ema_slow (uptrend)
    """
    if not candles or len(candles) < slow:
        return False, 0.0, 0.0

    try:
        closes = [float(c.get("trade_price", 0)) for c in reversed(candles) if c.get("trade_price")]
        if len(closes) < slow:
            return False, 0.0, 0.0

        ema_fast = _calc_ema_simple(closes, fast)
        ema_slow = _calc_ema_simple(closes, slow)

        if ema_fast is None or ema_slow is None:
            return False, 0.0, 0.0

        is_golden = ema_fast > ema_slow
        return is_golden, ema_fast, ema_slow
    except (KeyError, AttributeError, TypeError, ValueError):
        _logger.warning("[Analysis] _check_ema_cross failed", exc_info=True)
        return False, 0.0, 0.0


def _calc_rsi_macd_from_candles(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate RSI and MACD from candle data.

    Returns:
        rsi: RSI(14) value (0~100)
        macd_line: MACD line value
        macd_signal: MACD signal value
        macd_histogram: MACD histogram
        macd_trend: "bullish" | "bearish" | "neutral"
        change_24h: 24-hour change rate (%)
    """
    result = {
        "rsi": 50.0,
        "macd_line": 0.0,
        "macd_signal": 0.0,
        "macd_histogram": 0.0,
        "macd_trend": "neutral",
        "change_24h": 0.0,
        "data_valid": False,
    }

    if not candles or len(candles) < 26:
        return result

    try:
        # candles are newest-first, so reverse them
        closes = [float(c.get("trade_price") or 0) for c in reversed(candles)]

        if len(closes) < 26:
            return result

        # 24-hour change rate (96 of 15-min candles = 24h, but with only 30 candles ≈ 7.5h)
        if closes[0] > 0:
            result["change_24h"] = round((closes[-1] - closes[0]) / closes[0] * 100, 2)

        # RSI(14) calculation
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(max(0, diff))
            losses.append(max(0, -diff))

        if len(gains) >= 14:
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            if avg_loss > 0:
                rs = avg_gain / avg_loss
                result["rsi"] = round(100 - (100 / (1 + rs)), 1)
            else:
                result["rsi"] = 100.0

        # MACD (12, 26, 9) calculation
        def calc_ema(data: List[float], period: int) -> float:
            if len(data) < period:
                return sum(data) / len(data) if data else 0.0
            multiplier = 2 / (period + 1)
            ema = sum(data[:period]) / period
            for price in data[period:]:
                ema = (price - ema) * multiplier + ema
            return ema

        ema12 = calc_ema(closes, 12)
        ema26 = calc_ema(closes, 26)
        macd_line = ema12 - ema26

        # MACD Signal (9-period EMA of MACD line) - simplified
        # Properly this needs the MACD line history, but here we use an approximation
        result["macd_line"] = round(macd_line, 4)

        # previous MACD calculation (signal approximation)
        if len(closes) >= 27:
            prev_closes = closes[:-1]
            prev_ema12 = calc_ema(prev_closes, 12)
            prev_ema26 = calc_ema(prev_closes, 26)
            prev_macd = prev_ema12 - prev_ema26

            # simple signal approximation: average of current and previous MACD
            result["macd_signal"] = round((macd_line + prev_macd) / 2, 4)
            result["macd_histogram"] = round(macd_line - result["macd_signal"], 4)

            # trend determination
            if macd_line > 0 and macd_line > prev_macd:
                result["macd_trend"] = "bullish"
            elif macd_line < 0 and macd_line < prev_macd:
                result["macd_trend"] = "bearish"
            else:
                result["macd_trend"] = "neutral"

        result["data_valid"] = True
        return result
    except (KeyError, IndexError, AttributeError, TypeError, ValueError):
        _logger.warning("[Analysis] _compute_ti failed", exc_info=True)
        return result
