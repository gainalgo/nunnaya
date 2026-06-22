# -*- coding: utf-8 -*-
"""Bybit V5 private REST client. HMAC-SHA256 auth. TradeClient protocol."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Mapping, Optional

import requests
from requests.adapters import HTTPAdapter

from app.core.currency import Q
from app.core.bybit_trading import get_v5_order_category
from app.core.constants import (
    BYBIT_API_BASE, BYBIT_ORDER_CREATE, BYBIT_ORDER_CANCEL,
    BYBIT_ORDER_REALTIME, BYBIT_ACCOUNT_WALLET, BYBIT_MARKET_TICKERS,
)
from app.core.rate_limiter import bybit_rate_limiter, bybit_get
from app.integrations.bybit_instrument_cache import BybitInstrumentCache

logger = logging.getLogger(__name__)
_TRADE_RETRY_MAX = 4
_TRADE_RETRY_BASE_SEC = 1.5
_RECV_WINDOW = "5000"


class BybitAPIError(RuntimeError):
    def __init__(self, message, *, status_code=None, ret_code=None, response_text=None):
        super().__init__(message)
        self.status_code = status_code
        self.ret_code = ret_code
        self.response_text = response_text


def get_tick_size(symbol: str) -> float:
    return BybitInstrumentCache.get_tick_size(symbol)

def adjust_price_to_tick(price: float, side=None, *, symbol: str = "") -> float:
    if symbol:
        return BybitInstrumentCache.adjust_price(symbol, price, side or "")
    from decimal import ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP
    tick = Decimal("0.01")
    p = Decimal(str(price))
    s = str(side or "").lower()
    r = ROUND_FLOOR if s in ("buy","bid") else (ROUND_CEILING if s in ("sell","ask") else ROUND_HALF_UP)
    return float((p / tick).to_integral_value(rounding=r) * tick)

def adjust_qty_precision(qty: float, symbol: str = "") -> float:
    if symbol:
        return BybitInstrumentCache.adjust_qty(symbol, qty)
    return float(Decimal(str(qty)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))


# [2026-06-12] kline TTL 캐시 — dual(양방향) ON + threshold 완화로 raw get_kline
# 호출이 폭증해 공유 rate limiter 가 포화되고 GreenPen 스캔이 굶주림(524).
# 같은 (symbol,interval,limit) 봉을 짧은 TTL 내 공유해 API 호출을 줄인다.
# 30초 = 기존 _get_mtf_kline 기본 ttl 60초보다 더 신선 → 비-퇴행.
_KLINE_CACHE_TTL = 30.0


class BybitTradeClient:
    EXCHANGE_TYPE = "bybit"
    API_BASE = BYBIT_API_BASE

    def __init__(self, api_key=None, api_secret=None, *, timeout=10.0, category: Optional[str] = None):
        self.api_key = (api_key or os.getenv("BYBIT_API_KEY", "")).strip()
        self.api_secret = (api_secret or os.getenv("BYBIT_API_SECRET", "")).strip()
        self.timeout = float(timeout)
        c = (category or get_v5_order_category()).strip().lower()
        self._category = c if c in ("spot", "linear") else "spot"
        self._api_call_count = 0
        self._api_call_reset_ts = time.time()
        self._session = requests.Session()
        _adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        self._session.mount("https://", _adapter)
        # (symbol, interval, limit) -> (fetched_ts, data)  kline TTL 캐시
        self._kline_cache: dict = {}

    def _sign(self, timestamp, params_str):
        sign_str = f"{timestamp}{self.api_key}{_RECV_WINDOW}{params_str}"
        return hmac.new(self.api_secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()

    def _auth_headers(self, params_str):
        ts = str(int(time.time() * 1000))
        return {"X-BAPI-API-KEY": self.api_key, "X-BAPI-SIGN": self._sign(ts, params_str),
                "X-BAPI-SIGN-TYPE": "2", "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": _RECV_WINDOW, "Content-Type": "application/json"}

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
                "projected_calls_per_min": round(proj, 1), "limit_warning": proj >= 5760 if elapsed >= 5 else False}

    def _request(self, method, url, *, params=None, data=None, is_order=False):
        self._track_api_call()
        for attempt in range(_TRADE_RETRY_MAX):
            bybit_rate_limiter.acquire(is_order=is_order)
            try:
                if method.upper() == "GET":
                    from urllib.parse import urlencode
                    qs = urlencode(params) if params else ""
                    headers = self._auth_headers(qs)
                    resp = self._session.get(url, params=params, headers=headers, timeout=self.timeout)
                elif method.upper() == "POST":
                    import json as _json
                    body_str = _json.dumps(data) if data else ""
                    headers = self._auth_headers(body_str)
                    resp = self._session.post(url, json=data, headers=headers, timeout=self.timeout)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                try:
                    body = resp.json() if resp.text else {}
                except (ValueError, Exception):
                    logger.warning("[BybitTrade] Non-JSON response on attempt %d: %s", attempt + 1, resp.text[:200] if resp.text else "(empty)", exc_info=True)
                    if attempt < _TRADE_RETRY_MAX - 1:
                        time.sleep(_TRADE_RETRY_BASE_SEC * (2 ** attempt))
                        continue
                    raise BybitAPIError(f"Non-JSON response: {resp.text[:200]}", status_code=resp.status_code)

                ret_code = body.get("retCode", -1) if isinstance(body, dict) else -1
                if ret_code != 0:
                    ret_msg = body.get("retMsg", "Unknown error") if isinstance(body, dict) else str(body)[:200]
                    if ret_code == 10006 or resp.status_code == 429:
                        time.sleep(min(_TRADE_RETRY_BASE_SEC * (2 ** attempt), 10.0))
                        continue
                    raise BybitAPIError(f"Bybit API error: {ret_msg} (retCode={ret_code})",
                                       status_code=resp.status_code, ret_code=ret_code)
                return body.get("result", body)
            except requests.RequestException as e:
                is_last = attempt >= _TRADE_RETRY_MAX - 1
                if is_last:
                    logger.error("Bybit API request FAILED after %d attempts: %s", _TRADE_RETRY_MAX, e, exc_info=True)
                else:
                    logger.warning("Bybit API request failed (attempt %d/%d): %s — retrying", attempt + 1, _TRADE_RETRY_MAX, e)
                    time.sleep(_TRADE_RETRY_BASE_SEC * (2 ** attempt))
                    continue
                raise BybitAPIError(f"Request failed: {e}") from e
        raise BybitAPIError(f"Exhausted {_TRADE_RETRY_MAX} retries on {url}", status_code=429)

    def _normalize_symbol(self, symbol):
        return Q.normalize(symbol)

    @staticmethod
    def get_tick_size(symbol):
        return BybitInstrumentCache.get_tick_size(symbol)

    @staticmethod
    def adjust_price_to_tick(price, side=None, *, symbol=""):
        return adjust_price_to_tick(price, side, symbol=symbol)

    def accounts(self, *, skip_currencies=None, **kw):
        result = self._request("GET", BYBIT_ACCOUNT_WALLET, params={"accountType": "UNIFIED"})
        skip = set(skip_currencies or [])
        filtered = []
        for account in result.get("list", []):
            for coin in account.get("coin", []):
                cur = coin.get("coin", "")
                if cur in skip:
                    continue
                bal = float(coin.get("walletBalance", 0) or 0)
                locked = float(coin.get("locked", 0) or 0)
                if bal <= 0 and locked <= 0:
                    continue
                filtered.append({"currency": cur, "balance": str(bal), "locked": str(locked),
                                "avg_buy_price": "0", "unit_currency": "USDT"})
        return filtered

    def get_balance(self, currency, *, include_locked=False):
        cur = str(currency).upper()
        try:
            result = self._request("GET", BYBIT_ACCOUNT_WALLET, params={"accountType": "UNIFIED"})
            for acc in result.get("list", []):
                for coin in acc.get("coin", []):
                    if coin.get("coin") == cur:
                        bal = float(coin.get("walletBalance", 0) or 0)
                        if include_locked:
                            bal += float(coin.get("locked", 0) or 0)
                        return bal
            return 0.0
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error("Balance error %s: %s", currency, e)
            return 0.0

    def list_orders(self, *, state="wait", market=None, limit=50, **kw):
        params = {"category": self._category, "limit": min(limit, 50)}
        if market:
            params["symbol"] = self._normalize_symbol(market)
        url = f"{self.API_BASE}/v5/order/history" if state in ("done", "cancel") else BYBIT_ORDER_REALTIME
        result = self._request("GET", url, params=params)
        return [self._convert_order(o) for o in result.get("list", [])]

    def list_wait_orders(self, *, market=None, **kw):
        return self.list_orders(state="wait", market=market)

    def list_done_orders(self, *, market=None, **kw):
        return self.list_orders(state="done", market=market)

    def get_order(self, *, uuid, market=None):
        params = {"category": self._category, "orderId": uuid}
        if market:
            params["symbol"] = self._normalize_symbol(market)
        result = self._request("GET", BYBIT_ORDER_REALTIME, params=params)
        orders = result.get("list", [])
        if not orders:
            raise BybitAPIError(f"Order not found: {uuid}")
        return self._convert_order(orders[0])

    def place_order(self, *, market, side, ord_type, volume=None, price=None, **kw):
        symbol = self._normalize_symbol(market)
        s = str(side).lower()
        bybit_side = "Buy" if s in ("bid","buy","long") else "Sell"
        ot = str(ord_type).lower()
        bybit_type = "Market" if ot in ("market","price") else "Limit"
        data = {"category": self._category, "symbol": symbol, "side": bybit_side, "orderType": bybit_type}
        if volume is not None:
            data["qty"] = str(BybitInstrumentCache.adjust_qty(symbol, float(volume)))
        if price is not None and bybit_type == "Limit":
            data["price"] = str(BybitInstrumentCache.adjust_price(symbol, float(price), side=s))
        tif = kw.get("time_in_force", "")
        if tif:
            data["timeInForce"] = tif.upper()
        elif bybit_type == "Limit":
            data["timeInForce"] = "GTC"
        if (
            self._category == "spot"
            and bybit_type == "Market"
            and bybit_side == "Buy"
            and kw.get("_market_unit") == "quoteCoin"
        ):
            data["marketUnit"] = "quoteCoin"
        # Linear futures: positionIdx=0 for one-way mode
        if self._category == "linear":
            data["positionIdx"] = kw.get("positionIdx", 0)
            if kw.get("reduce_only") or kw.get("reduceOnly"):
                data["reduceOnly"] = True
        result = self._request("POST", BYBIT_ORDER_CREATE, data=data, is_order=True)
        oid = result.get("orderId", "")
        if oid:
            try:
                time.sleep(0.2)
                return self.get_order(uuid=oid, market=symbol)
            except BybitAPIError:
                logger.warning("get_order fallback for orderId=%s symbol=%s", oid, symbol, exc_info=True)
                return self._convert_order({"orderId": oid, "symbol": symbol, "side": bybit_side})
        return self._convert_order(result)

    def _linear_last_price(self, symbol: str) -> float:
        resp = bybit_get(
            BYBIT_MARKET_TICKERS,
            params={"category": self._category, "symbol": symbol},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        lst = resp.json().get("result", {}).get("list") or []
        if not lst:
            raise BybitAPIError(f"No ticker for {symbol} (category={self._category})")
        last = float(lst[0].get("lastPrice") or 0)
        if last <= 0:
            raise BybitAPIError(f"Invalid lastPrice for {symbol}")
        return last

    def market_buy(self, market, quote_amount, **kw):
        symbol = self._normalize_symbol(market)
        min_n = BybitInstrumentCache.get_min_notional(symbol)
        if quote_amount < min_n:
            raise BybitAPIError(f"Amount {quote_amount} < min {min_n} for {symbol}")
        if quote_amount <= 0 or quote_amount > 1_000_000:
            raise BybitAPIError(f"Amount sanity check failed: {quote_amount}")
        logger.info("[MARKET_BUY] %s quote=%.2f category=%s", symbol, quote_amount, self._category)
        if self._category == "linear":
            last = self._linear_last_price(symbol)
            qty_raw = float(quote_amount) / last
            qty = BybitInstrumentCache.adjust_qty(symbol, qty_raw)
            min_q = BybitInstrumentCache.get_min_qty(symbol)
            if qty < min_q:
                raise BybitAPIError(f"Linear qty {qty} < min {min_q} for {symbol}")
            return self.place_order(market=market, side="Buy", ord_type="Market", volume=qty)
        return self.place_order(
            market=market, side="Buy", ord_type="Market", volume=quote_amount, _market_unit="quoteCoin"
        )

    def market_sell(self, market, qty, **kw):
        symbol = self._normalize_symbol(market)
        qty = BybitInstrumentCache.adjust_qty(symbol, qty)
        min_q = BybitInstrumentCache.get_min_qty(symbol)
        if qty < min_q:
            raise BybitAPIError(f"Qty {qty} < min {min_q} for {symbol}")
        # [2026-05-30] Linear safety: spot 가정 plugin(LADDER 등) 의 market_sell = LONG 청산 의도.
        # reduce_only 미적용 시 SHORT 신규 진입 사고. 명시적 SHORT 신규 진입은
        # place_order(side="Sell", reduce_only=False) 직접 호출 사용.
        if self._category == "linear":
            return self.place_order(market=market, side="Sell", ord_type="Market", volume=qty, reduce_only=True)
        return self.place_order(market=market, side="Sell", ord_type="Market", volume=qty)

    def market_sell_usdt(self, market, quote_amount, **kw):
        """USDT notional → base qty (linear only). Spot: use market_sell with base qty."""
        symbol = self._normalize_symbol(market)
        min_n = BybitInstrumentCache.get_min_notional(symbol)
        if quote_amount < min_n:
            raise BybitAPIError(f"Amount {quote_amount} < min {min_n} for {symbol}")
        if quote_amount <= 0 or quote_amount > 1_000_000:
            raise BybitAPIError(f"Amount sanity check failed: {quote_amount}")
        if self._category != "linear":
            raise BybitAPIError("market_sell_usdt is supported for BYBIT_V5_CATEGORY=linear only")
        last = self._linear_last_price(symbol)
        qty_raw = float(quote_amount) / last
        qty = BybitInstrumentCache.adjust_qty(symbol, qty_raw)
        min_q = BybitInstrumentCache.get_min_qty(symbol)
        if qty < min_q:
            raise BybitAPIError(f"Linear qty {qty} < min {min_q} for {symbol}")
        logger.info("[MARKET_SELL_USDT] %s quote=%.2f category=%s", symbol, quote_amount, self._category)
        # [2026-05-30] Linear safety: market_sell_usdt = 청산 의도 (linear only). reduce_only 강제.
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
        # 1단계: 주문 전송 (실패 시 예외 전파 — 주문이 안 나갔으므로 안전)
        order = self.place_order(market=market, side="Sell", ord_type="Limit",
                                price=price, volume=qty, time_in_force="IOC")
        result["order"] = order
        # 2단계: 체결 확인 (주문은 이미 나갔으므로 에러 시에도 주문 UUID 보존)
        try:
            time.sleep(0.3)
            order_uuid = order.get("uuid", "")
            status_ok = False
            # 최대 2회 재시도로 stale 데이터 방지
            for _attempt in range(2):
                try:
                    order = self.get_order(uuid=order_uuid)
                    result["order"] = order
                    status_ok = True
                    break
                except BybitAPIError as api_err:
                    logger.warning("[QUICK_SELL] get_order attempt %d failed for %s: %s",
                                   _attempt + 1, market, api_err)
                    if _attempt == 0:
                        time.sleep(0.5)
            if not status_ok:
                # get_order 완전 실패 — stale 데이터 사용 대신 unknown 반환
                logger.error("[QUICK_SELL] get_order FAILED for %s uuid=%s — returning unknown status",
                             market, order_uuid)
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
            logger.error("[QUICK_SELL] Order PLACED but status check failed %s: %s (order=%s)",
                        market, e, order, exc_info=True)
            result.update(success=True, action="unknown",
                         message=f"Order placed (uuid={order.get('uuid','')}), status check failed: {e}")
        return result

    def cancel_order(self, *, uuid, market=None):
        data = {"category": self._category, "orderId": uuid}
        if market:
            data["symbol"] = self._normalize_symbol(market)
        return self._convert_order(self._request("POST", BYBIT_ORDER_CANCEL, data=data, is_order=True))

    def wait_order(self, *, uuid, market=None, timeout_sec=30.0, poll_interval=1.0):
        end_ts = time.time() + float(timeout_sec)
        last = {}
        consecutive_failures = 0
        while time.time() < end_ts:
            try:
                last = self.get_order(uuid=uuid, market=market)
                consecutive_failures = 0
                if str(last.get("state", "")).lower() in ("done", "cancel"):
                    return last
            except BybitAPIError as api_err:
                consecutive_failures += 1
                logger.warning("[WAIT_ORDER] get_order failed (attempt %d) uuid=%s: %s",
                               consecutive_failures, uuid, api_err)
                if consecutive_failures >= 3:
                    logger.error("[WAIT_ORDER] 3 consecutive failures for uuid=%s — aborting poll", uuid)
                    last["_poll_error"] = str(api_err)
                    last["_poll_failed"] = True
                    return last
            time.sleep(float(poll_interval))
        if not last:
            logger.warning("[WAIT_ORDER] timeout with NO successful poll for uuid=%s", uuid)
        return last

    def _convert_order(self, order):
        raw_side = str(order.get("side", "")).lower()
        # NOTE: "bid"/"ask" are Upbit-era names kept for internal API compat (22 call sites).
        # Bybit native: "Buy"/"Sell". Consider migrating if doing a major refactor.
        side = "bid" if raw_side == "buy" else ("ask" if raw_side == "sell" else raw_side)
        raw_st = str(order.get("orderStatus", order.get("state", "")))
        sm = {"New":"wait","PartiallyFilled":"wait","Untriggered":"wait","Filled":"done",
              "Cancelled":"cancel","PartiallyFilledCanceled":"cancel","Rejected":"cancel","Deactivated":"cancel"}
        state = sm.get(raw_st, raw_st.lower() if raw_st else "")
        rt = str(order.get("orderType", "")).lower()
        ord_type = "market" if rt == "market" else ("limit" if rt == "limit" else rt)
        return {"uuid": str(order.get("orderId", order.get("uuid", ""))),
                "side": side, "ord_type": ord_type,
                "price": str(order.get("price") or order.get("avgPrice") or "0"),
                "state": state,
                "market": str(order.get("symbol", order.get("market", ""))),
                "volume": str(order.get("qty") or order.get("volume") or "0"),
                "remaining_volume": str(order.get("leavesQty") or order.get("remaining_volume") or "0"),
                "executed_volume": str(order.get("cumExecQty") or order.get("executed_volume") or "0"),
                "avg_price": str(order.get("avgPrice") or order.get("price") or "0"),
                "trades_count": int(order.get("trades_count", 0)),
                "created_at": str(order.get("createdTime", order.get("created_at", ""))),
                "paid_fee": str(order.get("cumExecFee", order.get("paid_fee", 0)) or 0),
                "fee_currency": "USDT", "_raw": order}

    def summarize_order(self, o):
        try:
            return f"uuid={o.get('uuid','')} {o.get('market','')} {o.get('side','')} state={o.get('state','')} price={o.get('price','')} vol={o.get('volume','')}"
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[BybitTrade] summarize_order failed, falling back to str()", exc_info=True)
            return str(o)

    def get_min_order_amount(self, symbol):
        sym = self._normalize_symbol(symbol)
        return {"min_amount": BybitInstrumentCache.get_min_qty(sym),
                "min_cost": BybitInstrumentCache.get_min_notional(sym), "min_price": 0.0}

    def get_order_chance(self, market):
        return {"balance": self.accounts()}

    # ── Futures / Position Management ───────────────────────

    def set_leverage(self, symbol: str, buy_leverage: int, sell_leverage: Optional[int] = None):
        """Set leverage for a linear perpetual symbol."""
        from app.core.constants import BYBIT_POSITION_SET_LEVERAGE
        data = {
            "category": "linear",
            "symbol": symbol.upper(),
            "buyLeverage": str(int(buy_leverage)),
            "sellLeverage": str(int(sell_leverage or buy_leverage)),
        }
        resp = self._request("POST", BYBIT_POSITION_SET_LEVERAGE, data=data)
        return resp

    def get_positions(self, symbol: Optional[str] = None, settle_coin: str = "USDT"):
        """Get open positions for linear perpetual."""
        from app.core.constants import BYBIT_POSITION_LIST
        params = {"category": "linear", "settleCoin": settle_coin}
        if symbol:
            params["symbol"] = symbol.upper()
        resp = self._request("GET", BYBIT_POSITION_LIST, params=params)
        return resp.get("list", []) if isinstance(resp, dict) else []

    def switch_position_mode(self, symbol: str, mode: str = "MergedSingle"):
        """Switch position mode: MergedSingle (one-way) or BothSide (hedge).

        MergedSingle = one-way mode (simpler, recommended for FOCUS).
        BothSide = hedge mode (can hold long+short simultaneously).
        """
        from app.core.constants import BYBIT_POSITION_SWITCH_MODE
        data = {
            "category": "linear",
            "symbol": symbol.upper(),
            "mode": 0 if mode == "MergedSingle" else 3,
        }
        resp = self._request("POST", BYBIT_POSITION_SWITCH_MODE, data=data)
        return resp

    def set_trading_stop(self, symbol: str, *, take_profit: Optional[float] = None,
                         stop_loss: Optional[float] = None, position_idx: int = 0):
        """Set server-side TP/SL for an open position (safety net)."""
        from app.core.constants import BYBIT_POSITION_TRADING_STOP
        data = {
            "category": "linear",
            "symbol": symbol.upper(),
            "positionIdx": position_idx,
        }
        if take_profit is not None:
            data["takeProfit"] = str(round(float(take_profit), 4))
        if stop_loss is not None:
            data["stopLoss"] = str(round(float(stop_loss), 4))
        try:
            resp = self._request("POST", BYBIT_POSITION_TRADING_STOP, data=data)
            return resp
        except BybitAPIError as exc:
            # 34040 = "not modified" — TP/SL already set to identical values → treat as success
            if exc.ret_code == 34040:
                logger.debug("[BybitTrade] set_trading_stop %s: already set (34040 not modified)", symbol)
                return {}
            raise

    def get_kline(self, symbol: str, interval: str = "240", limit: int = 50):
        """Fetch kline (candlestick) data from Bybit V5.

        Args:
            symbol: e.g. "BTCUSDT"
            interval: "1","3","5","15","30","60","120","240","360","720","D","W","M"
            limit: max 1000, default 50
        Returns:
            List of [startTime, open, high, low, close, volume, turnover]
        """
        from app.core.constants import BYBIT_MARKET_KLINE
        sym = symbol.upper()
        iv = str(interval)
        lim = min(int(limit), 1000)
        # ── TTL 캐시 ──────────────────────────────────────────────
        # ★ 1분봉(micro 점화 타이밍) + limit<=2(라이브 가격/형성중 봉 스냅샷)은
        #   캐시 제외 → 항상 신선. 그 외는 _KLINE_CACHE_TTL 동안 봉 공유.
        ttl = 0.0 if (iv == "1" or lim <= 2) else _KLINE_CACHE_TTL
        ck = (sym, iv, lim)
        if ttl > 0.0:
            hit = self._kline_cache.get(ck)
            if hit and (time.time() - hit[0]) < ttl:
                return hit[1]
        params = {
            "category": self._category,
            "symbol": sym,
            "interval": iv,
            "limit": str(lim),
        }
        resp = self._request("GET", BYBIT_MARKET_KLINE, params=params)
        raw = resp.get("list", []) if isinstance(resp, dict) else []
        # Bybit returns newest first → reverse to oldest first
        data = list(reversed(raw))
        if ttl > 0.0 and data:
            self._kline_cache[ck] = (time.time(), data)
        return data
