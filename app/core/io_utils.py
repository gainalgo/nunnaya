"""Atomic JSON file I/O with fsync — prevents data loss on power failure.

Usage:
    from app.core.io_utils import safe_write_json, safe_load_json

    safe_write_json(path, data)           # save
    data = safe_load_json(path, default={})  # load
"""
from __future__ import annotations
import json
import os
import time
import logging
from typing import Any

logger = logging.getLogger(__name__)


def safe_write_json(path: str, data: Any, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    """Atomic JSON write: tmp → flush → fsync → replace.

    - tmp filename: {path}.tmp.{pid}.{timestamp_ms}
    - mkdir -p created automatically
    - tmp auto-cleaned on failure
    """
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    tmp = f"{path}.tmp.{os.getpid()}.{int(time.time() * 1000)}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            f.flush()
            os.fsync(f.fileno())

        # Windows: os.replace can fail if target is locked
        for retry in range(3):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                if retry < 2:
                    time.sleep(0.1 * (retry + 1))
                else:
                    raise
    except Exception:
        # clean up tmp
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def safe_load_json(path: str, default: Any = None) -> Any:
    """Safe JSON load with fallback to default."""
    if not path or not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("[io_utils] Failed to load %s: %s", path, exc)
        return default if default is not None else {}
