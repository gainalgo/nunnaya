# ============================================================
# File: app/core/currency.py
# Autocoin OS v3-H — Quote Currency Abstraction Layer
# ============================================================
# 
# 이 모듈은 시스템 전체에서 사용하는 기축통화(Quote Currency)를
# 추상화합니다. Bybit USDT 전용.
#
# 사용법:
#   from app.core.currency import Q
#
#   Q.symbol          # "USDT"
#   Q.format(50000)   # "50,000.00 USDT"
#   Q.market("BTC")   # "BTCUSDT"
#   Q.min_order       # 5
# ============================================================

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass(frozen=True)
class CurrencyConfig:
    """기축통화별 설정."""
    symbol: str                    # "USDT"
    name: str                      # "Korean Won" or "Tether"
    min_order: float               # 최소 주문금액
    order_unit: float              # 주문 단위
    decimals: int                  # 소수점 자릿수 (금액 표시용)
    market_prefix: str             # 마켓 접두사 (Bybit: "")
    market_suffix: str             # 마켓 접미사 ("" or "USDT")
    market_separator: str          # 마켓 구분자 ("-" or "/")
    display_suffix: str            # 표시 접미사 (" USDT")
    exchange: str                  # 거래소 이름 ("bybit")
    api_base: str                  # API 기본 URL
    order_side_buy: str            # 매수 표현 ("bid" or "buy")
    order_side_sell: str           # 매도 표현 ("ask" or "sell")
    # API 키 환경변수명
    env_access_key: str            # 액세스 키 환경변수명
    env_secret_key: str            # 시크릿 키 환경변수명


# Bybit USDT 통화 설정
_CURRENCY_CONFIGS = {
    "USDT": CurrencyConfig(
        symbol="USDT",
        name="Tether",
        min_order=5.0,
        order_unit=0.01,
        decimals=2,
        market_prefix="",
        market_suffix="USDT",
        market_separator="/",
        display_suffix=" USDT",
        exchange="bybit",
        api_base="https://api.bybit.com",
        order_side_buy="Buy",
        order_side_sell="Sell",
        env_access_key="BYBIT_API_KEY",
        env_secret_key="BYBIT_API_SECRET",
    ),
}


