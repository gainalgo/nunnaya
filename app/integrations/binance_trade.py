# -*- coding: utf-8 -*-
"""Binance private REST client. HMAC-SHA256 (query-string) auth. TradeClient protocol.

Mirror of Bybit's BybitTradeClient — implements the same method signatures
(place_order/get_balance/get_kline/accounts/market_buy/market_sell/cancel_order/wait_order...)
so a manager can swap one for the other transparently.

category:
  - "spot"   → Binance Spot   (api.binance.com /api/v3)
  - "linear" → Binance USDT-M futures (fapi.binance.com /fapi/v1, /fapi/v2)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter

from app.integrations.binance_instrument_cache import BinanceInstrumentCache

logger = logging.getLogger(__name__)
_TRADE_RETRY_MAX = 4
_TRADE_RETRY_BASE_SEC = 1.5
# ★ [audit medium#2] recvWindow is env-configurable — guards against an environment-dependent
#   risk where server clock drift >5s makes every signed call fail with -1021. Default 5000
#   (Bybit parity); raise via BINANCE_RECV_WINDOW if needed (max 60000).
_RECV_WINDOW = str(min(max(int(os.getenv("BINANCE_RECV_WINDOW", "5000") or "5000"), 1000), 60000))
_KLINE_CACHE_TTL = 30.0

# Bybit/internal interval (minute number / D,W,M) → Binance interval mapping.
_INTERVAL_MAP = {
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
    "60": "1h", "120": "2h", "240": "4h", "360": "6h", "480": "8h",
    "720": "12h", "D": "1d", "W": "1w", "M": "1M",
    "1440": "1d",
}


class BinanceAPIError(RuntimeError):
    def __init__(self, message, *, status_code=None, ret_code=None, response_text=None):
        super().__init__(message)
        self.status_code = status_code
        self.ret_code = ret_code
        self.response_text = response_text


def _is_testnet() -> bool:
    return str(os.getenv("BINANCE_TESTNET", "0")).strip().lower() in ("1", "true", "yes")


class BinanceTradeClient:
    EXCHANGE_TYPE = "binance"

    def __init__(self, api_key=None, api_secret=None, *, timeout=10.0, category: Optional[str] = None):
        self.api_key = (api_key or os.getenv("BINANCE_API_KEY", "")).strip()
        self.api_secret = (api_secret or os.getenv("BINANCE_API_SECRET", "")).strip()
        self.timeout = float(timeout)
        c = (category or os.getenv("BINANCE_CATEGORY", "spot")).strip().lower()
        self._category = c if c in ("spot", "linear") else "spot"
        self._api_call_count = 0
        self._api_call_reset_ts = time.time()
        self._session = requests.Session()
        _adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        self._session.mount("https://", _adapter)
        self._kline_cache: dict = {}

    # ── base URL / path ─────────────────────────────────────
    @property
    def _base(self) -> str:
        if self._category == "linear":
            return "https://testnet.binancefuture.com" if _is_testnet() else "https://fapi.binance.com"
        return "https://testnet.binance.vision" if _is_testnet() else "https://api.binance.com"

    def _path(self, spot_path: str, fut_path: str) -> str:
        return fut_path if self._category == "linear" else spot_path

    # ── signing ─────────────────────────────────────────────
    def _sign(self, query: str) -> str:
        return hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _track_api_call(self):
        now = time.time()
        if now - self._api_call_reset_ts >= 60:
            self._api_call_count = 0
            self._api_call_reset_ts = now
        self._api_call_count += 1

    def get_api_stats(self):
        elapsed = time.time() - self._api_call_reset_ts
        proj = self._api_call_count * (60.0 / max(elapsed, 1.0))
        return {"calls_per_min": self._api_call_count, "seconds_elapsed": int(elapsed),
                "projected_calls_per_min": round(proj, 1),
                "limit_warning": proj >= 1100 if elapsed >= 5 else False}

    def _request(self, method, path, *, params=None, signed=False, is_order=False):
        self._track_api_call()
        url = f"{self._base}{path}"
        for attempt in range(_TRADE_RETRY_MAX):
            try:
                p = dict(params or {})
                headers = {}
                if signed:
                    if not self.api_key or not self.api_secret:
                        raise BinanceAPIError("Binance API key/secret missing for signed request")
                    p["timestamp"] = str(int(time.time() * 1000))
                    p["recvWindow"] = _RECV_WINDOW
                    qs = urlencode(p)
                    p_signed = qs + "&signature=" + self._sign(qs)
                    headers["X-MBX-APIKEY"] = self.api_key
                    full = f"{url}?{p_signed}"
                    resp = self._session.request(method.upper(), full, headers=headers, timeout=self.timeout)
                else:
                    resp = self._session.request(method.upper(), url, params=p, headers=headers, timeout=self.timeout)

                try:
                    body = resp.json() if resp.text else {}
                except (ValueError, Exception):
                    logger.warning("[BinanceTrade] Non-JSON response attempt %d: %s", attempt + 1,
                                   resp.text[:200] if resp.text else "(empty)")
                    if attempt < _TRADE_RETRY_MAX - 1:
                        time.sleep(_TRADE_RETRY_BASE_SEC * (2 ** attempt))
                        continue
                    raise BinanceAPIError(f"Non-JSON response: {resp.text[:200]}", status_code=resp.status_code)

                # Binance error payloads: {"code": -xxxx, "msg": "..."}.
                # ★ [audit bug#10] Treat only a negative code as an error — so endpoints whose
                #   success response is {code:200,msg:"success"} (e.g. positionSide/dual, leverage)
                #   are not misjudged (endswith allowlist dropped).
                _code = body.get("code") if isinstance(body, dict) else None
                if isinstance(_code, int) and _code < 0:
                    if _code in (-1003, -1015) or resp.status_code in (429, 418):
                        time.sleep(min(_TRADE_RETRY_BASE_SEC * (2 ** attempt), 10.0))
                        continue
                    raise BinanceAPIError(f"Binance API error: {body.get('msg')} (code={_code})",
                                          status_code=resp.status_code, ret_code=_code)
                if resp.status_code >= 400:
                    if resp.status_code in (429, 418):
                        time.sleep(min(_TRADE_RETRY_BASE_SEC * (2 ** attempt), 10.0))
                        continue
                    raise BinanceAPIError(f"HTTP {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code)
                return body
            except requests.RequestException as e:
                is_last = attempt >= _TRADE_RETRY_MAX - 1
                if is_last:
                    logger.error("Binance API request FAILED after %d attempts: %s", _TRADE_RETRY_MAX, e, exc_info=True)
                    raise BinanceAPIError(f"Request failed: {e}") from e
                logger.warning("Binance API request failed (attempt %d/%d): %s — retrying",
                               attempt + 1, _TRADE_RETRY_MAX, e)
                time.sleep(_TRADE_RETRY_BASE_SEC * (2 ** attempt))
        raise BinanceAPIError(f"Exhausted {_TRADE_RETRY_MAX} retries on {path}", status_code=429)

    # ── symbol / precision ──────────────────────────────────
    def _normalize_symbol(self, symbol) -> str:
        return str(symbol or "").replace("/", "").replace("-", "").strip().upper()

    def get_tick_size(self, symbol):
        return BinanceInstrumentCache.get_tick_size(self._normalize_symbol(symbol), category=self._category)

    def _adj_qty(self, symbol, qty):
        return BinanceInstrumentCache.adjust_qty(symbol, float(qty), category=self._category)

    def _adj_price(self, symbol, price, side):
        return BinanceInstrumentCache.adjust_price(symbol, float(price), side=side, category=self._category)

    # ── account / balance ───────────────────────────────────
    def accounts(self, *, skip_currencies=None, **kw):
        skip = set(skip_currencies or [])
        out = []
        if self._category == "linear":
            res = self._request("GET", "/fapi/v2/account", signed=True)
            for a in res.get("assets", []):
                cur = a.get("asset", "")
                if cur in skip:
                    continue
                bal = float(a.get("walletBalance", 0) or 0)
                locked = float(a.get("initialMargin", 0) or 0)
                if bal <= 0 and locked <= 0:
                    continue
                out.append({"currency": cur, "balance": str(bal), "locked": str(locked),
                            "avg_buy_price": "0", "unit_currency": "USDT"})
        else:
            res = self._request("GET", "/api/v3/account", signed=True)
            for b in res.get("balances", []):
                cur = b.get("asset", "")
                if cur in skip:
                    continue
                free = float(b.get("free", 0) or 0)
                locked = float(b.get("locked", 0) or 0)
                if free <= 0 and locked <= 0:
                    continue
                out.append({"currency": cur, "balance": str(free), "locked": str(locked),
                            "avg_buy_price": "0", "unit_currency": "USDT"})
        return out

    def get_balance(self, currency, *, include_locked=False):
        cur = str(currency).upper()
        try:
            for acc in self.accounts():
                if acc["currency"] == cur:
                    bal = float(acc["balance"])
                    if include_locked:
                        bal += float(acc.get("locked", 0) or 0)
                    return bal
            return 0.0
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error("Balance error %s: %s", currency, e)
            return 0.0

    # ── orders: query ───────────────────────────────────────
    def list_orders(self, *, state="wait", market=None, limit=50, **kw):
        params = {}
        if market:
            params["symbol"] = self._normalize_symbol(market)
        if state in ("wait",):
            path = self._path("/api/v3/openOrders", "/fapi/v1/openOrders")
        else:
            if not market:
                raise BinanceAPIError("Binance order history requires a market symbol")
            params["limit"] = min(int(limit), 50)
            path = self._path("/api/v3/allOrders", "/fapi/v1/allOrders")
        res = self._request("GET", path, params=params, signed=True)
        rows = res if isinstance(res, list) else []
        return [self._convert_order(o) for o in rows]

    def list_wait_orders(self, *, market=None, **kw):
        return self.list_orders(state="wait", market=market)

    def list_done_orders(self, *, market=None, **kw):
        return self.list_orders(state="done", market=market)

    def get_order(self, *, uuid, market=None):
        if not market:
            raise BinanceAPIError("Binance get_order requires a market symbol")
        params = {"symbol": self._normalize_symbol(market), "orderId": uuid}
        path = self._path("/api/v3/order", "/fapi/v1/order")
        res = self._request("GET", path, params=params, signed=True)
        return self._convert_order(res)

    # ── orders: place ───────────────────────────────────────
    def place_order(self, *, market, side, ord_type, volume=None, price=None, **kw):
        symbol = self._normalize_symbol(market)
        s = str(side).lower()
        b_side = "BUY" if s in ("bid", "buy", "long") else "SELL"
        ot = str(ord_type).lower()
        b_type = "MARKET" if ot in ("market", "price") else "LIMIT"
        params: Dict[str, Any] = {"symbol": symbol, "side": b_side, "type": b_type}

        # Spot market BUY by quote amount (quoteOrderQty)
        if (self._category == "spot" and b_type == "MARKET" and b_side == "BUY"
                and kw.get("_market_unit") == "quoteCoin"):
            params["quoteOrderQty"] = str(volume)
        elif volume is not None:
            params["quantity"] = str(self._adj_qty(symbol, float(volume)))

        if price is not None and b_type == "LIMIT":
            params["price"] = str(self._adj_price(symbol, float(price), s))
            params["timeInForce"] = str(kw.get("time_in_force", "GTC")).upper()
        elif b_type == "LIMIT":
            params["timeInForce"] = str(kw.get("time_in_force", "GTC")).upper()

        if self._category == "linear" and (kw.get("reduce_only") or kw.get("reduceOnly")):
            params["reduceOnly"] = "true"

        # ★ [audit bug#8] A futures market POST defaults to an ACK response (status=NEW, avgPrice=0)
        #   → no fill price returned. Requesting RESULT returns it with avgPrice/executedQty filled
        #   (0 extra calls). Protects exit-journal accuracy.
        if self._category == "linear" and b_type == "MARKET":
            params["newOrderRespType"] = "RESULT"

        # ★ [audit high#1] Idempotency key (newClientOrderId) — even if a _request retry
        #   (timeout/429) resends the same order, Binance rejects the duplicate clientOrderId
        #   (-2010/-4015), blocking a *double fill* (real-funds loss). Generated once in
        #   place_order → retry attempts send the same key (_request reuses the params dict
        #   outside the loop, so the key stays constant). Not attached to cancel (DELETE).
        #   Spec: ^[A-Za-z0-9_-]{1,36}$
        import uuid as _uuid
        params["newClientOrderId"] = "x-" + _uuid.uuid4().hex[:30]

        path = self._path("/api/v3/order", "/fapi/v1/order")
        res = self._request("POST", path, params=params, signed=True, is_order=True)
        oid = res.get("orderId", "")
        # Market orders return fills immediately on spot; futures may still be ACK → re-query.
        if oid and (not res.get("status") or str(res.get("status")) == "NEW"):
            try:
                time.sleep(0.2)
                return self.get_order(uuid=oid, market=symbol)
            except BinanceAPIError:
                logger.warning("get_order fallback orderId=%s symbol=%s", oid, symbol, exc_info=True)
        return self._convert_order(res)

    def _last_price(self, symbol: str) -> float:
        path = self._path("/api/v3/ticker/price", "/fapi/v1/ticker/price")
        res = self._request("GET", path, params={"symbol": symbol})
        last = float(res.get("price") or 0)
        if last <= 0:
            raise BinanceAPIError(f"Invalid last price for {symbol}")
        return last

    def market_buy(self, market, quote_amount, **kw):
        symbol = self._normalize_symbol(market)
        min_n = BinanceInstrumentCache.get_min_notional(symbol, category=self._category)
        if quote_amount < min_n:
            raise BinanceAPIError(f"Amount {quote_amount} < min notional {min_n} for {symbol}")
        if quote_amount <= 0 or quote_amount > 1_000_000:
            raise BinanceAPIError(f"Amount sanity check failed: {quote_amount}")
        logger.info("[MARKET_BUY] %s quote=%.2f category=%s", symbol, quote_amount, self._category)
        if self._category == "linear":
            last = self._last_price(symbol)
            qty = self._adj_qty(symbol, float(quote_amount) / last)
            min_q = BinanceInstrumentCache.get_min_qty(symbol, category=self._category)
            if qty < min_q:
                raise BinanceAPIError(f"Linear qty {qty} < min {min_q} for {symbol}")
            return self.place_order(market=market, side="Buy", ord_type="Market", volume=qty)
        return self.place_order(market=market, side="Buy", ord_type="Market",
                                volume=quote_amount, _market_unit="quoteCoin")

    def market_sell(self, market, qty, **kw):
        symbol = self._normalize_symbol(market)
        qty = self._adj_qty(symbol, qty)
        min_q = BinanceInstrumentCache.get_min_qty(symbol, category=self._category)
        if qty < min_q:
            raise BinanceAPIError(f"Qty {qty} < min {min_q} for {symbol}")
        # Linear: market_sell means intent to close a LONG → force reduce_only (prevents an accidental new SHORT entry).
        if self._category == "linear":
            return self.place_order(market=market, side="Sell", ord_type="Market", volume=qty, reduce_only=True)
        return self.place_order(market=market, side="Sell", ord_type="Market", volume=qty)

    def market_sell_usdt(self, market, quote_amount, **kw):
        symbol = self._normalize_symbol(market)
        if self._category != "linear":
            raise BinanceAPIError("market_sell_usdt is supported for category=linear only")
        min_n = BinanceInstrumentCache.get_min_notional(symbol, category=self._category)
        if quote_amount < min_n:
            raise BinanceAPIError(f"Amount {quote_amount} < min notional {min_n} for {symbol}")
        last = self._last_price(symbol)
        qty = self._adj_qty(symbol, float(quote_amount) / last)
        min_q = BinanceInstrumentCache.get_min_qty(symbol, category=self._category)
        if qty < min_q:
            raise BinanceAPIError(f"Linear qty {qty} < min {min_q} for {symbol}")
        logger.info("[MARKET_SELL_USDT] %s quote=%.2f", symbol, quote_amount)
        return self.place_order(market=market, side="Sell", ord_type="Market", volume=qty, reduce_only=True)

    def market_buy_usdt(self, market, amount, **kw):
        return self.market_buy(market, amount, **kw)

    def market_sell_qty(self, market, qty, **kw):
        return self.market_sell(market, qty, **kw)

    def limit_buy(self, market, price, volume):
        return self.place_order(market=market, side="Buy", ord_type="Limit", price=price, volume=volume)

    def limit_sell(self, market, price, volume):
        return self.place_order(market=market, side="Sell", ord_type="Limit", price=price, volume=volume)

    def quick_sell(self, market, qty, price):
        result = {"success": False, "action": "error", "filled_qty": 0.0, "remaining_qty": qty,
                  "avg_price": 0.0, "order": None, "message": ""}
        order = self.place_order(market=market, side="Sell", ord_type="Limit",
                                 price=price, volume=qty, time_in_force="IOC")
        result["order"] = order
        try:
            time.sleep(0.3)
            order_uuid = order.get("uuid", "")
            status_ok = False
            for _attempt in range(2):
                try:
                    order = self.get_order(uuid=order_uuid, market=market)
                    result["order"] = order
                    status_ok = True
                    break
                except BinanceAPIError as api_err:
                    logger.warning("[QUICK_SELL] get_order attempt %d failed %s: %s", _attempt + 1, market, api_err)
                    if _attempt == 0:
                        time.sleep(0.5)
            if not status_ok:
                result.update(success=True, action="unknown",
                              message=f"Order placed (uuid={order_uuid}), status check failed after retries")
                return result
            filled = float(order.get("executed_volume", 0) or 0)
            remaining = float(order.get("remaining_volume", 0) or 0)
            avg_p = float(order.get("avg_price", 0) or 0)
            state = str(order.get("state", "")).lower()
            result["filled_qty"] = filled
            result["remaining_qty"] = remaining
            result["avg_price"] = avg_p
            if state == "done" or (filled > 0 and remaining == 0):
                result.update(success=True, action="filled", message=f"Filled: {filled} @ {avg_p}")
            elif filled > 0:
                result.update(success=True, action="partial", message=f"Partial: {filled}/{qty}")
            else:
                result.update(action="cancelled", message="IOC not filled")
        except Exception as e:
            logger.error("[QUICK_SELL] Order PLACED but status check failed %s: %s", market, e, exc_info=True)
            result.update(success=True, action="unknown",
                          message=f"Order placed (uuid={order.get('uuid','')}), status check failed: {e}")
        return result

    def cancel_order(self, *, uuid, market=None):
        if not market:
            raise BinanceAPIError("Binance cancel_order requires a market symbol")
        params = {"symbol": self._normalize_symbol(market), "orderId": uuid}
        path = self._path("/api/v3/order", "/fapi/v1/order")
        return self._convert_order(self._request("DELETE", path, params=params, signed=True, is_order=True))

    def wait_order(self, *, uuid, market=None, timeout_sec=30.0, poll_interval=1.0):
        end_ts = time.time() + float(timeout_sec)
        last: Dict[str, Any] = {}
        consecutive_failures = 0
        while time.time() < end_ts:
            try:
                last = self.get_order(uuid=uuid, market=market)
                consecutive_failures = 0
                if str(last.get("state", "")).lower() in ("done", "cancel"):
                    return last
            except BinanceAPIError as api_err:
                consecutive_failures += 1
                logger.warning("[WAIT_ORDER] get_order failed (attempt %d) uuid=%s: %s",
                               consecutive_failures, uuid, api_err)
                if consecutive_failures >= 3:
                    logger.error("[WAIT_ORDER] 3 consecutive failures uuid=%s — aborting", uuid)
                    last["_poll_error"] = str(api_err)
                    last["_poll_failed"] = True
                    return last
            time.sleep(float(poll_interval))
        if not last:
            logger.warning("[WAIT_ORDER] timeout with NO successful poll uuid=%s", uuid)
        return last

    def _convert_order(self, order):
        raw_side = str(order.get("side", "")).lower()
        side = "bid" if raw_side == "buy" else ("ask" if raw_side == "sell" else raw_side)
        raw_st = str(order.get("status", order.get("state", "")))
        sm = {"NEW": "wait", "PARTIALLY_FILLED": "wait", "PENDING_NEW": "wait",
              "FILLED": "done", "CANCELED": "cancel", "REJECTED": "cancel",
              "EXPIRED": "cancel", "EXPIRED_IN_MATCH": "cancel"}
        state = sm.get(raw_st, raw_st.lower() if raw_st else "")
        rt = str(order.get("type", "")).lower()
        ord_type = "market" if rt == "market" else ("limit" if rt == "limit" else rt)
        executed = order.get("executedQty") or order.get("executed_volume") or "0"
        orig = order.get("origQty") or order.get("volume") or "0"
        try:
            remaining = str(float(orig) - float(executed))
        except (TypeError, ValueError):
            remaining = "0"
        # avgPrice: futures provides it; spot derives from cummulativeQuoteQty / executedQty.
        avg = order.get("avgPrice")
        if avg is None:
            cq = order.get("cummulativeQuoteQty")
            try:
                avg = str(float(cq) / float(executed)) if cq and float(executed) > 0 else (order.get("price") or "0")
            except (TypeError, ValueError, ZeroDivisionError):
                avg = order.get("price") or "0"
        return {"uuid": str(order.get("orderId", order.get("uuid", ""))),
                "side": side, "ord_type": ord_type,
                "price": str(order.get("price") or avg or "0"),
                "state": state,
                "market": str(order.get("symbol", order.get("market", ""))),
                "volume": str(orig),
                "remaining_volume": str(remaining),
                "executed_volume": str(executed),
                "avg_price": str(avg or "0"),
                "trades_count": int(order.get("trades_count", 0)),
                "created_at": str(order.get("time", order.get("updateTime", order.get("created_at", "")))),
                "paid_fee": str(order.get("paid_fee", 0) or 0),
                "fee_currency": "USDT", "_raw": order}

    def summarize_order(self, o):
        try:
            return (f"uuid={o.get('uuid','')} {o.get('market','')} {o.get('side','')} "
                    f"state={o.get('state','')} price={o.get('price','')} vol={o.get('volume','')}")
        except (KeyError, AttributeError, TypeError, ValueError):
            return str(o)

    def get_min_order_amount(self, symbol):
        sym = self._normalize_symbol(symbol)
        return {"min_amount": BinanceInstrumentCache.get_min_qty(sym, category=self._category),
                "min_cost": BinanceInstrumentCache.get_min_notional(sym, category=self._category),
                "min_price": 0.0}

    def get_order_chance(self, market):
        return {"balance": self.accounts()}

    # ── Futures / Position Management (linear only) ─────────
    def set_leverage(self, symbol: str, buy_leverage: int, sell_leverage: Optional[int] = None):
        if self._category != "linear":
            raise BinanceAPIError("set_leverage is linear-only")
        params = {"symbol": self._normalize_symbol(symbol), "leverage": int(buy_leverage)}
        return self._request("POST", "/fapi/v1/leverage", params=params, signed=True)

    def get_positions(self, symbol: Optional[str] = None, settle_coin: str = "USDT"):
        if self._category != "linear":
            return []
        params = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        res = self._request("GET", "/fapi/v2/positionRisk", params=params, signed=True)
        rows = res if isinstance(res, list) else []
        # Return only held positions (amt != 0)
        return [r for r in rows if abs(float(r.get("positionAmt", 0) or 0)) > 0]

    def switch_position_mode(self, symbol: str = "", mode: str = "MergedSingle"):
        """One-way(MergedSingle) vs Hedge(BothSide). Binance: account-wide dualSidePosition."""
        if self._category != "linear":
            raise BinanceAPIError("switch_position_mode is linear-only")
        dual = "false" if mode == "MergedSingle" else "true"
        try:
            return self._request("POST", "/fapi/v1/positionSide/dual",
                                  params={"dualSidePosition": dual}, signed=True)
        except BinanceAPIError as exc:
            # -4059: no need to change position side → already set
            if exc.ret_code == -4059:
                return {}
            raise

    def set_trading_stop(self, symbol: str, *, take_profit: Optional[float] = None,
                         stop_loss: Optional[float] = None, position_idx: int = 0):
        """Server-side TP/SL — on Binance futures these are separate STOP_MARKET / TAKE_PROFIT_MARKET (closePosition) orders.

        Plants the exit prices for a position on the exchange so they fill regardless of whether the bot is alive (mirrors Bybit set_trading_stop).
        Returns: {"sl": <order|None>, "tp": <order|None>}.
        """
        if self._category != "linear":
            raise BinanceAPIError("set_trading_stop is linear-only")
        sym = self._normalize_symbol(symbol)
        pos = self.get_positions(symbol=sym)
        if not pos:
            raise BinanceAPIError(f"No open position for {sym}")
        amt = float(pos[0].get("positionAmt", 0) or 0)
        close_side = "SELL" if amt > 0 else "BUY"  # LONG → SELL stop, SHORT → BUY stop
        # ★ [audit bug#4] Bybit set_trading_stop is an in-place idempotent update, but on Binance
        #   it is a new order. POSTing every time without cancelling leaves the old STOP/TP in place
        #   (breaks trailing / old target fills early) and eventually fails once the algo-order limit
        #   is reached. → Cancel this symbol's existing STOP_MARKET/TAKE_PROFIT_MARKET(closePosition) before re-placing.
        self._cancel_conditional_orders(sym)
        import uuid as _uuid
        out: Dict[str, Any] = {"sl": None, "tp": None}
        if stop_loss is not None:
            sp = self._adj_price(sym, stop_loss, close_side.lower())
            # ★ [audit high#1] Idempotency key — prevents duplicate SL creation on retry timeout.
            out["sl"] = self._request("POST", "/fapi/v1/order", signed=True, is_order=True, params={
                "symbol": sym, "side": close_side, "type": "STOP_MARKET",
                "stopPrice": str(sp), "closePosition": "true", "workingType": "MARK_PRICE",
                "newClientOrderId": "xs-" + _uuid.uuid4().hex[:29]})
        if take_profit is not None:
            tp = self._adj_price(sym, take_profit, close_side.lower())
            out["tp"] = self._request("POST", "/fapi/v1/order", signed=True, is_order=True, params={
                "symbol": sym, "side": close_side, "type": "TAKE_PROFIT_MARKET",
                "stopPrice": str(tp), "closePosition": "true", "workingType": "MARK_PRICE",
                "newClientOrderId": "xt-" + _uuid.uuid4().hex[:29]})
        return out

    def _cancel_conditional_orders(self, sym: str):
        """Cancel this symbol's existing STOP_MARKET/TAKE_PROFIT_MARKET(closePosition) conditional orders (for set_trading_stop re-placement).
        Filters by type so entry limit orders etc. are left untouched. Failures are ignored (the next placement overrides)."""
        try:
            opens = self._request("GET", "/fapi/v1/openOrders", params={"symbol": sym}, signed=True)
            for o in (opens if isinstance(opens, list) else []):
                otype = str(o.get("type", "") or o.get("origType", ""))
                # ★ [audit low] Reclaim only the bot's SL/TP with closePosition=true — preserve
                #   user-placed conditional orders and partial-exit reduceOnly STOPs (implementation matches docstring intent).
                _is_close = str(o.get("closePosition", "")).lower() == "true"
                if otype in ("STOP_MARKET", "TAKE_PROFIT_MARKET") and _is_close:
                    try:
                        self._request("DELETE", "/fapi/v1/order", signed=True, is_order=True,
                                      params={"symbol": sym, "orderId": o.get("orderId")})
                    except Exception as exc:
                        logger.debug("[BinanceTrade] cancel cond order %s failed: %s", o.get("orderId"), exc)
        except Exception as exc:
            logger.debug("[BinanceTrade] list openOrders for %s failed: %s", sym, exc)

    def get_kline(self, symbol: str, interval: str = "240", limit: int = 50):
        """Fetch klines (public). Returns: [[openTime, o, h, l, c, volume, ...], ...] oldest first.

        interval accepts both Bybit/internal notation ("240","1","D") and Binance notation ("4h","1m","1d").
        """
        sym = self._normalize_symbol(symbol)
        iv = _INTERVAL_MAP.get(str(interval), str(interval))
        lim = min(int(limit), 1000)
        ttl = 0.0 if (iv == "1m" or lim <= 2) else _KLINE_CACHE_TTL
        ck = (sym, iv, lim)
        if ttl > 0.0:
            hit = self._kline_cache.get(ck)
            if hit and (time.time() - hit[0]) < ttl:
                return hit[1]
        path = self._path("/api/v3/klines", "/fapi/v1/klines")
        res = self._request("GET", path, params={"symbol": sym, "interval": iv, "limit": str(lim)})
        data = res if isinstance(res, list) else []  # Binance returns oldest first already
        if ttl > 0.0 and data:
            self._kline_cache[ck] = (time.time(), data)
        return data

    # ── Exchange abstraction (mirrors BybitTradeClient — FocusManager consumes via the client) ──
    #   Return keys match Bybit-native so FocusManager consumer code needs 0 changes.
    def _linear_last_price(self, symbol: str) -> float:
        """Bybit-name-compatible alias (FocusManager._get_current_price fallback etc. call by this name)."""
        return self._last_price(self._normalize_symbol(symbol))

    def get_market_tickers(self, *, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """24h ticker list — mapped to Bybit-native keys (symbol/lastPrice/turnover24h/price24hPcnt/highPrice/lowPrice)."""
        path = self._path("/api/v3/ticker/24hr", "/fapi/v1/ticker/24hr")
        params = {"symbol": self._normalize_symbol(symbol)} if symbol else {}
        res = self._request("GET", path, params=params)
        rows = res if isinstance(res, list) else [res]
        out: List[Dict[str, Any]] = []
        for t in rows:
            if not isinstance(t, dict) or not t.get("symbol"):
                continue
            out.append({
                "symbol": str(t.get("symbol", "")),
                "lastPrice": str(t.get("lastPrice", "0")),
                "turnover24h": str(t.get("quoteVolume", "0")),       # Bybit turnover24h = quote-denominated volume
                "price24hPcnt": str(float(t.get("priceChangePercent", 0) or 0) / 100.0),  # Bybit = fractional ratio
                "highPrice24h": str(t.get("highPrice", "0")),        # match Bybit key name (24h suffix)
                "lowPrice24h": str(t.get("lowPrice", "0")),
                "volume24h": str(t.get("volume", "0")),
            })
        return out

    def get_instrument_info(self, symbol: str) -> Dict[str, float]:
        """Symbol trading rules (qty_step/min_qty/max_qty) — via BinanceInstrumentCache."""
        sym = self._normalize_symbol(symbol)
        return {"qty_step": BinanceInstrumentCache.get_qty_step(sym, category=self._category),
                "min_qty": BinanceInstrumentCache.get_min_qty(sym, category=self._category),
                "max_qty": float((BinanceInstrumentCache.get(sym, category=self._category) or {}).get("max_qty", 0) or 0)}

    def get_available_margin(self) -> float:
        """USDT-M futures available balance (availableBalance). 0.0 on failure."""
        if self._category != "linear":
            return self.get_balance("USDT")
        try:
            res = self._request("GET", "/fapi/v2/account", signed=True)
            return float(res.get("availableBalance", 0) or 0)
        except Exception as exc:
            logger.debug("[BinanceTrade] get_available_margin failed: %s", exc)
            return 0.0

    def list_open_positions(self, *, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Held positions — normalized to Bybit-native keys (symbol/size/side(Buy|Sell)/avgPrice)."""
        rows = self.get_positions(symbol=symbol)  # positionRisk: positionAmt/entryPrice
        out: List[Dict[str, Any]] = []
        for r in rows:
            amt = float(r.get("positionAmt", 0) or 0)
            out.append({"symbol": str(r.get("symbol", "")),
                        "size": str(abs(amt)),
                        "side": "Buy" if amt > 0 else "Sell",
                        "avgPrice": str(r.get("entryPrice", 0) or 0),
                        # ★ [audit bug#7] Preserve leverage — FocusManager's post-entry LEVERAGE MISMATCH
                        #   check reads bp.get('leverage') (present in Bybit rows). If omitted, the check is permanently skipped.
                        "leverage": str(r.get("leverage", 0) or 0)})
        return out
