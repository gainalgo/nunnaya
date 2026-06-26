# ============================================================
# Telegram Notify
# ============================================================
from __future__ import annotations

import logging
import os
import time
from typing import Optional, Dict, Tuple

import requests

from app.core.constants import TELEGRAM_API_BASE, DEFAULT_REQUEST_TIMEOUT_SEC

logger = logging.getLogger(__name__)

# [2026-02-02] Prevent repeated alerts: apply a cooldown per message key
_message_cooldowns: Dict[str, float] = {}
_COOLDOWN_SEC = 3600.0  # 1-hour cooldown


def _telegram_env() -> Tuple[bool, Optional[str], Optional[str]]:
    """Load telegram env at call time.

    - Backward-compatible token key:
      * TELEGRAM_TOKEN (new)
      * TELEGRAM_BOT_TOKEN (legacy)
    - Sending is disabled when TELEGRAM_ENABLE=0
    """
    enabled_raw = str(os.getenv("TELEGRAM_ENABLE", "1")).strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    token = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    return enabled, token, chat_id


def send_telegram(message: str, *, timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC, cooldown_key: str = None) -> bool:
    """Send a Telegram message.

    Args:
        message: Message to send
        timeout: Request timeout (seconds)
        cooldown_key: De-duplication key (e.g. "sl_BTCUSDT"). When set, prevents duplicate sends within 1 hour

    Returns:
        Whether the send succeeded
    """
    enabled, token, chat_id = _telegram_env()
    if (not enabled) or (not token) or (not chat_id):
        return False
    
    # [2026-02-02] Cooldown check: skip if the same key was sent recently
    if cooldown_key:
        now = time.time()
        last_sent = _message_cooldowns.get(cooldown_key, 0.0)
        if now - last_sent < _COOLDOWN_SEC:
            return False  # within cooldown, skip send
        _message_cooldowns[cooldown_key] = now

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    for attempt in range(2):  # first attempt + one retry on failure
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.status_code == 200:
                return True
            logger.warning("Telegram API returned %s (attempt %d)", resp.status_code, attempt + 1)
        except requests.RequestException as exc:
            logger.warning("Telegram send failed (attempt %d): %s", attempt + 1, exc)
        if attempt == 0:
            time.sleep(1.0)  # wait 1 second before retry
    return False
