# ============================================================
# File: autocoin/hyper/event/hyper_event_types.py
# Hyper Event Type Definitions
# ============================================================

from enum import Enum, auto


class HyperEventType(Enum):

    PRICE_TICK = auto()

    ENGINE_START = auto()
    ENGINE_STOP = auto()
    ENGINE_TICK = auto()
    ENGINE_SIGNAL = auto()

    SYSTEM_BOOT = auto()
    SYSTEM_SHUTDOWN = auto()
    SYSTEM_HEALTH = auto()

    CUSTOM = auto()
