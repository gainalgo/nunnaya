"""
거래소 통합 어댑터 인터페이스

모든 거래소 어댑터가 구현해야 하는 공통 인터페이스를 정의합니다.
이를 통해 Bybit 거래소를 통일된 방식으로 사용할 수 있습니다.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from enum import Enum


class OrderSide(Enum):
    """주문 방향"""
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """주문 유형"""
    MARKET = "market"  # 시장가
    LIMIT = "limit"    # 지정가


class OrderStatus(Enum):
    """주문 상태"""
    PENDING = "pending"      # 대기 중
    FILLED = "filled"        # 체결 완료
    CANCELLED = "cancelled"  # 취소됨
    FAILED = "failed"        # 실패


@dataclass
class MarketInfo:
    """마켓 정보 표준 포맷"""
    exchange: str           # 거래소명 (BYBIT)
    symbol: str             # 마켓 심볼 (BTCUSDT)
    base_currency: str      # 기초 통화 (BTC, ETH)
    quote_currency: str     # 견적 통화 (USDT)
    min_order_size: float   # 최소 주문 크기
    tradable: bool          # 거래 가능 여부


@dataclass
class TickerInfo:
    """시세 정보 표준 포맷"""
    exchange: str               # 거래소명
    market_code: str            # 마켓 코드
    current_price: Any          # 현재가 (Decimal)
    bid_price: Any              # 매수 호가 (Decimal)
    ask_price: Any              # 매도 호가 (Decimal)
    volume_24h: Any             # 24시간 거래량 (Decimal)
    change_24h_pct: Any         # 24시간 변화율 % (Decimal)
    high_24h: Any               # 24시간 고가 (Decimal)
    low_24h: Any                # 24시간 저가 (Decimal)
    timestamp: int              # 타임스탬프 (ms)


@dataclass
class OrderbookUnit:
    """호가 단위"""
    price: float    # 가격
    size: float     # 수량


@dataclass
class OrderbookInfo:
    """호가 정보 표준 포맷"""
    exchange: str               # 거래소명
    market_code: str            # 마켓 코드
    bids: List[tuple]          # 매수 호가 [(가격, 수량), ...]
    asks: List[tuple]          # 매도 호가 [(가격, 수량), ...]
    timestamp: int              # 타임스탬프 (ms)


@dataclass
class BalanceInfo:
    """잔고 정보 표준 포맷"""
    exchange: str           # 거래소명
    currency: str           # 화폐 코드
    available: Any          # 사용 가능 수량 (Decimal)
    locked: Any             # 주문 중 묶인 수량 (Decimal)
    total: Any              # 총 수량 (Decimal)


@dataclass
class OrderResult:
    exchange: str               # 거래소명
    order_id: str               # 주문 고유 ID
    market_code: str            # 마켓 코드
    side: OrderSide             # 주문 방향
    order_type: str             # 주문 유형
    price: Optional[Any]        # 주문 가격 (Decimal, 시장가는 None)
    amount: Any                 # 주문 수량 (Decimal)
    filled_amount: Any          # 체결 수량 (Decimal)
    status: OrderStatus         # 주문 상태
    timestamp: str              # 주문 시각
    raw_data: Dict              # 원본 데이터 (wait, done, cancel)
    created_at: str         # 주문 시각


class ExchangeAdapter(ABC):
    """
    거래소 통합 어댑터 추상 클래스
    
    모든 거래소 어댑터는 이 클래스를 상속받아 구현해야 합니다.
    """

    # SLArbiter/청산 모듈 거래소 분기 선택자 (DESIGN_A §4.2, INV-3).
    #   True=선물(청산 있음→fast_cut) / False=현물(청산 없음→longhold 허용).
    #   기본 True(선물 가정). 현물 어댑터(UpbitAdapter)가 False 로 override.
    has_liquidation = True

    @abstractmethod
    def get_name(self) -> str:
        """
        거래소 이름 반환
        
        Returns:
            str: 거래소 이름 (예: "BYBIT")
        """
        pass
    
    @abstractmethod
    def get_quote_currency(self) -> str:
        """
        거래소의 기본 견적 통화 반환
        
        Returns:
            str: 견적 통화 (예: "USDT")
        """
        pass
    
    # ========== 시세 조회 API ==========
    
    @abstractmethod
    def get_markets(self) -> List[MarketInfo]:
        """
        거래 가능한 전체 마켓 목록 조회
        
        Returns:
            List[MarketInfo]: 마켓 정보 리스트
        """
        pass
    
    @abstractmethod
    def get_ticker(self, market: str) -> TickerInfo:
        """
        특정 마켓의 현재가 정보 조회
        
        Args:
            market: 마켓 코드 (예: "BTCUSDT")
        
        Returns:
            TickerInfo: 시세 정보
        """
        pass
    
    @abstractmethod
    def get_tickers(self, markets: Optional[List[str]] = None) -> List[TickerInfo]:
        """
        여러 마켓의 현재가 정보 일괄 조회
        
        Args:
            markets: 마켓 코드 리스트 (None이면 전체)
        
        Returns:
            List[TickerInfo]: 시세 정보 리스트
        """
        pass
    
    @abstractmethod
    def get_orderbook(self, market: str) -> OrderbookInfo:
        """
        특정 마켓의 호가 정보 조회
        
        Args:
            market: 마켓 코드
        
        Returns:
            OrderbookInfo: 호가 정보
        """
        pass
    
    # ========== 계정 조회 API ==========
    
    @abstractmethod
    def get_balances(self) -> List[BalanceInfo]:
        """
        전체 보유 자산 조회
        
        Returns:
            List[BalanceInfo]: 잔고 정보 리스트
        """
        pass
    
    @abstractmethod
    def get_balance(self, currency: str) -> Optional[BalanceInfo]:
        """
        특정 화폐의 잔고 조회
        
        Args:
            currency: 화폐 코드 (예: "USDT", "BTC")
        
        Returns:
            Optional[BalanceInfo]: 잔고 정보 (없으면 None)
        """
        pass
    
    # ========== 주문 API ==========
    
    @abstractmethod
    def buy_market_order(
        self, 
        market: str, 
        volume: Optional[float] = None,
        price: Optional[float] = None
    ) -> OrderResult:
        """
        시장가 매수 주문
        
        Args:
            market: 마켓 코드
            volume: 매수할 수량 (Binance 방식)
            price: 매수 금액 (USDT)
        
        Returns:
            OrderResult: 주문 결과
        
        Note:
            - Bybit: quote amount (USDT)
            - Binance: volume (매수 수량) 사용
        """
        pass
    
    @abstractmethod
    def sell_market_order(
        self, 
        market: str, 
        volume: float
    ) -> OrderResult:
        """
        시장가 매도 주문
        
        Args:
            market: 마켓 코드
            volume: 매도할 수량
        
        Returns:
            OrderResult: 주문 결과
        """
        pass
    
    @abstractmethod
    def buy_limit_order(
        self,
        market: str,
        price: float,
        volume: float
    ) -> OrderResult:
        """
        지정가 매수 주문
        
        Args:
            market: 마켓 코드
            price: 지정 가격
            volume: 매수 수량
        
        Returns:
            OrderResult: 주문 결과
        """
        pass
    
    @abstractmethod
    def sell_limit_order(
        self,
        market: str,
        price: float,
        volume: float
    ) -> OrderResult:
        """
        지정가 매도 주문
        
        Args:
            market: 마켓 코드
            price: 지정 가격
            volume: 매도 수량
        
        Returns:
            OrderResult: 주문 결과
        """
        pass
    
    @abstractmethod
    def cancel_order(self, uuid: str) -> Dict[str, Any]:
        """
        주문 취소
        
        Args:
            uuid: 주문 고유 ID
        
        Returns:
            Dict: 취소 결과
        """
        pass
    
    @abstractmethod
    def get_order(self, uuid: str) -> Dict[str, Any]:
        """
        개별 주문 조회
        
        Args:
            uuid: 주문 고유 ID
        
        Returns:
            Dict: 주문 상세 정보
        """
        pass
    
    @abstractmethod
    def get_orders(
        self, 
        market: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        주문 리스트 조회
        
        Args:
            market: 마켓 코드 (None이면 전체)
            state: 주문 상태 (wait, done, cancel 등)
            limit: 조회 개수
        
        Returns:
            List[Dict]: 주문 리스트
        """
        pass
    
    # ========== 유틸리티 메서드 ==========
    
    @abstractmethod
    def normalize_market_code(self, market: str) -> str:
        """
        마켓 코드를 거래소 형식으로 정규화
        
        Args:
            market: 입력 마켓 코드
        
        Returns:
            str: 정규화된 마켓 코드
        
        Example:
            Bybit: "BTC" -> "BTCUSDT"
            Binance: "BTC" -> "BTCUSDT"
        """
        pass
    
    @abstractmethod
    def parse_market_code(self, market: str) -> tuple[str, str]:
        """
        마켓 코드를 기초/견적 통화로 분리
        
        Args:
            market: 마켓 코드
        
        Returns:
            tuple[str, str]: (기초통화, 견적통화)
        
        Example:
            "BTCUSDT" -> ("BTC", "USDT")
            "BTCUSDT" -> ("BTC", "USDT")
        """
        pass
    
    @abstractmethod
    def get_fee_rate(self, order_type: str = "market") -> float:
        """
        거래 수수료율 반환
        
        Args:
            order_type: 주문 유형 (market, limit)
        
        Returns:
            float: 수수료율 (0.0005 = 0.05%)
        """
        pass
    
    @abstractmethod
    def get_min_order_amount(self, market: str) -> float:
        """
        최소 주문 금액/수량 반환
        
        Args:
            market: 마켓 코드
        
        Returns:
            float: 최소 주문 크기
        """
        pass
    
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(exchange={self.get_name()}, quote={self.get_quote_currency()})>"
