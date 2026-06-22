# -*- coding: utf-8 -*-
"""Bybit V5 SPOT 트레이드 클라이언트 — SpotGazuaManager 의 client 인터페이스를 Bybit 현물로 어댑트.

[2026-06-17 부모] Bybit 현물부터 만들어 "USDT-현물 FOCUS" 기준판을 세운다(이후 Binance 현물은 상속).
SpotGazuaManager 는 거래소 무관(client 메서드 인터페이스에만 의존)이라, Upbit 스타일 메서드를
Bybit v5 spot 으로 구현하면 두뇌(스캔/진입/청산/점수)는 그대로 재사용된다.

핵심 매수/매도/잔고/주문/캔들은 BybitTradeClient(category="spot") 가 이미 제공 → 상속.
빠진 공개 시장조회(시장목록/티커/현재가/호가)·헬퍼(open_orders/MIN_ORDER)만 채운다.

KRW 현물과 다른 점:
  - quote = USDT (KRW 아님). 심볼 = "BTCUSDT" (KRW-BTC 아님).
    ※ Upbit base_currency("BTCUSDT")=="BTC" 이미 동작(to_upbit_market 이 USDT 접미사 처리) → 매니저 호환.
  - 투자유의/주의환기(market_warning) 없음 → get_market_warnings() = {} (차단 안 함).
  - 최소주문 = USDT notional (심볼별 min_notional 은 base 의 BybitInstrumentCache 가 처리).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from app.core.constants import (
    BYBIT_MARKET_INSTRUMENTS,
    BYBIT_MARKET_ORDERBOOK,
    BYBIT_MARKET_TICKERS,
)
from app.core.rate_limiter import bybit_get
from app.integrations.bybit_trade import BybitTradeClient

logger = logging.getLogger(__name__)

# Upbit MIN_ORDER_KRW(5000) 자리에 들어갈 USDT 최소주문 코어스 floor (심볼별 정밀 min 은 base 가 검증).
MIN_ORDER_USDT = 5.0


def bybit_base_currency(symbol: str) -> str:
    """Bybit 심볼("BTCUSDT") → base("BTC"). quote 접미사 제거. (Upbit base_currency 와 결과 일치 보조용)"""
    s = str(symbol).upper().replace("-", "").replace("/", "")
    for q in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


class BybitSpotTradeClient(BybitTradeClient):
    """Bybit 현물(spot). UpbitTradeClient 인터페이스 호환 — BybitTradeClient(category="spot") 상속."""

    # 매니저가 getattr(client, "MIN_ORDER_KRW", 5000.0) 로 읽음 (이름만 유지, 단위=USDT).
    MIN_ORDER_KRW = MIN_ORDER_USDT

    def __init__(self, api_key: str = "", api_secret: str = "", *, timeout: float = 10.0):
        super().__init__(api_key or None, api_secret or None, timeout=timeout, category="spot")
        self._mkt_cache: tuple = (0.0, [])

    # ── base 헬퍼 ─────────────────────────────────────────────────────
    @staticmethod
    def base_currency(symbol: str) -> str:
        return bybit_base_currency(symbol)

    # ── 시장 목록 (USDT 현물 전체) — Upbit shape 미러 ────────────────────
    def get_all_markets(self) -> List[Dict[str, Any]]:
        """USDT 현물 마켓 전체. Upbit shape({market, market_event}) 미러(현물 경고 없음=빈 event)."""
        ts0, cached = self._mkt_cache
        if cached and (time.time() - ts0) < 300.0:
            return cached
        try:
            resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": "spot"}, timeout=self.timeout)
            resp.raise_for_status()
            lst = resp.json().get("result", {}).get("list", []) or []
            out: List[Dict[str, Any]] = []
            for it in lst:
                sym = str(it.get("symbol", ""))
                if not sym.endswith("USDT"):
                    continue
                st = str(it.get("status", "")).lower()
                if st and st != "trading":
                    continue
                out.append({"market": sym, "korean_name": sym, "english_name": sym, "market_event": {}})
            if out:
                self._mkt_cache = (time.time(), out)
            return out
        except Exception as e:
            logger.warning("[BybitSpot] get_all_markets failed: %s", e)
            return list(cached) if cached else []

    def get_market_warnings(self, *, ttl: float = 300.0) -> Dict[str, Dict[str, Any]]:
        """Bybit 현물엔 한국식 투자유의/주의환기 없음 → 빈 dict(차단 안 함, fail-open)."""
        return {}

    # ── 티커 / 현재가 — Upbit shape 미러 ────────────────────────────────
    def get_tickers(self, markets: List[str]) -> List[Dict[str, Any]]:
        """현재가+24h 거래대금. Upbit shape({market, trade_price, acc_trade_price_24h}) 미러.
        markets 비면 전체(셀렉터 거래대금 랭킹용)."""
        out: List[Dict[str, Any]] = []
        try:
            resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": "spot"}, timeout=self.timeout)
            resp.raise_for_status()
            lst = resp.json().get("result", {}).get("list", []) or []
            want = {self._normalize_symbol(m) for m in markets} if markets else None
            for t in lst:
                sym = str(t.get("symbol", ""))
                if not sym.endswith("USDT"):
                    continue
                if want is not None and sym not in want:
                    continue
                out.append({
                    "market": sym,
                    "trade_price": float(t.get("lastPrice", 0) or 0),
                    "acc_trade_price_24h": float(t.get("turnover24h", 0) or 0),
                    "acc_trade_volume_24h": float(t.get("volume24h", 0) or 0),
                    "signed_change_rate": float(t.get("price24hPcnt", 0) or 0),
                })
        except Exception as e:
            logger.warning("[BybitSpot] get_tickers failed: %s", e)
        return out

    def get_price(self, market: str) -> float:
        try:
            t = self.get_tickers([market])
            return float(t[0].get("trade_price", 0) or 0) if t else 0.0
        except Exception as e:
            logger.warning("[BybitSpot] get_price %s failed: %s", market, e)
            return 0.0

    # ── 호가창 — Upbit shape 미러 ───────────────────────────────────────
    def get_orderbook(self, market: str, *, depth: int = 15) -> Dict[str, Any]:
        """Upbit shape({market, bids:[{price,size}], asks:[{price,size}], ts}) 미러.
        Bybit v5 orderbook: b=[[price,size],...](매수 내림차순), a=[...](매도 오름차순)."""
        sym = self._normalize_symbol(market)
        bids: List[Dict[str, float]] = []
        asks: List[Dict[str, float]] = []
        try:
            resp = bybit_get(
                BYBIT_MARKET_ORDERBOOK,
                params={"category": "spot", "symbol": sym, "limit": min(max(int(depth), 1), 50)},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            r = resp.json().get("result", {}) or {}
            for row in (r.get("b") or [])[: max(int(depth), 1)]:
                bids.append({"price": float(row[0]), "size": float(row[1])})
            for row in (r.get("a") or [])[: max(int(depth), 1)]:
                asks.append({"price": float(row[0]), "size": float(row[1])})
        except Exception as e:
            logger.warning("[BybitSpot] get_orderbook %s failed: %s", market, e)
        return {"market": sym, "bids": bids, "asks": asks, "ts": 0}

    # ── 미체결 주문 (Upbit open_orders 인터페이스) ──────────────────────
    def open_orders(self, market: str, *, side: Optional[str] = None) -> List[Dict[str, Any]]:
        orders = self.list_wait_orders(market=market)
        if side:
            s = "bid" if str(side).lower() in ("bid", "buy", "long") else "ask"
            orders = [o for o in orders if str(o.get("side", "")).lower() == s]
        return orders
