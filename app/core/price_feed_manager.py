# -*- coding: utf-8 -*-
"""
Price Feed Manager - automatic WebSocket/REST switching.

- Falls back to WebSocket data automatically when REST is banned
- Status display support
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from threading import Lock
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class FeedStatus(str, Enum):
    NORMAL = "normal"           # 🟢 normal
    REST_LIMITED = "limited"    # 🟡 REST limited, using WebSocket
    DEGRADED = "degraded"       # 🟠 some features limited


class PriceFeedManager:
    """Price feed status manager."""
    
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
        """Set the REST API limited state."""
        with self._lock:
            self._status = FeedStatus.REST_LIMITED
            self._status_message = f"REST API limited ({int(remaining_sec)}s), using realtime data"
            logger.info("[PriceFeed] %s", self._status_message)
    
    def set_normal(self) -> None:
        """Return to the normal state."""
        with self._lock:
            if self._status != FeedStatus.NORMAL:
                self._status = FeedStatus.NORMAL
                self._status_message = ""
                logger.info("[PriceFeed] returned to normal state")
    
    def record_ws_update(self) -> None:
        """Record WebSocket data reception."""
        with self._lock:
            self._ws_last_update = time.time()
    
    def is_ws_alive(self, timeout_sec: float = 30.0) -> bool:
        """Check whether the WebSocket is alive."""
        with self._lock:
            if self._ws_last_update == 0:
                return False
            return (time.time() - self._ws_last_update) < timeout_sec
    
    def status_dict(self) -> Dict[str, Any]:
        """Status info for the dashboard."""
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


# singleton
price_feed_manager = PriceFeedManager()
