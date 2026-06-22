# ============================================================
# File: app/manager/order_state_machine.py
# Autocoin OS v3-H — Order State Machine (LIVE safety layer)
# ------------------------------------------------------------
# 목표:
# - 부분체결/지연/네트워크 오류에서도 "중복 주문"을 막고
#   "pending" 상태를 일관되게 유지/복구할 수 있게 한다.
# - 타임아웃 시: cancel → 잔량 재시도 → 실패시 RECOVERY/EMERGENCY
# - 슬리피지(체결가 이탈) 탐지 시:
#     * soft: 경고/원장 기록
#     * hard: (기본) exit는 전역 EMERGENCY, entry는 시장 단위 COOLDOWN
# ============================================================

from __future__ import annotations

import logging
import os
import time
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, Union, TYPE_CHECKING

from app.manager.trade_ledger import TradeLedger
from app.core.hyper_price_store import orderbook_store

logger = logging.getLogger(__name__)

# Exchange abstraction - accept any TradeClient implementation
if TYPE_CHECKING:
    from app.integrations.exchange.base import TradeClient

# Import concrete implementations for backwards compatibility
from app.integrations.bybit_trade import BybitTradeClient as BybitTradeClient, adjust_price_to_tick as _adjust_price_to_tick

# Generic trade client error
class TradeClientError(RuntimeError):
    """Generic trade client error for exchange-agnostic handling."""
    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        response_text: Optional[str] = None,
        original_error: Optional[Exception] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text
        self.original_error = original_error

try:
    from app.notify.telegram import send_telegram
except ImportError:
    logger.warning("telegram import failed, using no-op send_telegram", exc_info=True)
    def send_telegram(msg: str) -> bool:
        return False

if TYPE_CHECKING:
    # For type checking only; avoid runtime import/circular deps.
    from app.engine.hyper_engine_context import HyperEngineContext

from app.core.constants import env_float as _env_float, env_int as _env_int, env_bool as _env_bool
from app.core.currency import Q


def _now() -> float:
    return time.time()


def _slippage_bps(expected: float, actual: float, *, side: str) -> float:
    """슬리피지 bps.

    side:
      - 'bid'(buy): actual이 expected보다 높으면 슬리피지 +
      - 'ask'(sell): actual이 expected보다 낮으면 슬리피지 +
    """
    if expected <= 0 or actual <= 0:
        return 0.0
    if side == "bid":
        return (actual - expected) / expected * 10000.0
    # sell
    return (expected - actual) / expected * 10000.0


@dataclass
class PendingOrder:
    uuid: str
    market: str
    side: str  # 'bid' or 'ask'
    ord_type: str

    requested_usdt: float = 0.0
    requested_qty: float = 0.0

    state: str = "wait"  # wait/done/cancel
    created_ts: float = field(default_factory=_now)
    last_check_ts: float = 0.0

    attempts: int = 1
    max_retries: int = 2

    timeout_sec: float = 8.0
    poll_interval: float = 0.25

    expected_price: Optional[float] = None
    limit_price: Optional[float] = None  # for LIMIT orders
    max_slippage_bps_soft: float = 80.0
    max_slippage_bps_hard: float = 200.0

    executed_volume: float = 0.0
    funds: float = 0.0
    avg_price: Optional[float] = None
    paid_fee: float = 0.0

    reason: str = ""
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "market": self.market,
            "side": self.side,
            "ord_type": self.ord_type,
            "requested_usdt": self.requested_usdt,
            "requested_qty": self.requested_qty,
            "state": self.state,
            "created_ts": self.created_ts,
            "last_check_ts": self.last_check_ts,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "timeout_sec": self.timeout_sec,
            "poll_interval": self.poll_interval,
            "expected_price": self.expected_price,
            "limit_price": self.limit_price,
            "max_slippage_bps_soft": self.max_slippage_bps_soft,
            "max_slippage_bps_hard": self.max_slippage_bps_hard,
            "executed_volume": self.executed_volume,
            "funds": self.funds,
            "avg_price": self.avg_price,
            "paid_fee": self.paid_fee,
            "reason": self.reason,
            "last_error": self.last_error,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PendingOrder":
        return PendingOrder(
            uuid=str(d.get("uuid") or ""),
            market=str(d.get("market") or ""),
            side=str(d.get("side") or ""),
            ord_type=str(d.get("ord_type") or ""),
            requested_usdt=float(d.get("requested_usdt") or 0.0),
            requested_qty=float(d.get("requested_qty") or 0.0),
            state=str(d.get("state") or "wait"),
            created_ts=float(d.get("created_ts") or _now()),
            last_check_ts=float(d.get("last_check_ts") or 0.0),
            attempts=int(d.get("attempts") or 1),
            max_retries=int(d.get("max_retries") or 2),
            timeout_sec=float(d.get("timeout_sec") or 8.0),
            poll_interval=float(d.get("poll_interval") or 0.25),
            expected_price=(float(d["expected_price"]) if d.get("expected_price") is not None else None),
            limit_price=(float(d["limit_price"]) if d.get("limit_price") is not None else None),
            max_slippage_bps_soft=float(d.get("max_slippage_bps_soft") or 80.0),
            max_slippage_bps_hard=float(d.get("max_slippage_bps_hard") or 200.0),
            executed_volume=float(d.get("executed_volume") or 0.0),
            funds=float(d.get("funds") or 0.0),
            avg_price=(float(d["avg_price"]) if d.get("avg_price") is not None else None),
            paid_fee=float(d.get("paid_fee") or 0.0),
            reason=str(d.get("reason") or ""),
            last_error=(str(d.get("last_error")) if d.get("last_error") is not None else None),
        )


