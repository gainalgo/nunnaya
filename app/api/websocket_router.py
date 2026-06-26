# ============================================================
# File: app/api/websocket_router.py
# Autocoin OS v3-H — WebSocket Router for Real-time Dashboard
# ============================================================

import asyncio
import base64
import os
import secrets
import time as time_module
from http.cookies import SimpleCookie
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request

from app.core.currency import Q
from app.core.rate_limiter import bybit_rate_limiter

import logging
logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

# Multi-timeframe price history cache
_price_history_cache: Dict[str, Dict[str, Any]] = {}
_price_history_cache_ts: float = 0.0
PRICE_HISTORY_CACHE_TTL = 30.0  # 30s cache

MAX_CONNECTIONS = 50  # max concurrent WebSocket connections (DoS protection)

class ConnectionManager:
    """WebSocket connection manager."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._broadcast_task: asyncio.Task | None = None
        self._running = False

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        if len(self.active_connections) >= MAX_CONNECTIONS:
            await websocket.close(code=1008, reason="Too many connections")
            return
        self.active_connections.append(websocket)
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
    
    async def broadcast(self, message: dict):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.warning("websocket_router.disconnect L59 except", exc_info=True)
                disconnected.append(connection)
        
        for conn in disconnected:
            self.disconnect(conn)
    
    @property
    def connection_count(self) -> int:
        return len(self.active_connections)

manager = ConnectionManager()

def _get_ws_cookie(websocket: WebSocket, key: str) -> str:
    """Extract cookie from WebSocket scope/cookie header safely."""
    try:
        direct = (websocket.cookies or {}).get(key)
        if direct:
            return str(direct)
    except Exception as exc:
        logger.warning("[websocket_router] %s: %s", 'websocket_router._get_ws_cookie fallback', exc, exc_info=True)
    try:
        raw_cookie = (websocket.headers.get("cookie", "") or "").strip()
        if raw_cookie:
            jar = SimpleCookie()
            jar.load(raw_cookie)
            morsel = jar.get(key)
            if morsel is not None:
                return str(morsel.value or "")
    except Exception as exc:
        logger.warning("[websocket_router] %s: %s", 'websocket_router._get_ws_cookie fallback', exc, exc_info=True)
    return ""

def _trusted_ws_origin(websocket: WebSocket) -> bool:
    """
    Best-effort browser WebSocket trust check for auth fallback.
    Accept when origin matches forwarded/host header or trusted domain suffix.
    """
    try:
        origin = (websocket.headers.get("origin", "") or "").strip()
        if not origin:
            return False
        p = urlparse(origin)
        if p.scheme not in ("http", "https"):
            return False
        origin_host = (p.hostname or "").lower().strip()
        if not origin_host:
            return False

        host_candidates: List[str] = []
        for hk in ("x-forwarded-host", "host"):
            hv = (websocket.headers.get(hk, "") or "").strip()
            if hv:
                host_candidates.extend([x.strip().lower() for x in hv.split(",") if x.strip()])
        for cand in host_candidates:
            cand_base = cand.split(":", 1)[0]
            if cand_base and cand_base == origin_host:
                return True

        trusted_csv = (os.getenv("DASHBOARD_WS_TRUSTED_ORIGINS", "") or "").strip()
        trusted = [x.strip().lower() for x in trusted_csv.split(",") if x.strip()]
        # Safe public-deploy default: extra trusted origins are configured only via the DASHBOARD_WS_TRUSTED_ORIGINS env (no hardcoded domains).
        # When unset, rely solely on the same-host check above.
        for t in trusted:
            th = urlparse(t).hostname.lower().strip() if "://" in t else t
            th = th[1:] if th.startswith(".") else th
            if not th:
                continue
            if origin_host == th or origin_host.endswith("." + th):
                return True
        return False
    except Exception:
        logger.error("websocket_router._trusted_ws_origin L132 except", exc_info=True)
        return False

async def broadcast_rankings_loop(app):
    """Broadcast rankings data every 10 seconds."""
    while manager._running:
        if manager.connection_count > 0:
            try:
                system = getattr(app.state, "system", None)
                data = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_rankings_data_sync, system
                )
                await manager.broadcast({
                    "type": "rankings",
                    "data": data,
                })
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                logger.warning("websocket_router._trusted_ws_origin L149: %s", e)
                await manager.broadcast({
                    "type": "error",
                    "message": str(e),
                })
        await asyncio.sleep(10)

def fetch_multi_timeframe_prices(symbols: List[str]) -> Dict[str, Dict[str, List[float]]]:
    """
    Fetch multi-timeframe price history.

    Returns:
        {symbol: {"5m": [prices...], "15m": [...], "1h": [...], "4h": [...], "1d": [...]}}
    """
    import requests
    global _price_history_cache, _price_history_cache_ts
    
    now = time_module.time()

    # If the cache is valid, return only the requested symbols from the cache
    if now - _price_history_cache_ts < PRICE_HISTORY_CACHE_TTL and _price_history_cache:
        result = {}
        for sym in symbols:
            if sym in _price_history_cache:
                result[sym] = _price_history_cache[sym]
        return result
    
    from app.core.constants import BYBIT_MARKET_KLINE, bybit_v5_rest_category, parse_bybit_list
    TIMEFRAMES = [("5", 10), ("15", 10), ("60", 10), ("240", 10)]

    result: Dict[str, Dict[str, List[float]]] = {}

    session = _build_bybit_session()
    try:
        for symbol in symbols[:25]:
            result[symbol] = {}
            market = Q.market(symbol) if not symbol.startswith(Q.config.market_prefix) else symbol
            for tf, limit in TIMEFRAMES:
                try:
                    bybit_rate_limiter.acquire()
                    resp = session.get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": tf, "limit": limit}, timeout=2.0)
                    if resp.status_code == 200:
                        raw = parse_bybit_list(resp.json())
                        prices = [float(k[4]) for k in reversed(raw) if isinstance(k, (list, tuple)) and len(k) >= 5]
                        result[symbol][tf] = prices
                    time_module.sleep(0.02)
                except (ConnectionError, TimeoutError, OSError) as e:
                    logger.warning("[WS] price fetch %s/%s network error: %s", symbol, tf, e)
                    result[symbol][tf] = []
                except (KeyError, IndexError, AttributeError, TypeError, ValueError):
                    logger.warning("[WS] price fetch %s/%s failed", symbol, tf, exc_info=True)
                    result[symbol][tf] = []
    finally:
        session.close()

    # Update cache
    _price_history_cache = result
    _price_history_cache_ts = now

    return result

def _build_bybit_session():
    """requests.Session with TCP/SSL connection reuse + built-in automatic retries."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=5)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

