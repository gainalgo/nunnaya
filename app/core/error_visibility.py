from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any

_THROTTLE_LOCK = threading.Lock()
_THROTTLE_STATE: dict[str, dict[str, float]] = {}

# Network/transient errors — one-line summary is enough, no full traceback
_BRIEF_EXCEPTIONS = (
    ConnectionError, TimeoutError, OSError,
)

try:
    import requests.exceptions as _req_exc
    _BRIEF_EXCEPTIONS = (*_BRIEF_EXCEPTIONS, _req_exc.ConnectionError, _req_exc.Timeout)
except ImportError:
    pass

try:
    import urllib3.exceptions as _u3_exc
    _BRIEF_EXCEPTIONS = (*_BRIEF_EXCEPTIONS, _u3_exc.ProtocolError, _u3_exc.ReadTimeoutError)
except ImportError:
    pass


def _is_network_error() -> bool:
    """Check if the current exception is a transient network error."""
    exc = sys.exc_info()[1]
    return isinstance(exc, _BRIEF_EXCEPTIONS)


def report_suppressed_exception(
    logger_or_name: Any,
    context: str,
    *,
    level: int = logging.WARNING,
    throttle_sec: float = 60.0,
) -> None:
    """Log an exception that would otherwise be silently swallowed.

    This helper is designed for `except ...:` blocks where the code wants to
    continue running, but operators still need visibility into failures.
    Repeated exceptions are rate-limited per logger/context pair to avoid
    overwhelming hot paths such as background loops.

    Network/transient errors (ConnectionError, Timeout, etc.) are logged
    as a single line without full traceback to reduce log noise.
    """

    if isinstance(logger_or_name, logging.Logger):
        logger = logger_or_name
        logger_name = logger.name
    else:
        logger_name = str(logger_or_name or __name__)
        logger = logging.getLogger(logger_name)

    key = f"{logger_name}:{context}:{int(level)}"
    now = time.monotonic()

    with _THROTTLE_LOCK:
        state = _THROTTLE_STATE.setdefault(key, {"last": 0.0, "suppressed": 0.0})
        elapsed = now - float(state.get("last", 0.0) or 0.0)
        if elapsed < float(throttle_sec):
            state["suppressed"] = float(state.get("suppressed", 0.0) or 0.0) + 1.0
            return

        repeat_count = int(state.get("suppressed", 0.0) or 0.0)
        state["last"] = now
        state["suppressed"] = 0.0

    # Network errors: brief one-line log. Other errors: full traceback.
    show_traceback = not _is_network_error()
    exc = sys.exc_info()[1]
    brief_msg = f": {type(exc).__name__}: {exc}" if exc and not show_traceback else ""

    if repeat_count > 0:
        logger.log(
            level,
            "[suppressed-exception] %s (repeated %d times during %.0fs)%s",
            context,
            repeat_count,
            float(throttle_sec),
            brief_msg,
            exc_info=show_traceback,
        )
        return

    logger.log(level, "[suppressed-exception] %s%s", context, brief_msg, exc_info=show_traceback)
