# -*- coding: utf-8 -*-
"""API Rate Limiter."""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

def _env_bool(key: str, default: bool = False) -> bool:
    v = str(os.getenv(key, "1" if default else "0")).strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        logger.warning("[RateLimiter] _env_float(%s) parse failed, using default %s", key, default)
        return default

def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, str(default))))
    except (TypeError, ValueError):
        logger.warning("[RateLimiter] _env_int(%s) parse failed, using default %s", key, default)
        return default

@dataclass
class RateLimitState:
    weight_used: int = 0
    window_start: float = field(default_factory=time.time)
    banned_until: Optional[float] = None
    last_request: float = 0.0
    error_count: int = 0

class RateLimiter:
    """
    API Rate Limiter.
    
    - Configurable request weight / window
    - Ban detection support
    """
    
    WINDOW_SEC = 60
    WEIGHT_LIMIT = 600  # Bybit: 600 requests / 분
    
    def __init__(self) -> None:
        self._state = RateLimitState()
        self._lock = Lock()
        
        self.soft_threshold = _env_float("API_RATE_SOFT_THRESHOLD", 0.6)  # 70% → 60% (더 보수적)
        self.hard_threshold = _env_float("API_RATE_HARD_THRESHOLD", 0.75)  # 90% → 75% (더 보수적)
        self.weight_limit = _env_int("API_RATE_WEIGHT_LIMIT", self.WEIGHT_LIMIT)
        self.min_backoff_sec = _env_float("API_RATE_MIN_BACKOFF", 0.1)
        self.max_backoff_sec = _env_float("API_RATE_MAX_BACKOFF", 30.0)
        self.enabled = _env_bool("API_RATE_LIMITER_ENABLED", True)
    
    def _reset_window_if_needed(self) -> None:
        now = time.time()
        if now - self._state.window_start >= self.WINDOW_SEC:
            self._state.weight_used = 0
            self._state.window_start = now
            self._state.error_count = 0
    
    def is_banned(self) -> bool:
        with self._lock:
            if self._state.banned_until is None:
                return False
            now = time.time()
            if now >= self._state.banned_until:
                self._state.banned_until = None
                logger.info("[RateLimiter] Ban expired, resuming")
                # price_feed_manager에 정상 복귀 알림
                try:
                    from app.core.price_feed_manager import price_feed_manager
                    price_feed_manager.set_normal()
                except ImportError as exc:
                    logger.info("[RateLimiter] price_feed_manager not available for set_normal")
                    logger.warning("[rate_limiter] %s: %s", 'price_feed_manager에 정상 복귀 알림', exc, exc_info=True)
                return False
            return True
    
    def get_ban_remaining(self) -> float:
        with self._lock:
            if self._state.banned_until is None:
                return 0.0
            return max(0.0, self._state.banned_until - time.time())
    
    def set_banned(self, until_ts: float) -> None:
        with self._lock:
            self._state.banned_until = until_ts
            remaining = until_ts - time.time()
            logger.warning("[RateLimiter] Banned until %s (%.0f sec remaining)",
                          time.strftime("%H:%M:%S", time.localtime(until_ts)),
                          remaining)
            # price_feed_manager에 상태 알림
            try:
                from app.core.price_feed_manager import price_feed_manager
                price_feed_manager.set_rest_limited(remaining)
            except ImportError as exc:
                logger.info("[RateLimiter] price_feed_manager not available for set_rest_limited")
                logger.warning("[rate_limiter] %s: %s", 'price_feed_manager에 상태 알림', exc, exc_info=True)
    
    def can_request(self, weight: int = 1) -> bool:
        if not self.enabled:
            return True
        if self.is_banned():
            return False
        with self._lock:
            self._reset_window_if_needed()
            projected = self._state.weight_used + weight
            return projected <= self.weight_limit * self.hard_threshold
    
    def acquire(self, weight: int = 1) -> bool:
        if not self.enabled:
            return True
        if self.is_banned():
            return False
        with self._lock:
            self._reset_window_if_needed()
            projected = self._state.weight_used + weight
            if projected > self.weight_limit * self.hard_threshold:
                logger.warning("[RateLimiter] Blocked: %d/%d (%.1f%%)",
                              self._state.weight_used, self.weight_limit,
                              self._state.weight_used / self.weight_limit * 100)
                return False
            self._state.weight_used += weight
            self._state.last_request = time.time()
            usage = self._state.weight_used / self.weight_limit
            if usage >= self.soft_threshold:
                logger.warning("[RateLimiter] Approaching limit: %d/%d (%.1f%%)",
                              self._state.weight_used, self.weight_limit, usage * 100)
            return True
    
    def record_success(self) -> None:
        with self._lock:
            self._state.error_count = 0
    
    def record_error(self, error_code: Optional[int] = None, ban_until: Optional[float] = None) -> None:
        with self._lock:
            self._state.error_count += 1
            if ban_until is not None:
                self._state.banned_until = ban_until
            elif error_code == -1003:
                self._state.banned_until = time.time() + 60
    
    def parse_ban_from_error(self, error_msg: str) -> Optional[float]:
        """에러 메시지에서 ban until timestamp 추출."""
        try:
            if "IP banned until" in error_msg:
                import re
                match = re.search(r"banned until (\d+)", error_msg)
                if match:
                    ban_ts_ms = int(match.group(1))
                    return ban_ts_ms / 1000.0
        except (TypeError, ValueError) as exc:
            logger.warning("[RateLimiter] parse_ban_from_error failed", exc_info=True)
            logger.warning("[rate_limiter] %s: %s", 'rate_limiter.parse_ban_from_error fallback', exc, exc_info=True)
        return None
    
    def handle_api_error(self, error_msg: str, error_code: Optional[int] = None) -> None:
        """API 에러 처리 - ban 감지 시 자동 설정."""
        ban_ts = self.parse_ban_from_error(error_msg)
        if ban_ts:
            self.set_banned(ban_ts)
        elif error_code == -1003:
            self.set_banned(time.time() + 60)
        else:
            self.record_error(error_code)
    
    def reset(self) -> None:
        with self._lock:
            self._state.weight_used = 0
            self._state.error_count = 0
            self._state.banned_until = None
    
    def status(self) -> dict:
        with self._lock:
            self._reset_window_if_needed()
            usage = self._state.weight_used / self.weight_limit
            # ★ DEADLOCK FIX: is_banned()/get_ban_remaining()도 _lock 사용 → 직접 접근
            banned_until = self._state.banned_until
            now = time.time()
            is_ban = banned_until is not None and now < banned_until
            ban_remain = max(0.0, banned_until - now) if is_ban else 0.0
            return {
                "enabled": self.enabled,
                "weight_used": self._state.weight_used,
                "weight_limit": self.weight_limit,
                "usage_pct": round(usage * 100, 1),
                "banned": is_ban,
                "ban_remaining_sec": round(ban_remain, 1),
                "error_count": self._state.error_count,
            }

