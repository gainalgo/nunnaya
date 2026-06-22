# ============================================================
# File: autocoin/hyper/event/hyper_event_dispatcher.py
# 고급 Dispatcher – async/sync handler 자동 지원
# ============================================================

import asyncio
import logging
from typing import Callable, Dict, Any, Coroutine
from .hyper_event_bus import HyperEventBus, HyperEvent
from .hyper_event_types import HyperEventType

logger = logging.getLogger(__name__)


class HyperEventDispatcher:
    def __init__(self, bus: HyperEventBus):
        self.bus = bus
        self._handlers: Dict[str, Callable[[HyperEvent], Any]] = {}

    # --------------------------------------------------------
    # Handler Registration
    # --------------------------------------------------------
    def register(self, channel: str, handler: Callable[[HyperEvent], Any]):
        self._handlers[channel] = handler
        asyncio.create_task(self._listen(channel, handler))

    # --------------------------------------------------------
    # Event Publish Wrappers
    # --------------------------------------------------------
    async def emit(self, channel: str, event_type: HyperEventType, data: Any):
        await self.bus.publish(event_type, channel, data)

    async def broadcast(self, event_type: HyperEventType, data: Any):
        await self.bus.broadcast(event_type, data)

    # --------------------------------------------------------
    # Internal Listener Task
    # --------------------------------------------------------
    async def _listen(self, channel: str, handler: Callable):
        async for event in self.bus.subscribe(channel):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    # sync handler 는 thread executor 로 실행
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, handler, event)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                logger.warning("[HyperEventDispatcher] handler error: %s", e, exc_info=True)
