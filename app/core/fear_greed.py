"""
Fear & Greed Index Module
- Crypto Fear & Greed Index API 연동
- 역발상 예산 배율 적용 (공포 시 매수, 탐욕 시 경계)

[MIGRATED 2026-01-23] CoinStock → Autocoin
- 통화 중립적 모듈이므로 변환 불필요
"""

from __future__ import annotations
from typing import Optional, Dict, Any
from dataclasses import dataclass
import logging
import time
import asyncio

from app.core.constants import env_bool, env_float, env_int

logger = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    logger.info("[FearGreed] httpx not available, F&G API calls will use fallback")
    httpx = None

@dataclass
class FearGreedResult:
    value: int  # 0-100
    classification: str  # "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"
    budget_multiplier: float
    timestamp: float
    source: str  # "api" or "cache" or "fallback"

class FearGreedIndex:
    """Fear & Greed Index 연동"""
    
    API_URL = "https://api.alternative.me/fng/?limit=1"
    
    def __init__(self):
        self.enabled = env_bool("OMA_FEAR_GREED_ENABLED", default=False)
        self.cache_sec = env_float("OMA_FEAR_GREED_CACHE_SEC", default=3600.0)  # 1시간
        self.max_stale_sec = env_float("OMA_FEAR_GREED_MAX_STALE_SEC", default=21600.0)  # 6시간
        
        # 역발상 예산 배율
        self.extreme_fear_mult = env_float("OMA_FG_EXTREME_FEAR_MULT", default=1.30)
        self.fear_mult = env_float("OMA_FG_FEAR_MULT", default=1.15)
        self.neutral_mult = env_float("OMA_FG_NEUTRAL_MULT", default=1.00)
        self.greed_mult = env_float("OMA_FG_GREED_MULT", default=0.85)
        self.extreme_greed_mult = env_float("OMA_FG_EXTREME_GREED_MULT", default=0.70)
        
        # 캐시
        self._cached_result: Optional[FearGreedResult] = None
    
    def get_multiplier_for_value(self, value: int) -> float:
        """F&G 값에 따른 예산 배율 반환"""
        if value <= 25:
            return self.extreme_fear_mult
        elif value <= 45:
            return self.fear_mult
        elif value <= 55:
            return self.neutral_mult
        elif value <= 75:
            return self.greed_mult
        else:
            return self.extreme_greed_mult
    
    def get_classification(self, value: int) -> str:
        """F&G 값에 따른 분류 반환"""
        if value <= 25:
            return "Extreme Fear"
        elif value <= 45:
            return "Fear"
        elif value <= 55:
            return "Neutral"
        elif value <= 75:
            return "Greed"
        else:
            return "Extreme Greed"
    
    def fetch(self, force_refresh: bool = False) -> FearGreedResult:
        """동기 방식으로 F&G 지수 가져오기"""
        if not self.enabled:
            return self._fallback_result()
        
        now = time.time()
        
        # 캐시 확인
        if not force_refresh and self._cached_result:
            if (now - self._cached_result.timestamp) < self.cache_sec:
                return FearGreedResult(
                    value=self._cached_result.value,
                    classification=self._cached_result.classification,
                    budget_multiplier=self._cached_result.budget_multiplier,
                    timestamp=self._cached_result.timestamp,
                    source="cache"
                )
        
        # API 호출
        try:
            if httpx is None:
                return self._fallback_result()
            
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(self.API_URL)
                resp.raise_for_status()
                data = resp.json()
            
            if "data" in data and len(data["data"]) > 0:
                fg_data = data["data"][0]
                value = int(fg_data.get("value", 50))
                classification = str(fg_data.get("value_classification", "Neutral"))
                
                result = FearGreedResult(
                    value=value,
                    classification=classification,
                    budget_multiplier=self.get_multiplier_for_value(value),
                    timestamp=now,
                    source="api"
                )
                self._cached_result = result
                return result
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.warning("[FearGreed] sync API network error: %s", e)
        except Exception as exc:
            logger.warning("[FearGreed] sync API fetch failed", exc_info=True)
            logger.warning("[fear_greed] %s: %s", 'API 호출', exc, exc_info=True)

        # 캐시가 있으면 반환 (max_stale 초과 시 neutral fallback)
        if self._cached_result:
            stale_duration = now - self._cached_result.timestamp
            if stale_duration > self.max_stale_sec:
                # 너무 오래된 캐시 - neutral fallback으로 안전하게
                return FearGreedResult(
                    value=50,
                    classification="Neutral (stale)",
                    budget_multiplier=1.0,
                    timestamp=self._cached_result.timestamp,
                    source="cache_expired"
                )
            return FearGreedResult(
                value=self._cached_result.value,
                classification=self._cached_result.classification,
                budget_multiplier=self._cached_result.budget_multiplier,
                timestamp=self._cached_result.timestamp,
                source="cache_stale"
            )
        
        return self._fallback_result()
    
    async def fetch_async(self, force_refresh: bool = False) -> FearGreedResult:
        """비동기 방식으로 F&G 지수 가져오기"""
        if not self.enabled:
            return self._fallback_result()
        
        now = time.time()
        
        # 캐시 확인
        if not force_refresh and self._cached_result:
            if (now - self._cached_result.timestamp) < self.cache_sec:
                return FearGreedResult(
                    value=self._cached_result.value,
                    classification=self._cached_result.classification,
                    budget_multiplier=self._cached_result.budget_multiplier,
                    timestamp=self._cached_result.timestamp,
                    source="cache"
                )
        
        # API 호출
        try:
            if httpx is None:
                return self._fallback_result()
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self.API_URL)
                resp.raise_for_status()
                data = resp.json()
            
            if "data" in data and len(data["data"]) > 0:
                fg_data = data["data"][0]
                value = int(fg_data.get("value", 50))
                classification = str(fg_data.get("value_classification", "Neutral"))
                
                result = FearGreedResult(
                    value=value,
                    classification=classification,
                    budget_multiplier=self.get_multiplier_for_value(value),
                    timestamp=now,
                    source="api"
                )
                self._cached_result = result
                return result
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.warning("[FearGreed] async API network error: %s", e)
        except Exception as exc:
            logger.warning("[FearGreed] async API fetch failed", exc_info=True)
            logger.warning("[fear_greed] %s: %s", 'API 호출', exc, exc_info=True)

        # 캐시가 있으면 반환 (max_stale 초과 시 neutral fallback)
        if self._cached_result:
            stale_duration = now - self._cached_result.timestamp
            if stale_duration > self.max_stale_sec:
                return FearGreedResult(
                    value=50,
                    classification="Neutral (stale)",
                    budget_multiplier=1.0,
                    timestamp=self._cached_result.timestamp,
                    source="cache_expired"
                )
            return FearGreedResult(
                value=self._cached_result.value,
                classification=self._cached_result.classification,
                budget_multiplier=self._cached_result.budget_multiplier,
                timestamp=self._cached_result.timestamp,
                source="cache_stale"
            )
        
        return self._fallback_result()
    
    def _fallback_result(self) -> FearGreedResult:
        """API 실패 시 기본값"""
        return FearGreedResult(
            value=50,
            classification="Neutral",
            budget_multiplier=1.0,
            timestamp=time.time(),
            source="fallback"
        )
    
    def get_last_result(self) -> Optional[FearGreedResult]:
        """마지막 캐시된 결과 반환"""
        return self._cached_result

# 싱글톤
_fear_greed: Optional[FearGreedIndex] = None

def get_fear_greed() -> FearGreedIndex:
    global _fear_greed
    if _fear_greed is None:
        _fear_greed = FearGreedIndex()
    return _fear_greed

# Alias for API compatibility
get_fear_greed_index = get_fear_greed
