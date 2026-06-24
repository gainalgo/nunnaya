# -*- coding: utf-8 -*-
"""Paper Trading Client — BybitTradeClient 동일 인터페이스, 모의 체결.

PaperTradeClient는 실제 거래소 API를 호출하지 않고,
현재 시장가 기반으로 즉시 체결을 시뮬레이션합니다.
모든 전략이 LIVE 모드와 동일하게 동작하되, 실제 주문은 나가지 않습니다.

Usage:
    client = PaperTradeClient(initial_usdt=1000.0)
    # OrderStateMachine에 BybitTradeClient 대신 주입
    osm = OrderStateMachine(client=client, ledger=ledger)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_FEE_RATE = 0.001  # 0.1% taker fee (Bybit 기본)
_STATE_FILE = "paper_positions.json"


class PaperTradeClient:
    """BybitTradeClient 프로토콜 호환 모의 거래 클라이언트."""

    def __init__(
        self,
        initial_usdt: float = 1000.0,
        fee_rate: float = _DEFAULT_FEE_RATE,
        state_dir: str = "",
        slippage_bps: float = 0.0,
    ):
        self._fee_rate = float(fee_rate)
        # ★ [2026-06-24] paper 슬리피지(편도 bps) — 매수=비싸게/매도=싸게 체결로 가정해 live 근접.
        self._slip_bps = max(0.0, float(slippage_bps))
        self._lock = threading.Lock()
        self._state_file = os.path.join(
            state_dir or os.path.join(os.getcwd(), "runtime"),
            _STATE_FILE,
        )

        # 상태 초기화
        self._usdt_balance: float = float(initial_usdt)
        self._coin_balances: Dict[str, float] = {}  # {"BTC": 0.005, ...}
        self._order_history: List[Dict[str, Any]] = []
        self._initial_usdt = float(initial_usdt)
        self._created_ts = time.time()

        # 기존 상태 로드 시도
        self._load_state()

        # API 통계 (BybitTradeClient 호환)
        self._api_call_count = 0
        self._api_call_reset_ts = time.time()

        logger.info(
            "[PaperTrade] Initialized: balance=%.2f USDT, coins=%d, fee=%.3f%%",
            self._usdt_balance, len(self._coin_balances), self._fee_rate * 100,
        )

    # ================================================================
    # State Persistence
    # ================================================================
    def _load_state(self):
        """runtime/paper_positions.json에서 상태 복원."""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._usdt_balance = float(data.get("usdt_balance", self._initial_usdt))
                self._coin_balances = {
                    k: float(v) for k, v in (data.get("coin_balances") or {}).items()
                    if float(v) > 0
                }
                self._order_history = list(data.get("order_history") or [])[-500:]
                self._created_ts = float(data.get("created_ts", time.time()))
                logger.info(
                    "[PaperTrade] State loaded: %.2f USDT, %d coins, %d orders",
                    self._usdt_balance, len(self._coin_balances), len(self._order_history),
                )
        except Exception as e:
            logger.warning("[PaperTrade] State load failed, using initial: %s", e)

    def _save_state(self):
        """상태를 JSON으로 저장 (thread-safe)."""
        try:
            from app.core.io_utils import safe_write_json
            data = {
                "usdt_balance": round(self._usdt_balance, 4),
                "coin_balances": {k: round(v, 8) for k, v in self._coin_balances.items() if v > 0},
                "order_history": self._order_history[-500:],
                "created_ts": self._created_ts,
                "last_updated_ts": time.time(),
                "initial_usdt": self._initial_usdt,
            }
            safe_write_json(self._state_file, data)
        except Exception as e:
            logger.warning("[PaperTrade] State save failed: %s", e)

    # ================================================================
    # Price Lookup
    # ================================================================
    def _get_current_price(self, market: str) -> float:
        """price_store에서 현재가 조회."""
        try:
            from app.core.hyper_price_store import price_store
            p = price_store.get_price(market)
            if p and p > 0:
                return float(p)
        except Exception:
            pass
        logger.warning("[PaperTrade] No price for %s", market)
        return 0.0

    def _normalize_symbol(self, market: str) -> str:
        m = str(market or "").strip().upper()
        if not m.endswith("USDT"):
            m = m + "USDT"
        return m

    def _base_currency(self, market: str) -> str:
        m = self._normalize_symbol(market)
        return m.replace("USDT", "")

    # ================================================================
    # Order Simulation
    # ================================================================
    def _make_order_response(
        self, market: str, side: str, price: float,
        qty: float, cost: float, fee: float,
    ) -> Dict[str, Any]:
        """BybitTradeClient._convert_order() 와 동일한 포맷."""
        oid = f"PAPER-{uuid.uuid4().hex[:12]}"
        return {
            "uuid": oid,
            "side": side,  # "bid" or "ask"
            "ord_type": "market",
            "price": str(round(price, 8)),
            "state": "done",
            "market": self._normalize_symbol(market),
            "volume": str(round(cost if side == "bid" else qty, 8)),
            "remaining_volume": "0",
            "executed_volume": str(round(qty, 8)),
            "avg_price": str(round(price, 8)),
            "trades_count": 1,
            "created_at": str(int(time.time() * 1000)),
            "paid_fee": str(round(fee, 8)),
            "fee_currency": "USDT",
            "_raw": {"paper": True},
        }

    # ================================================================
    # TradeClient Protocol — 핵심 메서드
    # ================================================================
    def market_buy(self, market: str, amount: float, **kw) -> Dict[str, Any]:
        """시장가 매수 시뮬레이션 (amount = USDT 금액)."""
        return self.market_buy_usdt(market, amount, **kw)

    def market_buy_usdt(self, market: str, amount: float, **kw) -> Dict[str, Any]:
        """USDT 기준 시장가 매수."""
        symbol = self._normalize_symbol(market)
        price = self._get_current_price(symbol)
        if price <= 0:
            raise RuntimeError(f"[PaperTrade] No price for {symbol}")

        price *= (1.0 + self._slip_bps / 10000.0)  # ★ paper 슬리피지 — 매수 불리(비싸게) 체결
        amount = float(amount)
        fee = amount * self._fee_rate
        net_amount = amount - fee
        qty = net_amount / price

        with self._lock:
            if self._usdt_balance < amount:
                raise RuntimeError(
                    f"[PaperTrade] Insufficient balance: need {amount:.2f}, have {self._usdt_balance:.2f}"
                )
            self._usdt_balance -= amount
            base = self._base_currency(symbol)
            self._coin_balances[base] = self._coin_balances.get(base, 0.0) + qty

            order = self._make_order_response(symbol, "bid", price, qty, amount, fee)
            self._order_history.append(order)
            self._save_state()

        logger.info(
            "[PaperTrade] BUY %s: %.4f @ $%.4f = $%.2f (fee $%.4f) | bal=$%.2f",
            symbol, qty, price, amount, fee, self._usdt_balance,
        )
        return order

    def market_sell(self, market: str, qty: float, **kw) -> Dict[str, Any]:
        """시장가 매도 시뮬레이션 (qty = 코인 수량)."""
        return self.market_sell_qty(market, qty, **kw)

    def market_sell_qty(self, market: str, qty: float, **kw) -> Dict[str, Any]:
        """수량 기준 시장가 매도."""
        symbol = self._normalize_symbol(market)
        price = self._get_current_price(symbol)
        if price <= 0:
            raise RuntimeError(f"[PaperTrade] No price for {symbol}")

        price *= (1.0 - self._slip_bps / 10000.0)  # ★ paper 슬리피지 — 매도 불리(싸게) 체결
        qty = float(qty)
        proceeds = qty * price
        fee = proceeds * self._fee_rate
        net_proceeds = proceeds - fee

        with self._lock:
            base = self._base_currency(symbol)
            held = self._coin_balances.get(base, 0.0)
            if held < qty * 0.999:  # 0.1% 오차 허용
                raise RuntimeError(
                    f"[PaperTrade] Insufficient {base}: need {qty:.8f}, have {held:.8f}"
                )
            self._coin_balances[base] = max(0.0, held - qty)
            if self._coin_balances[base] < 1e-10:
                del self._coin_balances[base]
            self._usdt_balance += net_proceeds

            order = self._make_order_response(symbol, "ask", price, qty, proceeds, fee)
            self._order_history.append(order)
            self._save_state()

        logger.info(
            "[PaperTrade] SELL %s: %.4f @ $%.4f = $%.2f (fee $%.4f) | bal=$%.2f",
            symbol, qty, price, proceeds, fee, self._usdt_balance,
        )
        return order

    def market_sell_usdt(self, market: str, quote_amount: float, **kw) -> Dict[str, Any]:
        """USDT 금액 기준 매도."""
        symbol = self._normalize_symbol(market)
        price = self._get_current_price(symbol)
        if price <= 0:
            raise RuntimeError(f"[PaperTrade] No price for {symbol}")
        qty = quote_amount / price
        return self.market_sell_qty(market, qty, **kw)

    # ================================================================
    # Limit Orders (즉시 체결로 시뮬레이션)
    # ================================================================
    def limit_buy(self, market: str, price: float, volume: float, **kw) -> Dict[str, Any]:
        """지정가 매수 → 즉시 체결로 시뮬레이션."""
        cost = float(price) * float(volume)
        return self.market_buy_usdt(market, cost, **kw)

    def limit_sell(self, market: str, price: float, volume: float, **kw) -> Dict[str, Any]:
        """지정가 매도 → 즉시 체결로 시뮬레이션."""
        return self.market_sell_qty(market, volume, **kw)

    # ================================================================
    # Place Order (통합 인터페이스)
    # ================================================================
    def place_order(self, *, market, side, ord_type, volume=None, price=None, **kw) -> Dict[str, Any]:
        s = str(side).lower()
        is_buy = s in ("bid", "buy", "long")

        if is_buy:
            if volume and price:
                return self.limit_buy(market, float(price), float(volume))
            elif volume:
                return self.market_buy_usdt(market, float(volume))
            else:
                raise ValueError("[PaperTrade] place_order buy: volume required")
        else:
            if volume:
                return self.market_sell_qty(market, float(volume))
            else:
                raise ValueError("[PaperTrade] place_order sell: volume required")

    # ================================================================
    # Account / Balance
    # ================================================================
    def accounts(self, *, skip_currencies=None, **kw) -> List[Dict[str, Any]]:
        """계좌 잔고 조회 (BybitTradeClient.accounts() 호환)."""
        skip = set(skip_currencies or [])
        result = []
        with self._lock:
            if "USDT" not in skip:
                result.append({
                    "currency": "USDT",
                    "balance": str(round(self._usdt_balance, 4)),
                    "locked": "0",
                    "avg_buy_price": "0",
                    "unit_currency": "USDT",
                })
            for coin, qty in sorted(self._coin_balances.items()):
                if coin in skip or qty <= 0:
                    continue
                result.append({
                    "currency": coin,
                    "balance": str(round(qty, 8)),
                    "locked": "0",
                    "avg_buy_price": "0",
                    "unit_currency": "USDT",
                })
        return result

    def get_balance(self, currency: str, *, include_locked: bool = False) -> float:
        cur = str(currency).upper()
        with self._lock:
            if cur == "USDT":
                return self._usdt_balance
            return self._coin_balances.get(cur, 0.0)

    # ================================================================
    # Order Query
    # ================================================================
    def get_order(self, *, uuid: str, market=None) -> Dict[str, Any]:
        with self._lock:
            for o in reversed(self._order_history):
                if o.get("uuid") == uuid:
                    return o
        return {"uuid": uuid, "state": "done", "market": market or "", "_raw": {"paper": True}}

    def list_orders(self, *, state="wait", market=None, limit=50, **kw) -> List[Dict[str, Any]]:
        """대기 주문 조회 — Paper는 즉시 체결이므로 항상 빈 리스트."""
        return []

    def list_done_orders(self, *, market=None, **kw) -> List[Dict[str, Any]]:
        with self._lock:
            orders = list(self._order_history[-50:])
        if market:
            m = self._normalize_symbol(market)
            orders = [o for o in orders if o.get("market") == m]
        return orders

    def cancel_order(self, *, uuid: str, market=None) -> Dict[str, Any]:
        """Paper 주문 취소 — 이미 체결됐으므로 no-op."""
        return self.get_order(uuid=uuid, market=market)

    def wait_order(self, *, uuid: str, market=None, timeout_sec=30.0, poll_interval=1.0) -> Dict[str, Any]:
        """Paper 주문 대기 — 즉시 체결이므로 바로 반환."""
        return self.get_order(uuid=uuid, market=market)

    # ================================================================
    # Instrument Info
    # ================================================================
    def get_min_order_amount(self, symbol: str) -> Dict[str, Any]:
        try:
            from app.integrations.bybit_instrument_cache import BybitInstrumentCache
            sym = self._normalize_symbol(symbol)
            return {
                "min_amount": BybitInstrumentCache.get_min_qty(sym),
                "min_cost": BybitInstrumentCache.get_min_notional(sym),
                "min_price": 0.0,
            }
        except Exception:
            return {"min_amount": 0.001, "min_cost": 5.0, "min_price": 0.0}

    def get_order_chance(self, market: str) -> Dict[str, Any]:
        return {"balance": self.accounts()}

    # ================================================================
    # API Stats (BybitTradeClient 호환)
    # ================================================================
    def get_api_stats(self) -> Dict[str, Any]:
        elapsed = time.time() - self._api_call_reset_ts
        return {
            "calls_per_min": 0,
            "seconds_elapsed": int(elapsed),
            "projected_per_min": 0,
            "paper_mode": True,
        }

    # ================================================================
    # Futures No-ops (BybitTradeClient 호환)
    # ================================================================
    def set_leverage(self, symbol: str, leverage: int = 1, **kw):
        logger.debug("[PaperTrade] set_leverage(%s, %d) — no-op", symbol, leverage)
        return {"retCode": 0}

    def switch_position_mode(self, mode: str = "BothSide", **kw):
        logger.debug("[PaperTrade] switch_position_mode(%s) — no-op", mode)
        return {"retCode": 0}

    def get_positions(self, symbol: str = "", **kw) -> List[Dict[str, Any]]:
        """가상 포지션 조회."""
        results = []
        with self._lock:
            for coin, qty in self._coin_balances.items():
                if qty <= 0:
                    continue
                sym = coin + "USDT"
                if symbol and sym != self._normalize_symbol(symbol):
                    continue
                price = self._get_current_price(sym)
                results.append({
                    "symbol": sym,
                    "side": "Buy",
                    "size": str(qty),
                    "positionValue": str(round(qty * price, 4)),
                    "avgPrice": str(round(price, 4)),
                    "unrealisedPnl": "0",
                    "leverage": "1",
                    "positionIdx": "1",
                })
        return results

    @staticmethod
    def adjust_price_to_tick(price, side=None, *, symbol=""):
        from app.integrations.bybit_trade import adjust_price_to_tick
        return adjust_price_to_tick(price, side, symbol=symbol)

    def summarize_order(self, o: Dict) -> str:
        try:
            return (
                f"uuid={o.get('uuid','')} {o.get('market','')} "
                f"{o.get('side','')} state={o.get('state','')} "
                f"price={o.get('price','')} vol={o.get('volume','')}"
            )
        except Exception:
            return str(o)

    # ================================================================
    # Paper-specific 관리
    # ================================================================
    def reset(self, initial_usdt: float = 0.0):
        """Paper 잔고 초기화."""
        with self._lock:
            self._usdt_balance = initial_usdt or self._initial_usdt
            self._coin_balances.clear()
            self._order_history.clear()
            self._created_ts = time.time()
            self._save_state()
        logger.info("[PaperTrade] Reset to $%.2f", self._usdt_balance)

    def get_summary(self) -> Dict[str, Any]:
        """Paper 거래 요약."""
        with self._lock:
            total_value = self._usdt_balance
            positions = []
            for coin, qty in self._coin_balances.items():
                price = self._get_current_price(coin + "USDT")
                value = qty * price
                total_value += value
                positions.append({
                    "coin": coin, "qty": round(qty, 8),
                    "price": round(price, 4), "value": round(value, 2),
                })
            return {
                "mode": "PAPER",
                "initial_usdt": self._initial_usdt,
                "usdt_balance": round(self._usdt_balance, 4),
                "total_value": round(total_value, 4),
                "pnl": round(total_value - self._initial_usdt, 4),
                "pnl_pct": round((total_value / self._initial_usdt - 1) * 100, 2) if self._initial_usdt > 0 else 0,
                "positions": positions,
                "total_trades": len(self._order_history),
                "created_ts": self._created_ts,
            }
