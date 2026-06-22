# -*- coding: utf-8 -*-
"""
Price Feed Manager - WebSocket/REST 자동 전환.

- REST ban 시 WebSocket 데이터 자동 사용
- 상태 표시 지원
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from threading import Lock
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class FeedStatus(str, Enum):
    NORMAL = "normal"           # 🟢 정상
    REST_LIMITED = "limited"    # 🟡 REST 제한, WebSocket 사용 중
    DEGRADED = "degraded"       # 🟠 일부 기능 제한


class PriceFeedManager:
    """가격 피드 상태 관리자."""
    
    def __init__(self) -> None:
        self._lock = Lock()
        self._status = FeedStatus.NORMAL
        self._status_message = ""
        self._ws_last_update = 0.0
    
    def get_status(self) -> FeedStatus:
        with self._lock:
            return self._status
    
    def get_status_message(self) -> str:
        with self._lock:
            return self._status_message
    
    def set_rest_limited(self, remaining_sec: float) -> None:
        """REST API 제한 상태 설정."""
        with self._lock:
            self._status = FeedStatus.REST_LIMITED
            self._status_message = f"REST API 제한 중 ({int(remaining_sec)}초), 실시간 데이터 사용"
            logger.info("[PriceFeed] %s", self._status_message)
    
    def set_normal(self) -> None:
        """정상 상태로 복귀."""
        with self._lock:
            if self._status != FeedStatus.NORMAL:
                self._status = FeedStatus.NORMAL
                self._status_message = ""
                logger.info("[PriceFeed] 정상 상태 복귀")
    
    def record_ws_update(self) -> None:
        """WebSocket 데이터 수신 기록."""
        with self._lock:
            self._ws_last_update = time.time()
    
    def is_ws_alive(self, timeout_sec: float = 30.0) -> bool:
        """WebSocket이 살아있는지 확인."""
        with self._lock:
            if self._ws_last_update == 0:
                return False
            return (time.time() - self._ws_last_update) < timeout_sec
    
    def status_dict(self) -> Dict[str, Any]:
        """대시보드용 상태 정보."""
        with self._lock:
            return {
                "status": self._status.value,
                "message": self._status_message,
                "ws_alive": self.is_ws_alive(),
                "icon": {
                    FeedStatus.NORMAL: "🟢",
                    FeedStatus.REST_LIMITED: "🟡", 
                    FeedStatus.DEGRADED: "🟠",
                }.get(self._status, "⚪"),
            }


# 싱글톤
price_feed_manager = PriceFeedManager()
