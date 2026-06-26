"""
Cross Exchange Monitor
Monitors price differences and leading indicators across exchanges
"""

import asyncio
import logging
import time
from collections import deque
from typing import Dict, List, Optional, Any
from decimal import Decimal
from dataclasses import dataclass

from app.integrations.exchange_adapter import ExchangeAdapter, TickerInfo
# TODO: Re-enable when Bithumb adapter is implemented
# from app.integrations.exchange_factory import create_bithumb

logger = logging.getLogger(__name__)


@dataclass
class ArbitrageOpportunity:
    """Arbitrage opportunity"""
    coin: str
    buy_exchange: str
    sell_exchange: str
    buy_price: Decimal
    sell_price: Decimal
    diff_pct: float
    profit_estimate: Decimal
    timestamp: float


@dataclass
class LeadingIndicatorSignal:
    """Leading indicator signal"""
    coin: str
    leader_exchange: str
    follower_exchange: str
    leader_change_pct: float
    follower_change_pct: float
    direction: str  # 'UP' or 'DOWN'
    confidence: float
    timestamp: float


@dataclass
class KimchiPremium:
    """Kimchi premium info"""
    coin: str
    bybit_price_usdt: Decimal
    binance_price_usdt: Decimal
    binance_price_usdt: Decimal
    premium_pct: float
    signal: str  # 'OVERHEATED', 'NORMAL', 'UNDERVALUED'
    timestamp: float


