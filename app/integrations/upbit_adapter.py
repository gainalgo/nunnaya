"""Upbit Exchange Adapter — 현물(Spot) / KRW 마켓.

`exchange_factory.create_exchange_adapter("UPBIT")` 로 생성.
FOCUS 엔진은 `trade_client`(UpbitTradeClient)를 직접 쓰지만, 이 어댑터는
거래소-통합 레이어(`ExchangeAdapter`) 정합성을 위해 제공한다.
"""
import logging
from decimal import Decimal
from typing import List, Optional

from app.integrations.exchange_adapter import (
    ExchangeAdapter, MarketInfo, TickerInfo,
    BalanceInfo, OrderResult, OrderSide, OrderStatus,
)
from app.integrations.upbit_trade import UpbitTradeClient, to_upbit_market, base_currency

logger = logging.getLogger(__name__)


class UpbitAdapter(ExchangeAdapter):
    # 현물 = 청산 없음. SLArbiter/청산 모듈이 거래소 분기에 쓰는 선택자(DESIGN §4.2).
    has_liquidation = False

    def __init__(self, access_key: str = "", secret_key: str = ""):
        self.trade_client = UpbitTradeClient(access_key, secret_key)
        self._exchange_name = "UPBIT"

    @property
    def exchange_name(self):
        return self._exchange_name

    def get_name(self):
        return self._exchange_name

    def get_quote_currency(self):
        return "KRW"

    # ── 시세 ────────────────────────────────────────────────
    def get_markets(self):
        try:
            data = self.trade_client.get_all_markets()
            return [MarketInfo(exchange=self._exchange_name, symbol=m.get("market", ""),
                               base_currency=base_currency(m.get("market", "")), quote_currency="KRW",
                               min_order_size=5000.0, tradable=True)
                    for m in data]
        except Exception as e:
            logger.error("Failed to get Upbit markets: %s", e)
            return []

    def get_ticker(self, market):
        res = self.get_tickers([market])
        return res[0] if res else None

    def get_tickers(self, markets=None):
        try:
            mlist = markets or [m.get("market") for m in self.trade_client.get_all_markets()]
            tickers = self.trade_client.get_tickers(mlist)
            return [TickerInfo(exchange=self._exchange_name, market_code=t.get("market", ""),
                               current_price=Decimal(str(t.get("trade_price", 0))),
                               bid_price=Decimal(str(t.get("trade_price", 0))),
                               ask_price=Decimal(str(t.get("trade_price", 0))),
                               volume_24h=Decimal(str(t.get("acc_trade_volume_24h", 0) or 0)),
                               change_24h_pct=Decimal(str(t.get("signed_change_rate", 0) or 0)) * 100,
                               high_24h=Decimal(str(t.get("high_price", 0) or 0)),
                               low_24h=Decimal(str(t.get("low_price", 0) or 0)),
                               timestamp=int(t.get("timestamp", 0) or 0)) for t in tickers]
        except Exception as e:
            logger.error("Upbit ticker error: %s", e)
            return []

    def get_orderbook(self, market):
        return None

    # ── 잔고 ────────────────────────────────────────────────
    def get_balances(self, currency=None):
        try:
            data = self.trade_client.accounts()
            return [BalanceInfo(exchange=self._exchange_name, currency=b.get("currency", ""),
                                available=Decimal(str(b.get("balance", 0))),
                                locked=Decimal(str(b.get("locked", 0))),
                                total=Decimal(str(b.get("balance", 0))) + Decimal(str(b.get("locked", 0))))
                    for b in data if not currency or b.get("currency") == currency]
        except Exception as e:
            logger.error("Upbit balance error: %s", e)
            return []

    def get_balance(self, currency):
        res = self.get_balances(currency)
        return res[0] if res else None

    # ── 주문 ────────────────────────────────────────────────
    def buy_market_order(self, market, volume=None, price=None):
        # Upbit 시장가 매수 = KRW 금액 기준. price(금액) 우선, 없으면 volume 을 금액으로 취급.
        amount = price if price is not None else volume
        od = self.trade_client.market_buy(market, float(amount))
        return self._to_result(od, market, OrderSide.BUY)

    def sell_market_order(self, market, volume):
        od = self.trade_client.market_sell(market, float(volume))
        return self._to_result(od, market, OrderSide.SELL)

    def buy_limit_order(self, market, price, volume):
        od = self.trade_client.limit_buy(market, price, volume)
        return self._to_result(od, market, OrderSide.BUY)

    def sell_limit_order(self, market, price, volume):
        od = self.trade_client.limit_sell(market, price, volume)
        return self._to_result(od, market, OrderSide.SELL)

    def cancel_order(self, uuid):
        try:
            self.trade_client.cancel_order(uuid=uuid)
            return {"success": True, "uuid": uuid}
        except Exception as exc:
            logger.error("cancel_order FAILED uuid=%s: %s", uuid, exc)
            return {"success": False, "uuid": uuid, "error": str(exc)}

    def get_order(self, uuid):
        try:
            return self.trade_client.get_order(uuid=uuid)
        except Exception as exc:
            logger.error("get_order FAILED uuid=%s: %s", uuid, exc)
            return None

    def get_orders(self, market=None, state=None, limit=100):
        return []

    def _to_result(self, od, market, side):
        try:
            return OrderResult(
                exchange=self._exchange_name, order_id=od.get("uuid", ""),
                market_code=to_upbit_market(market), side=side, order_type="market",
                price=None, amount=Decimal(str(od.get("volume", 0) or 0)),
                filled_amount=Decimal(str(od.get("executed_volume", 0) or 0)),
                status=self._map_state(od.get("state", "")),
                timestamp=od.get("created_at", ""), raw_data=od,
                created_at=od.get("created_at", ""))
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error("Order PLACED but parse failed %s: %s (raw=%s)", market, e, od, exc_info=True)
            return OrderResult(
                exchange=self._exchange_name, order_id=od.get("uuid", "") if isinstance(od, dict) else "",
                market_code=to_upbit_market(market), side=side, order_type="market",
                price=None, amount=Decimal("0"), filled_amount=Decimal("0"),
                status=OrderStatus.PENDING, timestamp="", raw_data=od if isinstance(od, dict) else {},
                created_at="")

    def _map_state(self, state):
        return {"wait": OrderStatus.PENDING, "done": OrderStatus.FILLED,
                "cancel": OrderStatus.CANCELLED}.get(state, OrderStatus.FAILED)

    # ── 유틸 ────────────────────────────────────────────────
    def normalize_market_code(self, market, base_currency="KRW"):
        return to_upbit_market(market)

    def parse_market_code(self, market_code):
        mk = to_upbit_market(market_code)
        if "-" in mk:
            quote, base = mk.split("-", 1)
            return (base, quote)
        return (mk, "KRW")

    def get_fee_rate(self, order_type="market"):
        return 0.0005  # Upbit KRW 마켓 ~0.05%

    def get_min_order_amount(self, market):
        return 5000.0  # KRW
