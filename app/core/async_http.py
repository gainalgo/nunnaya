# ============================================================
# File: app/core/async_http.py
# Autocoin OS — Async HTTP wrapper (requests → asyncio.to_thread)
# ============================================================

import asyncio
import logging
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0


async def async_get(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
) -> requests.Response:
    def _do():
        return requests.get(url, params=params, headers=headers, timeout=timeout)
    try:
        return await asyncio.wait_for(asyncio.to_thread(_do), timeout=timeout + 2.0)
    except asyncio.TimeoutError:
        logger.warning("[async_get] timeout %.1fs url=%s", timeout, url)
        raise


async def async_post(
    url: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    data: Optional[Any] = None,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
) -> requests.Response:
    def _do():
        return requests.post(url, json=json, data=data, headers=headers, timeout=timeout)
    try:
        return await asyncio.wait_for(asyncio.to_thread(_do), timeout=timeout + 2.0)
    except asyncio.TimeoutError:
        logger.warning("[async_post] timeout %.1fs url=%s", timeout, url)
        raise


async def async_delete(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
) -> requests.Response:
    def _do():
        return requests.delete(url, params=params, headers=headers, timeout=timeout)
    try:
        return await asyncio.wait_for(asyncio.to_thread(_do), timeout=timeout + 2.0)
    except asyncio.TimeoutError:
        logger.warning("[async_delete] timeout %.1fs url=%s", timeout, url)
        raise
