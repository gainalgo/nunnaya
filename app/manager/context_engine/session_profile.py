"""Session Profile — conviction adjustment by KST time slot.

Observations:
- 01:00~06:00 KST (low liquidity): slippage / fake wicks frequent → enter conservatively
- 06:00~09:30 KST (around Asian open): direction wobbles → overlaps with morning_guard
- 09:30~15:00 KST (Asian afternoon): relatively stable
- 21:00~24:00 KST (Europe/US overlap): real trends emerge → small bonus

Example (default OFF):
- quiet hours = 01:00~06:00 → conviction -1
- active hours = 21:00~24:00 → conviction +1

morning_guard is separate logic (existing FocusManager) — **no double-counting here**.
Review point: if morning_guard is already boosting conviction in the same slot, session skips.
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
        """KST time as a decimal hour. e.g. 01:30 → 1.5"""
        # time.gmtime(now_ts) returns a UTC tuple
        kst = time.gmtime(now_ts + KST_OFFSET_SEC)
        return kst.tm_hour + kst.tm_min / 60.0

    def _in_range(self, hour: float, start: float, end: float) -> bool:
        """start <= hour < end. Also supports wrap-around ranges (e.g. 22~02)."""
        if start <= end:
            return start <= hour < end
        # wrap-around
        return hour >= start or hour < end

    def evaluate(self, direction: str, now_ts: float) -> Dict[str, Any]:
        """Return the conviction delta.

        Returns:
            {"delta": int, "slot": "quiet|active|neutral", "kst_hour": float}
        """
        out: Dict[str, Any] = {"delta": 0, "slot": "neutral", "kst_hour": 0.0}
        cfg = self.config
        if not getattr(cfg, "session_profile_enabled", False):
            return out

        hour = self._kst_hour(now_ts)
        out["kst_hour"] = round(hour, 2)

        # Skip the slot where morning_guard is already active (07:00~09:30) — avoid double-counting
        mg_enabled = getattr(cfg, "morning_guard_enabled", False)
        mg_end = float(getattr(cfg, "morning_guard_end_hour_kst", 9.5))
        if mg_enabled and 7.0 <= hour < mg_end:
            out["slot"] = "morning_guard_defer"
            return out

        # quiet slot
        q_start = float(getattr(cfg, "sess_quiet_start_kst", 1.0))
        q_end = float(getattr(cfg, "sess_quiet_end_kst", 6.0))
        q_delta = float(getattr(cfg, "sess_quiet_delta", -10.0))  # [2026-05-17 100-scale ×10] -1→-10
        if self._in_range(hour, q_start, q_end):
            out["slot"] = "quiet"
            out["delta"] = q_delta
            return out

        # active slot
        a_start = float(getattr(cfg, "sess_active_start_kst", 21.0))
        a_end = float(getattr(cfg, "sess_active_end_kst", 24.0))
        a_delta = float(getattr(cfg, "sess_active_delta", 10.0))  # [2026-05-17 100-scale ×10] 1→10
        if self._in_range(hour, a_start, a_end):
            out["slot"] = "active"
            out["delta"] = a_delta
            return out

        return out