# 싱글톤
rate_limiter = RateLimiter()

# ============================================================================
# Bybit Rate Limiter (초당 8회 제한)
# ============================================================================

from collections import deque
from functools import wraps
from typing import Callable

class BybitRateLimiter:
    """
    Bybit API Rate Limiter (Token Bucket)
    
    Bybit API 제한:
    - 초당 10회 (10 req/sec)
    - 분당 600회 (600 req/min)
    
    안전 마진을 위해:
    - 초당 8회로 제한 (80%)
    - 분당 480회로 제한 (80%)
    """
    
    _instance = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        # Token Bucket 설정
        self.max_per_sec = 8  # 초당 8회 (안전 마진)
        self.max_per_min = 480  # 분당 480회
        
        # 최근 요청 타임스탬프 큐
        self.requests_sec = deque(maxlen=self.max_per_sec)
        self.requests_min = deque(maxlen=self.max_per_min)
        
        # Thread-safe Lock
        self._lock = Lock()
        
        BybitRateLimiter._initialized = True
        
        logger.info(
            f"[BybitRateLimiter] Initialized: {self.max_per_sec} req/sec, "
            f"{self.max_per_min} req/min"
        )
    
    def acquire(self) -> float:
        """
        Rate limit 확인 후 필요 시 대기
        
        Returns:
            대기 시간 (초)
        """
        with self._lock:
            now = time.time()
            wait = 0.0
            
            # 1. 초당 제한 확인
            if len(self.requests_sec) >= self.max_per_sec:
                oldest = self.requests_sec[0]
                elapsed = now - oldest
                
                if elapsed < 1.0:
                    # 1초 안에 max_per_sec회 요청 발생
                    delay = 1.0 - elapsed + 0.01  # 10ms 버퍼
                    logger.debug(f"[BybitRateLimiter] Per-sec limit: wait {delay:.3f}s")
                    time.sleep(delay)
                    wait += delay
                    now = time.time()
            
            # 2. 분당 제한 확인
            if len(self.requests_min) >= self.max_per_min:
                oldest = self.requests_min[0]
                elapsed = now - oldest
                
                if elapsed < 60.0:
                    # 60초 안에 max_per_min회 요청 발생
                    delay = 60.0 - elapsed + 0.1  # 100ms 버퍼
                    logger.warning(f"[BybitRateLimiter] Per-min limit: wait {delay:.3f}s")
                    time.sleep(delay)
                    wait += delay
                    now = time.time()
            
            # 3. 요청 타임스탬프 기록
            self.requests_sec.append(now)
            self.requests_min.append(now)
            
            return wait
    
    def stats(self) -> dict:
        """통계 반환"""
        with self._lock:
            return {
                "max_per_sec": self.max_per_sec,
                "max_per_min": self.max_per_min,
                "recent_sec": len(self.requests_sec),
                "recent_min": len(self.requests_min),
            }