def fetch_rankings_data_sync(system) -> dict:
    """Synchronous version of the rankings data fetch."""
    import requests
    from app.core.constants import (
        BYBIT_MARKET_TICKERS,
        BYBIT_MARKET_KLINE,
        BYBIT_MARKET_INSTRUMENTS,
        bybit_v5_rest_category,
        parse_bybit_list,
        normalize_bybit_ticker,
    )

    top_n = 5
    min_volume_usdt = 500_000 if Q.is_usdt else 1_000_000_000
    min_price = 0.01
    max_spread_bps = 50

    session = _build_bybit_session()
    try:
        bybit_rate_limiter.acquire()
        markets_resp = session.get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        markets = [Q.normalize(str(m.get("symbol") or "")) for m in parse_bybit_list(markets_resp.json()) if isinstance(m, dict) and str(m.get("symbol") or "")]
        if not markets:
            return {"ok": True, "message": "No markets", "rankings": {}}
        bybit_rate_limiter.acquire()
        _market_set = set(m.upper() for m in markets[:100])
        ticker_resp = session.get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0)
        all_tickers = [normalize_bybit_ticker(t) for t in parse_bybit_list(ticker_resp.json()) if isinstance(t, dict) and Q.normalize(str(t.get("symbol") or "")).upper() in _market_set]
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
        logger.warning("websocket_router.fetch_rankings_data_sync L248: %s", e)
        return {"ok": False, "error": f"Failed to fetch tickers: {e}"}
    finally:
        session.close()

    if not all_tickers:
        return {"ok": True, "message": "No markets", "rankings": {}}

    tickers = {}
    for t in all_tickers:
        market = t.get("market", "")
        vol_24h = float(t.get("acc_trade_price_24h") or 0)
        last_price = float(t.get("trade_price") or 0)

        if vol_24h < min_volume_usdt:
            continue
        if min_price > 0 and last_price < min_price:
            continue

        tickers[market] = t

    if not tickers:
        return {"ok": True, "message": "No markets meet filter criteria", "rankings": {}}

    markets = list(tickers.keys())[:50]

    candle_data = {}
    session = _build_bybit_session()
    try:
        for market in markets:
            try:
                bybit_rate_limiter.acquire()
                candle_resp = session.get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": "15", "limit": 60}, timeout=3.0)
                raw = parse_bybit_list(candle_resp.json())
                candles = [{"trade_price": float(k[4]), "high_price": float(k[2]), "low_price": float(k[3]), "opening_price": float(k[1]), "candle_acc_trade_volume": float(k[5])} for k in raw if isinstance(k, (list, tuple)) and len(k) >= 6]
                if candles and len(candles) >= 26:
                    candle_data[market] = list(reversed(candles))
                time_module.sleep(0.05)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[websocket_router] %s: %s", 'websocket_router fallback', exc, exc_info=True)
    finally:
        session.close()
    
    def calc_ema(data, period):
        if len(data) < period:
            return data[-1] if data else 0
        multiplier = 2 / (period + 1)
        ema_val = sum(data[:period]) / period
        for price in data[period:]:
            ema_val = (price - ema_val) * multiplier + ema_val
        return ema_val
    
    def calc_rsi(closes, period=14):
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    rebound_list = []
    rsi_list = []
    tech_list = []
    upside_list = []
    
    for market_code, ticker in tickers.items():
        currency = Q.extract_base(market_code)
        market = market_code
        last_price = float(ticker.get("trade_price") or 0)
        price_change_pct = float(ticker.get("signed_change_rate") or 0) * 100
        vol_24h = float(ticker.get("acc_trade_price_24h") or 0)
        high_24h = float(ticker.get("high_price") or 0)
        low_24h = float(ticker.get("low_price") or 0)
        
        candles = candle_data.get(market_code, [])
        closes = [float(c["trade_price"]) for c in candles] if candles else []
        
        rsi_val = calc_rsi(closes) if len(closes) >= 15 else 50.0
        
        ema9 = calc_ema(closes, 9) if len(closes) >= 9 else last_price
        ema21 = calc_ema(closes, 21) if len(closes) >= 21 else last_price
        ema_gap_pct = ((ema9 - ema21) / ema21 * 100) if ema21 > 0 else 0
        
        range_24h = high_24h - low_24h if high_24h > low_24h else 0
        dist_from_low_pct = ((last_price - low_24h) / range_24h * 100) if range_24h > 0 else 50
        
        rebound_score = max(0, -price_change_pct * 2 + (100 - dist_from_low_pct) / 10 + max(0, 30 - rsi_val) / 3)
        if price_change_pct < -3 and rsi_val < 40:
            rebound_list.append({
                "market": market,
                "price": round(last_price, 6),
                "change_24h_pct": round(price_change_pct, 2),
                "rsi": round(rsi_val, 1),
                "dist_from_low_pct": round(dist_from_low_pct, 1),
                "rebound_score": round(rebound_score, 2),
            })
        
        if rsi_val < 35:
            rsi_list.append({
                "market": market,
                "price": round(last_price, 6),
                "rsi": round(rsi_val, 1),
                "change_24h_pct": round(price_change_pct, 2),
                "volume_24h_usdt": round(vol_24h, 0),
            })
        
        tech_score = (
            max(0, 30 - rsi_val) * 2 +
            max(0, -ema_gap_pct) * 5 +
            max(0, 100 - dist_from_low_pct) / 5 +
            max(0, -price_change_pct) * 1.5
        )
        tech_list.append({
            "market": market,
            "price": round(last_price, 6),
            "tech_score": round(tech_score, 2),
            "rsi": round(rsi_val, 1),
            "ema_gap_pct": round(ema_gap_pct, 2),
            "change_24h_pct": round(price_change_pct, 2),
        })
        
        ema50 = calc_ema(closes, 50) if len(closes) >= 50 else last_price
        upside_pct = ((ema50 - last_price) / last_price * 100) if last_price > 0 else 0
        if upside_pct > 0:
            upside_list.append({
                "market": market,
                "price": round(last_price, 6),
                "ema50": round(ema50, 6),
                "upside_pct": round(upside_pct, 2),
                "rsi": round(rsi_val, 1),
                "volume_24h_usdt": round(vol_24h, 0),
            })
    
    rebound_list.sort(key=lambda x: x["rebound_score"], reverse=True)
    rsi_list.sort(key=lambda x: x["rsi"])
    tech_list.sort(key=lambda x: x["tech_score"], reverse=True)
    upside_list.sort(key=lambda x: x["upside_pct"], reverse=True)
    
    # Fetch multi-timeframe price history for the TOP 5 coins
    all_top_symbols = set()
    for lst in [rebound_list[:top_n], rsi_list[:top_n], tech_list[:top_n], upside_list[:top_n]]:
        for item in lst:
            market = item.get("market", "")
            symbol = Q.extract_base(market) if market.startswith(Q.config.market_prefix) else market
            if symbol:
                all_top_symbols.add(symbol)
    
    price_history_map = fetch_multi_timeframe_prices(list(all_top_symbols))
    
    def to_section_format(items):
        result = []
        for item in items[:top_n]:
            market = item.get("market", "")
            symbol = Q.extract_base(market) if market.startswith(Q.config.market_prefix) else market
            formatted = {
                "market": market,
                "symbol": symbol,
                "price": item.get("price", 0),
                "change_pct": item.get("change_24h_pct", 0),
                "rsi": item.get("rsi", 50),
            }
            if "rebound_score" in item:
                formatted["score"] = round(item["rebound_score"], 1)
            if "tech_score" in item:
                formatted["score"] = round(item["tech_score"], 1)
                formatted["signal"] = "매수" if item["tech_score"] > 50 else "관망"
            if "upside_pct" in item:
                formatted["score"] = round(item["upside_pct"], 1)
                formatted["ai_score"] = 0.7
            if "rsi" in item and "rebound_score" not in item and "tech_score" not in item:
                formatted["rsi_status"] = "Oversold" if item["rsi"] < 30 else "Neutral"

            # Attach multi-timeframe price history
            if symbol in price_history_map:
                formatted["price_history"] = price_history_map[symbol]
            
            result.append(formatted)
        return {"items": result}
    
    return {
        "ok": True,
        "rankings": {
            "rebound": to_section_format(rebound_list),
            "rsi_oversold": to_section_format(rsi_list),
            "tech_score": to_section_format(tech_list),
            "upside": to_section_format(upside_list),
        },
    }

