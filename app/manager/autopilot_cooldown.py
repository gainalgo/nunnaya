# ============================================================
# File: app/manager/autopilot_cooldown.py
# Autocoin OS v3-H — Autopilot Cooldown Management Mixin
# Phase 3-B: Extracted from autopilot_manager.py
# ============================================================

from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


class CooldownMixin:
    """Cooldown persistence + per-strategy loss tracking Mixin.

    Expects in __init__:
        self.system, self.cooldown, self.cooldown_path,
        self._strategy_loss_streak, self._strategy_loss_cooldown_until
    """

    # --------------------------------------------------------
    # Cooldown Logic
    # --------------------------------------------------------
    def _load_cooldown(self) -> None:
        if not self.cooldown_path or not os.path.exists(self.cooldown_path):
            return
        try:
            with open(self.cooldown_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            m = data.get('markets') if isinstance(data.get('markets'), dict) else data
            if not isinstance(m, dict):
                return
            out: Dict[str, Dict[str, Any]] = {}
            for k, v in m.items():
                mk = str(k or '').strip().upper()
                if not mk: continue
                if isinstance(v, dict):
                    until_ts = v.get('until_ts')
                    reason = v.get('reason')
                else:
                    until_ts = v
                    reason = ''
                try:
                    until_f = float(until_ts or 0.0)
                except (TypeError, ValueError):
                    logger.warning("[Cooldown] until_ts parse failed for %s", mk, exc_info=True)
                    until_f = 0.0
                out[mk] = {
                    'until_ts': until_f,
                    'reason': str(reason or ''),
                    'ts': float(data.get('ts') or 0.0) if isinstance(data, dict) else 0.0,
                }
            self.cooldown = out
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[AP_COOLDOWN] _load_cooldown: %s", exc, exc_info=True)

    def _save_cooldown(self) -> None:
        if not self.cooldown_path:
            return
        try:
            from app.core.io_utils import safe_write_json
            safe_write_json(self.cooldown_path, {
                'ts': time.time(),
                'markets': dict(self.cooldown or {}),
            })
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("[AP_COOLDOWN] _save_cooldown: %s", exc, exc_info=True)

    def prune_cooldown(self, *, now_ts: Optional[float] = None) -> None:
        now_ts = float(now_ts or time.time())
        m = dict(self.cooldown or {})
        if not m:
            return
        changed = False
        for mk in list(m.keys()):
            try:
                until_ts = float((m.get(mk) or {}).get('until_ts') or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[Cooldown] prune until_ts parse failed for %s", mk, exc_info=True)
                until_ts = 0.0
            if until_ts and until_ts <= now_ts:
                m.pop(mk, None)
                changed = True
        if changed:
            self.cooldown = m
            try:
                self._save_cooldown()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[AP_COOLDOWN] prune_cooldown save: %s", exc, exc_info=True)

    def get_cooldown_markets(self, *, now_ts: Optional[float] = None) -> Set[str]:
        now_ts = float(now_ts or time.time())
        out: Set[str] = set()
        m = self.cooldown or {}
        if not isinstance(m, dict):
            return out
        for mk, v in m.items():
            try:
                until_ts = float((v or {}).get('until_ts') or 0.0) if isinstance(v, dict) else float(v or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[Cooldown] get_cooldown until_ts parse failed for %s", mk, exc_info=True)
                until_ts = 0.0
            if until_ts and until_ts > now_ts:
                out.add(str(mk).strip().upper())
        return out

    def mark_cooldown(self, market: str, *, minutes: Optional[int] = None, reason: str = '') -> None:
        market = str(market or '').strip().upper()
        if not market:
            return

        # System config access
        try:
            def_min = int(getattr(self.system, 'autopilot_cooldown_min', 0) or 0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Cooldown] autopilot_cooldown_min parse failed", exc_info=True)
            def_min = 0

        try:
            mins = int(minutes) if minutes is not None else def_min
        except (TypeError, ValueError):
            logger.warning("[Cooldown] minutes parse failed", exc_info=True)
            mins = def_min

        mins = max(0, mins)
        if mins <= 0:
            return

        now_ts = time.time()
        until_ts = float(now_ts + (mins * 60))
        m = dict(self.cooldown or {})
        prev_until = 0.0
        try:
            prev_until = float((m.get(market) or {}).get('until_ts') or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Cooldown] prev_until parse failed for %s", market, exc_info=True)
            prev_until = 0.0
        # extend only
        if prev_until and prev_until > until_ts:
            until_ts = prev_until
        m[market] = {
            'until_ts': until_ts,
            'reason': str(reason or ''),
            'ts': now_ts,
        }
        self.cooldown = m
        try:
            self._save_cooldown()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[AP_COOLDOWN] mark_cooldown save: %s", exc, exc_info=True)

    # --------------------------------------------------------
    # Phase 3-B: Strategy Loss Cooldown
    # --------------------------------------------------------
    def record_strategy_trade_result(self, strategy: str, is_win: bool) -> None:
        """Record per-strategy trade result. Triggers a 30-min cooldown on a 3-loss streak."""
        strategy = str(strategy or "").strip().upper()
        if not strategy:
            return
        if is_win:
            self._strategy_loss_streak[strategy] = 0
        else:
            streak = self._strategy_loss_streak.get(strategy, 0) + 1
            self._strategy_loss_streak[strategy] = streak
            try:
                threshold = max(2, int(getattr(self.system, "autopilot_loss_streak_threshold", 3) or 3))
            except (TypeError, ValueError):
                logger.warning("[Cooldown] loss_streak_threshold parse failed", exc_info=True)
                threshold = 3
            try:
                cooldown_min = max(5, int(getattr(self.system, "autopilot_loss_cooldown_min", 30) or 30))
            except (TypeError, ValueError):
                logger.warning("[Cooldown] loss_cooldown_min parse failed", exc_info=True)
                cooldown_min = 30
            if streak >= threshold:
                # [2026-03-30] Adaptive Cooldown: scale proportionally to streak
                # streak=3 → 1.0x, streak=4 → 1.5x, streak=5 → 2.0x, streak=6+ → 2.5x (cap)
                _scale = min(2.5, 1.0 + (streak - threshold) * 0.5)
                _effective_min = int(cooldown_min * _scale)
                self._strategy_loss_cooldown_until[strategy] = time.time() + _effective_min * 60
                logger.info(
                    f"[Autopilot/LossCooldown] {strategy} streak={streak} → "
                    f"cooldown {_effective_min}min (scale={_scale:.1f}x)"
                )
