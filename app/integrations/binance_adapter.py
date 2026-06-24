"""Binance Exchange Adapter — mirrors BybitAdapter. Reuses BinanceSpotTradeClient for market lookups."""
import logging
from decimal import Decimal

from app.integrations.exchange_adapter import (
    ExchangeAdapter, MarketInfo, TickerInfo,
    BalanceInfo, OrderResult, OrderSide, OrderStatus,
)
from app.integrations.binance_spot_trade import BinanceSpotTradeClient

logger = logging.getLogger(__name__)


class BinanceAdapter(ExchangeAdapter):
    def __init__(self, api_key: str, api_secret: str):
        self.trade_client = BinanceSpotTradeClient(api_key, api_secret)
        self._exchange_name = "BINANCE"

    @property
    def exchange_name(self):
        return self._exchange_name

    def get_markets(self):
        try:
            data = self.trade_client.get_all_markets()
            return [MarketInfo(exchange=self._exchange_name,
                               symbol=m.get("market", ""),
                               base_currency=self.trade_client.base_currency(m.get("market", "")),
                               quote_currency="USDT", min_order_size=Decimal("5"),
                               tradable=True)
                    for m in data]
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error("Failed to get Binance markets: %s", e)
            return []

    def get_ticker(self, market_code):
        try:
            markets = [m.strip() for m in market_code.split(",")] if "," in market_code else [market_code]
            tickers = self.trade_client.get_tickers(markets)
            results = [TickerInfo(exchange=self._exchange_name, market_code=t.get("market", ""),
                                  current_price=Decimal(str(t.get("trade_price", 0))),
                                  bid_price=Decimal(str(t.get("trade_price", 0))),
                                  ask_price=Decimal(str(t.get("trade_price", 0))),
                                  volume_24h=Decimal(str(t.get("acc_trade_volume_24h", 0))),
                                  change_24h_pct=Decimal(str(t.get("signed_change_rate", 0))) * 100,
                                  high_24h=Decimal("0"), low_24h=Decimal("0"),
                                  timestamp=0) for t in tickers]
            return results if "," in market_code else (results[0] if results else None)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
            logger.error("Ticker error %s: %s", market_code, e)
            return [] if "," in market_code else None

    def get_orderbook(self, market_code):
        return self.trade_client.get_orderbook(market_code)

    def get_balance(self, currency=None):
        try:
            data = self.trade_client.accounts()
            return [BalanceInfo(exchange=self._exchange_name, currency=b.get("currency", ""),
                                available=Decimal(str(b.get("balance", 0))),
                                locked=Decimal(str(b.get("locked", 0))),
                                total=Decimal(str(b.get("balance", 0))) + Decimal(str(b.get("locked", 0))))
                    for b in data if not currency or b.get("currency") == currency]
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error("Balance error: %s", e)
            return []

    def buy_market_order(self, market_code, amount):
        od = self.trade_client.market_buy(market_code, float(amount))
        return OrderResult(exchange=self._exchange_name, order_id=od.get("uuid", ""),
                           market_code=market_code, side=OrderSide.BUY, order_type="market",
                           price=None, amount=Decimal(str(od.get("volume", 0))),
                           filled_amount=Decimal(str(od.get("executed_volume", 0))),
                           status=self._map_state(od.get("state", "")),
                           timestamp=od.get("created_at", ""), raw_data=od)

    def sell_market_order(self, market_code, amount):
        od = self.trade_client.market_sell(market_code, float(amount))
        return OrderResult(exchange=self._exchange_name, order_id=od.get("uuid", ""),
                           market_code=market_code, side=OrderSide.SELL, order_type="market",
                           price=None, amount=Decimal(str(amount)),
                           filled_amount=Decimal(str(od.get("executed_volume", 0))),
                           status=self._map_state(od.get("state", "")),
                           timestamp=od.get("created_at", ""), raw_data=od)

    def get_order_status(self, order_id):
        return None

    def cancel_order(self, order_id):
        logger.error("Binance cancel_order via adapter requires market symbol — use trade_client.cancel_order")
        return False

    def _map_state(self, state):
        return {"wait": OrderStatus.PENDING, "done": OrderStatus.FILLED,
                "cancel": OrderStatus.CANCELLED}.get(state, OrderStatus.FAILED)

    def get_name(self): return self._exchange_name
    def get_quote_currency(self): return "USDT"
    def get_tickers(self, markets): return [self.get_ticker(m) for m in markets]
    def get_balances(self, currency=None): return self.get_balance(currency)
    def get_order(self, order_id): return self.get_order_status(order_id)
    def get_orders(self, market=None): return []
    def buy_limit_order(self, market_code, price, amount): return None
    def sell_limit_order(self, market_code, price, amount): return None
    def get_fee_rate(self, market=None): return {"maker": 0.001, "taker": 0.001}
    def get_min_order_amount(self, market): return Decimal("5")
    def normalize_market_code(self, symbol, base_currency="USDT"): return f"{symbol}{base_currency}"

    def parse_market_code(self, market_code):
        if market_code.endswith("USDT"):
            return {"base": market_code[:-4], "quote": "USDT"}
        return {"base": market_code, "quote": "USDT"}