async def start_broadcast_task(app):
    """Start the broadcast task."""
    if manager._broadcast_task is None or manager._broadcast_task.done():
        manager._running = True
        manager._broadcast_task = asyncio.create_task(broadcast_rankings_loop(app))

async def stop_broadcast_task():
    """Stop the broadcast task."""
    manager._running = False
    if manager._broadcast_task:
        manager._broadcast_task.cancel()
        try:
            await manager._broadcast_task
        except asyncio.CancelledError:
            pass  # normal shutdown

@router.websocket("/ws/dashboard")
async def websocket_endpoint(websocket: WebSocket):
    """
    Dashboard WebSocket endpoint.

    Registers the client on connect and pushes rankings data every 10 seconds.
    Clients can keep the connection alive by sending ping messages.
    """
    user = os.getenv("DASHBOARD_USER", "").strip()
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if user and password:
        # Basic Auth header (legacy path)
        auth_header = websocket.headers.get("Authorization", "") or websocket.headers.get("authorization", "")
        ok = False
        if auth_header.startswith("Basic "):
            try:
                encoded = auth_header[6:]
                decoded = base64.b64decode(encoded).decode("utf-8")
                req_user, req_pass = decoded.split(":", 1)
                ok = secrets.compare_digest(req_user, user) and secrets.compare_digest(req_pass, password)
            except (KeyError, IndexError, TypeError):
                logger.warning("websocket_router.to_section_format L484 except", exc_info=True)
                ok = False
        # Session cookie path (same as HTTP middleware) for mobile/browser WebSocket.
        if not ok:
            try:
                session_token = _get_ws_cookie(websocket, "autocoin_session")
                if session_token:
                    from app import main as app_main  # runtime import to avoid hard cycle
                    sessions = getattr(app_main, "_AUTH_SESSIONS", set())
                    ok = session_token in sessions
            except (KeyError, AttributeError, TypeError):
                logger.warning("websocket_router.to_section_format L494 except", exc_info=True)
                ok = False
        # [2026-04-09 security hardening] removed same-origin bypass.
        # The Origin header can be forged, so trust only cookies / Basic Auth.
        if not ok:
            await websocket.close(code=1008)
            return

    await manager.connect(websocket)
    
    app = websocket.app
    await start_broadcast_task(app)
    
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("[WebSocket] client disconnected")
        manager.disconnect(websocket)
    except Exception:
        logger.error("websocket_router.to_section_format L516 except", exc_info=True)
        manager.disconnect(websocket)