class QuoteCurrency:
    """기축통화 추상화 클래스.
    
    시스템 전체에서 일관된 통화 처리를 위한 싱글톤 인터페이스.
    """
    
    def __init__(self, symbol: str = "USDT"):
        self._symbol = symbol.upper().strip()
        if self._symbol not in _CURRENCY_CONFIGS:
            raise ValueError(f"Unsupported currency: {self._symbol}. Use 'USDT'.")
        self._config = _CURRENCY_CONFIGS[self._symbol]
    
    @property
    def config(self) -> CurrencyConfig:
        """현재 통화 설정 반환."""
        return self._config
    
    @property
    def symbol(self) -> str:
        """통화 심볼 (예: "USDT")."""
        return self._config.symbol
    
    @property
    def name(self) -> str:
        """통화 이름."""
        return self._config.name
    
    @property
    def min_order(self) -> float:
        """최소 주문금액."""
        return self._config.min_order
    
    @property
    def order_unit(self) -> float:
        """주문 단위."""
        return self._config.order_unit
    
    @property
    def decimals(self) -> int:
        """소수점 자릿수."""
        return self._config.decimals
    
    @property
    def exchange(self) -> str:
        """거래소 이름."""
        return self._config.exchange
    
    @property
    def api_base(self) -> str:
        """API 기본 URL."""
        return self._config.api_base
    
    @property
    def is_usdt(self) -> bool:
        """USDT 모드인지 확인."""
        return self._symbol == "USDT"
    
    # =========================================================================
    # API 키 관련
    # =========================================================================
    
    @property
    def env_access_key(self) -> str:
        """액세스 키 환경변수명."""
        return self._config.env_access_key
    
    @property
    def env_secret_key(self) -> str:
        """시크릿 키 환경변수명."""
        return self._config.env_secret_key
    
    def get_access_key(self) -> str:
        """환경변수에서 액세스 키 조회."""
        return os.getenv(self._config.env_access_key, "").strip()
    
    def get_secret_key(self) -> str:
        """환경변수에서 시크릿 키 조회."""
        return os.getenv(self._config.env_secret_key, "").strip()
    
    def has_api_keys(self) -> bool:
        """API 키가 설정되어 있는지 확인."""
        return bool(self.get_access_key() and self.get_secret_key())
    
    # =========================================================================
    # 금액 포맷팅
    # =========================================================================
    
    def format(self, amount: float, *, with_suffix: bool = True) -> str:
        """금액을 통화에 맞게 포맷팅.
        
        Examples:
            50.00 → "50.00 USDT"
            1234.56 → "1,234.56 USDT"
        """
        try:
            val = float(amount)
            if self._config.decimals == 0:
                formatted = f"{val:,.0f}"
            else:
                formatted = f"{val:,.{self._config.decimals}f}"
            
            if with_suffix:
                return f"{formatted}{self._config.display_suffix}"
            return formatted
        except (TypeError, ValueError):
            logger.warning("[Currency] format() failed for amount=%r", amount)
            return str(amount)
    
    def format_compact(self, amount: float) -> str:
        """금액을 압축 형식으로 포맷팅 (K, M, B 접미사).
        
        Examples:
            1500000 → "1.5M"
            50000 → "50K"
        """
        try:
            val = abs(float(amount))
            sign = "-" if float(amount) < 0 else ""
            
            if val >= 1_000_000_000:
                return f"{sign}{val/1_000_000_000:.1f}B"
            elif val >= 1_000_000:
                return f"{sign}{val/1_000_000:.1f}M"
            elif val >= 1_000:
                return f"{sign}{val/1_000:.1f}K"
            else:
                return self.format(amount, with_suffix=False)
        except (TypeError, ValueError):
            logger.warning("[Currency] format_compact() failed for amount=%r", amount)
            return str(amount)
    
    # =========================================================================
    # 마켓 심볼 변환
    # =========================================================================
    
    def market(self, base: str) -> str:
        """기본 통화로 마켓 심볼 생성.
        
        Args:
            base: 기본 통화 (예: "BTC", "ETH")
        
        Returns:
            Example: "BTCUSDT"
            USDT: "BTCUSDT"
        """
        b = str(base).upper().strip()
        # 이미 완전한 형식이면 그대로 반환
        if self._is_complete_market(b):
            return b
        # 접두사/접미사가 있으면 제거
        b = self._extract_base(b)
        
        if self._config.market_prefix:
            return f"{self._config.market_prefix}{b}"
        elif self._config.market_suffix:
            return f"{b}{self._config.market_suffix}"
        else:
            return f"{b}{self._config.market_separator}{self._config.symbol}"
    
    def market_ccxt(self, base: str) -> str:
        """CCXT 형식의 마켓 심볼 생성 (예: "BTC/USDT").
        
        Args:
            base: 기본 통화 (예: "BTC")
        
        Returns:
            "BTC/USDT"
        """
        b = self._extract_base(base)
        return f"{b}/{self._config.symbol}"
    
    def market_ws(self, base: str) -> str:
        """WebSocket 형식의 마켓 심볼 생성 (소문자).
        
        Args:
            base: 기본 통화 (예: "BTC")
        
        Returns:
            Example: "btcusdt"
            USDT: "btcusdt"
        """
        return self.market(base).lower()
    
    def parse_market(self, market: str) -> Tuple[str, str]:
        """마켓 심볼에서 base와 quote 추출.
        
        Args:
            market: 마켓 심볼 (예: "BTCUSDT", "BTC/USDT")
        
        Returns:
            (base, quote) 튜플 (예: ("BTC", "USDT"))
        """
        m = str(market).upper().strip()
        
        # Dash format (BTC-XXX) — Upbit/Poloniex legacy, not used on Bybit
        if m.startswith("BTC-"):
            return (m[4:], "BTC")
        
        # BTC/USDT 형식
        if "/" in m:
            parts = m.split("/", 1)
            return (parts[0], parts[1])
        
        # BTCUSDT 형식
        for quote in ("USDT", "BUSD", "USDC"):
            # Bare quote symbols ("USDT") are not complete markets.
            # Require at least 1-char base to avoid parsing base="".
            if len(m) > len(quote) and m.endswith(quote):
                return (m[:-len(quote)], quote)
        
        # 알 수 없는 형식 - base만 반환
        return (m, self._config.symbol)
    
    def extract_base(self, market: str) -> str:
        """마켓 심볼에서 기본 통화(base) 추출.
        
        Args:
            market: 마켓 심볼 (예: "BTCUSDT")
        
        Returns:
            기본 통화 (예: "BTC")
        """
        base, _ = self.parse_market(market)
        return base
    
    def normalize(self, market: str) -> str:
        """마켓 심볼을 현재 통화 형식으로 정규화.
        
        다양한 입력 형식을 현재 설정된 통화 형식으로 변환.
        
        Examples:
            Bybit: "BTC" → "BTCUSDT"
            
        """
        base, _ = self.parse_market(market)
        return self.market(base)
    
    def _is_complete_market(self, s: str) -> bool:
        """완전한 마켓 심볼인지 확인."""
        return s.endswith("USDT") or s.endswith("BUSD") or "/" in s
    
    def _extract_base(self, s: str) -> str:
        """문자열에서 base 통화만 추출."""
        base, _ = self.parse_market(s)
        return base
    
    # =========================================================================
    # 주문 방향 변환
    # =========================================================================
    
    @property
    def side_buy(self) -> str:
        """매수 방향 문자열."""
        return self._config.order_side_buy
    
    @property
    def side_sell(self) -> str:
        """매도 방향 문자열."""
        return self._config.order_side_sell
    
    def normalize_side(self, side: str) -> str:
        """주문 방향을 현재 거래소 형식으로 정규화.
        
        Args:
            side: "buy", "sell", "bid", "ask" 중 하나
        
        Returns:
            현재 거래소에 맞는 방향 문자열
        """
        s = str(side).lower().strip()
        if s in ("buy", "bid", "long"):
            return self._config.order_side_buy
        elif s in ("sell", "ask", "short"):
            return self._config.order_side_sell
        return s
    
    def is_buy_side(self, side: str) -> bool:
        """매수 방향인지 확인."""
        return str(side).lower().strip() in ("buy", "bid", "long")
    
    def is_sell_side(self, side: str) -> bool:
        """매도 방향인지 확인."""
        return str(side).lower().strip() in ("sell", "ask", "short")
    
    # =========================================================================
    # 금액 검증 및 변환
    # =========================================================================
    
    def floor_to_unit(self, amount: float) -> float:
        """금액을 주문 단위로 내림.
        
        Examples:
            USDT: 10.567 → 10.56
        """
        if self._config.order_unit <= 0:
            return float(amount)
        return float(int(float(amount) / self._config.order_unit) * self._config.order_unit)
    
    def is_valid_order_amount(self, amount: float) -> bool:
        """주문 금액이 최소 금액 이상인지 확인."""
        return float(amount) >= self._config.min_order
    
    def validate_order_amount(self, amount: float) -> Tuple[bool, str]:
        """주문 금액 검증 및 메시지 반환.
        
        Returns:
            (valid, message) 튜플
        """
        val = float(amount)
        if val < self._config.min_order:
            return (False, f"최소 주문금액 미달: {self.format(val)} < {self.format(self._config.min_order)}")
        return (True, "")
    
    # =========================================================================
    # 유틸리티
    # =========================================================================
    
    def __repr__(self) -> str:
        return f"QuoteCurrency({self._symbol})"
    
    def __str__(self) -> str:
        return self._symbol
    
    def to_dict(self) -> dict:
        """설정을 딕셔너리로 반환."""
        return {
            "symbol": self._config.symbol,
            "name": self._config.name,
            "min_order": self._config.min_order,
            "order_unit": self._config.order_unit,
            "decimals": self._config.decimals,
            "exchange": self._config.exchange,
            "api_base": self._config.api_base,
            "market_prefix": self._config.market_prefix,
            "market_suffix": self._config.market_suffix,
            "is_usdt": self.is_usdt,
            "has_api_keys": self.has_api_keys(),
            "env_access_key": self._config.env_access_key,
            "env_secret_key": self._config.env_secret_key,
        }