class OrderStateMachine:
    """시장별 1개 주문만 pending을 허용하는 FSM.

    시스템 설계 관점:
    - ctx.order_state (dict)에 저장해서 리셋 복구 가능
    - 정상 상황에서는 거의 '개입'하지 않는다.
    - 비정상(지연/부분체결/오류)일 때만 보호 동작을 한다.

    정책(중요):
    - hard slippage는 "항상 전역 정지"가 되면 진입의 방벽이 될 수 있다.
      따라서 기본값은:
        * sell(exit) hard slippage → EMERGENCY(전역)
        * buy(entry) hard slippage → 해당 market만 ENTRY COOLDOWN
    """

    def __init__(
        self,
        *,
        client: Union[BybitTradeClient, "TradeClient", Any],
        ledger: TradeLedger,
        exchange_type: str = "bybit",
    ) -> None:
        self.client = client
        self.ledger = ledger
        self.exchange_type = exchange_type
        self.quote_currency = self._detect_quote_currency(client=client, exchange_type=exchange_type)

        # 환경 튜닝 (Quote Currency 기준)
        self.min_order_usdt = _env_float("OMA_MIN_ORDER_USDT", Q.min_order)

        # "주문 가능한 금액이 부족합니다." 대응: 실제 주문가능 USDT를 보수적으로 사용
        # (수수료/라운딩/잠김(locked) 등을 감안하여 일부 버퍼를 둠)
        self.buy_available_fraction = _env_float("OMA_BUY_AVAILABLE_FRACTION", 0.995)

        # "주문 가능한 금액/수량이 부족합니다."를 시스템 에러로 보지 않고, 조용히 대기하는 시간
        self.insufficient_funds_pause_sec = _env_float("OMA_INSUFFICIENT_FUNDS_PAUSE_SEC", 60.0)
        self.insufficient_funds_max_pause_sec = _env_float("OMA_INSUFFICIENT_FUNDS_MAX_PAUSE_SEC", 600.0)
        self.insufficient_qty_pause_sec = _env_float("OMA_INSUFFICIENT_QTY_PAUSE_SEC", 60.0)

        # 부족금액 발생 시 재시도 방식
        # - MIN: 최소 주문금액(min_order_usdt)으로 1회 시도
        # - MAX: 주문가능 USDT 범위 내에서 가능한 큰 금액으로 1회 시도
        self.insufficient_funds_fallback = str(os.getenv("OMA_INSUFFICIENT_FUNDS_FALLBACK", "MIN")).upper()
        self.order_timeout_sec = _env_float("OMA_ORDER_TIMEOUT_SEC", 9.0)
        self.poll_interval = _env_float("OMA_ORDER_POLL_SEC", 0.25)
        self.max_retries = _env_int("OMA_ORDER_MAX_RETRIES", 2)

        # per-side overrides (entry/exit separated)
        # - entry(buy): 과도한 retry는 진입의 방벽/중복 위험을 키울 수 있으므로 보수적으로
        # - exit(sell): 미청산은 치명적이므로 필요 시 더 강하게
        self.order_timeout_sec_buy = _env_float("OMA_ORDER_TIMEOUT_SEC_BUY", self.order_timeout_sec)
        self.order_timeout_sec_sell = _env_float("OMA_ORDER_TIMEOUT_SEC_SELL", self.order_timeout_sec)
        self.max_retries_buy = _env_int("OMA_ORDER_MAX_RETRIES_BUY", self.max_retries)
        self.max_retries_sell = _env_int("OMA_ORDER_MAX_RETRIES_SELL", self.max_retries)


        self.slip_soft = _env_float("OMA_SLIPPAGE_SOFT_BPS", 80.0)
        self.slip_hard = _env_float("OMA_SLIPPAGE_HARD_BPS", 200.0)

        self.exit_fail_to_recovery = _env_bool("OMA_EXIT_FAIL_TO_RECOVERY", True)

        # entry hard slippage / entry unresolved → 시장 단위 재진입 쿨다운
        self.entry_slippage_hard_cooldown_sec = _env_float("OMA_ENTRY_COOLDOWN_AFTER_SLIPPAGE_SEC", 60.0)
        self.entry_unresolved_cooldown_sec = _env_float("OMA_ENTRY_COOLDOWN_AFTER_UNRESOLVED_SEC", 60.0)

        # Sell-fill callbacks: list of callables(ctx, market, strategy, pnl_pct, entry_price, exit_price, qty, hold_sec)
        self._sell_fill_callbacks: list = []
        # Buy-fill callbacks: list of callables(ctx, market, strategy, entry_price, qty, funds, fee)
        self._buy_fill_callbacks: list = []

    # --------------------------------------------------------
    # Sell-fill notification
    # --------------------------------------------------------
    def _notify_sell_filled(
        self,
        *,
        ctx: Any,
        market: str,
        strategy: str,
        pnl_pct: float,
        entry_price: float,
        exit_price: float,
        qty: float,
        hold_sec: float,
    ) -> None:
        for cb in self._sell_fill_callbacks:
            try:
                cb(
                    ctx=ctx, market=market, strategy=strategy,
                    pnl_pct=pnl_pct, entry_price=entry_price,
                    exit_price=exit_price, qty=qty, hold_sec=hold_sec,
                )
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                logger.warning("[recovered] sell_fill_callback error market=%s: %s", market, e)

    # --------------------------------------------------------
    # Buy-fill notification
    # --------------------------------------------------------
    def _notify_buy_filled(
        self,
        *,
        ctx: Any,
        market: str,
        strategy: str,
        entry_price: float,
        qty: float,
        funds: float,
        fee: float,
        reason: str,
    ) -> None:
        for cb in self._buy_fill_callbacks:
            try:
                cb(
                    ctx=ctx, market=market, strategy=strategy,
                    entry_price=entry_price, qty=qty, funds=funds,
                    fee=fee, reason=reason,
                )
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                logger.warning("[recovered] buy_fill_callback error market=%s: %s", market, e)

    # --------------------------------------------------------
    # Client compatibility helpers
    # --------------------------------------------------------
    @staticmethod
    def _detect_quote_currency(*, client: Any, exchange_type: str) -> str:
        """Detect quote currency from client/exchange with safe fallback."""
        q = str(getattr(client, "quote_currency", "") or "").upper().strip()
        if q:
            return q
        # Bybit always uses USDT
        return "USDT"

    def _client_market_buy_usdt(self, market: str, usdt_amount: float) -> Dict[str, Any]:
        """TradeClient 구현 차이를 흡수 (quote amount 기준)."""
        amount = float(usdt_amount)

        # 1) Bybit-style explicit method
        fn_buy_usdt = getattr(self.client, "market_buy_usdt", None)
        if callable(fn_buy_usdt):
            try:
                # BybitTradeClient.market_buy_usdt(market, amount, **kw) — keyword is `amount`, not usdt_amount
                return fn_buy_usdt(market=market, amount=amount)  # type: ignore[misc]
            except TypeError:
                logger.warning("OrderStateMachine._client_market_buy_usdt suppressed exception", exc_info=True)
                return fn_buy_usdt(market, amount)  # type: ignore[misc]

        # 2) Unified market_buy(market, quote_amount)
        fn_market_buy = getattr(self.client, "market_buy", None)
        if callable(fn_market_buy):
            try:
                return fn_market_buy(market=market, quote_amount=amount)  # type: ignore[misc]
            except TypeError:
                try:
                    return fn_market_buy(market=market, amount=amount)  # type: ignore[misc]
                except TypeError:
                    logger.warning("OrderStateMachine._client_market_buy_usdt suppressed exception", exc_info=True)
                    return fn_market_buy(market, amount)  # type: ignore[misc]

        # 3) Generic place_order fallback
        fn_place = getattr(self.client, "place_order", None)
        if callable(fn_place):
            return fn_place(market=market, side="bid", ord_type="price", price=amount)  # type: ignore[misc]

        raise AttributeError("trade_client_missing_market_buy_api")

    def _client_market_sell_qty(self, market: str, qty: float) -> Dict[str, Any]:
        """TradeClient 구현 차이를 흡수한다 (Bybit 기준).

        BybitTradeClient.market_sell_qty(market, qty, **kw) 시그니처 기준.
        TypeError 발생 시 숨기지 않고 로깅 후 대체 경로 시도.
        """
        qtyf = float(qty)

        # 1) market_sell_qty — Bybit 표준 시그니처: positional(market, qty)
        fn_sell_qty = getattr(self.client, "market_sell_qty", None)
        if callable(fn_sell_qty):
            try:
                return fn_sell_qty(market, qtyf)
            except TypeError as e:
                logger.error("[MARKET_SELL] market_sell_qty(%s, %s) TypeError: %s — trying market_sell",
                             market, qtyf, e)

        # 2) market_sell — fallback: positional(market, qty)
        fn_market_sell = getattr(self.client, "market_sell", None)
        if callable(fn_market_sell):
            try:
                return fn_market_sell(market, qtyf)
            except TypeError as e:
                logger.error("[MARKET_SELL] market_sell(%s, %s) TypeError: %s — trying place_order",
                             market, qtyf, e)

        # 3) generic place_order — last resort
        fn_place = getattr(self.client, "place_order", None)
        if callable(fn_place):
            return fn_place(market=market, side="ask", ord_type="market", volume=qtyf)

        raise AttributeError("trade_client_missing_market_sell_api")

    def _normalize_limit_price(self, price: float) -> float:
        """Apply exchange-specific tick normalization when available."""
        p = float(price)
        if p <= 0:
            return p

        # 1) client-provided adjuster
        fn_adjust = getattr(self.client, "adjust_price_to_tick", None)
        if callable(fn_adjust):
            try:
                return float(fn_adjust(p))
            except (TypeError, ValueError) as e:
                logger.error("[TICK_NORM] adjust_price_to_tick(%s) failed: %s — using raw price", p, e)
                return p

        # 2) legacy Bybit adjuster
        if str(self.exchange_type or "").lower() == "bybit":
            try:
                return float(_adjust_price_to_tick(p))
            except (TypeError, ValueError) as e:
                logger.error("[TICK_NORM] _adjust_price_to_tick(%s) failed: %s — using raw price", p, e)
                return p

        # 3) non-bybit exchanges keep raw price
        return p

    def _summarize_order_fields(
        self, order: Dict[str, Any]
    ) -> Tuple[str, float, float, Optional[float], float]:
        """order(dict) -> (state, executed_volume, funds, avg_price, paid_fee)

        * Bybit 응답 기준으로 funds/avg_price를 계산한다.
        """

        def _f(x: Any) -> float:
            try:
                return float(x)
            except (TypeError, ValueError):
                logger.warning("OrderStateMachine._f suppressed exception", exc_info=True)
                return 0.0

        # 0) client-side summarize_order 지원(호환)
        fn = getattr(self.client, "summarize_order", None)
        if callable(fn):
            try:
                s = fn(order)  # type: ignore[misc]
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.warning("OrderStateMachine._f suppressed exception", exc_info=True)
                s = None

            # summarize_order가 문자열을 반환하는 구현도 있어서(디버그용),
            # 그런 경우에는 여기서 무시하고 직접 파싱한다.
            if s is not None and not isinstance(s, str):
                if isinstance(s, dict):
                    state = str(s.get("state") or "").lower()
                    executed_volume = _f(s.get("executed_volume"))
                    funds = _f(s.get("funds"))
                    avg_price = s.get("avg_price")
                    avg_price_f = float(avg_price) if avg_price is not None else (funds / executed_volume if executed_volume > 0 and funds > 0 else None)
                    paid_fee = _f(s.get("paid_fee"))
                    return state, executed_volume, funds, avg_price_f, paid_fee

                if hasattr(s, "state"):
                    state = str(getattr(s, "state", "") or "").lower()
                    executed_volume = _f(getattr(s, "executed_volume", 0.0))
                    funds = _f(getattr(s, "funds", 0.0))
                    avg_price = getattr(s, "avg_price", None)
                    avg_price_f = float(avg_price) if avg_price is not None else (funds / executed_volume if executed_volume > 0 and funds > 0 else None)
                    paid_fee = _f(getattr(s, "paid_fee", 0.0))
                    return state, executed_volume, funds, avg_price_f, paid_fee

        # 1) raw order parsing
        state = str(order.get("state") or "").lower()
        executed_volume = _f(order.get("executed_volume") or 0.0)
        paid_fee = _f(order.get("paid_fee") or 0.0)

        funds = 0.0
        avg_price: Optional[float] = None

        trades = order.get("trades")
        if isinstance(trades, list) and trades:
            funds = sum(_f(t.get("funds")) for t in trades)
            vol2 = sum(_f(t.get("volume")) for t in trades)
            if vol2 > 0:
                executed_volume = max(executed_volume, vol2)
            if executed_volume > 0 and funds > 0:
                avg_price = funds / executed_volume
        else:
            ord_type = str(order.get("ord_type") or "")
            price = order.get("price")

            if ord_type == "price":
                # market buy (USDT). Bybit 응답에서 price는 총 USDT인 경우가 많다.
                funds = _f(price)
                if executed_volume > 0 and funds > 0:
                    avg_price = funds / executed_volume
            else:
                # limit order 등: price가 "단가"일 수 있다.
                unit = _f(price)
                if unit > 0 and executed_volume > 0:
                    funds = unit * executed_volume
                    avg_price = unit

        return state, executed_volume, funds, avg_price, paid_fee



    # ---------- orderbook meta (ledger attribution) ----------
    def _calc_spread_bps(self, best_bid: float, best_ask: float) -> float:
        if best_bid <= 0 or best_ask <= 0:
            return 999999.0
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return 999999.0
        return ((best_ask - best_bid) / mid) * 10000.0

    def _calc_depth_notional(
        self,
        units: Any,
        *,
        best_bid: float,
        best_ask: float,
        depth_bps: float,
    ) -> Tuple[float, float]:
        if best_bid <= 0 or best_ask <= 0 or depth_bps <= 0:
            return 0.0, 0.0

        ask_lim = best_ask * (1.0 + float(depth_bps) / 10000.0)
        bid_lim = best_bid * (1.0 - float(depth_bps) / 10000.0)

        ask_notional = 0.0
        bid_notional = 0.0
        for u in (units or [])[:15]:
            try:
                ap = float(u.get("ask_price") or 0.0)
                asz = float(u.get("ask_size") or 0.0)
                bp = float(u.get("bid_price") or 0.0)
                bsz = float(u.get("bid_size") or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.warning("[DEPTH_NOTIONAL] bad orderbook unit: %s data=%s", e, u)
                continue

            if ap > 0 and asz > 0 and ap <= ask_lim:
                ask_notional += ap * asz
            if bp > 0 and bsz > 0 and bp >= bid_lim:
                bid_notional += bp * bsz

        return float(ask_notional), float(bid_notional)

    def _orderbook_meta(self, market: str) -> Dict[str, Any]:
        """Best-effort orderbook snapshot for ledger attribution.

        Purpose:
        - Enable accurate post-hoc what-if analysis (e.g., spread/depth guards).
        - Record the market microstructure at *submit time* for both BUY/SELL.

        Notes:
        - This does not affect trading decisions (logging only).
        - If no orderbook is available, returns {ob_ok: False}.
        """
        try:
            ob = orderbook_store.get(str(market))
        except (KeyError, AttributeError, TypeError):
            logger.warning("OrderStateMachine._orderbook_meta suppressed exception", exc_info=True)
            ob = None

        if not ob:
            return {"ob_ok": False}

        try:
            now_ts = _now()
            ob_ts = float(ob.get("ts") or 0.0)
            best_bid = float(ob.get("best_bid") or 0.0)
            best_ask = float(ob.get("best_ask") or 0.0)
            units = ob.get("units") or []
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("OrderStateMachine._orderbook_meta suppressed exception", exc_info=True)
            return {"ob_ok": False}

        # Match system defaults (even if dashboard overrides exist).
        try:
            depth_bps = float(os.getenv("OMA_ENTRY_OB_DEPTH_BPS", "50") or "50")
        except (TypeError, ValueError):
            logger.warning("OrderStateMachine._orderbook_meta suppressed exception", exc_info=True)
            depth_bps = 50.0

        spread_bps = self._calc_spread_bps(best_bid, best_ask)
        depth_ask_usdt, depth_bid_usdt = self._calc_depth_notional(
            units,
            best_bid=best_bid,
            best_ask=best_ask,
            depth_bps=depth_bps,
        )

        age_sec = None
        try:
            if ob_ts > 0:
                age_sec = max(0.0, float(now_ts) - float(ob_ts))
        except (TypeError, ValueError):
            logger.warning("OrderStateMachine._orderbook_meta suppressed exception", exc_info=True)
            age_sec = None

        return {
            "ob_ok": True,
            "ob_ts": float(ob_ts),
            "ob_age_sec": float(age_sec) if age_sec is not None else None,
            "best_bid": float(best_bid),
            "best_ask": float(best_ask),
            "spread_bps": float(spread_bps),
            "depth_bps": float(depth_bps),
            "depth_ask_usdt": float(depth_ask_usdt),
            "depth_bid_usdt": float(depth_bid_usdt),
        }


    # --------------------------------------------------------
    # Submit helpers
    # --------------------------------------------------------
    def _scaled_timeout(self, attempts: int, base_timeout: float) -> float:
        """Scale timeout based on retry attempts (gradual increase)."""
        a = max(1, int(attempts))
        return float(base_timeout) * (1.0 + 0.5 * float(a - 1))

    # ---------- soft-fail helpers (에러 해석 / 잔고 조회 / 라운딩) ----------

    def _client_get_balance(self, currency: str, *, include_locked: bool = False) -> float:
        """거래 클라이언트의 get_balance 시그니처가 달라도 동작하도록 래핑."""
        if not hasattr(self.client, "get_balance"):
            return 0.0
        try:
            return float(self.client.get_balance(currency, include_locked=include_locked))
        except TypeError:
            logger.warning("OrderStateMachine._client_get_balance suppressed exception", exc_info=True)
            # 구버전 시그니처: include_locked 인자를 지원하지 않을 수 있음
            return float(self.client.get_balance(currency))

    @staticmethod
    def _extract_api_error(exc: Exception) -> Tuple[Optional[str], Optional[str], str]:
        """Extract error payload (name/message) from API exception."""
        raw = str(getattr(exc, "response_text", "") or "")
        if not raw:
            raw = str(exc)

        name: Optional[str] = None
        msg: Optional[str] = None
        try:
            j = json.loads(raw)
            if isinstance(j, dict):
                err = j.get("error")
                if isinstance(err, dict):
                    if err.get("name") is not None:
                        name = str(err.get("name"))
                    if err.get("message") is not None:
                        msg = str(err.get("message"))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[API_ERROR_PARSE] failed to parse API error response: %s raw=%s", e, raw[:200] if raw else "")

        return name, msg, raw

    def _is_insufficient_funds(self, exc: Exception) -> bool:
        name, msg, raw = self._extract_api_error(exc)
        hay = " ".join([str(name or ""), str(msg or ""), raw])
        # 업비트 에러 문구 변형 대응:
        # - "주문 가능한 금액이 부족합니다."
        # - "주문 가능한 금액(USDT)이 부족합니다."
        # 공백/괄호 유무와 상관없이 판별하기 위해 normalize 문자열도 함께 확인한다.
        hay_norm = hay.replace(" ", "").replace("(", "").replace(")", "")
        return (
            ("주문 가능한 금액이 부족합니다" in hay)
            or ("주문가능한금액" in hay_norm and "부족" in hay_norm)
            or ("insufficient_funds" in hay)
            or ("insufficient_fund" in hay)
            or ("insufficient_balance" in hay)
        )

    def _is_insufficient_qty(self, exc: Exception) -> bool:
        name, msg, raw = self._extract_api_error(exc)
        hay = " ".join([str(name or ""), str(msg or ""), raw])
        return (
            ("주문 가능한 수량이 부족합니다" in hay)
            or ("insufficient_volume" in hay)
            or ("insufficient_amount" in hay)
            or ("insufficient_qty" in hay)
            or ("insufficient position" in hay.lower())
        )

    @staticmethod
    def _floor_to_unit(value: float, unit: float) -> float:
        if unit <= 0:
            return float(value)
        try:
            return float(int(value // unit) * unit)
        except (TypeError, ValueError):
            logger.warning("OrderStateMachine._floor_to_unit suppressed exception", exc_info=True)
            return float(value)

    def _retry_buy_usdt(self, *, requested_usdt: float, available_usdt: float) -> Optional[float]:
        """부족한 경우 재시도 금액 산출.

        - OMA_INSUFFICIENT_FUNDS_FALLBACK=MIN:
            최소 주문금액(min_order_usdt)으로 1회 시도
        - OMA_INSUFFICIENT_FUNDS_FALLBACK=MAX:
            주문가능 USDT 범위 내에서 가능한 큰 금액으로 1회 시도(버퍼 포함)
        """
        avail = max(0.0, float(available_usdt))

        mode = str(getattr(self, "insufficient_funds_fallback", "MIN") or "MIN").upper()
        if mode == "MIN":
            if avail >= float(self.min_order_usdt):
                return float(self.min_order_usdt)
            return None

        # mode == "MAX"
        avail_eff = avail * float(self.buy_available_fraction)
        usdt = min(float(requested_usdt), avail_eff)
        # 소수점 방지
        usdt = self._floor_to_unit(usdt, 1.0)
        if usdt >= float(self.min_order_usdt):
            return float(usdt)
        return None

    @staticmethod
    def _retry_sell_qty(*, requested_qty: float, available_qty: float) -> Optional[float]:
        """부족한 경우, 가능한 수량 내에서 재시도 수량 산출."""
        qty = min(float(requested_qty), max(0.0, float(available_qty)))
        # Bybit는 통상 8dp 이내로 안전
        qty = round(qty, 8)
        if qty > 0:
            return float(qty)
        return None
    def submit_market_buy(
        self,
        *,
        ctx: Any,
        market: str,
        usdt_amount: float,
        expected_price: Optional[float],
        reason: str,
        attempts: int = 1,
        max_retries: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        시장가 매수(=ord_type=price) 제출.

        주의:
        - client.market_buy_usdt(...)는 dict(order)를 반환하는 구현(Bybit 기본)을 기준으로 uuid를 추출한다.
        - expected_price는 주문 API로 전달하지 않고, 내부 슬리피지/로그 기준값으로만 사용한다.
        """

        # 이미 pending 이면 중복 제출 방지
        if getattr(ctx, "order_state", None) is not None:
            return False, "order_pending"

        if usdt_amount < self.min_order_usdt:
            return False, f"min_order_blocked:{usdt_amount:.0f}<{self.min_order_usdt:.0f}"

        now = _now()
        a = max(1, int(attempts))
        mr = int(max_retries) if max_retries is not None else int(self.max_retries_buy)

        def _uuid_from(resp: Any) -> str:
            if isinstance(resp, str):
                return resp.strip()
            if isinstance(resp, dict):
                return str(resp.get("uuid") or resp.get("id") or "").strip()
            return ""

        # 1차 제출
        try:
            obmeta = self._orderbook_meta(market)
            self.ledger.append(
                "ORDER_SUBMIT",
                market=market,
                side="bid",
                ord_type="price",
                usdt_amount=float(usdt_amount),
                expected_price=expected_price,
                reason=reason,
                attempts=a,
                max_retries=mr,
                **obmeta,
            )
            resp = self._client_market_buy_usdt(market, float(usdt_amount))
            oid = _uuid_from(resp)
            if not oid:
                raise RuntimeError(f"no uuid in buy response: {resp!r}")

        except (TypeError, ValueError) as exc:
            logger.warning("OrderStateMachine._uuid_from except: %s", exc, exc_info=True)
            # 부족한 USDT는 soft-fail 처리
            if self._is_insufficient_funds(exc):
                quote_ccy = str(getattr(self, "quote_currency", "USDT") or "USDT").upper()
                avail_quote = self._client_get_balance(quote_ccy, include_locked=False)
                retry_usdt = self._retry_buy_usdt(requested_usdt=float(usdt_amount), available_usdt=avail_quote)

                # 가능한 범위가 있다면 1회 축소 제출
                if retry_usdt is not None and retry_usdt + 1e-9 < float(usdt_amount):
                    try:
                        obmeta = self._orderbook_meta(market)
                        self.ledger.append(
                            "ORDER_SUBMIT",
                            market=market,
                            side="bid",
                            ord_type="price",
                            usdt_amount=float(retry_usdt),
                            expected_price=expected_price,
                            reason=f"{reason}:resize(insufficient_funds)",
                            resized_from_usdt=float(usdt_amount),
                            available_quote=float(avail_quote),
                            attempts=a,
                            max_retries=mr,
                            **obmeta,
                        )
                        resp2 = self._client_market_buy_usdt(market, float(retry_usdt))
                        oid2 = _uuid_from(resp2)
                        if not oid2:
                            raise RuntimeError(f"no uuid in buy response: {resp2!r}")

                        po = PendingOrder(
                            uuid=str(oid2),
                            market=str(market),
                            side="bid",
                            ord_type="price",
                            requested_usdt=float(retry_usdt),
                            requested_qty=0.0,
                            state="wait",
                            created_ts=now,
                            last_check_ts=0.0,
                            timeout_sec=self._scaled_timeout(a, self.order_timeout_sec_buy),
                            poll_interval=float(self.poll_interval),
                            attempts=a,
                            max_retries=mr,
                            expected_price=float(expected_price) if expected_price is not None else None,
                            max_slippage_bps_soft=float(self.slip_soft),
                            max_slippage_bps_hard=float(self.slip_hard),
                            reason=str(reason),
                        )
                        setattr(ctx, "order_state", po.to_dict())
                        setattr(ctx, "last_order_ts", now)

                        # 부족 상황에서는 이후 엔트리를 잠시 멈춘다(조용히 대기)
                        if hasattr(ctx, "entry_block_until_ts"):
                            prev = float(getattr(ctx, "entry_block_until_ts") or 0.0)
                            _n_insuf = int(getattr(ctx, "_insufficient_funds_streak", 0)) + 1
                            ctx._insufficient_funds_streak = _n_insuf
                            _pause = min(float(self.insufficient_funds_pause_sec) * (2 ** min(_n_insuf - 1, 4)), float(self.insufficient_funds_max_pause_sec))
                            setattr(ctx, "entry_block_until_ts", max(prev, now + _pause))
                        if hasattr(ctx, "entry_block_reason"):
                            setattr(ctx, "entry_block_reason", "insufficient_funds")

                        return True, (
                            f"{oid2}|WARN:INSUFFICIENT_FUNDS:"
                            f"requested_usdt={float(usdt_amount):.0f},submitted_usdt={float(retry_usdt):.0f},"
                            f"available_{quote_ccy.lower()}={float(avail_quote):.0f}"
                        )

                    except (KeyError, AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("OrderStateMachine._uuid_from except: %s", exc2, exc_info=True)
                        if self._is_insufficient_funds(exc2):
                            if hasattr(ctx, "entry_block_until_ts"):
                                prev = float(getattr(ctx, "entry_block_until_ts") or 0.0)
                                _n_insuf = int(getattr(ctx, "_insufficient_funds_streak", 0)) + 1
                                ctx._insufficient_funds_streak = _n_insuf
                                _pause = min(float(self.insufficient_funds_pause_sec) * (2 ** min(_n_insuf - 1, 4)), float(self.insufficient_funds_max_pause_sec))
                                setattr(ctx, "entry_block_until_ts", max(prev, now + _pause))
                            if hasattr(ctx, "entry_block_reason"):
                                setattr(ctx, "entry_block_reason", "insufficient_funds")

                            return False, (
                                "SOFT:INSUFFICIENT_FUNDS:"
                                f"requested_usdt={float(usdt_amount):.0f},available_{quote_ccy.lower()}={float(avail_quote):.0f},"
                                f"min_order_usdt={float(self.min_order_usdt):.0f}"
                            )

                        # 다른 오류는 기존대로 에러로 처리
                        self.ledger.append(
                            "ORDER_SUBMIT_ERROR",
                            market=market,
                            side="bid",
                            error=str(exc2),
                            reason=reason,
                        )
                        return False, str(exc2)

                # 축소도 불가능(최소 주문금액 미만) → 조용히 대기
                if hasattr(ctx, "entry_block_until_ts"):
                    prev = float(getattr(ctx, "entry_block_until_ts") or 0.0)
                    _n_insuf = int(getattr(ctx, "_insufficient_funds_streak", 0)) + 1
                    ctx._insufficient_funds_streak = _n_insuf
                    _pause = min(float(self.insufficient_funds_pause_sec) * (2 ** min(_n_insuf - 1, 4)), float(self.insufficient_funds_max_pause_sec))
                    setattr(ctx, "entry_block_until_ts", max(prev, now + _pause))
                if hasattr(ctx, "entry_block_reason"):
                    setattr(ctx, "entry_block_reason", "insufficient_funds")

                return False, (
                    "SOFT:INSUFFICIENT_FUNDS:"
                    f"requested_usdt={float(usdt_amount):.0f},available_{quote_ccy.lower()}={float(avail_quote):.0f},"
                    f"min_order_usdt={float(self.min_order_usdt):.0f}"
                )

            # 그 외 오류는 기존대로
            self.ledger.append(
                "ORDER_SUBMIT_ERROR",
                market=market,
                side="bid",
                error=str(exc),
                reason=reason,
            )
            return False, str(exc)

        # ACK 성공
        po = PendingOrder(
            uuid=str(oid),
            market=str(market),
            side="bid",
            ord_type="price",
            requested_usdt=float(usdt_amount),
            requested_qty=0.0,
            state="wait",
            created_ts=now,
            last_check_ts=0.0,
            timeout_sec=self._scaled_timeout(a, self.order_timeout_sec_buy),
            poll_interval=float(self.poll_interval),
            attempts=a,
            max_retries=mr,
            expected_price=float(expected_price) if expected_price is not None else None,
            max_slippage_bps_soft=float(self.slip_soft),
            max_slippage_bps_hard=float(self.slip_hard),
            reason=str(reason),
        )
        setattr(ctx, "order_state", po.to_dict())
        setattr(ctx, "last_order_ts", now)

        return True, str(oid)

    def submit_market_sell(
        self,
        *,
        ctx: Any,
        market: str,
        qty: float,
        expected_price: Optional[float],
        reason: str,
        attempts: int = 1,
        max_retries: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        시장가 매도(=ord_type=market) 제출.

        - client.market_sell_qty(...)는 dict(order)를 반환하는 구현(Bybit 기본)을 기준으로 uuid를 추출한다.
        - expected_price는 주문 API로 전달하지 않고, 내부 슬리피지/로그 기준값으로만 사용한다.
        """

        if getattr(ctx, "order_state", None) is not None:
            return False, "order_pending"

        if qty <= 0:
            return False, "qty<=0"

        # [FIX] 전량 매도 시 업비트 실잔고로 교체 (dust 방지)
        # position.qty와 실잔고가 미세하게 다를 수 있으므로, 실잔고를 사용
        try:
            from decimal import Decimal, ROUND_DOWN
            currency = Q.extract_base(market)
            real_balance = self._client_get_balance(currency, include_locked=False)
            if real_balance and real_balance > 0:
                pos_qty = 0.0
                try:
                    pos = getattr(ctx, "position", None)
                    if isinstance(pos, dict):
                        pos_qty = float(pos.get("qty") or 0.0)
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    logger.warning("[recovered] pos_qty read for dust-fix market=%s: %s", market, e)
                
                # 요청 qty가 position qty의 99% 이상이면 전량 매도로 간주
                if pos_qty > 0 and qty >= pos_qty * 0.99:
                    # [FIX] Decimal로 정밀하게 처리하여 먼지 방지
                    qty_decimal = Decimal(str(real_balance)).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
                    qty = float(qty_decimal)  # 실잔고 전량 사용
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.warning("[recovered] sell qty dust-fix market=%s: %s", market, e)

        # [FIX] Prevent API spam for dust/small orders (Bybit min ~5 USDT)
        if expected_price is not None and expected_price > 0:
            est_val = float(qty) * float(expected_price)
            if est_val < self.min_order_usdt:
                return False, f"min_value_blocked:{est_val:.0f}<{self.min_order_usdt:.0f}"

        now = _now()
        a = max(1, int(attempts))
        mr = int(max_retries) if max_retries is not None else int(self.max_retries_sell)

        def _uuid_from(resp: Any) -> str:
            if isinstance(resp, str):
                return resp.strip()
            if isinstance(resp, dict):
                return str(resp.get("uuid") or resp.get("id") or "").strip()
            return ""

        # 1차 제출
        try:
            obmeta = self._orderbook_meta(market)
            self.ledger.append(
                "ORDER_SUBMIT",
                market=market,
                side="ask",
                ord_type="market",
                qty=float(qty),
                expected_price=expected_price,
                reason=reason,
                attempts=a,
                max_retries=mr,
                **obmeta,
            )
            resp = self._client_market_sell_qty(market, float(qty))
            oid = _uuid_from(resp)
            if not oid:
                raise RuntimeError(f"no uuid in sell response: {resp!r}")

        except (TypeError, ValueError) as exc:
            if self._is_insufficient_qty(exc) or self._is_insufficient_funds(exc):
                # market -> base currency (e.g., "XRPUSDT" -> "XRP", "BTCUSDT" -> "BTC")
                currency = Q.extract_base(market)
                total_qty = self._client_get_balance(currency, include_locked=True)
                avail_qty = self._client_get_balance(currency, include_locked=False)

                # 컨텍스트 position이 과대(예: reconcile/orphan + fill 중복)인 경우, 계정 기준으로 먼저 정정
                try:
                    pos = getattr(ctx, "position", None)
                    if isinstance(pos, dict):
                        pos["qty"] = float(total_qty)
                        setattr(ctx, "position", pos)
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    logger.warning("[recovered] position qty correction market=%s: %s", market, e)

                # 계정에 보유 수량이 없으면 "이미 청산된 것"으로 간주하고 position을 제거
                if float(total_qty) <= 1e-12:
                    try:
                        ctx.position = None
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                        logger.warning("OrderStateMachine._uuid_from suppressed exception", exc_info=True)
                        setattr(ctx, "position", None)

                    self.ledger.append(
                        "POSITION_CLEAR_NO_BALANCE",
                        market=market,
                        reason="insufficient_funds_ask and balance=0",
                    )
                    return True, "cleared(no_balance)"

                retry_qty = self._retry_sell_qty(requested_qty=float(qty), available_qty=avail_qty)

                if retry_qty is not None and retry_qty + 1e-12 < float(qty):
                    try:
                        obmeta = self._orderbook_meta(market)
                        self.ledger.append(
                            "ORDER_SUBMIT",
                            market=market,
                            side="ask",
                            ord_type="market",
                            qty=float(retry_qty),
                            expected_price=expected_price,
                            reason=f"{reason}:resize(insufficient_qty)",
                            resized_from_qty=float(qty),
                            available_qty=float(avail_qty),
                            attempts=a,
                            max_retries=mr,
                            **obmeta,
                        )
                        resp2 = self._client_market_sell_qty(market, float(retry_qty))
                        oid2 = _uuid_from(resp2)
                        if not oid2:
                            raise RuntimeError(f"no uuid in sell response: {resp2!r}")

                        po = PendingOrder(
                            uuid=str(oid2),
                            market=str(market),
                            side="ask",
                            ord_type="market",
                            requested_usdt=0.0,
                            requested_qty=float(retry_qty),
                            state="wait",
                            created_ts=now,
                            last_check_ts=0.0,
                            timeout_sec=self._scaled_timeout(a, self.order_timeout_sec_sell),
                            poll_interval=float(self.poll_interval),
                            attempts=a,
                            max_retries=mr,
                            expected_price=float(expected_price) if expected_price is not None else None,
                            max_slippage_bps_soft=float(self.slip_soft),
                            max_slippage_bps_hard=float(self.slip_hard),
                            reason=str(reason),
                        )
                        setattr(ctx, "order_state", po.to_dict())
                        setattr(ctx, "last_order_ts", now)

                        return True, (
                            f"{oid2}|WARN:INSUFFICIENT_QTY:"
                            f"requested_qty={float(qty):.8f},submitted_qty={float(retry_qty):.8f},"
                            f"available_qty={float(avail_qty):.8f}"
                        )

                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("OrderStateMachine._uuid_from except: %s", exc2, exc_info=True)
                        if self._is_insufficient_qty(exc2):
                            if hasattr(ctx, "exit_block_until_ts"):
                                prev = float(getattr(ctx, "exit_block_until_ts") or 0.0)
                                setattr(ctx, "exit_block_until_ts", max(prev, now + float(self.insufficient_qty_pause_sec)))
                            if hasattr(ctx, "exit_block_reason"):
                                setattr(ctx, "exit_block_reason", "insufficient_qty")

                            return False, (
                                "SOFT:INSUFFICIENT_QTY:"
                                f"requested_qty={float(qty):.8f},available_qty={float(avail_qty):.8f}"
                            )

                        self.ledger.append(
                            "ORDER_SUBMIT_ERROR",
                            market=market,
                            side="ask",
                            error=str(exc2),
                            reason=reason,
                        )
                        return False, str(exc2)

                # 재시도할 수량이 없거나(0) 최소단위 이슈 등 → 조용히 대기
                if hasattr(ctx, "exit_block_until_ts"):
                    prev = float(getattr(ctx, "exit_block_until_ts") or 0.0)
                    setattr(ctx, "exit_block_until_ts", max(prev, now + float(self.insufficient_qty_pause_sec)))
                if hasattr(ctx, "exit_block_reason"):
                    setattr(ctx, "exit_block_reason", "insufficient_qty")

                return False, (
                    "SOFT:INSUFFICIENT_QTY:"
                    f"requested_qty={float(qty):.8f},available_qty={float(avail_qty):.8f}"
                )

            self.ledger.append(
                "ORDER_SUBMIT_ERROR",
                market=market,
                side="ask",
                error=str(exc),
                reason=reason,
            )
            return False, str(exc)

        # ACK 성공
        po = PendingOrder(
            uuid=str(oid),
            market=str(market),
            side="ask",
            ord_type="market",
            requested_usdt=0.0,
            requested_qty=float(qty),
            state="wait",
            created_ts=now,
            last_check_ts=0.0,
            timeout_sec=self._scaled_timeout(a, self.order_timeout_sec_sell),
            poll_interval=float(self.poll_interval),
            attempts=a,
            max_retries=mr,
            expected_price=float(expected_price) if expected_price is not None else None,
            max_slippage_bps_soft=float(self.slip_soft),
            max_slippage_bps_hard=float(self.slip_hard),
            reason=str(reason),
        )
        setattr(ctx, "order_state", po.to_dict())
        setattr(ctx, "last_order_ts", now)

        return True, str(oid)

    # --------------------------------------------------------
    # Fill apply (ctx position update) + ledger
    # --------------------------------------------------------

    # ====================================================================
    # PATCH 2025-12-26
    # Limit SELL support (TP exit)
    # - Used by Pingpong TP EXIT: limit at best_bid with timeout/cancel/retry.
    # ====================================================================
    # ====================================================================
    # PATCH 2025-12-26
    # Limit SELL support (TP exit)
    # - Used by Pingpong TP EXIT: limit at best_bid with timeout/cancel/retry.
    # ====================================================================
    def submit_limit_sell(
        self,
        *,
        ctx: "HyperEngineContext",
        market: str,
        qty: float,
        limit_price: float,
        expected_price: Optional[float] = None,
        reason: str = "",
        attempts: Optional[int] = None,
        max_retries: Optional[int] = None,
        timeout_sec: Optional[float] = None,
    ) -> Tuple[bool, str]:
        qty = float(qty)
        limit_price = float(limit_price)
        if qty <= 0 or limit_price <= 0:
            return False, "invalid_qty_or_price"

        limit_price = self._normalize_limit_price(limit_price)

        # [FIX] Prevent API spam for dust/small orders
        if (qty * limit_price) < self.min_order_usdt:
            return False, f"min_value_blocked:{(qty*limit_price):.0f}<{self.min_order_usdt:.0f}"

        try:
            res = self.client.place_order(
                market=market,
                side="ask",
                ord_type="limit",
                volume=qty,
                price=limit_price,
            )
            oid = str(res.get("uuid") or "")
        except Exception as e:
            logger.warning("OrderStateMachine.submit_limit_sell except: %s", e, exc_info=True)
            return False, f"place_order_failed:{e}"

        po = PendingOrder(
            uuid=oid,
            market=market,
            side="ask",
            ord_type="limit",
            requested_qty=qty,
            requested_usdt=0.0,
            state="wait",
            created_ts=_now(),
            timeout_sec=float(timeout_sec if timeout_sec is not None else self.order_timeout_sec_sell),
            poll_interval=self.poll_interval,
            expected_price=float(expected_price) if expected_price is not None else float(limit_price),
            limit_price=float(limit_price),
            max_slippage_bps_soft=float(self.slip_soft),
            max_slippage_bps_hard=float(self.slip_hard),
            attempts=int(attempts or 1),
            max_retries=int(max_retries if max_retries is not None else self.max_retries_sell),
            reason=str(reason or ""),
        )

        self.pending = po
        ctx.order_state = po.to_dict()
        ctx.last_order_ts = _now()

        # Mark pending
        ctx.exit_pending = True

        self.ledger.append(
            "ORDER_SUBMIT",
            market=market,
            uuid=oid,
            side="ask",
            ord_type="limit",
            qty=qty,
            price=limit_price,
            expected_price=po.expected_price,
            timeout_sec=po.timeout_sec,
            attempts=po.attempts,
            max_retries=po.max_retries,
            reason=po.reason,
        )
        self.ledger.append(
            "ORDER_ACK",
            market=market,
            uuid=oid,
            side="ask",
            ord_type="limit",
            state="wait",
            qty=qty,
            price=limit_price,
            reason=po.reason,
        )

        return True, oid

    # ====================================================================
    # PATCH 2025-01-15
    # Quick SELL (IOC) - 즉시 체결 안 되면 자동 취소
    # - 슬리피지 방지, 원하는 가격에만 매도
    # - 익절/일반 청산용 (긴급 손절은 market_sell 사용)
    # ====================================================================
    def submit_quick_sell(
        self,
        *,
        ctx: "HyperEngineContext",
        market: str,
        qty: float,
        price: float,
        reason: str = "",
        fallback_to_market: bool = False,
    ) -> Tuple[bool, str]:
        """빠른 지정가 매도 (IOC) - 미체결 시 자동 취소.
        
        Args:
            ctx: 엔진 컨텍스트
            market: 마켓 심볼
            qty: 매도 수량
            price: 매도 희망가 (보통 best_bid)
            reason: 매도 사유
            fallback_to_market: True면 미체결 시 시장가로 재시도 (손절용)
            
        Returns:
            (success, message)
            - success=True: 전량 또는 일부 체결
            - success=False: 미체결 (다음 기회 대기)
        """
        qty = float(qty)
        price = float(price)
        if qty <= 0 or price <= 0:
            return False, "invalid_qty_or_price"

        # 최소 주문금액 체크
        if (qty * price) < self.min_order_usdt:
            return False, f"min_value_blocked:{(qty*price):.0f}<{self.min_order_usdt:.0f}"

        # quick_sell 메서드 확인
        if not hasattr(self.client, "quick_sell"):
            # fallback: 일반 limit_sell 사용
            return self.submit_limit_sell(
                ctx=ctx,
                market=market,
                qty=qty,
                limit_price=price,
                reason=f"quick_fallback:{reason}",
            )

        try:
            result = self.client.quick_sell(market=market, qty=qty, price=price)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("OrderStateMachine.submit_quick_sell except: %s", e, exc_info=True)
            self.ledger.append(
                "QUICK_SELL_ERROR",
                market=market,
                qty=qty,
                price=price,
                reason=reason,
                error=str(e),
            )
            return False, f"quick_sell_error:{e}"

        action = result.get("action", "error")
        filled_qty = float(result.get("filled_qty", 0) or 0)
        remaining_qty = float(result.get("remaining_qty", 0) or 0)
        avg_price = float(result.get("avg_price", 0) or 0)
        message = result.get("message", "")

        # 원장 기록
        self.ledger.append(
            "QUICK_SELL",
            market=market,
            action=action,
            requested_qty=qty,
            filled_qty=filled_qty,
            remaining_qty=remaining_qty,
            price=price,
            avg_price=avg_price,
            reason=reason,
            message=message,
        )

        if action == "filled":
            # Pre-extract entry info before position clear
            _qs_entry = 0.0
            _qs_entry_ts = 0.0
            _qs_strategy = ""
            try:
                _qs_pos = getattr(ctx, "position", None) or {}
                _qs_entry = float(_qs_pos.get("entry", 0) or 0)
                _qs_entry_ts = float(_qs_pos.get("entry_ts", 0) or getattr(ctx, "entry_ts", 0) or 0)
                _qs_strategy = str(getattr(ctx, "selected_strategy", "") or "")
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.warning("[recovered] quick_sell entry extraction market=%s: %s", market, e)

            # 전량 체결 - 포지션 청산
            self._update_position_after_sell(ctx=ctx, market=market, qty=filled_qty, avg_price=avg_price)
            self._send_quick_sell_telegram(ctx, market, filled_qty, avg_price, "filled")

            # Sell-fill callbacks
            if self._sell_fill_callbacks and _qs_strategy and _qs_entry > 0:
                _qs_pnl = (avg_price - _qs_entry) / _qs_entry * 100
                _qs_hold = (time.time() - _qs_entry_ts) if _qs_entry_ts > 0 else 0.0
                self._notify_sell_filled(
                    ctx=ctx, market=market, strategy=_qs_strategy,
                    pnl_pct=_qs_pnl, entry_price=_qs_entry,
                    exit_price=avg_price, qty=filled_qty, hold_sec=_qs_hold,
                )

            return True, f"filled:{filled_qty}@{avg_price}"

        elif action == "partial":
            # 부분 체결 - 포지션 일부 청산
            self._update_position_after_sell(ctx=ctx, market=market, qty=filled_qty, avg_price=avg_price)
            self._send_quick_sell_telegram(ctx, market, filled_qty, avg_price, "partial", remaining_qty)
            
            # fallback_to_market이면 나머지 시장가 매도
            if fallback_to_market and remaining_qty > 0:
                return self.submit_market_sell(
                    ctx=ctx,
                    market=market,
                    qty=remaining_qty,
                    expected_price=price,
                    reason=f"quick_fallback:{reason}",
                )
            return True, f"partial:{filled_qty}/{qty}@{avg_price}"

        elif action == "cancelled":
            # 미체결 - 다음 기회 대기
            self._send_quick_sell_telegram(ctx, market, 0, price, "cancelled")
            if fallback_to_market:
                return self.submit_market_sell(
                    ctx=ctx,
                    market=market,
                    qty=qty,
                    expected_price=price,
                    reason=f"quick_fallback:{reason}",
                )
            return False, "cancelled:wait_next_opportunity"

        else:
            # error
            if fallback_to_market:
                return self.submit_market_sell(
                    ctx=ctx,
                    market=market,
                    qty=qty,
                    expected_price=price,
                    reason=f"quick_fallback:{reason}",
                )
            return False, f"error:{message}"

    def _update_position_after_sell(
        self,
        *,
        ctx: Any,
        market: str,
        qty: float,
        avg_price: float,
    ) -> None:
        """매도 후 포지션 업데이트."""
        try:
            pos = getattr(ctx, "position", None)
            if pos and isinstance(pos, dict):
                old_qty = float(pos.get("qty", 0) or 0)
                new_qty = max(0.0, old_qty - qty)
                if new_qty <= 0:
                    # 포지션 청산
                    ctx.position = {}
                    ctx.exit_pending = False
                else:
                    pos["qty"] = new_qty
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.warning("[recovered] _update_position_after_sell market=%s: %s", market, e)

    def _send_quick_sell_telegram(
        self,
        ctx: Any,
        market: str,
        filled_qty: float,
        avg_price: float,
        action: str,
        remaining_qty: float = 0.0,
    ) -> None:
        """Quick Sell 결과 텔레그램 알림."""
        try:
            coin = Q.extract_base(market)
            
            if action == "filled":
                # 수익 계산
                entry_price = 0.0
                profit_usdt = 0.0
                profit_pct = 0.0
                _qs_entry_ts_local = 0.0
                _qs_strategy_local = ""
                try:
                    pos = getattr(ctx, "position", None) or {}
                    entry_price = float(
                        pos.get("entry", 0)
                        or pos.get("avg_price", 0)
                        or pos.get("entry_price", 0)
                        or getattr(ctx, "avg_buy_price", 0)
                        or 0
                    )
                    _qs_entry_ts_local = float(pos.get("entry_ts", 0) or getattr(ctx, "entry_ts", 0) or 0)
                    _qs_strategy_local = str(getattr(ctx, "selected_strategy", "") or "")
                    if entry_price > 0:
                        profit_pct = (avg_price - entry_price) / entry_price * 100
                        profit_usdt = (avg_price - entry_price) * filled_qty
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    logger.warning("[recovered] quick_sell pnl calc market=%s: %s", market, e)
                
                funds = filled_qty * avg_price
                sell_fee_est = funds * 0.0005
                buy_fee_est = entry_price * filled_qty * 0.0005 if entry_price > 0 else 0.0
                net_profit = profit_usdt - sell_fee_est - buy_fee_est
                profit_sign = "+" if net_profit >= 0 else ""
                profit_emoji = "🟢" if net_profit >= 0 else "🔴"
                msg = (
                    f"⚡ [QUICK SELL] {market}\n"
                    f"• 수량: {filled_qty:.6g} {coin}\n"
                    f"• 금액: {Q.format(funds)}\n"
                    f"• 매입가: {Q.format(entry_price, with_suffix=False)} → 체결가: {Q.format(avg_price, with_suffix=False)}"
                )
                if entry_price > 0:
                    total_fee = sell_fee_est + buy_fee_est
                    msg += (
                        f"\n💹 총 이윤: {profit_sign}{Q.format(profit_usdt)} ({profit_sign}{profit_pct:.2f}%)"
                        f"\n{profit_emoji} 순이익: {profit_sign}{Q.format(net_profit)}"
                        f"\n💰 수수료: {Q.format(total_fee)}"
                    )
                else:
                    msg += "\n⚠️ 매입가 미상 — 수익 계산 불가"
                if _qs_entry_ts_local > 0:
                    _hold = time.time() - _qs_entry_ts_local
                    if _hold >= 3600:
                        msg += f"\n⏱️ 보유: {_hold / 3600:.1f}시간"
                    else:
                        msg += f"\n⏱️ 보유: {_hold / 60:.0f}분"
                if _qs_strategy_local:
                    msg += f"\n📋 전략: {_qs_strategy_local}"
                send_telegram(msg)
                
            elif action == "partial":
                funds = filled_qty * avg_price
                send_telegram(
                    f"⚡ [QUICK SELL] {market} 부분체결\n"
                    f"• 체결: {filled_qty:.6g} {coin}\n"
                    f"• 미체결: {remaining_qty:.6g} {coin}\n"
                    f"• 금액: {Q.format(funds)}\n"
                    f"• 체결가: {Q.format(avg_price, with_suffix=False)}"
                )
                
            elif action == "cancelled":
                send_telegram(
                    f"⏸️ [QUICK SELL] {market} 미체결\n"
                    f"• 희망가: {Q.format(avg_price, with_suffix=False)}\n"
                    f"• 상태: 다음 기회 대기 중..."
                )
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
            logger.warning("[OSM] telegram buy notification failed", exc_info=True)

    def _apply_fill_and_log(self, *, ctx: Any, po: PendingOrder, market: str) -> Optional[float]:
        """
        최종 체결(done/cancel) 시점에 ctx 포지션을 반영하고, FILL_* 이벤트를 기록한다.

        Returns:
            slippage_bps (float) if calculable else None
        """

        side = str(po.side or "").lower()
        avg = po.avg_price
        if avg is None and po.executed_volume and po.funds and po.executed_volume > 0 and po.funds > 0:
            avg = float(po.funds) / float(po.executed_volume)

        # ask 시장가 매도 fallback — Bybit가 funds=0 반환 시 expected_price로 보정
        # (FILL_NONE 오탐 → 포지션 미청산 → 이중 매도 신호 방지)
        if avg is None and side in ("ask", "sell") and (po.executed_volume or 0.0) > 0.0 and po.expected_price:
            avg = float(po.expected_price)

        slippage_bps: Optional[float] = None
        if po.expected_price is not None and avg is not None:
            try:
                slippage_bps = float(_slippage_bps(float(po.expected_price), float(avg), side=side))
            except (TypeError, ValueError):
                logger.warning("OrderStateMachine._apply_fill_and_log suppressed exception", exc_info=True)
                slippage_bps = None

        # Nothing filled
        if (po.executed_volume or 0.0) <= 0.0 or avg is None:
            self.ledger.append(
                "FILL_NONE",
                market=market,
                uuid=po.uuid,
                side=po.side,
                state=po.state,
                executed_volume=float(po.executed_volume or 0.0),
                funds=float(po.funds or 0.0),
                avg_price=avg,
                paid_fee=float(po.paid_fee or 0.0),
                slippage_bps=slippage_bps,
                reason=po.reason,
            )
            # [FIX 2026-02-19] 매수 미체결 시 쿨다운 (무한 재시도 방지)
            # [2026-03-22] 연속 실패 시 에스컬레이팅: 2분→4분→8분→10분
            if side in ("bid", "buy"):
                import time as _t
                now = _t.time()
                prev = float(getattr(ctx, "entry_block_until_ts", 0.0) or 0.0)
                _n_fill = int(getattr(ctx, "_fill_none_streak", 0)) + 1
                ctx._fill_none_streak = _n_fill
                _fill_pause = min(120.0 * (2 ** min(_n_fill - 1, 3)), 600.0)
                ctx.entry_block_until_ts = max(prev, now + _fill_pause)
                ctx.entry_block_reason = "fill_none"
            return slippage_bps

        qty = float(po.executed_volume or 0.0)
        funds = float(po.funds or 0.0)
        fee = float(po.paid_fee or 0.0)

        if side in ("bid", "buy"):
            # ctx 포지션 반영
            adopted = False
            ctx._insufficient_funds_streak = 0
            ctx._fill_none_streak = 0  # 매수 성공 → FILL_NONE 연속 실패 카운터 리셋

            # Reconcile(orphan)로 이미 계정 보유량을 position에 세팅한 뒤,
            # 같은 주문의 fill을 다시 apply하면 qty가 "2배"로 누적될 수 있다.
            # → orphan position과 fill이 사실상 동일하면 "adopt"만 하고 누적은 하지 않는다.
            try:
                pos = getattr(ctx, "position", None)
                if isinstance(pos, dict) and str(pos.get("source") or "").lower() == "orphan":
                    old_qty = float(pos.get("qty") or 0.0)
                    old_entry = float(pos.get("entry") or 0.0)
                    if old_qty > 0.0 and float(qty) > 0.0 and float(avg) > 0.0:
                        qty_close = abs(old_qty - float(qty)) / max(float(qty), 1e-12) <= 0.05
                        entry_close = (old_entry > 0.0) and (abs(old_entry - float(avg)) / float(avg) <= 0.05)
                        if qty_close and entry_close:
                            pos["source"] = "bybit"
                            pos["entry"] = float(avg)
                            pos["usdt"] = float(funds) if float(funds) > 0.0 else (float(avg) * float(old_qty))
                            # [FIX 2026-02-19] orphan adopt 시 entry_ts 설정 (Grace Period 작동 보장)
                            import time as _t
                            pos["entry_ts"] = _t.time()
                            setattr(ctx, "position", pos)
                            adopted = True
                            self.ledger.append(
                                "FILL_BUY_ADOPT_ORPHAN",
                                market=market,
                                uuid=po.uuid,
                                qty=float(qty),
                                funds=float(funds),
                                avg_price=float(avg),
                                old_qty=float(old_qty),
                                old_entry=float(old_entry),
                                reason=po.reason,
                            )
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("OrderStateMachine._apply_fill_and_log suppressed exception", exc_info=True)
                adopted = False

            fn = getattr(ctx, "apply_fill_buy", None)
            if callable(fn) and not adopted:
                try:
                    fn(avg_price=float(avg), qty=float(qty), funds=float(funds), fee=float(fee), source="bybit")
                except TypeError:
                    logger.warning("OrderStateMachine._apply_fill_and_log suppressed exception", exc_info=True)
                    # 구버전 시그니처 대비
                    fn(float(avg), float(qty), float(funds), float(fee))

            _buy_strategy = str(getattr(ctx, "selected_strategy", "") or "").strip().upper()
            self.ledger.append(
                "FILL_BUY",
                market=market,
                uuid=po.uuid,
                side="bid",
                qty=float(qty),
                funds=float(funds),
                avg_price=float(avg),
                paid_fee=float(fee),
                slippage_bps=slippage_bps,
                adopted=adopted,
                expected_price=po.expected_price,
                reason=po.reason,
                strategy=_buy_strategy or None,
            )
            
            # Buy-fill callback (triage DCA 체결 확인 등)
            if self._buy_fill_callbacks and _buy_strategy:
                self._notify_buy_filled(
                    ctx=ctx, market=market, strategy=_buy_strategy,
                    entry_price=float(avg), qty=float(qty),
                    funds=float(funds), fee=float(fee),
                    reason=str(getattr(po, "reason", "") or ""),
                )

            # 텔레그램 알림 - 매수 체결 (UUID별 중복 방지)
            try:
                notified_key = f"fill_notified_{po.uuid}"
                already_notified = getattr(ctx, notified_key, False)
                if not already_notified:
                    setattr(ctx, notified_key, True)
                    coin = Q.extract_base(market)
                    send_telegram(
                        f"📈 [BUY] {market}\n"
                        f"• 수량: {qty:.6g} {coin}\n"
                        f"• 금액: {Q.format(funds)}\n"
                        f"• 체결가: {Q.format(avg, with_suffix=False)}"
                    )
            except (KeyError, AttributeError, TypeError):
                logger.warning("[OSM] telegram buy-fill notification failed", exc_info=True)

        else:
            # Pre-extract entry info before apply_fill_sell clears position
            _entry_price = 0.0
            _entry_ts = 0.0
            _strategy = ""
            try:
                _pos = getattr(ctx, "position", None) or {}
                _entry_price = float(
                    _pos.get("entry", 0)
                    or _pos.get("avg_price", 0)
                    or _pos.get("entry_price", 0)
                    or getattr(ctx, "avg_buy_price", 0)
                    or 0
                )
                _entry_ts = float(_pos.get("entry_ts", 0) or getattr(ctx, "entry_ts", 0) or 0)
                _strategy = str(getattr(ctx, "selected_strategy", "") or "")
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.warning("[recovered] sell_fill entry extraction market=%s: %s", market, e)

            fn = getattr(ctx, "apply_fill_sell", None)
            if callable(fn):
                try:
                    fn(avg_price=float(avg), qty=float(qty), funds=float(funds), fee=float(fee), source="bybit")
                except TypeError:
                    logger.warning("OrderStateMachine._apply_fill_and_log suppressed exception", exc_info=True)
                    fn(float(avg), float(qty), float(funds), float(fee))

            # 수익 계산 (pre-extracted entry_price 사용)
            entry_price = _entry_price
            profit_usdt = 0.0
            profit_pct = 0.0
            # 매수 시 수수료 추정 (entry_price 기반)
            buy_fee_est = entry_price * qty * 0.0005 if entry_price > 0 else 0.0
            net_profit_usdt = 0.0
            if entry_price > 0:
                profit_pct = (avg - entry_price) / entry_price * 100
                profit_usdt = (avg - entry_price) * qty
                net_profit_usdt = profit_usdt - float(fee) - buy_fee_est

            self.ledger.append(
                "FILL_SELL",
                market=market,
                uuid=po.uuid,
                side="ask",
                qty=float(qty),
                funds=float(funds),
                avg_price=float(avg),
                paid_fee=float(fee),
                slippage_bps=slippage_bps,
                expected_price=po.expected_price,
                reason=po.reason,
                profit_usdt=profit_usdt,
                profit_pct=profit_pct,
                strategy=(_strategy or "").strip().upper() or None,
            )
            
            # 텔레그램 알림 - 매도 체결 (UUID별 중복 방지)
            try:
                notified_key = f"fill_notified_{po.uuid}"
                already_notified = getattr(ctx, notified_key, False)
                if not already_notified:
                    setattr(ctx, notified_key, True)
                    coin = Q.extract_base(market)
                    profit_sign = "+" if net_profit_usdt >= 0 else ""
                    profit_emoji = "🟢" if net_profit_usdt >= 0 else "🔴"
                    msg = (
                        f"📉 [SELL] {market}\n"
                        f"• 수량: {qty:.6g} {coin}\n"
                        f"• 금액: {Q.format(funds)}\n"
                        f"• 매입가: {Q.format(entry_price, with_suffix=False)} → 체결가: {Q.format(avg, with_suffix=False)}"
                    )
                    if entry_price > 0:
                        total_fee = float(fee) + buy_fee_est
                        msg += (
                            f"\n💹 총 이윤: {profit_sign}{Q.format(profit_usdt)} ({profit_sign}{profit_pct:.2f}%)"
                            f"\n{profit_emoji} 순이익: {profit_sign}{Q.format(net_profit_usdt)}"
                            f"\n💰 수수료: {Q.format(total_fee)}"
                        )
                    else:
                        msg += "\n⚠️ 매입가 미상 — 수익 계산 불가"
                    # 보유 시간
                    if _entry_ts > 0:
                        _hold = time.time() - _entry_ts
                        if _hold >= 3600:
                            msg += f"\n⏱️ 보유: {_hold / 3600:.1f}시간"
                        else:
                            msg += f"\n⏱️ 보유: {_hold / 60:.0f}분"
                    # 전략 표시
                    if _strategy:
                        msg += f"\n📋 전략: {_strategy}"
                    send_telegram(msg)
            except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
                logger.warning("[OSM] telegram sell-fill notification failed", exc_info=True)

            # Sell-fill callbacks (autopilot loss tracking + online calibrator)
            if self._sell_fill_callbacks and _strategy:
                _hold_sec = (time.time() - _entry_ts) if _entry_ts > 0 else 0.0
                self._notify_sell_filled(
                    ctx=ctx, market=market, strategy=_strategy,
                    pnl_pct=profit_pct, entry_price=entry_price,
                    exit_price=float(avg), qty=float(qty), hold_sec=_hold_sec,
                )

        return slippage_bps

    def submit_limit_buy(
        self,
        *,
        ctx: Any,
        market: str,
        usdt_amount: float,
        limit_price: float,
        reason: str,
        attempts: int = 1,
        max_retries: Optional[int] = None,
        timeout_sec: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """지정가 매수(Limit Buy) 제출."""
        if getattr(ctx, "order_state", None) is not None:
            return False, "order_pending"

        if usdt_amount < self.min_order_usdt:
            return False, f"min_order_blocked:{usdt_amount:.0f}<{self.min_order_usdt:.0f}"

        if limit_price <= 0:
            return False, "invalid_price"

        limit_price = self._normalize_limit_price(limit_price)

        # Calculate qty from budget
        qty = usdt_amount / limit_price
        if (qty * limit_price) < self.min_order_usdt:
            return False, f"min_value_blocked:{(qty*limit_price):.0f}<{self.min_order_usdt:.0f}"

        try:
            res = self.client.place_order(
                market=market,
                side="bid",
                ord_type="limit",
                price=limit_price,
                volume=qty,
            )
            oid = str(res.get("uuid") or "")
        except Exception as e:
            logger.warning("OrderStateMachine.submit_limit_buy except: %s", e, exc_info=True)
            return False, f"place_order_failed:{e}"

        po = PendingOrder(
            uuid=oid,
            market=market,
            side="bid",
            ord_type="limit",
            requested_usdt=float(usdt_amount),
            requested_qty=qty,
            state="wait",
            created_ts=_now(),
            timeout_sec=float(timeout_sec if timeout_sec is not None else self.order_timeout_sec_buy),
            poll_interval=self.poll_interval,
            expected_price=float(limit_price),
            limit_price=float(limit_price),
            max_slippage_bps_soft=self.slip_soft,
            max_slippage_bps_hard=self.slip_hard,
            attempts=int(attempts),
            max_retries=int(max_retries if max_retries is not None else self.max_retries_buy),
            reason=str(reason),
        )

        ctx.order_state = po.to_dict()
        ctx.last_order_ts = _now()

        self.ledger.append(
            "ORDER_SUBMIT",
            market=market,
            uuid=oid,
            side="bid",
            ord_type="limit",
            usdt_amount=usdt_amount,
            qty=qty,
            price=limit_price,
            expected_price=po.expected_price,
            timeout_sec=po.timeout_sec,
            attempts=po.attempts,
            max_retries=po.max_retries,
            reason=po.reason,
        )
        self.ledger.append(
            "ORDER_ACK",
            market=market,
            uuid=oid,
            side="bid",
            ord_type="limit",
            state="wait",
            qty=qty,
            price=limit_price,
            reason=po.reason,
        )

        return True, oid
    def force_cancel_pending(self, *, ctx: Any, market: str, reason: str = "force_cancel") -> Dict[str, Any]:
        """강제 취소(베스트-에포트).

        - ctx.order_state 에 pending(uuid)가 있으면 cancel을 시도하고 state를 비운다.
        - 목적: TP limit-exit 등으로 pending 상태인 경우에도, 손절/강제청산/pp_exit를 즉시 실행할 수 있게 한다.

        주의:
        - 이미 체결 완료된 주문은 cancel이 실패할 수 있다. 이 경우 state를 비우지 않고,
          다음 process_pending()에서 정상 정리되도록 남겨둔다.
        """
        d = getattr(ctx, "order_state", None)
        if not isinstance(d, dict) or not d.get("uuid"):
            return {"cancelled": False, "cleared": False, "reason": "no_pending"}

        uuid = str(d.get("uuid") or "")
        if not uuid:
            return {"cancelled": False, "cleared": False, "reason": "no_uuid"}

        cancelled = False
        try:
            self.client.cancel_order(uuid=uuid)
            cancelled = True
            try:
                self.ledger.append("ORDER_FORCE_CANCEL_SENT", market=market, uuid=uuid, reason=reason)
            except (AttributeError, TypeError) as e:
                logger.warning("[recovered] ledger append ORDER_FORCE_CANCEL_SENT market=%s: %s", market, e)
        except Exception as exc:
            cancelled = False
            logger.error(
                "[CRITICAL] force_cancel_pending FAILED market=%s uuid=%s reason=%s: %s",
                market, uuid, reason, exc, exc_info=True,
            )

        if cancelled:
            try:
                ctx.order_state = {}
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                logger.warning("[recovered] order_state clear market=%s: %s", market, e)
                try:
                    d.clear()
                except (AttributeError, TypeError):
                    logger.warning("[OSM] force_cancel_pending: order_state dict clear failed", exc_info=True)
            try:
                if hasattr(ctx, "exit_pending"):
                    setattr(ctx, "exit_pending", False)
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning("[recovered] exit_pending reset market=%s: %s", market, e)

        return {"cancelled": bool(cancelled), "cleared": bool(cancelled), "uuid": uuid}



    def process_pending(self, *, ctx: Any, market: str, current_price: Optional[float] = None, current_bid: Optional[float] = None) -> Dict[str, Any]:
        """pending order가 있으면 상태를 진행시킨다."""

        d = getattr(ctx, "order_state", None)
        if not isinstance(d, dict) or not d.get("uuid"):
            return {"progressed": False, "done": False}

        po = PendingOrder.from_dict(d)

        now = _now()
        if po.last_check_ts and (now - po.last_check_ts) < po.poll_interval:
            return {"progressed": False, "done": False}

        po.last_check_ts = now

        # 1) 조회
        try:
            order = self.client.get_order(uuid=po.uuid, market=market)
            st, ev, funds, ap, fee = self._summarize_order_fields(order)

            po.state = st
            po.executed_volume = float(ev or 0.0)
            po.funds = float(funds or 0.0)
            po.avg_price = ap
            po.paid_fee = float(fee or 0.0)

            ctx.order_state = po.to_dict()

        except (TypeError, ValueError) as exc:
            logger.warning("OrderStateMachine.process_pending except: %s", exc, exc_info=True)
            po.last_error = str(exc)
            ctx.order_state = po.to_dict()
            self.ledger.append("ORDER_POLL_ERROR", market=market, uuid=po.uuid, error=str(exc), side=po.side)
            # 폴링 오류는 즉시 stop하지 않는다.
            return {"progressed": True, "done": False, "needs_emergency_stop": False, "reason": "poll_error"}

        # 2) done/cancel 이면 fill 적용
        if po.state in ("done", "cancel"):
            slip = self._apply_fill_and_log(ctx=ctx, po=po, market=market)

            # 원장: 주문 종료 요약(체결 0 포함)
            try:
                age_sec = max(0.0, float(now) - float(po.created_ts or 0.0))
            except (TypeError, ValueError):
                logger.warning("OrderStateMachine.process_pending suppressed exception", exc_info=True)
                age_sec = 0.0

            self.ledger.append(
                "ORDER_FINAL",
                market=market,
                uuid=po.uuid,
                side=po.side,
                state=po.state,
                executed_volume=float(po.executed_volume or 0.0),
                funds=float(po.funds or 0.0),
                avg_price=po.avg_price,
                paid_fee=float(po.paid_fee or 0.0),
                expected_price=po.expected_price,
                slippage_bps=slip,
                age_sec=age_sec,
                attempts=int(po.attempts or 1),
                max_retries=int(po.max_retries or 0),
                timeout_sec=float(po.timeout_sec or 0.0),
                reason=po.reason,
            )

            # slippage hard
            if slip is not None and slip >= po.max_slippage_bps_hard:
                self.ledger.append(
                    "SLIPPAGE_HARD_BREACH",
                    market=market,
                    uuid=po.uuid,
                    side=po.side,
                    expected_price=po.expected_price,
                    avg_price=po.avg_price,
                    slippage_bps=slip,
                )

                # order는 끝났으므로 pending 제거
                ctx.order_state = None

                # exit는 전역 정지(+회수 승격) / entry는 시장 단위 진입 쿨다운
                if po.side == "ask":
                    return {
                        "progressed": True,
                        "done": True,
                        "needs_emergency_stop": True,
                        "needs_recovery": bool(self.exit_fail_to_recovery),
                        "reason": "slippage_hard_exit",
                    }

                # entry hard → market entry cooldown
                try:
                    ctx.entry_block_until_ts = _now() + float(self.entry_slippage_hard_cooldown_sec)
                except (TypeError, ValueError) as e:
                    logger.warning("[recovered] entry_block_until_ts set (slippage hard) market=%s: %s", market, e)

                self.ledger.append(
                    "ENTRY_BLOCKED_SLIPPAGE",
                    market=market,
                    uuid=po.uuid,
                    slippage_bps=slip,
                    cooldown_sec=float(self.entry_slippage_hard_cooldown_sec),
                )
                return {
                    "progressed": True,
                    "done": True,
                    "needs_emergency_stop": False,
                    "needs_recovery": False,
                    "reason": "slippage_hard_entry",
                }

            # pending 제거
            ctx.order_state = None
            return {"progressed": True, "done": True, "needs_emergency_stop": False}

        # 3) wait 상태인데 timeout 초과 → cancel/재시도
        age = now - po.created_ts
        if age >= po.timeout_sec:
            self.ledger.append(
                "ORDER_TIMEOUT",
                market=market,
                uuid=po.uuid,
                side=po.side,
                age_sec=age,
                attempts=po.attempts,
                max_retries=po.max_retries,
                timeout_sec=po.timeout_sec,
            )

            # cancel 시도
            cancel_sent = False
            try:
                self.client.cancel_order(uuid=po.uuid)
                cancel_sent = True
                self.ledger.append("ORDER_CANCEL_SENT", market=market, uuid=po.uuid, side=po.side)
            except Exception as exc:
                logger.warning("OrderStateMachine.process_pending except: %s", exc, exc_info=True)
                po.last_error = f"cancel_failed:{exc}"
                self.ledger.append("ORDER_CANCEL_ERROR", market=market, uuid=po.uuid, side=po.side, error=str(exc))

            # cancel 이후 재조회
            try:
                order = self.client.get_order(uuid=po.uuid, market=market)
                st, ev, funds, ap, fee = self._summarize_order_fields(order)
                po.state = st
                po.executed_volume = float(ev or 0.0)
                po.funds = float(funds or 0.0)
                po.avg_price = ap
                po.paid_fee = float(fee or 0.0)
            except (TypeError, ValueError) as exc:
                logger.warning("OrderStateMachine.process_pending except: %s", exc, exc_info=True)
                po.last_error = f"post_cancel_poll_failed:{exc}"

            # cancel 요청을 보냈지만 아직 wait이면: 중복 주문 방지 위해 pending 유지
            if po.state not in ("done", "cancel"):
                if cancel_sent:
                    # 다음 timeout까지 대기(취소 반영 시간)
                    po.created_ts = _now()
                ctx.order_state = po.to_dict()
                return {"progressed": True, "done": False, "needs_emergency_stop": False, "reason": "wait_after_cancel"}

            # 여기부터는 order가 끝났다고 판단 → fill 반영 후 잔량 retry
            slip = self._apply_fill_and_log(ctx=ctx, po=po, market=market)

            # 원장: 주문 종료 요약(체결 0 포함)
            try:
                age_sec = max(0.0, float(now) - float(po.created_ts or 0.0))
            except (TypeError, ValueError):
                logger.warning("OrderStateMachine.process_pending suppressed exception", exc_info=True)
                age_sec = 0.0

            self.ledger.append(
                "ORDER_FINAL",
                market=market,
                uuid=po.uuid,
                side=po.side,
                state=po.state,
                executed_volume=float(po.executed_volume or 0.0),
                funds=float(po.funds or 0.0),
                avg_price=po.avg_price,
                paid_fee=float(po.paid_fee or 0.0),
                expected_price=po.expected_price,
                slippage_bps=slip,
                age_sec=age_sec,
                attempts=int(po.attempts or 1),
                max_retries=int(po.max_retries or 0),
                timeout_sec=float(po.timeout_sec or 0.0),
                reason=po.reason,
            )

            # 남은 잔량 계산
            remaining_usdt = 0.0
            remaining_qty = 0.0
            if po.side == "bid":
                remaining_usdt = max(0.0, float(po.requested_usdt) - float(po.funds))
            else:
                remaining_qty = max(0.0, float(po.requested_qty) - float(po.executed_volume))

            # hard slippage 처리(Timeout finalize에서도 동일)
            if slip is not None and slip >= po.max_slippage_bps_hard:
                self.ledger.append(
                    "SLIPPAGE_HARD_BREACH",
                    market=market,
                    uuid=po.uuid,
                    side=po.side,
                    expected_price=po.expected_price,
                    avg_price=po.avg_price,
                    slippage_bps=slip,
                )
                ctx.order_state = None

                if po.side == "ask":
                    return {
                        "progressed": True,
                        "done": True,
                        "needs_emergency_stop": True,
                        "needs_recovery": bool(self.exit_fail_to_recovery),
                        "reason": "slippage_hard_exit",
                    }

                # entry hard → market entry cooldown
                try:
                    ctx.entry_block_until_ts = _now() + float(self.entry_slippage_hard_cooldown_sec)
                except (TypeError, ValueError) as e:
                    logger.warning("[recovered] entry_block_until_ts set (slippage hard) market=%s: %s", market, e)

                self.ledger.append(
                    "ENTRY_BLOCKED_SLIPPAGE",
                    market=market,
                    uuid=po.uuid,
                    slippage_bps=slip,
                    cooldown_sec=float(self.entry_slippage_hard_cooldown_sec),
                )
                return {
                    "progressed": True,
                    "done": True,
                    "needs_emergency_stop": False,
                    "needs_recovery": False,
                    "reason": "slippage_hard_entry",
                }

            # 이전 pending 제거 (중복 주문 방지)
            ctx.order_state = None

            # 재시도 조건
            if po.attempts < po.max_retries:
                next_attempt = int(po.attempts) + 1

                if po.side == "bid":
                    if po.ord_type == "limit":
                        _market_fallback = str(os.getenv("OMA_ENTRY_LIMIT_MARKET_FALLBACK", "1")).strip().lower() in ("1", "true", "yes", "on")
                        if _market_fallback and remaining_usdt >= self.min_order_usdt:
                            self.ledger.append(
                                "ENTRY_LIMIT_UNFILLED",
                                market=market,
                                uuid=po.uuid,
                                remaining_usdt=remaining_usdt,
                                reason="limit_buy_market_fallback",
                            )
                            ok, msg = self.submit_market_buy(
                                ctx=ctx,
                                market=market,
                                usdt_amount=remaining_usdt,
                                expected_price=current_price or po.expected_price,
                                reason=f"market_fallback:{po.reason}",
                                attempts=next_attempt,
                                max_retries=0,
                            )
                            if ok:
                                self.ledger.append(
                                    "ORDER_RETRY",
                                    market=market,
                                    prev_uuid=po.uuid,
                                    new_uuid=str(msg),
                                    side=po.side,
                                    remaining_usdt=remaining_usdt,
                                    attempt=next_attempt,
                                    fallback="market",
                                )
                                return {"progressed": True, "done": False, "reason": "limit_to_market_fallback"}
                        self.ledger.append(
                            "ENTRY_LIMIT_UNFILLED",
                            market=market,
                            uuid=po.uuid,
                            remaining_usdt=remaining_usdt,
                            reason="limit_buy_no_retry",
                        )
                        try:
                            cooldown_sec = float(os.getenv("OMA_ENTRY_LIMIT_COOLDOWN_SEC", "30") or 30)
                            ctx.entry_block_until_ts = _now() + cooldown_sec
                        except (TypeError, ValueError) as e:
                            logger.warning("[recovered] limit_buy cooldown set market=%s: %s", market, e)
                        return {"progressed": True, "done": True, "reason": "entry_limit_unfilled"}
                    
                    # 시장가 BUY 재시도 (기존 로직)
                    if remaining_usdt >= self.min_order_usdt:
                        ok, msg = self.submit_market_buy(
                            ctx=ctx,
                            market=market,
                            usdt_amount=remaining_usdt,
                            expected_price=current_price or po.expected_price,
                            reason=f"retry:{po.reason}",
                            attempts=next_attempt,
                            max_retries=po.max_retries,
                        )
                        if ok:
                            self.ledger.append(
                                "ORDER_RETRY",
                                market=market,
                                prev_uuid=po.uuid,
                                new_uuid=str(msg),
                                side=po.side,
                                remaining_usdt=remaining_usdt,
                                attempt=next_attempt,
                            )
                            return {"progressed": True, "done": False, "reason": "retry_buy"}

                else:
                    if remaining_qty > 0:
                        if po.ord_type == "limit":
                            # PATCH 2025-12-26: retry LIMIT ask at latest best_bid (or last price fallback)
                            new_price = float(current_bid) if current_bid is not None else float(current_price or 0.0)
                            if new_price <= 0:
                                ok, msg = False, "retry_price_unavailable"
                            else:
                                ok, msg = self.submit_limit_sell(
                                    ctx=ctx,
                                    market=market,
                                    qty=remaining_qty,
                                    limit_price=new_price,
                                    expected_price=new_price,
                                    reason=f"retry:{po.reason}",
                                    attempts=next_attempt,
                                    max_retries=po.max_retries,
                                    timeout_sec=po.timeout_sec,
                                )
                        else:
                            ok, msg = self.submit_market_sell(
                                ctx=ctx,
                                market=market,
                                qty=remaining_qty,
                                expected_price=current_price or po.expected_price,
                                reason=f"retry:{po.reason}",
                                attempts=next_attempt,
                                max_retries=po.max_retries,
                            )
                        if ok:
                            self.ledger.append(
                                "ORDER_RETRY",
                                market=market,
                                prev_uuid=po.uuid,
                                new_uuid=str(msg),
                                side=po.side,
                                remaining_qty=remaining_qty,
                                attempt=next_attempt,
                            )
                            return {"progressed": True, "done": False, "reason": "retry_sell"}

            # 재시도 불가 → 안전 정지/회수 모드 승격
            if po.side == "ask":
                self.ledger.append("EXIT_UNRESOLVED", market=market, reason="max_retries_reached")
                return {
                    "progressed": True,
                    "done": False,
                    "needs_emergency_stop": True,
                    "needs_recovery": bool(self.exit_fail_to_recovery),
                    "reason": "exit_unresolved",
                }

            # entry 실패는 전역 emergency로 막지 않는다. 대신 market entry cooldown.
            try:
                ctx.entry_block_until_ts = _now() + float(self.entry_unresolved_cooldown_sec)
            except (TypeError, ValueError) as e:
                logger.warning("[recovered] entry_block_until_ts set (unresolved) market=%s: %s", market, e)

            self.ledger.append(
                "ENTRY_UNRESOLVED",
                market=market,
                reason="max_retries_reached",
                cooldown_sec=float(self.entry_unresolved_cooldown_sec),
            )
            return {
                "progressed": True,
                "done": False,
                "needs_emergency_stop": False,
                "needs_recovery": False,
                "reason": "entry_unresolved",
            }

        # wait인데 아직 timeout 전
        ctx.order_state = po.to_dict()
        return {"progressed": True, "done": False, "reason": "wait"}
