# ============================================================
# File: app/manager/reserved_queue.py
# Autocoin OS v3-H — Reserved Queue (Candidate Proposal Store)
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

def _now_ts() -> float:
    try:
        return float(time.time())
    except (TypeError, ValueError):
        logger.warning("[ReservedQueue] _now_ts: time.time() failed", exc_info=True)
        return 0.0

class ReservedQueue:
    """Persistent store for proposed markets (UI Reserved panel).

    Design goals:
    - Safe to read/write concurrently from multiple API requests.
    - Survive server restarts (runtime/reserved_queue.json).
    - Minimal coupling with trading/engine logic (proposal only).
    """

    def __init__(self, *, path: Optional[str] = None):
        self.path = path or os.getenv("OMA_RESERVED_STATE_PATH", "runtime/reserved_queue.json")

        # History (Approved/Rejected/Autopilot Promote/Demote logs)
        # - Persisted alongside the queue so operators can answer:
        #   "AutoApprove가 켜져 있는데 왜 Reserved가 비어있지?" (→ 방금 승격/강등됨)
        # - Keep bounded to avoid unbounded growth.
        self._history_max = 200
        try:
            self._history_max = int(float(os.getenv("OMA_RESERVED_HISTORY_MAX", "200")))
        except (TypeError, ValueError):
            logger.warning("OMA_RESERVED_HISTORY_MAX env parse failed, using default 200", exc_info=True)
            self._history_max = 200
        self._history_max = max(10, min(2000, int(self._history_max)))

        self._lock = Lock()
        self._items: List[Dict[str, Any]] = []
        self._history: List[Dict[str, Any]] = []
        self._meta: Dict[str, Any] = {
            "ts": _now_ts(),
            "last_refresh_ts": None,
            "summary": {},
        }
        self.load()

    # ------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------
    def load(self) -> bool:
        p = str(self.path or "").strip()
        if not p or not os.path.exists(p):
            return False

        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.loads(f.read())
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("[ReservedQueue] load: failed to read %s", p, exc_info=True)
            return False

        if not isinstance(raw, dict):
            return False

        items = raw.get("items")
        meta = raw.get("meta")
        history = raw.get("history")

        with self._lock:
            if isinstance(items, list):
                self._items = [x for x in items if isinstance(x, dict)]
            if isinstance(meta, dict):
                self._meta = dict(meta)
            if isinstance(history, list):
                self._history = [x for x in history if isinstance(x, dict)]
                self._trim_history_locked()
        return True

    def save(self) -> bool:
        from app.core.io_utils import safe_write_json
        p = str(self.path or "").strip()
        if not p:
            return False
        try:
            with self._lock:
                data = {
                    "ts": _now_ts(),
                    "meta": dict(self._meta or {}),
                    "items": list(self._items or []),
                    "history": list(self._history or []),
                }
            safe_write_json(p, data)
            return True
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("[ReservedQueue] save: failed to write %s", p, exc_info=True)
            return False

    # ------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "meta": dict(self._meta or {}),
                "items": list(self._items or []),
                "history": list(self._history or []),
            }

    # ------------------------------------------------------------
    # History helpers
    # ------------------------------------------------------------
    def _trim_history_locked(self) -> None:
        try:
            maxn = int(self._history_max)
        except (TypeError, ValueError):
            logger.warning("[ReservedQueue] _trim_history: history_max parse failed", exc_info=True)
            maxn = 200
        if maxn <= 0:
            self._history = []
            return
        if len(self._history) > maxn:
            self._history = self._history[-maxn:]

    def _normalize_history_event(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(ev or {})
        ts = out.get("ts")
        try:
            ts = float(ts) if ts is not None else _now_ts()
        except (TypeError, ValueError):
            logger.warning("[ReservedQueue] _normalize_history_event: ts parse failed", exc_info=True)
            ts = _now_ts()
        out["ts"] = ts

        # lightweight stable-ish id for UI signature / debugging
        if not out.get("id"):
            kind = str(out.get("kind") or "EV").upper()
            market = str(out.get("market") or "").upper()
            strat = str(out.get("strategy") or "").upper()
            out["id"] = f"{int(ts*1000)}:{kind}:{market}:{strat}".strip(":")
        return out

    def add_history(self, ev: Dict[str, Any]) -> None:
        if not isinstance(ev, dict) or not ev:
            return
        with self._lock:
            self._history.append(self._normalize_history_event(ev))
            self._trim_history_locked()
        self.save()

    def add_history_many(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        with self._lock:
            for ev in events:
                if isinstance(ev, dict) and ev:
                    self._history.append(self._normalize_history_event(ev))
            self._trim_history_locked()
        self.save()

    def clear_history(self) -> None:
        with self._lock:
            self._history = []
            self._meta = dict(self._meta or {})
            self._meta["history_cleared_ts"] = _now_ts()
        self.save()

    def replace(self, items: List[Dict[str, Any]], *, summary: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._items = [x for x in (items or []) if isinstance(x, dict)]
            self._meta = dict(self._meta or {})
            self._meta["last_refresh_ts"] = _now_ts()
            if summary is not None:
                self._meta["summary"] = dict(summary)
        self.save()

    def merge_round(
        self,
        new_items: List[Dict[str, Any]],
        round_strategies: List[str],
        *,
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Merge round-robin scan results: replace only items whose strategy
        is in *round_strategies*, keep items from other strategies untouched.

        This prevents round A from wiping round B/C candidates.
        """
        round_set = {s.upper() for s in (round_strategies or [])}
        new_valid = [x for x in (new_items or []) if isinstance(x, dict)]

        with self._lock:
            # keep items that belong to strategies NOT in this round
            kept = [
                it for it in self._items
                if str(it.get("strategy") or "").upper() not in round_set
            ]
            # append new items from this round
            kept.extend(new_valid)
            self._items = kept

            self._meta = dict(self._meta or {})
            self._meta["last_refresh_ts"] = _now_ts()
            if summary is not None:
                # merge summary: keep previous round summaries, overlay new ones
                prev = dict(self._meta.get("summary") or {})
                prev.update(summary)
                self._meta["summary"] = prev
        self.save()

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._meta = dict(self._meta or {})
            self._meta["summary"] = {"cleared": True, "cleared_ts": _now_ts()}
        self.save()

    def get(self, rid: str) -> Optional[Dict[str, Any]]:
        rid = str(rid or "").strip()
        if not rid:
            return None
        with self._lock:
            for it in self._items:
                if str(it.get("id") or "") == rid:
                    return dict(it)
        return None

    def pop(self, rid: str) -> Optional[Dict[str, Any]]:
        rid = str(rid or "").strip()
        if not rid:
            return None
        removed = None
        with self._lock:
            keep: List[Dict[str, Any]] = []
            for it in self._items:
                if removed is None and str(it.get("id") or "") == rid:
                    removed = dict(it)
                    continue
                keep.append(it)
            self._items = keep
        if removed is not None:
            self.save()
        return removed

    def push(self, item: Dict[str, Any], *, front: bool = False) -> Optional[str]:
        """Re-insert an item back into the queue (best-effort).

        Used by Autopilot when an auto-approve fails after popping an item.
        """
        if not isinstance(item, dict) or not item:
            return None
        it = dict(item)
        rid = str(it.get('id') or '').strip()
        if not rid:
            rid = uuid.uuid4().hex
            it['id'] = rid
        with self._lock:
            if front:
                self._items.insert(0, it)
            else:
                self._items.append(it)
            self._meta = dict(self._meta or {})
            self._meta['ts'] = _now_ts()
        try:
            self.save()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[reserved_queue] %s: %s", 'reserved_queue.push fallback', exc, exc_info=True)
        return rid

# process-wide singleton
reserved_queue = ReservedQueue()

# ✅ Alias for compatibility with candidate_scanner.py
ReservedQueueStore = ReservedQueue
