"""
Central logging configuration module.

Reads environment variables (LOG_LEVEL, LOG_FORMAT) to configure the root logger.
Supports standard format or JSON structured logging.
Call setup_logging() once at app startup.
"""

import json
import logging
import os
from datetime import datetime


def _json_formatter(record: logging.LogRecord) -> str:
    """Serialize a LogRecord into a JSON string."""
    data = {
        "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
        "level": record.levelname,
        "name": record.name,
        "message": record.getMessage(),
    }
    if record.exc_info:
        data["exception"] = logging.Formatter().formatException(record.exc_info)
    return json.dumps(data, ensure_ascii=False)


class JsonFormatter(logging.Formatter):
    """Formatter for JSON structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        return _json_formatter(record)


def setup_logging() -> None:
    """
    Configure the root logger based on environment variables.
    Call only once at app startup.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_format = os.environ.get("LOG_FORMAT", "standard").lower()

    root = logging.getLogger()
    root.setLevel(level)

    # Remove existing handlers (avoid duplicates)
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler()
    handler.setLevel(level)

    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))

    root.addHandler(handler)

    # ★ Add file handler — runtime/focus.log (for diagnostics)
    try:
        log_dir = os.path.join(os.getcwd(), "runtime")
        os.makedirs(log_dir, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(
            os.path.join(log_dir, "focus.log"),
            maxBytes=5_000_000,  # 5MB
            backupCount=2,
            encoding="utf-8",
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        # ★ 2-2: always record ERROR and above + record INFO for key namespaces
        _FILE_LOG_NAMESPACES = ("focus", "whale", "harpoon", "triage", "bybit", "recovery", "engine", "hyper")
        _FILE_LOG_TAGS = ("[FOCUS]", "[WHALE", "[HARPOON]", "[TRIAGE", "[BYBIT", "[RECOVERY", "[ENGINE", "[HYPER")

        def _file_log_filter(r: logging.LogRecord) -> bool:
            if r.levelno >= logging.ERROR:
                return True
            name_lower = r.name.lower()
            if any(ns in name_lower for ns in _FILE_LOG_NAMESPACES):
                return True
            msg = r.getMessage()
            if any(tag in msg for tag in _FILE_LOG_TAGS):
                return True
            return False

        fh.addFilter(_file_log_filter)
        root.addHandler(fh)
    except Exception:
        pass  # don't kill the server even if the file can't be created

    # ★ Suppress Windows asyncio ProactorEventLoop noise (no functional impact)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