@router.websocket("/ws/prices")
async def websocket_prices_endpoint(websocket: WebSocket):
    """Real-time price WebSocket — relays the Bybit ticker stream.

    [2026-04-19] Reduced perceived UI latency ("doing it right is the answer")
    - Before: dashboard_v2.js polled /api/strategy/focus/status every 5s → 5~8s lag
    - Now: clients register directly with the server's bybit_price_feed (WS ticker receiver)
            → every tick from the Bybit server immediately pushes { "market": "SOLUSDT", "price": 86.75, "volume": ... }
    - Effect: ms-level updates. Higher precision for manual-exit timing ↑
    - Price source: hyper_price_feed_bybit.BybitHyperPriceFeed._handle_ticker → _broadcast()
    """
    # Auth (same pattern as /ws/dashboard)
    user = os.getenv("DASHBOARD_USER", "").strip()
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if user and password:
        auth_header = websocket.headers.get("Authorization", "") or websocket.headers.get("authorization", "")
        ok = False
        if auth_header.startswith("Basic "):
            try:
                encoded = auth_header[6:]
                decoded = base64.b64decode(encoded).decode("utf-8")
                req_user, req_pass = decoded.split(":", 1)
                ok = secrets.compare_digest(req_user, user) and secrets.compare_digest(req_pass, password)
            except (KeyError, IndexError, TypeError):
                ok = False
        if not ok:
            try:
                session_token = _get_ws_cookie(websocket, "autocoin_session")
                if session_token:
                    from app import main as app_main
                    sessions = getattr(app_main, "_AUTH_SESSIONS", set())
                    ok = session_token in sessions
            except (KeyError, AttributeError, TypeError):
                ok = False
        if not ok:
            await websocket.close(code=1008)
            return

    # Access system.price_feed (main.py L412: app.state.system = system)
    price_feed = None
    try:
        system = websocket.app.state.system
        price_feed = getattr(system, "price_feed", None)
    except AttributeError:
        logger.warning("[ws/prices] app.state.system not available")

    if price_feed is None:
        await websocket.close(code=1011, reason="price_feed unavailable")
        return

    # [2026-04-19 this agent review UI#2] connection cap 20 (DoS protection, plenty for a single-user setup)
    try:
        current = len(getattr(price_feed, "clients", []))
    except Exception:
        current = 0
    if current >= 20:
        logger.warning("[ws/prices] too many clients (%d≥20) — rejecting", current)
        await websocket.close(code=1008, reason="too many price clients")
        return

    # register() performs ws.accept() + clients.add() internally
    await price_feed.register(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # [2026-04-19 this agent review UI#3] JSON ping is canonical, text "ping" is for backward compatibility
            is_ping = False
            if data == "ping":
                is_ping = True
            else:
                try:
                    import json as _json
                    parsed = _json.loads(data)
                    if isinstance(parsed, dict) and parsed.get("type") == "ping":
                        is_ping = True
                except Exception:
                    pass
            if is_ping:
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("[ws/prices] client disconnected")
    except Exception:
        logger.warning("[ws/prices] unexpected error", exc_info=True)
    finally:
        try:
            await price_feed.unregister(websocket)
        except Exception:
            logger.warning("[ws/prices] unregister failed", exc_info=True)


@router.get("/ws/status", summary="WebSocket connection status")
def websocket_status():
    """Query the current WebSocket connection status."""
    return {
        "ok": True,
        "active_connections": manager.connection_count,
        "broadcast_running": manager._running,
        "state_clients": state_manager.connection_count,
    }


# ============================================================
# Phase C-1 (2026-04-20): /ws/state — push engine state changes
# ============================================================
# Pushes changes that polling can't cover (POSITION_OPEN/CLOSE, CONFIG_CHANGED, STATE_TRANSITION)
# immediately from the engine hot path. Polling serves as fallback/sync correction.
#
# Message format:
#   { "type": "state_event", "event": "POSITION_OPEN", "payload": {...}, "ts": 1.7e9 }
#
# Engine hook interface:
#   - async path: `await state_broadcast(event, payload)`
#   - sync path:  `state_broadcast_safe(event, payload)`  ← fire-and-forget
# ============================================================

state_manager = ConnectionManager()


async def state_broadcast(event_type: str, payload: dict):
    """Push from engine → subscribed clients. Returns immediately if there are 0 subscribers (zero cost)."""
    if state_manager.connection_count == 0:
        return
    msg = {
        "type": "state_event",
        "event": event_type,
        "payload": payload,
        "ts": time_module.time(),
    }
    await state_manager.broadcast(msg)


def state_broadcast_safe(event_type: str, payload: dict):
    """Sync wrapper — fire-and-forget from the engine's sync hot path.

    Creates a task if an asyncio loop is running, otherwise silently skips (e.g. test environments).
    A failure does not affect the trading path (all exceptions are absorbed).
    """
    if state_manager.connection_count == 0:
        return
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(state_broadcast(event_type, payload))
    except Exception as exc:
        logger.debug("[ws/state] broadcast_safe skipped: %s", exc)


@router.websocket("/ws/state")
async def websocket_state_endpoint(websocket: WebSocket):
    """Engine state-change WebSocket — Phase C-1 (2026-04-20).

    The auth/cap/ping pattern is identical to /ws/prices.
    Engine hooks are added in the C-2 stage (POSITION_OPEN/CLOSE, CONFIG_CHANGED, STATE_TRANSITION).
    """
    # Auth (same pattern)
    user = os.getenv("DASHBOARD_USER", "").strip()
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if user and password:
        auth_header = websocket.headers.get("Authorization", "") or websocket.headers.get("authorization", "")
        ok = False
        if auth_header.startswith("Basic "):
            try:
                encoded = auth_header[6:]
                decoded = base64.b64decode(encoded).decode("utf-8")
                req_user, req_pass = decoded.split(":", 1)
                ok = secrets.compare_digest(req_user, user) and secrets.compare_digest(req_pass, password)
            except (KeyError, IndexError, TypeError):
                ok = False
        if not ok:
            try:
                session_token = _get_ws_cookie(websocket, "autocoin_session")
                if session_token:
                    from app import main as app_main
                    sessions = getattr(app_main, "_AUTH_SESSIONS", set())
                    ok = session_token in sessions
            except (KeyError, AttributeError, TypeError):
                ok = False
        if not ok:
            await websocket.close(code=1008)
            return

    # connection cap 20 (UI#2 pattern — plenty for a single-user setup)
    if state_manager.connection_count >= 20:
        logger.warning("[ws/state] too many clients (%d≥20) — rejecting", state_manager.connection_count)
        await websocket.close(code=1008, reason="too many state clients")
        return

    await state_manager.connect(websocket)
    logger.info("[ws/state] client connected (total=%d)", state_manager.connection_count)
    try:
        while True:
            data = await websocket.receive_text()
            # Ping/pong (UI#3 — JSON canonical, text "ping" backward-compatible)
            is_ping = False
            if data == "ping":
                is_ping = True
            else:
                try:
                    import json as _json
                    parsed = _json.loads(data)
                    if isinstance(parsed, dict) and parsed.get("type") == "ping":
                        is_ping = True
                except Exception:
                    pass
            if is_ping:
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("[ws/state] client disconnected")
        state_manager.disconnect(websocket)
    except Exception:
        logger.warning("[ws/state] unexpected error", exc_info=True)
        state_manager.disconnect(websocket)
