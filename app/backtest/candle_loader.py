# ============================================================
# File: app/backtest/candle_loader.py
# Autocoin OS v3-H — Historical Candle Data Loader (Bybit V5)
# ============================================================

from __future__ import annotations

import os
import time
import logging
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from app.core.rate_limiter import bybit_get, bybit_rate_limiter
from app.core.constants import BYBIT_MARKET_KLINE, bybit_v5_rest_category, parse_bybit_list

# Legacy aliases
# Using BYBIT_MARKET_KLINE directly
bybit_rate_limiter = bybit_rate_limiter

logger = logging.getLogger(__name__)

_INTERVAL_MAP = {
    1: "1", 3: "3", 5: "5", 15: "15", 30: "30",
    60: "60", 120: "120", 240: "240", 360: "360",
    720: "720", 1440: "D", 10080: "W",
}


class CandleLoader:
    """과거 캔들 데이터 로더 (Bybit V5)"""

    def __init__(self):
        self.max_429_retries = max(0, int(float(os.getenv("OMA_CANDLE_429_MAX_RETRIES", "4") or 4)))
        self.max_429_backoff_sec = max(1.0, float(os.getenv("OMA_CANDLE_429_BACKOFF_MAX_SEC", "8") or 8))

    def load_candles(
        self,
        market: str,
        days: int = 30,
        interval_minutes: int = 60,
        max_count: int = 200,
    ) -> List[Dict[str, Any]]:
        """과거 캔들 데이터 로드 (Bybit V5 kline)."""
        all_candles = []
        end_ms = int(time.time() * 1000)
        interval_str = _INTERVAL_MAP.get(interval_minutes, str(interval_minutes))
        candles_per_day = (24 * 60) // interval_minutes
        total_needed = candles_per_day * days

        logger.info("Loading %d candles for %s (%d days, %smin)", total_needed, market, days, interval_minutes)
        retry_429 = 0

        while len(all_candles) < total_needed:
            try:
                params = {
                    "category": bybit_v5_rest_category(),
                    "symbol": market,
                    "interval": interval_str,
                    "end": end_ms,
                    "limit": min(max_count, total_needed - len(all_candles)),
                }
                response = bybit_get(BYBIT_MARKET_KLINE, params=params, timeout=10)
                response.raise_for_status()
                retry_429 = 0

                klines = parse_bybit_list(response.json())
                if not klines:
                    break

                for k in klines:
                    if not isinstance(k, (list, tuple)) or len(k) < 6:
                        continue
                    ts_ms = int(k[0])
                    all_candles.append({
                        "candle_date_time_utc": datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%dT%H:%M:%S"),
                        "candle_date_time_kst": "",
                        "opening_price": float(k[1]),
                        "high_price": float(k[2]),
                        "low_price": float(k[3]),
                        "trade_price": float(k[4]),
                        "candle_acc_trade_volume": float(k[5]),
                        "candle_acc_trade_price": float(k[6]) if len(k) > 6 else 0.0,
                        "timestamp": ts_ms,
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    })

                oldest_ts = int(klines[-1][0])
                end_ms = oldest_ts - 1
                logger.debug("Loaded %d candles, total: %d", len(klines), len(all_candles))

            except requests.HTTPError as e:
                status = int((e.response.status_code if e.response is not None else 0) or 0)
                if status == 429:
                    if retry_429 < self.max_429_retries:
                        retry_429 += 1
                        backoff = min(self.max_429_backoff_sec, float(2 ** retry_429))
                        logger.warning("Rate limited (%s). retry %d/%d in %.1fs", market, retry_429, self.max_429_retries, backoff)
                        time.sleep(max(0.2, backoff))
                        continue
                    break
                logger.error("Failed to load candles: %s", e)
                break
            except Exception as e:
                logger.error("Failed to load candles: %s", e)
                break

        all_candles.reverse()
        logger.info("Loaded total %d candles for %s", len(all_candles), market)
        return all_candles

    def load_multiple_markets(self, markets: List[str], days: int = 30, interval_minutes: int = 60) -> Dict[str, List[Dict[str, Any]]]:
        result = {}
        for market in markets:
            try:
                candles = self.load_candles(market, days, interval_minutes)
                if candles:
                    result[market] = candles
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                logger.error("Candle load error %s: %s", market, e)
            time.sleep(0.3)
        return result

    def get_candle_at_time(self, candles: List[Dict[str, Any]], target_time: float) -> Optional[Dict[str, Any]]:
        if not candles:
            return None
        target_ms = int(target_time * 1000)
        left, right = 0, len(candles) - 1
        result = None
        while left <= right:
            mid = (left + right) // 2
            candle_ts = candles[mid].get("timestamp", 0)
            if candle_ts <= target_ms:
                result = candles[mid]
                left = mid + 1
            else:
                right = mid - 1
        return result
