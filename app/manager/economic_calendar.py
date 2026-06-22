"""경제 캘린더 자동 fetch — ForexFactory 주간 피드.

[2026-06-08 부모] Event Shield 의 이벤트 시각을 자동으로 가져온다.
BLS 직접은 봇 차단(Access Denied) → ForexFactory 무료 주간 JSON(API 키 불필요) 사용.
USD High impact 만 필터 → KST "YYYY-MM-DD HH:MM" 리스트(같은 시각 중복 제거).

설계 안전 원칙:
  - 인메모리 캐시 — _event_shield_label 이 매 polling 호출해도 가벼움(네트워크 X).
  - 비동기 refresh — ttl 만료 시 daemon thread 로 fetch, 메인 polling 블로킹 X
    ([[feedback_b12_journal_fullparse_dashboard_lag]] 회피).
  - 디스크 영속(runtime/econ_calendar.json) — 재시작·fetch 실패해도 마지막 값 유지.
  - fetch 실패 = 옛 캐시 유지(예외 삼키고 경고만). 수동 입력값과는 독립.
  - this/next week 둘 다 fetch → 최대 2주 미리 커버.
"""
import os
import json
import time
import threading
import logging
import ssl
import urllib.request
import datetime as _dt

logger = logging.getLogger(__name__)

_FEED_URLS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
)
_CACHE_PATH = os.path.join("runtime", "econ_calendar.json")
_KST = _dt.timezone(_dt.timedelta(hours=9))

# 인메모리 캐시: events = 정렬된 ["2026-06-10 21:30", ...] (KST)
_CACHE = {"fetched_at": 0.0, "events": []}
_CACHE_LOADED = False
_lock = threading.Lock()
_refreshing = False


def _to_kst_label(iso_str):
    """ForexFactory date("2026-06-10T08:30:00-04:00") → KST "YYYY-MM-DD HH:MM"."""
    try:
        dt = _dt.datetime.fromisoformat(str(iso_str))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None  # tz 없는 항목(All Day 등)은 시각 불명 → skip
    return dt.astimezone(_KST).strftime("%Y-%m-%d %H:%M")


def _fetch_one(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch_now(impact="High", country="USD"):
    """this/next week 합쳐 USD High impact 시각만 KST 라벨로. 한 피드 실패는 무시."""
    times = set()
    got_any = False
    for url in _FEED_URLS:
        try:
            data = _fetch_one(url)
        except Exception as e:
            logger.debug("[EconCal] feed fail %s: %s", url, e)
            continue
        got_any = True
        for d in data:
            if d.get("country") != country:
                continue
            if str(d.get("impact", "")).strip().lower() != impact.lower():
                continue
            lab = _to_kst_label(d.get("date"))
            if lab:
                times.add(lab)
    if not got_any:
        raise RuntimeError("all feeds failed")
    return sorted(times)


def _load_disk():
    global _CACHE, _CACHE_LOADED
    _CACHE_LOADED = True
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if isinstance(obj, dict) and isinstance(obj.get("events"), list):
            _CACHE = {"fetched_at": float(obj.get("fetched_at", 0.0)),
                      "events": [str(x) for x in obj["events"]]}
    except (FileNotFoundError, ValueError, OSError, TypeError):
        pass


def _save_disk():
    try:
        os.makedirs("runtime", exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_PATH)
    except OSError as e:
        logger.debug("[EconCal] save failed: %s", e)


def _do_refresh(impact, country):
    global _CACHE, _refreshing
    try:
        events = _fetch_now(impact, country)
        _CACHE = {"fetched_at": time.time(), "events": events}
        _save_disk()
        logger.info("[EconCal] refreshed: %d %s %s events: %s",
                    len(events), country, impact, ", ".join(events) or "-")
    except Exception as e:
        logger.warning("[EconCal] fetch failed (옛 캐시 유지): %s", e)
    finally:
        _refreshing = False


def maybe_refresh(ttl_sec=21600, impact="High", country="USD"):
    """ttl(기본 6h) 만료 시 백그라운드 비동기 fetch. 호출 즉시 반환(메인 블로킹 X)."""
    global _refreshing
    if not _CACHE_LOADED:
        _load_disk()
    if (time.time() - _CACHE.get("fetched_at", 0.0)) < ttl_sec:
        return
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    try:
        threading.Thread(target=_do_refresh, args=(impact, country), daemon=True).start()
    except Exception as e:
        _refreshing = False
        logger.debug("[EconCal] thread spawn failed: %s", e)


def get_event_times():
    """인메모리 캐시의 이벤트 시각 리스트 반환 (절대 네트워크 호출 안 함)."""
    if not _CACHE_LOADED:
        _load_disk()
    return list(_CACHE.get("events", []))


def get_status():
    """UI/디버그용 — 마지막 fetch 시각(KST 문자열)과 이벤트 수."""
    if not _CACHE_LOADED:
        _load_disk()
    ts = _CACHE.get("fetched_at", 0.0)
    when = ""
    if ts > 0:
        when = (_dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
                .astimezone(_KST).strftime("%Y-%m-%d %H:%M"))
    return {"fetched_at_kst": when, "count": len(_CACHE.get("events", [])),
            "events": list(_CACHE.get("events", []))}
