# ============================================================
# File: app/core/hyper_price_store.py
# ------------------------------------------------------------
# HyperPriceStore
# - 시장별 최신 가격을 저장하고 엔진에 제공하는 중앙 저장소
# - PriceFeed가 가격을 업데이트하고
#   Engine / System / Coordinator가 가격을 조회한다.
# - exchange namespace 지원 (멀티 거래소 동시 운영 대비)
# ============================================================

from __future__ import annotations
import logging
import time
from threading import Lock
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)

# Default exchange for backward compatibility
DEFAULT_EXCHANGE = "bybit"


def _make_key(exchange: str, market: str) -> str:
    """Create namespaced key: 'exchange:market'"""
    return f"{exchange}:{market}"


def _parse_key(key: str) -> Tuple[str, str]:
    """Parse namespaced key back to (exchange, market)"""
    if ":" in key:
        parts = key.split(":", 1)
        return parts[0], parts[1]
    return DEFAULT_EXCHANGE, key


class HyperPriceStore:
    """
    시장별 최신 가격을 저장하는 저장소.
    Thread-safe 보장을 위해 Lock 사용.
    exchange namespace 지원으로 멀티 거래소 동시 운영 가능.
    """

    # [MIGRATED 2026-01-23] 가격 히스토리 최대 보관 개수
    MAX_HISTORY = 200

    def __init__(self, default_exchange: str = DEFAULT_EXCHANGE):
        self._default_exchange = default_exchange
        self._prices: Dict[str, float] = {}
        self._price_ts: Dict[str, float] = {}             # per-market update timestamp
        self._volumes: Dict[str, float] = {}
        self._price_history: Dict[str, List[float]] = {}  # [MIGRATED 2026-01-23]
        self._lock = Lock()
        # candle.1m latest (accumulated within the candle)
        self._c1m_notional: Dict[str, float] = {}
        self._c1m_volume: Dict[str, float] = {}
        self._c1m_dt_utc: Dict[str, str] = {}
        self._c1m_ts: Dict[str, float] = {}
        self._last_update_ts: float = 0.0

    # --------------------------------------------------------
    # 가격 저장
    # --------------------------------------------------------
    def set_price(
        self, market: str, price: float, *, exchange: Optional[str] = None
    ) -> None:
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        with self._lock:
            self._prices[key] = price
            self._price_ts[key] = time.time()
            self._last_update_ts = time.time()
            # [MIGRATED 2026-01-23] 가격 히스토리 저장
            if key not in self._price_history:
                self._price_history[key] = []
            self._price_history[key].append(price)
            if len(self._price_history[key]) > self.MAX_HISTORY:
                self._price_history[key] = self._price_history[key][-self.MAX_HISTORY:]

    def set_volume(
        self, market: str, volume: float, *, exchange: Optional[str] = None
    ) -> None:
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        with self._lock:
            self._volumes[key] = volume

    # --------------------------------------------------------
    # 가격 조회
    # --------------------------------------------------------
    def get_price(
        self, market: str, *, exchange: Optional[str] = None,
        max_age_sec: Optional[float] = None,
    ) -> Optional[float]:
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        with self._lock:
            price = self._prices.get(key)
            if price is None:
                return None
            if max_age_sec is not None:
                ts = self._price_ts.get(key, 0.0)
                if time.time() - ts > max_age_sec:
                    return None  # stale — caller should use API fallback
            return price

    def get_volume(
        self, market: str, *, exchange: Optional[str] = None
    ) -> Optional[float]:
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        with self._lock:
            return self._volumes.get(key)

    def get_last_update_ts(self) -> float:
        """마지막 가격 업데이트 시각 (Unix timestamp)."""
        with self._lock:
            return self._last_update_ts

    # [MIGRATED 2026-01-23] 가격 히스토리 조회
    def get_prices(
        self, market: str, count: int = 60, *, exchange: Optional[str] = None
    ) -> List[float]:
        """최근 N개의 가격 히스토리 반환.

        Args:
            market: 마켓 심볼 (예: "BTCUSDT")
            count: 가져올 가격 개수
            exchange: 거래소 (기본값: default_exchange)

        Returns:
            가격 리스트 (오래된 것부터 최신 순)
            데이터 부족 시 가용한 만큼 반환, 없으면 빈 리스트
        """
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        with self._lock:
            history = self._price_history.get(key, [])
            if len(history) <= count:
                return list(history)
            return list(history[-count:])

    # --------------------------------------------------------
    # 여러 가격 조회
    # --------------------------------------------------------
    def get_all(self, *, exchange: Optional[str] = None) -> Dict[str, float]:
        """Get all prices, optionally filtered by exchange."""
        with self._lock:
            if exchange is None:
                # Return all with full keys for inspection
                return dict(self._prices)
            # Filter by exchange prefix
            prefix = f"{exchange}:"
            return {
                k[len(prefix):]: v
                for k, v in self._prices.items()
                if k.startswith(prefix)
            }

    # --------------------------------------------------------
    # 상태 출력
    # --------------------------------------------------------
    def status(self, *, exchange: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if exchange is None:
                return {
                    "markets": len(self._prices),
                    "stored_prices": dict(self._prices),
                }
            prefix = f"{exchange}:"
            filtered = {
                k[len(prefix):]: v
                for k, v in self._prices.items()
                if k.startswith(prefix)
            }
            return {
                "exchange": exchange,
                "markets": len(filtered),
                "stored_prices": filtered,
            }

    # -------------------------
    # Candle.1m (accumulated) helpers
    # -------------------------
    def set_candle_1m(
        self,
        market: str,
        acc_trade_price: float,
        acc_trade_volume: float,
        candle_dt_utc: Optional[str] = None,
        ts: Optional[float] = None,
        *,
        exchange: Optional[str] = None,
    ) -> None:
        """Store 1m candle accumulators."""
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        try:
            notional = float(acc_trade_price or 0.0)
        except (TypeError, ValueError):
            logger.warning("[PriceStore] Invalid acc_trade_price for %s: %r", market, acc_trade_price)
            notional = 0.0
        try:
            vol = float(acc_trade_volume or 0.0)
        except (TypeError, ValueError):
            logger.warning("[PriceStore] Invalid acc_trade_volume for %s: %r", market, acc_trade_volume)
            vol = 0.0
        t = float(ts) if ts is not None else time.time()
        with self._lock:
            self._c1m_notional[key] = notional
            self._c1m_volume[key] = vol
            if candle_dt_utc:
                self._c1m_dt_utc[key] = str(candle_dt_utc)
            self._c1m_ts[key] = t

    def get_candle_1m_notional(
        self, market: str, default: float = 0.0, *, exchange: Optional[str] = None
    ) -> float:
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        with self._lock:
            try:
                return float(self._c1m_notional.get(key, default) or default)
            except (TypeError, ValueError):
                logger.warning("[PriceStore] c1m_notional parse error for %s", market, exc_info=True)
                return float(default or 0.0)

    def get_candle_1m_volume(
        self, market: str, default: float = 0.0, *, exchange: Optional[str] = None
    ) -> float:
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        with self._lock:
            try:
                return float(self._c1m_volume.get(key, default) or default)
            except (TypeError, ValueError):
                logger.warning("[PriceStore] c1m_volume parse error for %s", market, exc_info=True)
                return float(default or 0.0)

    def get_candle_1m_dt_utc(
        self, market: str, *, exchange: Optional[str] = None
    ) -> str:
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        with self._lock:
            return str(self._c1m_dt_utc.get(key, "") or "")


# ============================================================================
# PATCH 2025-12-26
# Orderbook store (ENTRY spread/depth guard + TP limit EXIT pricing)
# - Adds a lightweight in-memory store for Bybit websocket 'orderbook' messages.
# ============================================================================

class HyperOrderbookStore:
    """
    Orderbook store with exchange namespace support.
    Keys are namespaced as 'exchange:market' for multi-exchange operation.
    """

    def __init__(self, default_exchange: str = DEFAULT_EXCHANGE):
        self._default_exchange = default_exchange
        self._orderbooks: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()

    def set_orderbook(
        self,
        market: str,
        *,
        ts: Optional[float] = None,
        best_bid: float,
        best_ask: float,
        units: List[Dict[str, float]],
        exchange: Optional[str] = None,
    ) -> None:
        """Store the latest orderbook snapshot for a market."""
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        now_ts = float(ts if ts is not None else time.time())
        with self._lock:
            self._orderbooks[key] = {
                "ts": now_ts,
                "best_bid": float(best_bid),
                "best_ask": float(best_ask),
                "units": units,
            }

    def get(
        self, market: str, *, exchange: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        ex = exchange or self._default_exchange
        key = _make_key(ex, market)
        with self._lock:
            ob = self._orderbooks.get(key)
            if not ob:
                return None
            return {
                "ts": float(ob.get("ts", 0.0) or 0.0),
                "best_bid": float(ob.get("best_bid", 0.0) or 0.0),
                "best_ask": float(ob.get("best_ask", 0.0) or 0.0),
                "units": list(ob.get("units") or []),
            }

    def snapshot(self, *, exchange: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Get snapshot, optionally filtered by exchange."""
        with self._lock:
            if exchange is None:
                return {k: dict(v) for k, v in self._orderbooks.items()}
            prefix = f"{exchange}:"
            return {
                k[len(prefix):]: dict(v)
                for k, v in self._orderbooks.items()
                if k.startswith(prefix)
            }


# ------------------------------------------------------------
# 글로벌 인스턴스 (Global singletons for backward compatibility)
# ------------------------------------------------------------
price_store = HyperPriceStore()
orderbook_store = HyperOrderbookStore()
