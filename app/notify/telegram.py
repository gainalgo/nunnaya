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

# [2026-02-02] 반복 알림 방지: 동일 메시지 키에 대해 쿨다운 적용
_message_cooldowns: Dict[str, float] = {}
_COOLDOWN_SEC = 3600.0  # 1시간 쿨다운


def _telegram_env() -> Tuple[bool, Optional[str], Optional[str]]:
    """Load telegram env at call time.

    - Backward-compatible token key:
      * TELEGRAM_TOKEN (new)
      * TELEGRAM_BOT_TOKEN (legacy)
    - TELEGRAM_ENABLE=0이면 전송 비활성화
    """
    enabled_raw = str(os.getenv("TELEGRAM_ENABLE", "1")).strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    token = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    return enabled, token, chat_id


def send_telegram(message: str, *, timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC, cooldown_key: str = None) -> bool:
    """텔레그램 메시지 전송.
    
    Args:
        message: 전송할 메시지
        timeout: 요청 타임아웃 (초)
        cooldown_key: 반복 방지 키 (예: "sl_BTCUSDT"). 설정시 1시간 내 중복 전송 방지
    
    Returns:
        성공 여부
    """
    enabled, token, chat_id = _telegram_env()
    if (not enabled) or (not token) or (not chat_id):
        return False
    
    # [2026-02-02] 쿨다운 체크: 같은 키로 최근에 보낸 경우 스킵
    if cooldown_key:
        now = time.time()
        last_sent = _message_cooldowns.get(cooldown_key, 0.0)
        if now - last_sent < _COOLDOWN_SEC:
            return False  # 쿨다운 중, 전송 스킵
        _message_cooldowns[cooldown_key] = now

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    for attempt in range(2):  # 최초 1회 + 실패 시 1회 재시도
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.status_code == 200:
                return True
            logger.warning("Telegram API returned %s (attempt %d)", resp.status_code, attempt + 1)
        except requests.RequestException as exc:
            logger.warning("Telegram send failed (attempt %d): %s", attempt + 1, exc)
        if attempt == 0:
            time.sleep(1.0)  # 재시도 전 1초 대기
    return False
