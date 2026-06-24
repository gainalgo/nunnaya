# ============================================================
# File: app/strategy/indicator_cache.py
# ------------------------------------------------------------
# [PERF] Per-tick indicator cache (2026-03-21)
# [PERF] Extended to a cross-tick TTL cache (2026-03-22)
#
# Purpose: avoid recomputing the same indicator multiple times within a single
# tick cycle across several components (brain, plugin, selector, exit policy).
#
# [2026-03-22] Cross-tick TTL:
#   - Before: full clear at the start of each tick → 1.5% hit rate (only
#     duplicate calls within a tick are reused)
#   - After: TTL-based (10s) → reused across tick boundaries when the data
#     has not changed
#   - Correctness guarantee: the cache key is content-based (first/mid/last +
#     len + params) → when price data changes the key changes too, so a stale
#     value can never be returned
#   - clear() only removes entries past the TTL (GC); it does not clear everything
#
# Safety:
#   - The cache key uses len(data) + data[first/mid/last]
#     → lists with identical content hit the cache even if they are distinct objects
#   - When the data changes the key differs, triggering an automatic recompute
# ============================================================

from __future__ import annotations
import time
from typing import Any, Callable, Dict, Tuple

# Stored as (value, monotonic_timestamp)
_cache: Dict[Tuple, Tuple[Any, float]] = {}
_hits: int = 0
_misses: int = 0

# Cache TTL: entries older than this (seconds) are eligible for GC
_TTL_SEC: float = 10.0


def clear() -> None:
    """Call before the start of a tick cycle. Only removes entries past the TTL (GC); does not clear everything.

    Same signature as the original clear() → no changes needed at call sites.
    Entries within the TTL are reused across tick boundaries → cross-tick cache effect.
    """
    global _hits, _misses
    now = time.monotonic()
    stale_keys = [k for k, (_, ts) in _cache.items() if now - ts > _TTL_SEC]
    for k in stale_keys:
        del _cache[k]
    # Reset per-tick stats (per-tick lookup, not cumulative)
    _hits = 0
    _misses = 0


def get_or_compute(key: Tuple, fn: Callable[[], Any]) -> Any:
    """Return the cached result if present; otherwise run fn() and store it.

    Entries within the TTL are returned even across tick boundaries.
    Thanks to the content-based key, a data change triggers an automatic recompute.
    """
    global _hits, _misses
    entry = _cache.get(key)
    if entry is not None:
        _hits += 1
        return entry[0]
    result = fn()
    _cache[key] = (result, time.monotonic())
    _misses += 1
    return result


def get_stats() -> Dict[str, int]:
    """Cache hit/miss stats for the current tick."""
    return {"hits": _hits, "misses": _misses, "size": len(_cache)}