# ============================================================
# 글로벌 싱글톤 인스턴스
# ============================================================

# 환경변수에서 기축통화 설정 로드 (Bybit = USDT)
_QUOTE_CURRENCY_SYMBOL = _env_str("QUOTE_CURRENCY", "USDT").upper()
if _QUOTE_CURRENCY_SYMBOL not in _CURRENCY_CONFIGS:
    _QUOTE_CURRENCY_SYMBOL = "USDT"

# 글로벌 인스턴스 (Q로 짧게 사용 가능)
Q = QuoteCurrency(_QUOTE_CURRENCY_SYMBOL)


# ============================================================
# 편의 함수 (직접 import 가능)
# ============================================================

def get_quote_currency() -> QuoteCurrency:
    """현재 설정된 기축통화 인스턴스 반환."""
    return Q


def set_quote_currency(symbol: str) -> QuoteCurrency:
    """기축통화 변경 (런타임 전환용).
    
    주의: 이 함수는 글로벌 상태를 변경합니다.
    일반적으로 서버 시작 시 환경변수로 설정하는 것을 권장합니다.
    """
    global Q
    Q = QuoteCurrency(symbol)
    return Q


def format_amount(amount: float, *, with_suffix: bool = True) -> str:
    """금액을 현재 통화에 맞게 포맷팅."""
    return Q.format(amount, with_suffix=with_suffix)


