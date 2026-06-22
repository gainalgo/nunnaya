# ============================================================
# Upbit Trade Client — 현물(Spot) / Long-only
# ------------------------------------------------------------
# Bybit `BybitTradeClient` 인터페이스를 미러한 Upbit REST 클라이언트.
# FOCUS 엔진이 거래소를 모르고 같은 메서드(market_buy/market_sell/
# get_kline/accounts...)로 대화할 수 있게 한다.
#
# 의존성 0 원칙(오픈소스 배포·run.ps1 비침습 철학):
#   - JWT(HS256)는 표준 라이브러리(hmac/hashlib/base64)로 직접 구현.
#     PyJWT 등 외부 패키지 설치 강요하지 않음.
#   - HTTP는 requests(이미 프로젝트 의존성)만 사용.
#
# 거래소 차이(가이드 §10 / DESIGN §4):
#   - 방향: 매수(bid)/매도(ask)만. SHORT 없음(현물 long_only).
#   - 시장가 매수 = 금액(KRW) 기준 (ord_type="price")
#   - 시장가 매도 = 수량 기준     (ord_type="market")
#   - 심볼: "BTCUSDT"/"BTC" → "KRW-BTC"
#   - 캔들: Upbit는 최신 먼저 → ★ reversed()로 oldest-first 반환
#           (Bybit get_kline 과 동일 포맷 = selector 재사용 가능)
#   - 최소주문: 5000 KRW
# ============================================================
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid as _uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

UPBIT_API_BASE = "https://api.upbit.com"

# Upbit 분봉 지원 단위 (그 외는 가장 가까운 것으로 fallback)
_UPBIT_MIN_UNITS = (1, 3, 5, 10, 15, 30, 60, 240)

MIN_ORDER_KRW = 5000.0

_RETRY_MAX = 3
_RETRY_BACKOFF = 0.5


class UpbitAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0, ret_code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.ret_code = ret_code


def to_upbit_market(symbol: str) -> str:
    """내부 심볼 → Upbit market 코드. "BTCUSDT"/"BTC"/"KRW-BTC" → "KRW-BTC"."""
    s = str(symbol).upper().replace("/", "").strip()
    if s.startswith("KRW-"):
        return s
    if "-" in s:  # 이미 "QUOTE-BASE" 형태(다른 quote)면 그대로
        return s
    for q in ("USDT", "USDC", "BUSD", "KRW", "BTC"):
        if s.endswith(q) and len(s) > len(q):
            return f"KRW-{s[:-len(q)]}"
    return f"KRW-{s}"


def base_currency(symbol: str) -> str:
    """Upbit market("KRW-BTC") 또는 내부 심볼에서 base("BTC") 추출."""
    mk = to_upbit_market(symbol)
    return mk.split("-", 1)[1] if "-" in mk else mk


def krw_tick_size(price: float) -> float:
    """Upbit KRW 마켓 호가단위 (공식표 docs/krw-market-info). 지정가 주문 가격은 이 단위 배수여야 함."""
    p = float(price)
    if p >= 1_000_000:
        return 1000.0
    if p >= 500_000:
        return 500.0
    if p >= 100_000:
        return 100.0
    if p >= 50_000:
        return 50.0
    if p >= 10_000:
        return 10.0
    if p >= 5_000:
        return 5.0
    if p >= 1_000:
        return 1.0
    if p >= 100:
        return 1.0
    if p >= 10:
        return 0.1
    if p >= 1:
        return 0.01
    if p >= 0.1:
        return 0.001
    if p >= 0.01:
        return 0.0001
    if p >= 0.001:
        return 0.00001
    if p >= 0.0001:
        return 0.000001
    if p >= 0.00001:
        return 0.0000001
    return 0.00000001


def adjust_price_to_tick_krw(price: float, side: str = "ask") -> float:
    """지정가를 Upbit 호가단위에 맞춤 (안 맞으면 주문 거부됨).
    매도(ask)=내림(체결 유리), 매수(bid)=올림."""
    import math
    p = float(price)
    tick = krw_tick_size(p)
    if tick <= 0:
        return p
    if str(side).lower() in ("ask", "sell"):
        adj = math.floor(round(p / tick, 9)) * tick
    else:
        adj = math.ceil(round(p / tick, 9)) * tick
    return round(adj, 8)


