# ============================================================
# File: autocoin/hyper/event/hyper_event_bus.py
# HyperEventBus – 초고속 비동기 Event Message Bus (개선판)
# ============================================================

import asyncio
from typing import Dict, List, Any, AsyncGenerator
from .hyper_event_types import HyperEventType


class HyperEvent:
    def __init__(self, event_type: HyperEventType, channel: str, data: Any):
        self.type = event_type
        self.channel = channel
        self.data = data

    def __repr__(self):
        return f"<HyperEvent {self.type.name} channel={self.channel}>"


class HyperEventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()  # race 방지

    # --------------------------------------------------------
    # Subscribe
    # --------------------------------------------------------
    async def subscribe(self, channel: str) -> AsyncGenerator[HyperEvent, None]:
        queue = asyncio.Queue()

        async with self._lock:
            if channel not in self._subscribers:
                self._subscribers[channel] = []
            self._subscribers[channel].append(queue)

        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            # 안전한 remove
            async with self._lock:
                if channel in self._subscribers and queue in self._subscribers[channel]:
                    self._subscribers[channel].remove(queue)

    # --------------------------------------------------------
    # Publish
    # --------------------------------------------------------
    async def publish(self, event_type: HyperEventType, channel: str, data: Any):
        async with self._lock:
            queues = list(self._subscribers.get(channel, []))

        if not queues:
            return

        event = HyperEvent(event_type, channel, data)

        for q in queues:
            await q.put(event)

    # --------------------------------------------------------
    # Broadcast
    # --------------------------------------------------------
    async def broadcast(self, event_type: HyperEventType, data: Any):
        async with self._lock:
            channels = list(self._subscribers.keys())

        for channel in channels:
            await self.publish(event_type, channel, data)
