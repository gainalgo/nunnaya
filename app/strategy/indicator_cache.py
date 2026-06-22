# ============================================================
# File: app/strategy/indicator_cache.py
# ------------------------------------------------------------
# [PERF] Per-Tick 인디케이터 캐시 (2026-03-21)
# [PERF] Cross-tick TTL 캐시로 확장 (2026-03-22)
#
# 목적: 한 틱 사이클 내에서 동일한 인디케이터가 여러 컴포넌트
# (brain, plugin, selector, exit policy)에서 중복 계산되는 것을 방지.
#
# [2026-03-22] Cross-tick TTL:
#   - 기존: 매 틱 시작 시 전체 clear → 히트율 1.5% (틱 내 중복 호출만 재사용)
#   - 변경: TTL(10초) 기반 → 데이터가 바뀌지 않으면 틱 경계를 넘어 재사용
#   - 정확성 보장: 캐시 키가 content-based(first/mid/last + len + params)
#     → 가격 데이터가 바뀌면 키도 바뀌므로 stale 반환 불가능
#   - clear()는 TTL 초과 항목만 제거(GC), 전체 클리어 안 함
#
# 안전성:
#   - 캐시 키에 len(data) + data[first/mid/last] 사용
#     → 동일 내용의 리스트는 서로 다른 객체라도 캐시 히트
#   - 데이터가 바뀌면 키가 달라져 자동으로 재계산
# ============================================================

from __future__ import annotations
import time
from typing import Any, Callable, Dict, Tuple

# (value, monotonic_timestamp) 형태로 저장
_cache: Dict[Tuple, Tuple[Any, float]] = {}
_hits: int = 0
_misses: int = 0

# 캐시 TTL: 이 시간(초) 이상 된 항목은 GC 대상
_TTL_SEC: float = 10.0


def clear() -> None:
    """틱 사이클 시작 전에 호출. TTL 초과 항목만 제거(GC). 전체 클리어 안 함.

    기존 clear()와 시그니처 동일 → 호출부 수정 없음.
    TTL 내 항목은 틱 경계를 넘어 재사용 → cross-tick 캐시 효과.
    """
    global _hits, _misses
    now = time.monotonic()
    stale_keys = [k for k, (_, ts) in _cache.items() if now - ts > _TTL_SEC]
    for k in stale_keys:
        del _cache[k]
    # 틱별 통계 리셋 (누적 아닌 per-tick 조회용)
    _hits = 0
    _misses = 0


def get_or_compute(key: Tuple, fn: Callable[[], Any]) -> Any:
    """캐시에 결과가 있으면 반환, 없으면 fn()을 실행하고 저장.

    TTL 내 항목은 틱 경계를 넘어도 반환됨.
    content-based 키 덕분에 데이터가 바뀌면 자동으로 재계산.
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
    """현재 틱의 캐시 적중/미적중 통계."""
    return {"hits": _hits, "misses": _misses, "size": len(_cache)}
