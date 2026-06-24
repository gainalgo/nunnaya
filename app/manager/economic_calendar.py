"""Economic calendar auto-fetch — ForexFactory weekly feed.

[2026-06-08 owner] Automatically fetches event times for the Event Shield.
BLS direct is bot-blocked (Access Denied) → use ForexFactory free weekly JSON (no API key needed).
Filters USD High impact only → KST "YYYY-MM-DD HH:MM" list (dedup same times).

Safe design principles:
  - In-memory cache — light even if _event_shield_label calls it every polling cycle (no network).
  - Async refresh — on ttl expiry, fetch in a daemon thread, no blocking of main polling
    (avoids [[feedback_b12_journal_fullparse_dashboard_lag]]).
  - Disk persistence (runtime/econ_calendar.json) — keeps last value across restart/fetch failure.
  - Fetch failure = keep old cache (swallow exception, warn only). Independent of manual inputs.
  - Fetch both this/next week → cover up to 2 weeks ahead.
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

# In-memory cache: events = sorted ["2026-06-10 21:30", ...] (KST)
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
        return None  # entries without tz (All Day, etc.) have unknown time → skip
    return dt.astimezone(_KST).strftime("%Y-%m-%d %H:%M")


def _fetch_one(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch_now(impact="High", country="USD"):
    """Merge this/next week, keep only USD High impact times as KST labels. One feed failing is ignored."""
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
        logger.warning("[EconCal] fetch failed (keeping old cache): %s", e)
    finally:
        _refreshing = False


def maybe_refresh(ttl_sec=21600, impact="High", country="USD"):
    """On ttl (default 6h) expiry, fetch async in background. Returns immediately (no main blocking)."""
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
    """Return the in-memory cache's list of event times (never makes a network call)."""
    if not _CACHE_LOADED:
        _load_disk()
    return list(_CACHE.get("events", []))


def get_status():
    """For UI/debug — last fetch time (KST string) and event count."""
    if not _CACHE_LOADED:
        _load_disk()
    ts = _CACHE.get("fetched_at", 0.0)
    when = ""
    if ts > 0:
        when = (_dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
                .astimezone(_KST).strftime("%Y-%m-%d %H:%M"))
    return {"fetched_at_kst": when, "count": len(_CACHE.get("events", [])),
            "events": list(_CACHE.get("events", []))}
