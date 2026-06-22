"""
마켓 심볼 정규화 유틸리티 (Bybit 전용)
- BTCUSDT ↔ BTC/USDT 상호 변환
- 잘못된 형식 자동 수정
"""
import re
from typing import Optional


class MarketSymbol:
    """마켓 심볼 정규화"""

    # Bybit native = "BTCUSDT", slash = "BTC/USDT"
    BINANCE_PATTERN = re.compile(r"^([A-Z0-9]{2,10})/([A-Z]{3,4})$")
    BYBIT_PATTERN = re.compile(r"^([A-Z0-9]{2,10})(USDT|USDC|BTC)$")

    @classmethod
    def normalize_bybit(cls, symbol: str) -> Optional[str]:
        """Bybit 형식으로 정규화 (BTCUSDT)."""
        if not symbol or not isinstance(symbol, str):
            return None
        symbol = symbol.strip().upper()
        if cls.BYBIT_PATTERN.match(symbol):
            return symbol
        # Legacy dash format → BTCUSDT
        if "-" in symbol:
            parts = symbol.split("-", 1)
            if parts[0] == "USDT":
                return f"{parts[1]}USDT"
            return f"{parts[0]}{parts[1]}"
        if "/" in symbol:
            parts = symbol.split("/", 1)
            return f"{parts[0]}{parts[1]}" if len(parts) == 2 else None
        if len(symbol) >= 2 and not any(c in symbol for c in ["-", "/"]):
            for q in ("USDT", "USDC", "BTC"):
                if symbol.endswith(q) and len(symbol) > len(q):
                    return symbol
            return f"{symbol}USDT"
        return None

    @classmethod
    def normalize_slash(cls, symbol: str) -> Optional[str]:
        """슬래시 형식으로 정규화 (BTC/USDT)."""
        if not symbol or not isinstance(symbol, str):
            return None
        symbol = symbol.strip().upper()
        if cls.BINANCE_PATTERN.match(symbol):
            return symbol
        # Bybit native → slash
        bybit_match = cls.BYBIT_PATTERN.match(symbol)
        if bybit_match:
            return f"{bybit_match.group(1)}/{bybit_match.group(2)}"
        if "-" in symbol:
            parts = symbol.split("-")
            if len(parts) == 2:
                # Legacy → BTC/USDT
                if parts[0] == "USDT":
                    return f"{parts[1]}/USDT"
                return f"{parts[1]}/{parts[0]}"
        # 슬래시 없는 형식 (BTCUSDT → BTC/USDT)
        if symbol.endswith("USDT") and len(symbol) > 6:
            base = symbol[:-4]
            return f"{base}/USDT"
        return None

    @classmethod
    def split(cls, symbol: str) -> Optional[tuple[str, str]]:
        """심볼을 (base, quote) 튜플로 분리."""
        if not symbol:
            return None
        symbol = symbol.strip().upper()
        # Slash format
        binance_match = cls.BINANCE_PATTERN.match(symbol)
        if binance_match:
            base, quote = binance_match.groups()
            return (base, quote)
        # Bybit native
        bybit_match = cls.BYBIT_PATTERN.match(symbol)
        if bybit_match:
            return (bybit_match.group(1), bybit_match.group(2))
        return None

    @classmethod
    def is_usdt_market(cls, symbol: str) -> bool:
        """USDT 마켓 여부 확인"""
        normalized = cls.normalize_bybit(symbol)
        return bool(normalized and normalized.endswith("USDT"))

    @classmethod
    def convert_exchange(cls, symbol: str, target_exchange: str = "bybit") -> Optional[str]:
        target = target_exchange.lower()
        if target == "bybit":
            return cls.normalize_bybit(symbol)
        elif target in ("binance", "slash"):
            return cls.normalize_slash(symbol)
        return None


# 편의 함수
def normalize_market(symbol: str, exchange: str = "bybit") -> Optional[str]:
    """마켓 심볼 정규화 (편의 함수)"""
    return MarketSymbol.convert_exchange(symbol, exchange)


def is_valid_market(symbol: str) -> bool:
    """유효한 마켓 심볼인지 검증"""
    return MarketSymbol.normalize_bybit(symbol) is not None


# 데코레이터: 자동 정규화
def auto_normalize_market(exchange: str = "bybit"):
    """
    함수의 market 파라미터를 자동으로 정규화하는 데코레이터

    사용 예:
    ```python
    @auto_normalize_market("bybit")
    def my_function(market: str, ...):
        # market은 항상 "BTCUSDT" 형식
        pass
    ```
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            import inspect
            sig = inspect.signature(func)
            params = list(sig.parameters.keys())

            if "market" in params:
                idx = params.index("market")
                if len(args) > idx:
                    market = args[idx]
                    normalized = MarketSymbol.convert_exchange(market, exchange)
                    if normalized:
                        args = list(args)
                        args[idx] = normalized
                        args = tuple(args)

            if "market" in kwargs:
                normalized = MarketSymbol.convert_exchange(kwargs["market"], exchange)
                if normalized:
                    kwargs["market"] = normalized

            return func(*args, **kwargs)

        return wrapper
    return decorator
