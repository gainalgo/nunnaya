"""Versioned config snapshots.

Whenever an engine's settings change, a timestamped copy of the *config* block
is written to ``runtime/config_history/<engine>/``. This lets you roll back to a
previous configuration after a reset or a bad tuning session.

Design notes
------------
* Snapshots capture only the ``config`` block (not live ``state``), so they are
  written on *settings* changes, not on every position tick. A module-level hash
  cache dedupes: nothing is written unless the config actually changed.
* Retention is a generous union -- a snapshot is kept if it is among the newest
  ``keep_n`` OR newer than ``keep_days``; everything else is pruned. Defaults are
  overridable via env (``CONFIG_HISTORY_KEEP_N`` / ``CONFIG_HISTORY_KEEP_DAYS``).
* All operations are best-effort: callers wrap in try/except so a history failure
  can never block a real config save.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

ROOT = os.path.join("runtime", "config_history")

_DEFAULT_KEEP_N = int(os.environ.get("CONFIG_HISTORY_KEEP_N", "60"))
_DEFAULT_KEEP_DAYS = int(os.environ.get("CONFIG_HISTORY_KEEP_DAYS", "15"))

# engine -> hash of the last snapshotted config (process-lifetime dedupe cache)
_LAST_HASH: Dict[str, str] = {}


def engine_key(config_path: str) -> str:
    """Derive a stable, collision-free engine key from a config file path.

    runtime/focus_config.json                 -> focus_config
    runtime/binance_futures/focus_config.json -> binance_futures_focus_config
    runtime/upbit/upbit_focus_config.json     -> upbit_focus_config
    """
    base = os.path.splitext(os.path.basename(config_path))[0]
    parent = os.path.basename(os.path.dirname(config_path))
    if parent and parent.lower() not in ("runtime", "", ".") and not base.startswith(parent):
        return f"{parent}_{base}"
    return base


def _history_dir(engine: str) -> str:
    d = os.path.join(ROOT, engine)
    os.makedirs(d, exist_ok=True)
    return d


def _hash_config(config: Dict[str, Any]) -> str:
    blob = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _snapshot_files(engine: str) -> List[str]:
    d = os.path.join(ROOT, engine)
    if not os.path.isdir(d):
        return []
    files = [os.path.join(d, f) for f in os.listdir(d)
             if f.endswith(".json")]
    # newest first; filename embeds the timestamp so it is a stable tiebreak
    # when mtimes are equal (rapid writes).
    files.sort(key=lambda p: (os.path.getmtime(p), os.path.basename(p)), reverse=True)
    return files


def _newest_hash(engine: str) -> Optional[str]:
    files = _snapshot_files(engine)
    if not files:
        return None
    try:
        with open(files[0], encoding="utf-8") as f:
            return json.load(f).get("_meta", {}).get("hash")
    except Exception:
        return None


def prune(engine: str, keep_n: int = _DEFAULT_KEEP_N,
          keep_days: int = _DEFAULT_KEEP_DAYS) -> int:
    """Delete snapshots that are BOTH beyond the newest keep_n AND older than
    keep_days. Returns the number deleted."""
    files = _snapshot_files(engine)  # newest first
    if not files:
        return 0
    cutoff = (datetime.now() - timedelta(days=keep_days)).timestamp()
    deleted = 0
    for idx, path in enumerate(files):
        within_n = idx < keep_n
        within_days = os.path.getmtime(path) >= cutoff
        if within_n or within_days:
            continue
        try:
            os.remove(path)
            deleted += 1
        except OSError:
            pass
    return deleted


def maybe_snapshot(config_path: str, config: Dict[str, Any], reason: str = "change",
                   *, force: bool = False,
                   keep_n: int = _DEFAULT_KEEP_N,
                   keep_days: int = _DEFAULT_KEEP_DAYS) -> Optional[str]:
    """Write a timestamped snapshot of ``config`` if it changed (or ``force``).

    Returns the snapshot path, or None if skipped/failed.
    """
    engine = engine_key(config_path)
    h = _hash_config(config)

    if not force:
        last = _LAST_HASH.get(engine)
        if last is None:
            last = _newest_hash(engine)  # seed cache from disk on first call
        if last == h:
            _LAST_HASH[engine] = h
            return None  # unchanged -> nothing to do

    ts = datetime.now()
    # millisecond suffix + collision counter so rapid changes never overwrite
    base_name = f"{engine}_{ts.strftime('%Y%m%d_%H%M%S')}_{ts.microsecond // 1000:03d}"
    hist_dir = _history_dir(engine)
    dest = os.path.join(hist_dir, base_name + ".json")
    _n = 1
    while os.path.exists(dest):
        dest = os.path.join(hist_dir, f"{base_name}_{_n}.json")
        _n += 1
    payload = {
        "_meta": {
            "engine": engine,
            "ts": ts.isoformat(timespec="seconds"),
            "reason": reason,
            "hash": h,
        },
        "config": config,
    }
    try:
        tmp = dest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dest)
    except Exception:
        return None
    _LAST_HASH[engine] = h
    prune(engine, keep_n=keep_n, keep_days=keep_days)
    return dest


def list_snapshots(engine: str) -> List[Dict[str, Any]]:
    """Newest-first list of snapshots for an engine (metadata only)."""
    out: List[Dict[str, Any]] = []
    for path in _snapshot_files(engine):
        meta: Dict[str, Any] = {}
        try:
            with open(path, encoding="utf-8") as f:
                meta = json.load(f).get("_meta", {})
        except Exception:
            pass
        out.append({
            "name": os.path.basename(path),
            "ts": meta.get("ts"),
            "reason": meta.get("reason"),
            "mtime": os.path.getmtime(path),
        })
    return out


def load_snapshot(engine: str, name: str) -> Optional[Dict[str, Any]]:
    """Return the ``config`` dict of a named snapshot (or None)."""
    # guard against path traversal
    safe = os.path.basename(name)
    path = os.path.join(ROOT, engine, safe)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("config")
    except Exception:
        return None
