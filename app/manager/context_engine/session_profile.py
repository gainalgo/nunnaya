"""Session Profile — KST 시간대별 conviction 가감.

관찰:
- 01:00~06:00 KST (저유동): 슬리피지·가짜 꼬리 잦음 → 진입 보수적
- 06:00~09:30 KST (아시아 개장 전후): 방향 휘청 → morning_guard 와 중복
- 09:30~15:00 KST (아시아 오후): 비교적 안정
- 21:00~24:00 KST (유럽/미국 중첩): 추세 진짜 나옴 → 소폭 가점

예시 (default OFF):
- quiet 시간 = 01:00~06:00 → conviction -1
- active 시간 = 21:00~24:00 → conviction +1

morning_guard 는 별도 로직 (기존 FocusManager) — 여기서는 **중복 가산 금지**.
형 검수 포인트: morning_guard 가 이미 동시간대 conviction boost 중이면 session 은 스킵.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

KST_OFFSET_SEC = 9 * 3600  # UTC+9


class SessionProfileModule:
    def __init__(self, config: Any):
        self.config = config

    @staticmethod
    def _kst_hour(now_ts: float) -> float:
        """KST 시각을 소수 시간으로. 예: 01:30 → 1.5"""
        # time.gmtime(now_ts) 가 UTC 튜플 반환
        kst = time.gmtime(now_ts + KST_OFFSET_SEC)
        return kst.tm_hour + kst.tm_min / 60.0

    def _in_range(self, hour: float, start: float, end: float) -> bool:
        """start <= hour < end. 자정 넘어가는 구간 (예: 22~02) 도 지원."""
        if start <= end:
            return start <= hour < end
        # wrap-around
        return hour >= start or hour < end

    def evaluate(self, direction: str, now_ts: float) -> Dict[str, Any]:
        """conviction delta 반환.

        Returns:
            {"delta": int, "slot": "quiet|active|neutral", "kst_hour": float}
        """
        out: Dict[str, Any] = {"delta": 0, "slot": "neutral", "kst_hour": 0.0}
        cfg = self.config
        if not getattr(cfg, "session_profile_enabled", False):
            return out

        hour = self._kst_hour(now_ts)
        out["kst_hour"] = round(hour, 2)

        # morning_guard 가 이미 활성화된 시간대(07:00~09:30) 는 스킵 — 이중 가산 방지
        mg_enabled = getattr(cfg, "morning_guard_enabled", False)
        mg_end = float(getattr(cfg, "morning_guard_end_hour_kst", 9.5))
        if mg_enabled and 7.0 <= hour < mg_end:
            out["slot"] = "morning_guard_defer"
            return out

        # quiet slot
        q_start = float(getattr(cfg, "sess_quiet_start_kst", 1.0))
        q_end = float(getattr(cfg, "sess_quiet_end_kst", 6.0))
        q_delta = float(getattr(cfg, "sess_quiet_delta", -10.0))  # [2026-05-17 100점 ×10] -1→-10
        if self._in_range(hour, q_start, q_end):
            out["slot"] = "quiet"
            out["delta"] = q_delta
            return out

        # active slot
        a_start = float(getattr(cfg, "sess_active_start_kst", 21.0))
        a_end = float(getattr(cfg, "sess_active_end_kst", 24.0))
        a_delta = float(getattr(cfg, "sess_active_delta", 10.0))  # [2026-05-17 100점 ×10] 1→10
        if self._in_range(hour, a_start, a_end):
            out["slot"] = "active"
            out["delta"] = a_delta
            return out

        return out