class CrossExchangeMonitor:
    """Cross-exchange price monitoring"""

    def __init__(self, use_mock: bool = True):
        """
        Args:
            use_mock: If True, use Binance/Bithumb Mock
        """
        self.use_mock = use_mock

        # Exchange adapters
        self.exchanges: Dict[str, ExchangeAdapter] = {}

        # Price history (5 minutes, one entry per second)
        self.price_history: Dict[str, Dict[str, deque]] = {
            'BYBIT': {},
            'BINANCE': {},
            'BITHUMB': {}
        }
        
        # Discovered opportunities
        self.opportunities: List[Any] = []

        # Common coins (tradable on all 3 exchanges)
        self.common_coins = [
            'BTC', 'ETH', 'XRP', 'ADA', 'DOT',
            'SOL', 'DOGE', 'AVAX'
        ]

        # Whether monitoring is active
        self.is_running = False

        # Kimchi premium warning throttle (last warning time per coin)
        self._kimchi_warn_ts: Dict[str, float] = {}
        self._kimchi_warn_cooldown = 300  # 5 minutes

        # Statistics
        self.stats = {
            'total_opportunities': 0,
            'arbitrage_count': 0,
            'leading_indicator_count': 0,
            'last_update': 0
        }
    
    async def initialize(self):
        """Initialize"""
        try:
            logger.info("Initializing Cross Exchange Monitor...")
            
            # Bybit is the primary exchange; cross-exchange comparison reserved for future Bithumb integration
            logger.info("✅ Cross Exchange Monitor initialized (Bithumb integration pending)")
            
            # Initialize price history
            for exchange in self.exchanges.keys():
                for coin in self.common_coins:
                    self.price_history[exchange][coin] = deque(maxlen=300)  # 5 minutes
            
            logger.info("✅ Cross Exchange Monitor initialized")
            return True
            
        except (KeyError, AttributeError, TypeError) as e:
            logger.error(f"Failed to initialize monitor: {e}")
            return False
    
    async def start_monitoring(self):
        """Start monitoring"""
        if self.is_running:
            logger.warning("Monitor is already running")
            return
        
        self.is_running = True
        logger.info("🚀 Starting cross-exchange monitoring...")
        
        try:
            while self.is_running:
                await self._monitor_cycle()
                await asyncio.sleep(60)  # every 60s (auxiliary indicators: kimchi premium/leading signal, frequent calls unnecessary)
                
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.error(f"Monitoring error: {e}")
        finally:
            self.is_running = False
    
    def stop_monitoring(self):
        """Stop monitoring"""
        logger.info("Stopping monitoring...")
        self.is_running = False
    
    async def _monitor_cycle(self):
        """One monitoring cycle"""
        try:
            # 1. Fetch prices from all exchanges concurrently
            prices = await self._fetch_all_prices()

            # 2. Save price history
            self._update_price_history(prices)

            # 3. Analyze arbitrage opportunities
            arb_opportunities = self._analyze_arbitrage(prices)

            # 4. Analyze leading indicator opportunities
            leading_opportunities = self._analyze_leading_indicator(prices)

            # 5. Analyze kimchi premium (get_exchange_rate is sync HTTP -> offload to thread)
            kimchi_data = await asyncio.to_thread(self._analyze_kimchi_premium, prices)

            # 6. Store opportunities
            if arb_opportunities or leading_opportunities:
                self.opportunities.extend(arb_opportunities)
                self.opportunities.extend(leading_opportunities)

                # Keep only the most recent 100
                self.opportunities = self.opportunities[-100:]

                # Update statistics
                self.stats['total_opportunities'] = len(self.opportunities)
                self.stats['arbitrage_count'] += len(arb_opportunities)
                self.stats['leading_indicator_count'] += len(leading_opportunities)

            self.stats['last_update'] = time.time()

            # 7. Log notable opportunities
            for opp in arb_opportunities:
                if opp.diff_pct > 0.5:
                    logger.info(f"💰 ARBITRAGE: {opp.coin} {opp.buy_exchange}→{opp.sell_exchange} "
                              f"+{opp.diff_pct:.2f}% (Est. {opp.profit_estimate:,.0f} KRW)")
            
            for opp in leading_opportunities:
                if abs(opp.leader_change_pct) > 2.0:
                    logger.info(f"🔮 LEADING: {opp.coin} {opp.leader_exchange} {opp.direction} "
                              f"{opp.leader_change_pct:+.2f}% (Conf: {opp.confidence:.0%})")
            
            # Kimchi premium warning (5-minute cooldown to prevent log flooding)
            _now = time.time()
            for k in kimchi_data:
                if k.signal == 'OVERHEATED':
                    _last = self._kimchi_warn_ts.get(k.coin, 0)
                    if _now - _last >= self._kimchi_warn_cooldown:
                        logger.warning(f"🌶️ KIMCHI PREMIUM OVERHEATED: {k.coin} +{k.premium_pct:.2f}%")
                        self._kimchi_warn_ts[k.coin] = _now
                elif k.signal == 'UNDERVALUED':
                    _last = self._kimchi_warn_ts.get(k.coin, 0)
                    if _now - _last >= self._kimchi_warn_cooldown:
                        logger.info(f"💎 KIMCHI DISCOUNT: {k.coin} {k.premium_pct:.2f}%")
                        self._kimchi_warn_ts[k.coin] = _now
            
            # 8. Update Signal Provider (used by the Reserved Selector)
            try:
                from app.manager.cross_exchange_signal import get_cross_exchange_signal_provider
                signal_provider = get_cross_exchange_signal_provider()

                # Compute liquidity score (based on volume + order book depth)
                liquidity_scores = {}
                for coin in self.common_coins:
                    bybit_ticker = prices.get('BYBIT', {}).get(coin)
                    if bybit_ticker:
                        vol_score = min(1.0, float(bybit_ticker.volume_24h * bybit_ticker.current_price) / 20_000_000.0)
                        liquidity_scores[coin] = vol_score
                
                # Store signal per coin
                for coin in self.common_coins:
                    # Arbitrage signal
                    arb_signal = None
                    arb_pct = 0.0
                    for opp in arb_opportunities:
                        if opp.coin == coin:
                            arb_signal = f"{opp.buy_exchange}→{opp.sell_exchange}"
                            arb_pct = opp.diff_pct
                            break

                    # Leading indicator signal
                    leading_signal = None
                    leading_conf = 0.0
                    leading_change = 0.0
                    for opp in leading_opportunities:
                        if opp.coin == coin:
                            leading_signal = opp.direction
                            leading_conf = opp.confidence
                            leading_change = opp.leader_change_pct
                            break

                    # Kimchi premium
                    kimchi_pct = 0.0
                    for k in kimchi_data:
                        if k.coin == coin:
                            kimchi_pct = k.premium_pct
                            break

                    # Update Signal Provider
                    signal_provider.update_signal(
                        coin=coin,
                        liquidity_score=liquidity_scores.get(coin, 0.5),
                        arbitrage_pct=arb_pct,
                        arbitrage_direction=arb_signal or "",
                        kimchi_premium_pct=kimchi_pct,
                        leading_signal=leading_signal or "",
                        leading_confidence=leading_conf,
                        leading_change_pct=leading_change
                    )
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.warning(f"Signal Provider update failed (non-critical): {e}", exc_info=True)
            
        except Exception as e:
            logger.error(f"Monitor cycle error: {e}", exc_info=True)
    
    async def _fetch_all_prices(self) -> Dict[str, Dict[str, TickerInfo]]:
        """Fetch prices from all exchanges concurrently (batch-optimized)"""
        prices = {
            'BYBIT': {},
            'BINANCE': {},
            'BITHUMB': {}
        }
        
        # Bybit: fetch all coins at once
        normalized_markets = [f"{coin}USDT" for coin in self.common_coins]
        bybit_batch_result = await self.__fetch_batch(normalized_markets)
        if bybit_batch_result:
            for coin, ticker in bybit_batch_result.items():
                prices['BYBIT'][coin] = ticker

        # Inject Bybit reference price into Mock adapters
        binance_adapter = self.exchanges.get('BINANCE')
        bithumb_adapter = self.exchanges.get('BITHUMB')
        for coin, ticker in bybit_batch_result.items():
            if binance_adapter and hasattr(binance_adapter, 'set_bybit_reference'):
                binance_adapter.set_bybit_reference(coin, ticker.current_price)
            if bithumb_adapter and hasattr(bithumb_adapter, 'set_bybit_reference'):
                bithumb_adapter.set_bybit_reference(coin, ticker.current_price)

        # Binance & Bithumb Mock: individual fetch (based on Bybit price)
        tasks = []
        for coin in self.common_coins:
            # Binance Mock
            market_code = f"{coin}USDT"
            tasks.append(self._fetch_ticker('BINANCE', market_code, coin))

            # Bithumb Mock
            market_code = f"{coin}USDT"
            tasks.append(self._fetch_ticker('BITHUMB', market_code, coin))

        # Run concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results
        for result in results:
            if isinstance(result, Exception):
                continue
            if result:
                exchange, coin, ticker = result
                prices[exchange][coin] = ticker
        
        return prices
    
    async def __fetch_batch(self, markets: list) -> Dict[str, TickerInfo]:
        """Bybit batch fetch (all markets at once)"""
        try:
            adapter = self.exchanges.get('BYBIT')
            if not adapter:
                return {}

            # Batch API call: "BTCUSDT,ETHUSDT,XRPUSDT..."
            markets_str = ",".join(markets)
            loop = asyncio.get_event_loop()
            tickers = await loop.run_in_executor(None, adapter.get_ticker, markets_str)

            if not tickers:
                return {}

            # Returned as a list, so parse it
            result = {}
            if isinstance(tickers, list):
                for ticker in tickers:
                    # market_code: "BTCUSDT" → coin: "BTC"
                    coin = ticker.market_code.replace("USDT", "")
                    result[coin] = ticker
            else:
                # Single ticker case
                coin = tickers.market_code.replace("USDT", "")
                result[coin] = tickers

            return result

        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.error(f"Failed to fetch Bybit batch: {e}")
            return {}
    
    async def _fetch_ticker(self, exchange: str, market_code: str, coin: str):
        """Fetch a single ticker"""
        try:
            adapter = self.exchanges.get(exchange)
            if not adapter:
                return None

            # Run the blocking call in an async executor
            loop = asyncio.get_event_loop()
            ticker = await loop.run_in_executor(None, adapter.get_ticker, market_code)
            return (exchange, coin, ticker) if ticker else None
            
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning(f"Failed to fetch {exchange} {coin}: {e}", exc_info=True)
            return None
    
    def _update_price_history(self, prices: Dict[str, Dict[str, TickerInfo]]):
        """Update price history"""
        timestamp = time.time()
        
        for exchange, coins in prices.items():
            for coin, ticker in coins.items():
                if ticker:
                    self.price_history[exchange][coin].append({
                        'timestamp': timestamp,
                        'price': ticker.current_price
                    })
    
    def _analyze_arbitrage(self, prices: Dict[str, Dict[str, TickerInfo]]) -> List[ArbitrageOpportunity]:
        """Analyze arbitrage opportunities"""
        opportunities = []

        for coin in self.common_coins:
            # Bybit vs Bithumb (both USDT)
            bybit_ticker = prices.get('BYBIT', {}).get(coin)
            bithumb_ticker = prices.get('BITHUMB', {}).get(coin)

            if bybit_ticker and bithumb_ticker:
                opp = self._check_arbitrage_pair(
                    coin,
                    'BYBIT', bybit_ticker.current_price,
                    'BITHUMB', bithumb_ticker.current_price,
                    fee_pct=0.003  # 0.05% + 0.25%
                )
                if opp:
                    opportunities.append(opp)
        
        return opportunities
    
    def _check_arbitrage_pair(
        self, 
        coin: str, 
        exchange_a: str, 
        price_a: Decimal,
        exchange_b: str,
        price_b: Decimal,
        fee_pct: float
    ) -> Optional[ArbitrageOpportunity]:
        """Check arbitrage between two exchanges"""

        # Price difference (%)
        diff_pct_ab = float((price_b - price_a) / price_a * 100)
        diff_pct_ba = float((price_a - price_b) / price_b * 100)

        # Minimum spread accounting for fees
        min_diff = fee_pct * 100

        # Buy on A, sell on B
        if diff_pct_ab > min_diff:
            profit = self._estimate_arbitrage_profit(price_a, price_b, fee_pct)
            
            return ArbitrageOpportunity(
                coin=coin,
                buy_exchange=exchange_a,
                sell_exchange=exchange_b,
                buy_price=price_a,
                sell_price=price_b,
                diff_pct=diff_pct_ab,
                profit_estimate=profit,
                timestamp=time.time()
            )
        
        # Buy on B, sell on A
        elif diff_pct_ba > min_diff:
            profit = self._estimate_arbitrage_profit(price_b, price_a, fee_pct)
            
            return ArbitrageOpportunity(
                coin=coin,
                buy_exchange=exchange_b,
                sell_exchange=exchange_a,
                buy_price=price_b,
                sell_price=price_a,
                diff_pct=diff_pct_ba,
                profit_estimate=profit,
                timestamp=time.time()
            )
        
        return None
    
    def _estimate_arbitrage_profit(self, buy_price: Decimal, sell_price: Decimal, fee_pct: float) -> Decimal:
        """Estimate arbitrage profit (assuming 1,000,000 USDT investment)"""
        investment = Decimal('1000000')
        fee_decimal = Decimal(str(fee_pct))

        # Compute profit after fees
        # 1. Buy: invest 1,000,000 USDT, deduct fee
        buy_fee = investment * fee_decimal
        actual_investment = investment - buy_fee  # actual buy amount after fee
        qty = actual_investment / buy_price       # quantity bought

        # 2. Sell: deduct fee from sale proceeds
        gross_revenue = qty * sell_price          # gross sale amount
        sell_fee = gross_revenue * fee_decimal    # sell fee
        net_revenue = gross_revenue - sell_fee    # net revenue after fee

        # 3. Final profit = net revenue - principal
        profit = net_revenue - investment

        return profit
    
    def _analyze_leading_indicator(self, prices: Dict[str, Dict[str, TickerInfo]]) -> List[LeadingIndicatorSignal]:
        """Analyze leading indicators"""
        opportunities = []
        
        for coin in self.common_coins:
            # Binance vs Bybit
            signal = self._check_leading_indicator(coin, 'BINANCE', 'BYBIT', prices)
            if signal:
                opportunities.append(signal)
            
            # Bybit vs Bithumb
            signal = self._check_leading_indicator(coin, 'BYBIT', 'BITHUMB', prices)
            if signal:
                opportunities.append(signal)
        
        return opportunities
    
    def _check_leading_indicator(
        self, 
        coin: str, 
        leader_ex: str, 
        follower_ex: str,
        prices: Dict[str, Dict[str, TickerInfo]]
    ) -> Optional[LeadingIndicatorSignal]:
        """Check leading indicator"""

        # Compute change over the last 1 minute
        leader_change = self._calculate_change_1min(leader_ex, coin)
        follower_change = self._calculate_change_1min(follower_ex, coin)

        if leader_change is None or follower_change is None:
            return None

        # Leader moved sharply but follower hasn't reacted yet
        if abs(leader_change) > 2.0 and abs(follower_change) < 1.0:

            # Check whether directions match
            if leader_change * follower_change < 0:
                # Opposite direction -> ignore
                return None

            direction = 'UP' if leader_change > 0 else 'DOWN'

            # Compute confidence (based on change magnitude and follower lag)
            confidence = min(abs(leader_change) / 5.0, 1.0) * 0.7
            if abs(follower_change) < 0.5:
                confidence += 0.2
            
            return LeadingIndicatorSignal(
                coin=coin,
                leader_exchange=leader_ex,
                follower_exchange=follower_ex,
                leader_change_pct=leader_change,
                follower_change_pct=follower_change,
                direction=direction,
                confidence=confidence,
                timestamp=time.time()
            )
        
        return None
    
    def _calculate_change_1min(self, exchange: str, coin: str) -> Optional[float]:
        """Compute price change over the last 1 minute"""
        history = self.price_history.get(exchange, {}).get(coin, deque())

        if len(history) < 60:
            return None

        # Price 60 seconds ago
        price_60s_ago = history[-60]['price']

        # Current price
        price_now = history[-1]['price']

        # Change (%)
        change_pct = float((price_now - price_60s_ago) / price_60s_ago * 100)
        
        return change_pct
    
    def _analyze_kimchi_premium(self, prices: Dict[str, Dict[str, TickerInfo]]) -> List[KimchiPremium]:
        """Analyze kimchi premium"""
        result = []

        for coin in self.common_coins:
            bybit_ticker = prices.get('BYBIT', {}).get(coin)
            binance_ticker = prices.get('BINANCE', {}).get(coin)

            if not bybit_ticker or not binance_ticker:
                continue

            # Bybit price (USDT)
            bybit_price_usdt = bybit_ticker.current_price

            # Binance price (USDT)
            binance_price_usdt = binance_ticker.current_price

            # Compute premium
            premium_pct = float((bybit_price_usdt / binance_price_usdt - 1) * 100)

            # Signal
            if premium_pct > 5.0:
                signal = 'OVERHEATED'
            elif premium_pct < -1.0:
                signal = 'UNDERVALUED'
            else:
                signal = 'NORMAL'
            
            result.append(KimchiPremium(
                coin=coin,
                bybit_price_usdt=bybit_price_usdt,
                binance_price_usdt=binance_price_usdt,
                premium_pct=premium_pct,
                signal=signal,
                timestamp=time.time()
            ))
        
        return result
    
    def get_latest_opportunities(self, limit: int = 10) -> List[Any]:
        """Get recently discovered opportunities"""
        return self.opportunities[-limit:]

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics"""
        return self.stats.copy()
