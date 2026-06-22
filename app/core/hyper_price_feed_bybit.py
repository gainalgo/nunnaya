# -*- coding: utf-8 -*-
"""Bybit WebSocket PriceFeed. Bybit WebSocket PriceFeed."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Set, Tuple

import websockets
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from app.core.hyper_price_store import price_store, orderbook_store
from app.core.bybit_trading import get_bybit_public_ws_url

logger = logging.getLogger(__name__)

try:
    from app.manager.oma_market_registry import oma_market_registry
except ImportError:
    logger.warning("[PriceFeed] oma_market_registry import failed, running without registry", exc_info=True)
    oma_market_registry = None


class BybitHyperPriceFeed:
    def __init__(self):
        self.running = False
        self._task = None
        self.clients: Set[WebSocket] = set()
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._resubscribe_flag = False
        self._manual_symbols: Set[str] = set()
        try:
            self._watch_subscribe_limit = max(0, int(os.getenv("OMA_WS_WATCH_SUBSCRIBE_LIMIT", "40")))
        except (TypeError, ValueError):
            logger.warning("[PriceFeed] watch_subscribe_limit parse error, using default 40", exc_info=True)
            self._watch_subscribe_limit = 40

    async def register(self, ws):
        await ws.accept()
        self.clients.add(ws)

    async def unregister(self, ws):
        self.clients.discard(ws)

    async def _broadcast(self, payload):
        # ★ [2026-05-11] 클라이언트 WS 끊김(WebSocketDisconnect 1006)에 Bybit 피드까지 재연결되던 버그 격리.
        #   브라우저 새로고침/탭 종료 시 WinError 10053 → ConnectionClosedError → WebSocketDisconnect.
        #   이전엔 OSError 만 잡아서 WebSocketDisconnect 가 _handle_orderbook → _run 까지 전파 → Bybit 재연결.
        #   해결: WebSocketDisconnect + 광범위 Exception 캐치 후 dead 마킹. 한 클라 죽음 ≠ 피드 전체 다운.
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except (WebSocketDisconnect, KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                dead.append(ws)
            except Exception as exc:  # noqa: BLE001 — broadcast must never break the feed
                logger.debug("[PRICEFEED] client send unexpected error, marking dead: %s", exc)
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def desired_codes(self):
        codes: Set[str] = set()
        if oma_market_registry is not None:
            try:
                codes.update(oma_market_registry.list_active())
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[PRICEFEED] desired_codes list_active fallback: %s", exc, exc_info=True)
            try:
                if hasattr(oma_market_registry, "list_recovery"):
                    codes.update(oma_market_registry.list_recovery())
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[PRICEFEED] desired_codes list_recovery fallback: %s", exc, exc_info=True)
            try:
                if hasattr(oma_market_registry, "list_prewarm"):
                    codes.update(oma_market_registry.list_prewarm())
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[PRICEFEED] desired_codes list_prewarm fallback: %s", exc, exc_info=True)
            try:
                if self._watch_subscribe_limit > 0 and hasattr(oma_market_registry, "list_watch"):
                    watch = sorted(list(oma_market_registry.list_watch() or []))
                    if len(watch) > self._watch_subscribe_limit:
                        watch = watch[:self._watch_subscribe_limit]
                    codes.update(watch)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[PRICEFEED] desired_codes list_watch fallback: %s", exc, exc_info=True)
        # ★ FOCUS 포지션 + lock_market 강제 구독 (OMA에 없어도 가격 필수)
        try:
            codes.update(_focus_position_symbols())
        except Exception:
            pass
        codes.update(self._manual_symbols)
        return tuple(sorted(codes))

    def add_symbol(self, symbol):
        self._manual_symbols.add(str(symbol).upper())
        self._resubscribe_flag = True

    def remove_symbol(self, symbol):
        self._manual_symbols.discard(str(symbol).upper())
        self._resubscribe_flag = True

    def request_resubscribe(self):
        self._resubscribe_flag = True

    async def _run(self):
        last_sig = tuple()
        while self.running:
            try:
                codes = self.desired_codes()
                if not codes:
                    await asyncio.sleep(1.0)
                    continue
                last_sig = codes
                self._resubscribe_flag = False
                args = []
                for code in codes:
                    args.append(f"tickers.{code}")
                    args.append(f"orderbook.1.{code}")
                async with websockets.connect(
                    get_bybit_public_ws_url(), ping_interval=None, ping_timeout=None, max_size=None
                ) as ws:
                    for i in range(0, len(args), 10):
                        await ws.send(json.dumps({"op": "subscribe", "args": args[i:i+10]}))
                    self._reconnect_delay = 1.0
                    last_ping = time.time()
                    last_ticker_ts = time.time()  # ★ 좀비 연결 감지용
                    while self.running:
                        if self._resubscribe_flag or self.desired_codes() != last_sig:
                            break
                        now = time.time()
                        # ★ 좀비 연결 감지: 60초간 ticker 없으면 강제 재연결
                        if now - last_ticker_ts > 60.0:
                            logger.warning(
                                "[PriceFeed] No ticker data for %.0fs — zombie connection, forcing reconnect",
                                now - last_ticker_ts,
                            )
                            break
                        if now - last_ping >= 15:
                            try:
                                await ws.send(json.dumps({"op": "ping"}))
                                last_ping = now
                            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                                logger.warning("[PriceFeed] ping send failed, reconnecting", exc_info=True)
                                break
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        # ★ [2026-05-11] per-message try — 한 메시지 처리 실패가 피드 전체 재연결로 번지지 않게 격리.
                        #   특히 클라이언트 broadcast 오류가 outer except 까지 도달하던 케이스 보강.
                        #   ConnectionError/OSError 류는 위쪽 except 가 잡고 reconnect — 여기서 swallow X.
                        try:
                            await self._handle_message(msg)
                        except (ConnectionError, OSError, asyncio.TimeoutError):
                            raise  # let outer reconnect path handle real network drops
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("[PRICEFEED] message handler error (continuing): %s", exc, exc_info=True)
                        # ★ ticker 메시지 수신 시 타이머 리셋
                        if '"topic":"tickers.' in (msg if isinstance(msg, str) else ""):
                            last_ticker_ts = time.time()
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "[PRICE_FEED] WebSocket connection lost, reconnecting in %.1fs: %s",
                    self._reconnect_delay, exc,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
            except Exception as exc:
                logger.error(
                    "[PRICE_FEED] WebSocket unexpected error, reconnecting in %.1fs: %s",
                    self._reconnect_delay, exc, exc_info=True,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def _handle_message(self, msg):
        try:
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8")
            data = json.loads(msg)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("[PriceFeed] message decode error", exc_info=True)
            return
        if data.get("op") in ("pong", "subscribe"):
            return
        topic = data.get("topic", "")
        if topic.startswith("tickers."):
            await self._handle_ticker(data)
        elif topic.startswith("orderbook."):
            await self._handle_orderbook(data)

    async def _handle_ticker(self, data):
        d = data.get("data", {})
        if not d:
            return
        market = d.get("symbol", "")
        if not market:
            return
        try:
            price_str = d.get("lastPrice", "")
            if price_str:
                price = float(price_str)
                if price > 0:
                    price_store.set_price(market, price)
                    vol = float(d.get("volume24h", "") or 0)
                    if vol > 0:
                        price_store.set_volume(market, vol)
                    # [2026-04-19 형 검수 UI#1] 메시지 타입 명시 — /ws/prices 클라이언트가 필터 가능
                    await self._broadcast({"type": "ticker", "market": market, "price": price, "volume": vol})
        except (ValueError, TypeError):
            logger.warning("[PriceFeed] ticker price parse error for %s", d.get("symbol", "?"), exc_info=True)
            return

    async def _handle_orderbook(self, data):
        d = data.get("data", {})
        if not d:
            return
        market = d.get("s", "")
        if not market:
            return
        bids, asks = d.get("b", []), d.get("a", [])
        if not bids and not asks:
            return
        try:
            best_bid = float(bids[0][0]) if bids else 0.0
            best_ask = float(asks[0][0]) if asks else 0.0
        except (IndexError, ValueError, TypeError):
            logger.warning("[PriceFeed] orderbook best bid/ask parse error for %s", market, exc_info=True)
            return
        units = []
        for i in range(min(len(bids), len(asks), 15)):
            try:
                units.append({"bid_price": float(bids[i][0]), "bid_size": float(bids[i][1]),
                              "ask_price": float(asks[i][0]), "ask_size": float(asks[i][1])})
            except (IndexError, ValueError, TypeError):
                logger.warning("[PriceFeed] orderbook unit parse error for %s at depth %d", market, i, exc_info=True)
                continue
        if units:
            orderbook_store.set_orderbook(market, ts=time.time(), best_bid=best_bid, best_ask=best_ask, units=units)
            # [2026-04-19 형 검수 UI#1] 메시지 타입 명시
            await self._broadcast({"type": "orderbook", "market": market, "best_bid": best_bid, "best_ask": best_ask})

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if not self.running:
            return
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info("[PriceFeed] stopped (shutdown)")

            self._task = None


def _focus_position_symbols() -> Set[str]:
    """FOCUS 포지션 + lock_market + selected_market 코인 목록.
    runtime/focus_config.json 에서 직접 읽어 circular import 회피.
    (FOCUS는 config+state를 focus_config.json 한 파일에 저장)"""
    symbols: Set[str] = set()
    try:
        import json as _json
        # focus_config.json = { "config": {..., "lock_market": ...}, "state": {"positions": [...], "selected_market": ...} }
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "runtime", "focus_config.json")
        if not os.path.exists(config_path):
            # fallback: 프로젝트 루트 기준
            config_path = os.path.join(os.getcwd(), "runtime", "focus_config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            # config 섹션에서 lock_market
            cfg = data.get("config", data)  # top-level fallback
            lm = cfg.get("lock_market", "")
            if lm:
                symbols.add(lm)
            # state 섹션에서 positions + selected_market
            st = data.get("state", {})
            for pos in st.get("positions", []):
                m = pos.get("market", "")
                if m:
                    symbols.add(m)
            sel = st.get("selected_market", "")
            if sel:
                symbols.add(sel)
    except Exception:
        pass
    return symbols


bybit_price_feed = BybitHyperPriceFeed()