def _kline_endpoint(interval: str):
    """Bybit interval 문자열 → Upbit candle (kind, unit)."""
    iv = str(interval).upper()
    if iv in ("D", "1D", "DAY", "DAYS"):
        return "days", None
    if iv in ("W", "WEEK", "WEEKS"):
        return "weeks", None
    if iv in ("M", "MONTH", "MONTHS"):
        return "months", None
    try:
        n = int(iv)
    except (TypeError, ValueError):
        n = 240
    if n in _UPBIT_MIN_UNITS:
        return "minutes", n
    nearest = min(_UPBIT_MIN_UNITS, key=lambda u: abs(u - n))
    logger.debug("[UpbitTrade] interval %s 미지원 → 가까운 %dm 사용", interval, nearest)
    return "minutes", nearest


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class UpbitTradeClient:
    """Upbit 현물 거래 클라이언트. BybitTradeClient 인터페이스 미러."""

    API_BASE = UPBIT_API_BASE
    MIN_ORDER_KRW = MIN_ORDER_KRW

    def __init__(self, access_key: str = "", secret_key: str = "", *, timeout: float = 10.0):
        self.access_key = access_key or ""
        self.secret_key = secret_key or ""
        self.timeout = timeout
        self._session = requests.Session()
        self._kline_cache: Dict[tuple, tuple] = {}
        self._warn_cache: tuple = (0.0, {})   # (ts, {market: {warning,caution,kinds}}) — 경고 TTL 캐시

    # ── 인증 ────────────────────────────────────────────────
    def _make_jwt(self, query: Optional[Dict[str, Any]] = None) -> str:
        if not self.access_key or not self.secret_key:
            raise UpbitAPIError("Upbit API key/secret not configured")
        payload: Dict[str, Any] = {
            "access_key": self.access_key,
            "nonce": str(_uuid.uuid4()),
        }
        if query:
            query_string = urlencode(query)
            payload["query_hash"] = hashlib.sha512(query_string.encode("utf-8")).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        header = {"alg": "HS256", "typ": "JWT"}
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
            + "."
            + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        )
        sig = hmac.new(
            self.secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        return signing_input + "." + _b64url(sig)

    # ── HTTP ────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, query: Optional[Dict[str, Any]] = None,
                 auth: bool = False) -> Any:
        url = self.API_BASE + path
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self._make_jwt(query)
        last_exc: Optional[Exception] = None
        for attempt in range(_RETRY_MAX):
            try:
                if method == "GET":
                    resp = self._session.get(url, params=query, headers=headers, timeout=self.timeout)
                elif method == "POST":
                    # ★ Upbit=form(data=) / Bithumb 2.0=JSON 본문(json=). query_hash 는 둘 다 urlencode 기준.
                    #   Bithumb 은 form 본문이면 서버가 파싱 실패→query_hash 불일치→401 invalid_query_payload.
                    if getattr(self, "_post_as_json", False):
                        resp = self._session.post(url, json=query, headers=headers, timeout=self.timeout)
                    else:
                        resp = self._session.post(url, data=query, headers=headers, timeout=self.timeout)
                elif method == "DELETE":
                    resp = self._session.delete(url, params=query, headers=headers, timeout=self.timeout)
                else:
                    raise UpbitAPIError(f"Unsupported method: {method}")
                if resp.status_code == 429:  # rate limit → backoff & retry
                    time.sleep(_RETRY_BACKOFF * (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    msg = resp.text
                    err_name = ""
                    try:
                        err_name = resp.json().get("error", {}).get("name", "")
                    except (ValueError, AttributeError):
                        pass
                    raise UpbitAPIError(
                        f"Upbit {method} {path} -> {resp.status_code}: {msg}",
                        status_code=resp.status_code, ret_code=err_name,
                    )
                return resp.json()
            except requests.RequestException as e:
                last_exc = e
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
        raise UpbitAPIError(f"Exhausted {_RETRY_MAX} retries on {path}: {last_exc}")

    def _normalize_symbol(self, symbol: str) -> str:
        return to_upbit_market(symbol)

    # ── 시장 데이터 (인증 불필요) ────────────────────────────
    def get_all_markets(self) -> List[Dict[str, Any]]:
        """KRW 마켓 전체 목록. isDetails=true → market_event(warning/caution) 포함."""
        data = self._request("GET", "/v1/market/all", query={"isDetails": "true"})
        return [m for m in data if str(m.get("market", "")).startswith("KRW-")]

    # 주의환기(caution) 종류 한글 라벨
    _CAUTION_LABELS = {
        "PRICE_FLUCTUATIONS": "가격급등락",
        "TRADING_VOLUME_SOARING": "거래량급증",
        "DEPOSIT_AMOUNT_SOARING": "입금량급증",
        "GLOBAL_PRICE_DIFFERENCES": "가격차이",
        "CONCENTRATION_OF_SMALL_ACCOUNTS": "소수계좌집중",
    }

    @staticmethod
    def _parse_market_flags(m: Dict[str, Any]) -> Dict[str, Any]:
        """1개 마켓 dict → {warning, caution, kinds}. market_event(신) + market_warning(구) 둘 다 방어.
        warning=투자유의(상폐위험) / caution=주의환기. legacy CAUTION 은 보수적으로 warning 취급."""
        ev = m.get("market_event") or {}
        warning = bool(ev.get("warning"))
        caution_obj = ev.get("caution") or {}
        kinds = [UpbitTradeClient._CAUTION_LABELS.get(k, k)
                 for k, v in caution_obj.items() if v]
        caution = bool(kinds)
        # 구 필드 fallback: market_warning == "CAUTION" → 투자유의(보수적 차단)
        if str(m.get("market_warning", "")).upper() == "CAUTION":
            warning = True
        return {"warning": warning, "caution": caution, "kinds": kinds}

    def get_market_warnings(self, *, ttl: float = 300.0) -> Dict[str, Dict[str, Any]]:
        """{market: {warning, caution, kinds}} — 거래소 투자유의/주의환기 플래그. TTL 캐시(기본 5분).
        실패 시 빈 dict(경고정보 없음=차단 안 함, 안전측 fail-open: 거래 자체는 막지 않음)."""
        ts0, cached = self._warn_cache
        if cached and (time.time() - ts0) < ttl:
            return cached
        try:
            markets = self.get_all_markets()
            out = {str(m.get("market", "")): self._parse_market_flags(m) for m in markets}
            self._warn_cache = (time.time(), out)
            return out
        except Exception as e:
            logger.warning("[UpbitTrade] get_market_warnings failed: %s", e)
            return dict(cached) if cached else {}

    def get_tickers(self, markets: List[str]) -> List[Dict[str, Any]]:
        """현재가 스냅샷. markets: ["KRW-BTC", ...] 또는 내부 심볼."""
        codes = ",".join(self._normalize_symbol(m) for m in markets)
        return self._request("GET", "/v1/ticker", query={"markets": codes})

    def get_price(self, market: str) -> float:
        try:
            t = self.get_tickers([market])
            return float(t[0].get("trade_price", 0) or 0) if t else 0.0
        except (UpbitAPIError, IndexError, KeyError, TypeError, ValueError) as e:
            logger.warning("[UpbitTrade] get_price %s failed: %s", market, e)
            return 0.0

    def get_orderbook(self, market: str, *, depth: int = 15) -> Dict[str, Any]:
        """공개 호가창 (인증 불필요). bids(매수)/asks(매도) price+size 정규화.
        Upbit /v1/orderbook 은 units 가 [{ask_price,bid_price,ask_size,bid_size}, ...]."""
        mk = self._normalize_symbol(market)
        data = self._request("GET", "/v1/orderbook", query={"markets": mk})
        ob = data[0] if isinstance(data, list) and data else (data or {})
        units = ob.get("orderbook_units", []) or []
        bids, asks = [], []
        for u in units[:max(depth, 1)]:
            try:
                bids.append({"price": float(u.get("bid_price", 0) or 0), "size": float(u.get("bid_size", 0) or 0)})
                asks.append({"price": float(u.get("ask_price", 0) or 0), "size": float(u.get("ask_size", 0) or 0)})
            except (TypeError, ValueError):
                continue
        return {
            "market": mk,
            "bids": bids,                 # 매수 (가격 내림차순: 최우선 매수가가 [0])
            "asks": asks,                 # 매도 (가격 오름차순: 최우선 매도가가 [0])
            "ts": ob.get("timestamp", 0),
        }

    def get_kline(self, symbol: str, interval: str = "240", limit: int = 50) -> List[list]:
        """캔들 조회. ★ Bybit get_kline 과 동일 포맷으로 반환:
        [[ts_ms, open, high, low, close, volume, turnover], ...] (oldest first)

        Upbit candle API는 최신 먼저 오므로 reversed() 로 뒤집는다(가이드 §10.3).
        """
        mk = self._normalize_symbol(symbol)
        kind, unit = _kline_endpoint(interval)
        lim = min(int(limit), 200)
        ck = (mk, kind, unit, lim)
        hit = self._kline_cache.get(ck)
        if hit and (time.time() - hit[0]) < 20.0:
            return hit[1]
        path = f"/v1/candles/{kind}" + (f"/{unit}" if unit else "")
        raw = self._request("GET", path, query={"market": mk, "count": lim})
        out: List[list] = []
        for c in reversed(raw):  # ★ 최신 먼저 → oldest first
            try:
                out.append([
                    int(c.get("timestamp", 0)),
                    float(c["opening_price"]),
                    float(c["high_price"]),
                    float(c["low_price"]),
                    float(c["trade_price"]),
                    float(c.get("candle_acc_trade_volume", 0) or 0),
                    float(c.get("candle_acc_trade_price", 0) or 0),
                ])
            except (KeyError, TypeError, ValueError):
                continue
        if out:
            self._kline_cache[ck] = (time.time(), out)
        return out

    # ── 잔고 (인증) ─────────────────────────────────────────
    def accounts(self, *, skip_currencies: Optional[List[str]] = None, **kw) -> List[Dict[str, Any]]:
        data = self._request("GET", "/v1/accounts", auth=True)
        skip = set(skip_currencies or [])
        out = []
        for b in data:
            cur = b.get("currency", "")
            if cur in skip:
                continue
            bal = float(b.get("balance", 0) or 0)
            locked = float(b.get("locked", 0) or 0)
            if bal <= 0 and locked <= 0:
                continue
            out.append({
                "currency": cur, "balance": str(bal), "locked": str(locked),
                "avg_buy_price": str(b.get("avg_buy_price", "0")),
                "unit_currency": b.get("unit_currency", "KRW"),
            })
        return out

    def get_balance(self, currency: str, *, include_locked: bool = False) -> float:
        cur = str(currency).upper()
        try:
            for b in self._request("GET", "/v1/accounts", auth=True):
                if str(b.get("currency", "")).upper() == cur:
                    bal = float(b.get("balance", 0) or 0)
                    if include_locked:
                        bal += float(b.get("locked", 0) or 0)
                    return bal
            return 0.0
        except UpbitAPIError as e:
            logger.error("[UpbitTrade] get_balance %s error: %s", currency, e)
            return 0.0

    # ── 주문 (인증) ─────────────────────────────────────────
    def place_order(self, *, market: str, side: str, ord_type: str,
                    volume: Optional[float] = None, price: Optional[float] = None, **kw) -> Dict[str, Any]:
        mk = self._normalize_symbol(market)
        s = str(side).lower()
        upbit_side = "bid" if s in ("bid", "buy", "long") else "ask"
        query: Dict[str, Any] = {"market": mk, "side": upbit_side, "ord_type": str(ord_type)}
        if volume is not None:
            query["volume"] = str(volume)
        if price is not None:
            query["price"] = str(price)
        return self._convert_order(self._request("POST", "/v1/orders", query=query, auth=True))

    def market_buy(self, market: str, quote_amount: float, **kw) -> Dict[str, Any]:
        """시장가 매수 — KRW 금액 기준 (ord_type='price')."""
        amt = float(quote_amount)
        if amt < self.MIN_ORDER_KRW:
            raise UpbitAPIError(f"Amount {amt} < min {self.MIN_ORDER_KRW} KRW for {market}")
        if amt <= 0 or amt > 1_000_000_000:
            raise UpbitAPIError(f"Amount sanity check failed: {amt}")
        mk = self._normalize_symbol(market)
        logger.info("[UPBIT_BUY] %s krw=%.0f", mk, amt)
        query = {"market": mk, "side": "bid", "ord_type": "price", "price": str(int(amt))}
        return self._convert_order(self._request("POST", "/v1/orders", query=query, auth=True))

    def market_sell(self, market: str, qty: float, **kw) -> Dict[str, Any]:
        """시장가 매도 — 수량 기준 (ord_type='market')."""
        q = float(qty)
        if q <= 0:
            raise UpbitAPIError(f"Sell qty must be > 0: {q}")
        mk = self._normalize_symbol(market)
        logger.info("[UPBIT_SELL] %s qty=%s", mk, q)
        query = {"market": mk, "side": "ask", "ord_type": "market", "volume": str(q)}
        return self._convert_order(self._request("POST", "/v1/orders", query=query, auth=True))

    # 별칭 (Bybit 호환)
    def market_buy_usdt(self, market, amount, **kw):
        return self.market_buy(market, amount, **kw)

    def market_sell_qty(self, market, qty, **kw):
        return self.market_sell(market, qty, **kw)

    def limit_buy(self, market: str, price: float, volume: float):
        adj = adjust_price_to_tick_krw(float(price), side="bid")
        return self.place_order(market=market, side="bid", ord_type="limit", price=adj, volume=volume)

    def limit_sell(self, market: str, price: float, volume: float):
        adj = adjust_price_to_tick_krw(float(price), side="ask")
        return self.place_order(market=market, side="ask", ord_type="limit", price=adj, volume=volume)

    def get_order(self, *, uuid: str, market: Optional[str] = None) -> Dict[str, Any]:
        return self._convert_order(self._request("GET", "/v1/order", query={"uuid": uuid}, auth=True))

    def cancel_order(self, *, uuid: str, market: Optional[str] = None) -> Dict[str, Any]:
        return self._convert_order(self._request("DELETE", "/v1/order", query={"uuid": uuid}, auth=True))

    def open_orders(self, market: str, *, side: Optional[str] = None) -> List[Dict[str, Any]]:
        """미체결 주문 목록 (GET /v1/orders/open). side='ask'/'bid' 필터 옵션."""
        mk = self._normalize_symbol(market)
        raw = self._request("GET", "/v1/orders/open", query={"market": mk}, auth=True)
        out = []
        for o in (raw or []):
            co = self._convert_order(o)
            if side and str(co.get("side", "")).lower() != side.lower():
                continue
            out.append(co)
        return out

    def wait_order(self, *, uuid: str, market: Optional[str] = None,
                   timeout_sec: float = 30.0, poll_interval: float = 1.0) -> Dict[str, Any]:
        end_ts = time.time() + float(timeout_sec)
        last: Dict[str, Any] = {}
        fails = 0
        while time.time() < end_ts:
            try:
                last = self.get_order(uuid=uuid, market=market)
                fails = 0
                if str(last.get("state", "")).lower() in ("done", "cancel"):
                    return last
            except UpbitAPIError as e:
                fails += 1
                logger.warning("[UpbitTrade] wait_order fail %d uuid=%s: %s", fails, uuid, e)
                if fails >= 3:
                    last["_poll_failed"] = True
                    return last
            time.sleep(float(poll_interval))
        return last

    def _convert_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Upbit 주문 응답 → 내부 공통 dict. Upbit가 bid/ask·wait/done/cancel 원조라 거의 1:1."""
        if not isinstance(order, dict):
            return {}
        # ★ 실제 체결 평단 — Upbit 시장가는 avg_price 필드가 없어 trades 에서 가중평균 계산.
        #   (체결가가 정확해야 entry_price·TP·SL 이 실제 진입가 기준으로 맞음)
        avg_price = float(order.get("avg_price") or 0)
        if avg_price <= 0:
            tot_v = tot_f = 0.0
            for t in (order.get("trades") or []):
                try:
                    v = float(t.get("volume", 0) or 0)
                    f = float(t.get("funds", 0) or 0) or float(t.get("price", 0) or 0) * v
                    tot_v += v
                    tot_f += f
                except (TypeError, ValueError):
                    continue
            if tot_v > 0 and tot_f > 0:
                avg_price = tot_f / tot_v
        if avg_price <= 0:
            avg_price = float(order.get("price") or 0)
        return {
            "uuid": str(order.get("uuid", "")),
            "side": str(order.get("side", "")),  # bid / ask
            "ord_type": str(order.get("ord_type", "")),
            "price": str(order.get("price") or avg_price or "0"),
            "state": str(order.get("state", "")),  # wait / done / cancel
            "market": str(order.get("market", "")),
            "volume": str(order.get("volume") or "0"),
            "remaining_volume": str(order.get("remaining_volume") or "0"),
            "executed_volume": str(order.get("executed_volume") or "0"),
            "avg_price": str(avg_price),
            "trades_count": int(order.get("trades_count", 0) or 0),
            "created_at": str(order.get("created_at", "")),
            "paid_fee": str(order.get("paid_fee", 0) or 0),
            "fee_currency": "KRW",
            "_raw": order,
        }

    def get_min_order_amount(self, symbol: str) -> Dict[str, float]:
        return {"min_amount": 0.0, "min_cost": self.MIN_ORDER_KRW, "min_price": 0.0}

    def summarize_order(self, o: Dict[str, Any]) -> str:
        try:
            return (f"uuid={o.get('uuid','')} {o.get('market','')} {o.get('side','')} "
                    f"state={o.get('state','')} price={o.get('price','')} vol={o.get('volume','')}")
        except (KeyError, AttributeError, TypeError):
            return str(o)