# Bybit 싱글톤
bybit_rate_limiter = BybitRateLimiter()

def bybit_rate_limit(func: Callable) -> Callable:
    """
    Bybit API Rate Limit 데코레이터

    Usage:
        @bybit_rate_limit
        def fetch_bybit_data():
            return requests.get(...)
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        wait = bybit_rate_limiter.acquire()
        if wait > 0.1:
            logger.debug(f"[{func.__name__}] Rate limited: waited {wait:.3f}s")
        return func(*args, **kwargs)

    return wrapper

# ============================================================================
# Bybit Rate Limiter (wrapper with is_order support)
# ============================================================================

class BybitRateLimiter:
    """Bybit V5 API Rate Limiter (wraps BybitRateLimiter + order rate limit)."""
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._inner = bybit_rate_limiter
        self._order_requests = deque(maxlen=8)
        self._order_lock = Lock()
        BybitRateLimiter._initialized = True

    def acquire(self, *, is_order: bool = False) -> float:
        wait = self._inner.acquire()
        if is_order:
            with self._order_lock:
                now = time.time()
                if len(self._order_requests) >= 8:
                    oldest = self._order_requests[0]
                    elapsed = now - oldest
                    if elapsed < 1.0:
                        delay = 1.0 - elapsed + 0.01
                        time.sleep(delay)
                        wait += delay
                        now = time.time()
                self._order_requests.append(now)
        return wait

    def stats(self):
        return {**self._inner.stats(), "order_recent_sec": len(self._order_requests)}

bybit_rate_limiter = BybitRateLimiter()

def bybit_rate_limit(func: Callable) -> Callable:
    @wraps(func)
    def wrapper(*args, **kwargs):
        bybit_rate_limiter.acquire()
        return func(*args, **kwargs)
    return wrapper

def bybit_order_rate_limit(func: Callable) -> Callable:
    @wraps(func)
    def wrapper(*args, **kwargs):
        bybit_rate_limiter.acquire(is_order=True)
        return func(*args, **kwargs)
    return wrapper

# ============================================================================
# Central Bybit HTTP GET — ALL Bybit reads must go through here
# ============================================================================

import threading
import requests as _requests
from requests.adapters import HTTPAdapter as _HTTPAdapter
from urllib3.util.retry import Retry as _Retry

_bybit_central_session: _requests.Session | None = None
_bybit_session_lock = threading.Lock()

# Concurrency gate: max 4 simultaneous in-flight requests to Bybit
_bybit_concurrency = threading.Semaphore(4)

def _get_central_session() -> _requests.Session:
    """Shared session with connection pooling + auto-retry on 502/503/504."""
    global _bybit_central_session
    if _bybit_central_session is None:
        with _bybit_session_lock:
            if _bybit_central_session is None:
                s = _requests.Session()
                retry = _Retry(
                    total=2,
                    backoff_factor=0.3,
                    status_forcelist=[502, 503, 504],
                    allowed_methods=["GET"],
                )
                adapter = _HTTPAdapter(
                    max_retries=retry,
                    pool_connections=4,
                    pool_maxsize=8,
                )
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _bybit_central_session = s
    return _bybit_central_session

def bybit_get(url: str, *, params: dict | None = None,
              timeout: float = 5.0) -> _requests.Response:
    """Rate-limited, connection-pooled GET to Bybit API.

    Every Bybit read request in the codebase should call this instead of
    ``requests.get()`` directly.  Benefits:
    - Rate limit enforced (8 req/sec, 480 req/min)
    - Max 4 concurrent in-flight requests (semaphore)
    - TCP/SSL connection reuse (shared Session)
    - Auto-retry on 502/503/504
    """
    bybit_rate_limiter.acquire()
    with _bybit_concurrency:
        return _get_central_session().get(url, params=params, timeout=timeout)
