"""
Cross Exchange Monitor
거래소 간 가격 차이 및 선행지표 모니터링
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
    """차익거래 기회"""
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
    """선행지표 시그널"""
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
    """김치 프리미엄 정보"""
    coin: str
    bybit_price_usdt: Decimal
    binance_price_usdt: Decimal
    binance_price_usdt: Decimal
    premium_pct: float
    signal: str  # 'OVERHEATED', 'NORMAL', 'UNDERVALUED'
    timestamp: float


class CrossExchangeMonitor:
    """거래소 간 가격 모니터링"""
    
    def __init__(self, use_mock: bool = True):
        """
        Args:
            use_mock: True면 Binance/Bithumb Mock 사용
        """
        self.use_mock = use_mock
        
        # 거래소 어댑터
        self.exchanges: Dict[str, ExchangeAdapter] = {}
        
        # 가격 히스토리 (5분치, 1초마다)
        self.price_history: Dict[str, Dict[str, deque]] = {
            'BYBIT': {},
            'BINANCE': {},
            'BITHUMB': {}
        }
        
        # 발견된 기회들
        self.opportunities: List[Any] = []
        
        # 공통 코인 (3개 거래소 모두 거래 가능한 코인)
        self.common_coins = [
            'BTC', 'ETH', 'XRP', 'ADA', 'DOT', 
            'SOL', 'DOGE', 'AVAX'
        ]
        
        # 모니터링 중 여부
        self.is_running = False
        
        # 김치 프리미엄 경고 throttle (코인별 마지막 경고 시각)
        self._kimchi_warn_ts: Dict[str, float] = {}
        self._kimchi_warn_cooldown = 300  # 5분
        
        # 통계
        self.stats = {
            'total_opportunities': 0,
            'arbitrage_count': 0,
            'leading_indicator_count': 0,
            'last_update': 0
        }
    
    async def initialize(self):
        """초기화"""
        try:
            logger.info("Initializing Cross Exchange Monitor...")
            
            # Bybit is the primary exchange; cross-exchange comparison reserved for future Bithumb integration
            logger.info("✅ Cross Exchange Monitor initialized (Bithumb integration pending)")
            
            # 가격 히스토리 초기화
            for exchange in self.exchanges.keys():
                for coin in self.common_coins:
                    self.price_history[exchange][coin] = deque(maxlen=300)  # 5분
            
            logger.info("✅ Cross Exchange Monitor initialized")
            return True
            
        except (KeyError, AttributeError, TypeError) as e:
            logger.error(f"Failed to initialize monitor: {e}")
            return False
    
    async def start_monitoring(self):
        """모니터링 시작"""
        if self.is_running:
            logger.warning("Monitor is already running")
            return
        
        self.is_running = True
        logger.info("🚀 Starting cross-exchange monitoring...")
        
        try:
            while self.is_running:
                await self._monitor_cycle()
                await asyncio.sleep(60)  # 60초마다 (보조 지표: 김프/선행신호, 잦은 호출 불필요)
                
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.error(f"Monitoring error: {e}")
        finally:
            self.is_running = False
    
    def stop_monitoring(self):
        """모니터링 중지"""
        logger.info("Stopping monitoring...")
        self.is_running = False
    
    async def _monitor_cycle(self):
        """1회 모니터링 사이클"""
        try:
            # 1. 모든 거래소 가격 동시 조회
            prices = await self._fetch_all_prices()
            
            # 2. 가격 히스토리 저장
            self._update_price_history(prices)
            
            # 3. 차익거래 기회 분석
            arb_opportunities = self._analyze_arbitrage(prices)
            
            # 4. 선행지표 기회 분석
            leading_opportunities = self._analyze_leading_indicator(prices)
            
            # 5. 김치 프리미엄 분석 (get_exchange_rate 동기 HTTP → 스레드 오프로드)
            kimchi_data = await asyncio.to_thread(self._analyze_kimchi_premium, prices)
            
            # 6. 기회 저장
            if arb_opportunities or leading_opportunities:
                self.opportunities.extend(arb_opportunities)
                self.opportunities.extend(leading_opportunities)
                
                # 최근 100개만 유지
                self.opportunities = self.opportunities[-100:]
                
                # 통계 업데이트
                self.stats['total_opportunities'] = len(self.opportunities)
                self.stats['arbitrage_count'] += len(arb_opportunities)
                self.stats['leading_indicator_count'] += len(leading_opportunities)
            
            self.stats['last_update'] = time.time()
            
            # 7. 중요한 기회 로깅
            for opp in arb_opportunities:
                if opp.diff_pct > 0.5:
                    logger.info(f"💰 ARBITRAGE: {opp.coin} {opp.buy_exchange}→{opp.sell_exchange} "
                              f"+{opp.diff_pct:.2f}% (Est. {opp.profit_estimate:,.0f}원)")
            
            for opp in leading_opportunities:
                if abs(opp.leader_change_pct) > 2.0:
                    logger.info(f"🔮 LEADING: {opp.coin} {opp.leader_exchange} {opp.direction} "
                              f"{opp.leader_change_pct:+.2f}% (Conf: {opp.confidence:.0%})")
            
            # 김치 프리미엄 경고 (5분 쿨다운으로 로그 폭주 방지)
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
            
            # 8. Signal Provider 업데이트 (Reserved Selector에서 사용)
            try:
                from app.manager.cross_exchange_signal import get_cross_exchange_signal_provider
                signal_provider = get_cross_exchange_signal_provider()
                
                # 유동성 스코어 계산 (거래량 + 호가 깊이 기반)
                liquidity_scores = {}
                for coin in self.common_coins:
                    bybit_ticker = prices.get('BYBIT', {}).get(coin)
                    if bybit_ticker:
                        vol_score = min(1.0, float(bybit_ticker.volume_24h * bybit_ticker.current_price) / 20_000_000.0)
                        liquidity_scores[coin] = vol_score
                
                # 각 코인별 시그널 저장
                for coin in self.common_coins:
                    # 차익거래 시그널
                    arb_signal = None
                    arb_pct = 0.0
                    for opp in arb_opportunities:
                        if opp.coin == coin:
                            arb_signal = f"{opp.buy_exchange}→{opp.sell_exchange}"
                            arb_pct = opp.diff_pct
                            break
                    
                    # 선행지표 시그널
                    leading_signal = None
                    leading_conf = 0.0
                    leading_change = 0.0
                    for opp in leading_opportunities:
                        if opp.coin == coin:
                            leading_signal = opp.direction
                            leading_conf = opp.confidence
                            leading_change = opp.leader_change_pct
                            break
                    
                    # 김치 프리미엄
                    kimchi_pct = 0.0
                    for k in kimchi_data:
                        if k.coin == coin:
                            kimchi_pct = k.premium_pct
                            break
                    
                    # Signal Provider 업데이트
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
        """모든 거래소 가격 동시 조회 (배치 최적화)"""
        prices = {
            'BYBIT': {},
            'BINANCE': {},
            'BITHUMB': {}
        }
        
        # Bybit: 모든 코인 한 번에 조회
        normalized_markets = [f"{coin}USDT" for coin in self.common_coins]
        bybit_batch_result = await self.__fetch_batch(normalized_markets)
        if bybit_batch_result:
            for coin, ticker in bybit_batch_result.items():
                prices['BYBIT'][coin] = ticker

        # Mock 어댑터에 Bybit 참조 가격 주입
        binance_adapter = self.exchanges.get('BINANCE')
        bithumb_adapter = self.exchanges.get('BITHUMB')
        for coin, ticker in bybit_batch_result.items():
            if binance_adapter and hasattr(binance_adapter, 'set_bybit_reference'):
                binance_adapter.set_bybit_reference(coin, ticker.current_price)
            if bithumb_adapter and hasattr(bithumb_adapter, 'set_bybit_reference'):
                bithumb_adapter.set_bybit_reference(coin, ticker.current_price)

        # Binance & Bithumb Mock: 개별 조회 (Bybit 가격 기반)
        tasks = []
        for coin in self.common_coins:
            # Binance Mock
            market_code = f"{coin}USDT"
            tasks.append(self._fetch_ticker('BINANCE', market_code, coin))

            # Bithumb Mock
            market_code = f"{coin}USDT"
            tasks.append(self._fetch_ticker('BITHUMB', market_code, coin))
        
        # 동시 실행
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 결과 정리
        for result in results:
            if isinstance(result, Exception):
                continue
            if result:
                exchange, coin, ticker = result
                prices[exchange][coin] = ticker
        
        return prices
    
    async def __fetch_batch(self, markets: list) -> Dict[str, TickerInfo]:
        """Bybit 배치 조회 (한 번에 모든 마켓)"""
        try:
            adapter = self.exchanges.get('BYBIT')
            if not adapter:
                return {}

            # 배치 API 호출: "BTCUSDT,ETHUSDT,XRPUSDT..."
            markets_str = ",".join(markets)
            loop = asyncio.get_event_loop()
            tickers = await loop.run_in_executor(None, adapter.get_ticker, markets_str)

            if not tickers:
                return {}

            # 리스트로 반환되므로 파싱
            result = {}
            if isinstance(tickers, list):
                for ticker in tickers:
                    # market_code: "BTCUSDT" → coin: "BTC"
                    coin = ticker.market_code.replace("USDT", "")
                    result[coin] = ticker
            else:
                # 단일 티커인 경우
                coin = tickers.market_code.replace("USDT", "")
                result[coin] = tickers

            return result

        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.error(f"Failed to fetch Bybit batch: {e}")
            return {}
    
    async def _fetch_ticker(self, exchange: str, market_code: str, coin: str):
        """개별 티커 조회"""
        try:
            adapter = self.exchanges.get(exchange)
            if not adapter:
                return None
            
            # Blocking call을 async executor에서 실행
            loop = asyncio.get_event_loop()
            ticker = await loop.run_in_executor(None, adapter.get_ticker, market_code)
            return (exchange, coin, ticker) if ticker else None
            
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning(f"Failed to fetch {exchange} {coin}: {e}", exc_info=True)
            return None
    
    def _update_price_history(self, prices: Dict[str, Dict[str, TickerInfo]]):
        """가격 히스토리 업데이트"""
        timestamp = time.time()
        
        for exchange, coins in prices.items():
            for coin, ticker in coins.items():
                if ticker:
                    self.price_history[exchange][coin].append({
                        'timestamp': timestamp,
                        'price': ticker.current_price
                    })
    
    def _analyze_arbitrage(self, prices: Dict[str, Dict[str, TickerInfo]]) -> List[ArbitrageOpportunity]:
        """차익거래 기회 분석"""
        opportunities = []
        
        for coin in self.common_coins:
            # Bybit vs Bithumb (둘 다 USDT)
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
        """두 거래소 간 차익 체크"""
        
        # 가격 차이 (%)
        diff_pct_ab = float((price_b - price_a) / price_a * 100)
        diff_pct_ba = float((price_a - price_b) / price_b * 100)
        
        # 수수료 고려한 최소 차익
        min_diff = fee_pct * 100
        
        # A에서 사서 B에서 팔기
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
        
        # B에서 사서 A에서 팔기
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
        """차익거래 예상 수익 계산 (100만 USDT 투자 기준)"""
        investment = Decimal('1000000')
        fee_decimal = Decimal(str(fee_pct))
        
        # 수수료 차감 후 수익 계산
        # 1. 매수: 100만 USDT 투자, 수수료 차감
        buy_fee = investment * fee_decimal
        actual_investment = investment - buy_fee  # 수수료 차감 후 실제 매수 금액
        qty = actual_investment / buy_price       # 매수 수량
        
        # 2. 매도: 매도 금액에서 수수료 차감
        gross_revenue = qty * sell_price          # 매도 총액
        sell_fee = gross_revenue * fee_decimal    # 매도 수수료
        net_revenue = gross_revenue - sell_fee    # 수수료 차감 후 순 수익
        
        # 3. 최종 수익 = 순수익 - 원금
        profit = net_revenue - investment
        
        return profit
    
    def _analyze_leading_indicator(self, prices: Dict[str, Dict[str, TickerInfo]]) -> List[LeadingIndicatorSignal]:
        """선행지표 분석"""
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
        """선행지표 체크"""
        
        # 최근 1분 변화율 계산
        leader_change = self._calculate_change_1min(leader_ex, coin)
        follower_change = self._calculate_change_1min(follower_ex, coin)
        
        if leader_change is None or follower_change is None:
            return None
        
        # 리더가 크게 움직였는데 팔로워는 아직 반응 안함
        if abs(leader_change) > 2.0 and abs(follower_change) < 1.0:
            
            # 방향 일치 여부 확인
            if leader_change * follower_change < 0:
                # 반대 방향이면 무시
                return None
            
            direction = 'UP' if leader_change > 0 else 'DOWN'
            
            # 신뢰도 계산 (변화율 크기와 팔로워 반응 지연 기반)
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
        """최근 1분 가격 변화율 계산"""
        history = self.price_history.get(exchange, {}).get(coin, deque())
        
        if len(history) < 60:
            return None
        
        # 60초 전 가격
        price_60s_ago = history[-60]['price']
        
        # 현재 가격
        price_now = history[-1]['price']
        
        # 변화율 (%)
        change_pct = float((price_now - price_60s_ago) / price_60s_ago * 100)
        
        return change_pct
    
    def _analyze_kimchi_premium(self, prices: Dict[str, Dict[str, TickerInfo]]) -> List[KimchiPremium]:
        """김치 프리미엄 분석"""
        result = []
        
        for coin in self.common_coins:
            bybit_ticker = prices.get('BYBIT', {}).get(coin)
            binance_ticker = prices.get('BINANCE', {}).get(coin)
            
            if not bybit_ticker or not binance_ticker:
                continue
            
            # Bybit 가격 (USDT)
            bybit_price_usdt = bybit_ticker.current_price

            # Binance 가격 (USDT)
            binance_price_usdt = binance_ticker.current_price
            
            # 프리미엄 계산
            premium_pct = float((bybit_price_usdt / binance_price_usdt - 1) * 100)
            
            # 시그널
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
        """최근 발견된 기회 조회"""
        return self.opportunities[-limit:]
    
    def get_stats(self) -> Dict[str, Any]:
        """통계 조회"""
        return self.stats.copy()