def market(base: str) -> str:
    """기본 통화로 마켓 심볼 생성."""
    return Q.market(base)


def normalize_market(market_str: str) -> str:
    """마켓 심볼을 현재 통화 형식으로 정규화."""
    return Q.normalize(market_str)


def extract_base(market_str: str) -> str:
    """마켓 심볼에서 기본 통화 추출."""
    return Q.extract_base(market_str)


# ============================================================
# 테스트용 예제
# ============================================================

if __name__ == "__main__":
    print(f"Current Quote Currency: {Q}")
    print(f"Config: {Q.to_dict()}")
    print()
    print("Format examples:")
    print(f"  50000 → {Q.format(50000)}")
    print(f"  1234567.89 → {Q.format(1234567.89)}")
    print(f"  Compact 1500000 → {Q.format_compact(1500000)}")
    print()
    print("Market examples:")
    print(f"  BTC → {Q.market('BTC')}")
    print(f"  ETH → {Q.market('ETH')}")
    print(f"  CCXT BTC → {Q.market_ccxt('BTC')}")
    print(f"  WS BTC → {Q.market_ws('BTC')}")
    print()
    print("Parse examples:")
    print(f"  BTCUSDT → {Q.parse_market('BTCUSDT')}")
    print(f"  BTCUSDT → {Q.parse_market('BTCUSDT')}")
    print(f"  BTC/USDT → {Q.parse_market('BTC/USDT')}")
    print()
    print("Normalize examples:")
    print(f"  BTCUSDT → {Q.normalize('BTCUSDT')}")
    print(f"  BTC/USDT → {Q.normalize('BTC/USDT')}")
    print(f"  ETHUSDT → {Q.normalize('ETHUSDT')}")
