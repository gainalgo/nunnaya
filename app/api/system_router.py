# ============================================================
# File: app/api/system_router.py
# Autocoin OS v3-H — System Router (Ledger + Emergency)
# ============================================================

from __future__ import annotations
from app.core.error_visibility import report_suppressed_exception
import os
import time
from fastapi import APIRouter, Body, Request, Query, Response
from fastapi.responses import PlainTextResponse
from typing import Dict, Any, List, Optional

from app.manager import ledger_pnl
from app.core.currency import Q
from app.core.rate_limiter import rate_limiter, bybit_rate_limiter
import json
import logging
from app.integrations.bybit_markets import (
    fetch_bybit_markets,
    load_bybit_markets_cache,
    ensure_bybit_markets_cache,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/system",
    tags=["system"],
)


@router.get("/health", summary="Health check", description="Check whether the server is operating normally")
def health(request: Request) -> Dict[str, Any]:
    """Server health check endpoint (extended)."""
    system = request.app.state.system

    # Basic checks
    checks = {
        "server": "ok",
        "websocket": "unknown",
        "exchange_api": "unknown",
        "memory_mb": 0,
        "orphan_markets": 0,
        "stuck_orders": 0,
        "rate_limiter": {},
        "price_feed": {},
        "ledger": {},
    }
    
    # 1. Memory usage
    try:
        import psutil
        process = psutil.Process()
        checks["memory_mb"] = int(process.memory_info().rss / 1024 / 1024)
    except (ImportError, AttributeError, TypeError, ValueError):
        # Fallback for environments without psutil (Windows first)
        logger.warning("system_router.health: psutil unavailable, trying ctypes fallback", exc_info=True)
        try:
            import ctypes
            import ctypes.wintypes as wt

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wt.DWORD),
                    ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            get_current_process = ctypes.windll.kernel32.GetCurrentProcess
            get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            ok = get_process_memory_info(
                get_current_process(),
                ctypes.byref(counters),
                counters.cb,
            )
            if ok:
                checks["memory_mb"] = int(float(counters.WorkingSetSize) / 1024 / 1024)
        except (AttributeError, ImportError, OSError, ValueError):
            report_suppressed_exception(__name__, 'Fallback for environments without psutil (Windows first)')

    # Last-resort fallback: use tracemalloc (Python heap) to avoid reporting 0
    if int(checks.get("memory_mb") or 0) <= 0:
        try:
            import tracemalloc
            if not tracemalloc.is_tracing():
                tracemalloc.start()
            current, peak = tracemalloc.get_traced_memory()
            checks["memory_mb"] = int(max(float(current), float(peak)) / 1024 / 1024)
        except (ImportError, AttributeError, ValueError):
            report_suppressed_exception(__name__, 'Last-resort fallback: use tracemalloc (Python heap) to avoid reporting 0')

    # 2. Check Exchange API response
    try:
        if hasattr(system, "query_client"):
            # Verify API status with a simple ticker query
            ticker = system.query_client.get_ticker("BTCUSDT")
            if ticker and ticker.get("trade_price"):
                checks["exchange_api"] = "ok"
            else:
                checks["exchange_api"] = "error"
    except (AttributeError, TypeError, ValueError):
        logger.warning("system_router.health L111 except", exc_info=True)
        checks["exchange_api"] = "error"
    
    # 3. WebSocket status (price feed)
    try:
        if hasattr(system, "price_feed"):
            pf = system.price_feed
            running = None
            if hasattr(pf, "is_running"):
                running = getattr(pf, "is_running")
                if callable(running):
                    running = running()
            if running is None and hasattr(pf, "running"):
                running = getattr(pf, "running")
            if running is not None:
                checks["websocket"] = "ok" if bool(running) else "stopped"
    except (AttributeError, TypeError, ValueError):
        logger.warning("system_router.health L127 except", exc_info=True)
        checks["websocket"] = "error"
    
    # 4. Recent reconcile orphan count
    # Prefer the in-memory last reconcile result (most accurate / lowest cost)
    try:
        last_result = getattr(system, "_last_reconcile_result", None)
        if isinstance(last_result, dict):
            orphans = last_result.get("orphans")
            if isinstance(orphans, list):
                checks["orphan_markets"] = int(len(orphans))
            elif orphans is not None:
                checks["orphan_markets"] = int(orphans)
    except (AttributeError, TypeError, ValueError):
        report_suppressed_exception(__name__, 'Prefer the in-memory last reconcile result (most accurate / lowest cost)')

    # fallback: recent ledger records
    try:
        if int(checks.get("orphan_markets") or 0) <= 0:
            now_ts = time.time()
            records = system.ledger.tail_records(since_ts=now_ts - 86400, tail_lines=5000)
            last_orphans = None
            for rec in reversed(records):
                if str(rec.get("event") or "") != "RECONCILE_OK":
                    continue
                data = rec.get("data") or {}
                try:
                    last_orphans = int(data.get("orphans") or 0)
                except (TypeError, ValueError):
                    logger.warning("system_router.health L155 except", exc_info=True)
                    last_orphans = 0
                break
            if last_orphans is not None:
                checks["orphan_markets"] = int(last_orphans)
    except (AttributeError, TypeError, ValueError):
        report_suppressed_exception(__name__, 'fallback: recent ledger records')

    # 5. Stale unfilled orders (older than 1 hour)
    #   2026-06-06: the old implementation was triple-broken — system.get_markets() +
    #   system.get_context() (both phantom on HyperSystem — get_context is coordinator-only)
    #   + ctx.get("order") (HyperEngineContext is neither a dict nor has an order field),
    #   spamming AttributeError logs on every health call.
    #   The current architecture (FOCUS market orders + server-side TP/SL) does not hold
    #   unfilled limit orders in the local context, so this metric is meaningless → fixed at 0.
    #   If a real stuck-order monitor is needed, attach Bybit open-orders via a separate
    #   async path (no synchronous polling here).
    checks["stuck_orders"] = 0

    # 6. Rate limiter status
    try:
        rl_status = rate_limiter.status()
        checks["rate_limiter"] = {
            "usage_pct": rl_status.get("usage_pct", 0),
            "banned": rl_status.get("banned", False),
            "ban_remaining_sec": rl_status.get("ban_remaining_sec", 0),
            "enabled": rl_status.get("enabled", True),
        }
        try:
            exchange_stats = bybit_rate_limiter.stats()
            checks["rate_limiter"]["exchange_recent_sec"] = exchange_stats.get("recent_sec", 0)
            checks["rate_limiter"]["exchange_recent_min"] = exchange_stats.get("recent_min", 0)
        except (AttributeError, TypeError):
            report_suppressed_exception(__name__, '6. Rate limiter status')
    except (AttributeError, TypeError, ValueError):
        logger.warning("system_router.health L200 except", exc_info=True)
        checks["rate_limiter"] = {"error": "unavailable"}

    # 7. Price feed status (last update timestamp)
    try:
        ps = getattr(system, "price_store", None)
        if ps is None:
            from app.core.hyper_price_store import price_store
            ps = price_store
        if ps is not None and hasattr(ps, "get_last_update_ts"):
            last_ts = ps.get_last_update_ts()
            checks["price_feed"] = {
                "last_update_ts": last_ts if last_ts > 0 else None,
                "age_sec": round(time.time() - last_ts, 1) if last_ts > 0 else None,
            }
        else:
            checks["price_feed"] = {"status": "unknown"}
    except (AttributeError, TypeError, ValueError):
        logger.warning("system_router.health L217 except", exc_info=True)
        checks["price_feed"] = {"error": "unavailable"}

    # 8. Ledger status (last write, total entries)
    try:
        ledger = system.ledger
        path = getattr(ledger, "path", getattr(ledger, "_path", None))
        last_write_ts = None
        total_entries = None
        if path and os.path.exists(path):
            last_write_ts = os.path.getmtime(path)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    total_entries = sum(1 for line in f if line.strip())
            except OSError:
                report_suppressed_exception(__name__, '8. Ledger status (last write, total entries)')
        checks["ledger"] = {
            "last_write_ts": last_write_ts,
            "total_entries": total_entries,
        }
    except (AttributeError, TypeError, ValueError, OSError):
        logger.warning("system_router.health L237 except", exc_info=True)
        checks["ledger"] = {"error": "unavailable"}
    
    # Overall status determination (healthy / degraded / critical)
    status = "healthy"
    rl = checks.get("rate_limiter") or {}
    pf = checks.get("price_feed") or {}
    if isinstance(rl, dict) and rl.get("banned"):
        status = "critical"
    elif checks["exchange_api"] == "error":
        status = "degraded"
    elif isinstance(rl, dict) and rl.get("usage_pct", 0) >= 80:
        status = "degraded"
    elif checks["orphan_markets"] > 5:
        status = "degraded"
    elif checks["stuck_orders"] > 3:
        status = "degraded"
    elif isinstance(pf, dict) and pf.get("age_sec") is not None and pf.get("age_sec", 0) > 300:
        # Price feed stale > 5 min
        status = "degraded"
    
    return {
        "ok": True,
        "status": status,
        "checks": checks,
        "timestamp": time.time(),
    }


@router.get("/currency", summary="Quote currency info", description="Get the currently configured quote currency information")
def currency() -> Dict[str, Any]:
    """Return the currently configured quote currency information.

    Returns:
        Currency settings such as symbol, min_order, decimals, exchange
    """
    return {"ok": True, **Q.to_dict()}


@router.get("/info", summary="System info", description="Get the system version and engine information")
def info() -> Dict[str, Any]:
    """Return basic system information."""
    return {
        "ok": True,
        "version": "v3-H",
        "engine": "HyperNunnayaEngine",
        "description": "Autocoin OS v3-H System API",
    }


@router.get("/markets", summary="Bybit market list", description="Get the Bybit market list without CORS (server proxy)")
def bybit_markets(
    quote: str = Query(Q.symbol, description="Quote currency filter (e.g., USDT)"),
    refresh: bool = Query(False, description="If True, query the exchange API immediately"),
    details: bool = Query(False, description="If True, include detailed market info"),
) -> Dict[str, Any]:
    """Get the Bybit market list for the UI (server proxy)."""
    quote_u = str(quote or Q.symbol).upper()
    markets: List[Dict[str, Any]] = []
    source = "cache"

    try:
        if not refresh:
            markets = load_bybit_markets_cache()

        if refresh or not markets:
            source = "api"
            markets = fetch_bybit_markets(is_details=bool(details), timeout=5.0)
            try:
                ensure_bybit_markets_cache(
                    markets=markets,
                    quote=quote_u,
                    is_details=bool(details),
                    min_interval_sec=0.0,
                )
            except (AttributeError, TypeError, ValueError):
                report_suppressed_exception(__name__, 'system_router.bybit_markets fallback')

        if not isinstance(markets, list):
            markets = []

        items = [m for m in markets if str(m.get("quote", "")).upper() == quote_u and m.get("market")]
        codes = [str(m.get("market")) for m in items]

        return {
            "ok": True,
            "source": source,
            "quote": quote_u,
            "count": len(codes),
            "markets": codes,
            "items": items if details else [],
        }
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.bybit_markets L329: %s", e)
        return {
            "ok": False,
            "error": str(e),
            "quote": quote_u,
            "markets": [],
            "items": [],
        }

@router.get("/feed-status", summary="Price feed status", description="Get REST/WebSocket feed status")
def feed_status() -> Dict[str, Any]:
    """Return the price feed status."""
    return {
        "ok": True,
        "mode": "REST",
        "rest_ok": True,
        "ws_ok": False,
        "banned": False,
        "ban_until": None,
    }

@router.get("/status", summary="System status", description="Get a real-time system snapshot")
def status(request: Request, response: Response) -> Dict[str, Any]:
    """Real-time system status snapshot.

    2-second cache: responds quickly even during thread-pool contention right after boot.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    # 2-second cache: prevents thread-pool overload under bursts of concurrent requests
    now = time.time()
    cached = getattr(request.app.state, "_status_cache", None)
    if cached and (now - cached[0]) < 2.0:
        return cached[1]

    system = request.app.state.system
    snap = system.status()
    snap["server_now_ts"] = now

    # API call stats (rate limit monitoring)
    # ★ [2026-06-23 owner] Unify on the FOCUS actual call counts — the old system.trade_client
    #   (status polling only ≈ 1) does not count real scan/order traffic (FOCUS is a separate
    #   client instance). Prefer FOCUS client's get_api_stats so the real usage (~20-25 during
    #   scans) is shown. (Display only, trade-unrelated; the Binance panel already matches.)
    try:
        _fm = getattr(system, "focus_manager", None)
        if _fm is not None and hasattr(_fm, "_get_client"):
            snap["api_stats"] = _fm._get_client().get_api_stats()
        elif hasattr(system, "trade_client") and hasattr(system.trade_client, "get_api_stats"):
            snap["api_stats"] = system.trade_client.get_api_stats()
    except (KeyError, AttributeError, TypeError):
        report_suppressed_exception(__name__, 'API call stats (rate limit monitoring)')

    result = {"ok": True, "system": snap}
    request.app.state._status_cache = (now, result)
    return result


# ------------------------------------------------------------
# Ledger
# ------------------------------------------------------------
@router.get("/ledger/tail")
def ledger_tail(
    request: Request,
    n: int = Query(200, ge=1, le=2000),
) -> Dict[str, Any]:
    system = request.app.state.system
    return {
        "ok": True,
        "items": system.ledger.tail(n),
    }


def _extract_equity_from_ledger_record(rec: Dict[str, Any]) -> Optional[float]:
    """Extract the account equity (equity_usdt) value from a ledger record."""
    try:
        data = rec.get("data") if isinstance(rec, dict) else None
        if not isinstance(data, dict):
            return None

        # 1) Most common case: ALLOC_REBALANCE.data.equity_usdt
        eq = data.get("equity_usdt")
        if eq is not None:
            v = float(eq)
            if v > 0:
                return v

        # 2) Handle some snapshot-style formats
        eq_obj = data.get("equity")
        if isinstance(eq_obj, dict):
            v2 = eq_obj.get("equity_usdt")
            if v2 is not None:
                fv2 = float(v2)
                if fv2 > 0:
                    return fv2
    except (AttributeError, TypeError, ValueError):
        logger.warning("system_router._extract_equity_from_ledger_record L419 except", exc_info=True)
        return None
    return None


@router.get("/equity/at", summary="Equity at reference time", description="Get the equity_usdt closest to the reference time from the ledger")
def get_equity_at_time(
    request: Request,
    dt: str = Query(..., description="Reference time (e.g., 2026-02-15T00:00)"),
    lookback_hours: int = Query(240, ge=1, le=720, description="Ledger search range (hours)"),
    tail_lines: int = Query(120000, ge=1000, le=400000, description="Max ledger lines to scan"),
) -> Dict[str, Any]:
    """
    Return the ledger equity snapshot closest to the given time.
    - Primary data source: ALLOC_REBALANCE.data.equity_usdt
    - Falls back to the latest status value if absent
    """
    from datetime import datetime

    try:
        dt_str = str(dt or "").strip()
        if not dt_str:
            return {"ok": False, "error": "dt is required"}

        # Interpret datetime-local ("YYYY-MM-DDTHH:MM") or ISO datetime as local time
        target_dt = datetime.fromisoformat(dt_str)
        target_ts = float(target_dt.timestamp())
    except (TypeError, ValueError) as e:
        logger.warning("system_router.get_equity_at_time L446: %s", e)
        return {"ok": False, "error": f"invalid dt: {e}"}

    system = request.app.state.system
    since_ts = max(0.0, target_ts - float(lookback_hours) * 3600.0)

    try:
        records = system.ledger.tail_records(since_ts=since_ts, tail_lines=tail_lines)
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.get_equity_at_time L454: %s", e)
        return {"ok": False, "error": f"ledger read failed: {e}"}

    best: Optional[Dict[str, Any]] = None
    best_diff: Optional[float] = None

    for rec in records:
        eq = _extract_equity_from_ledger_record(rec)
        if eq is None:
            continue
        ts = float(rec.get("ts", 0.0) or 0.0)
        if ts <= 0:
            continue
        diff = abs(ts - target_ts)
        if best is None or best_diff is None or diff < best_diff:
            best = rec
            best_diff = diff

    if best is None:
        # Fallback: current status value
        try:
            snap = system.status()
            now_eq = float(((snap.get("equity") or {}).get("equity_usdt")) or 0.0)
            if now_eq > 0:
                return {
                    "ok": True,
                    "equity_usdt": now_eq,
                    "source_event": "SYSTEM_STATUS_FALLBACK",
                    "target_ts": target_ts,
                    "actual_ts": float(time.time()),
                    "diff_sec": int(abs(time.time() - target_ts)),
                    "note": "ledger_match_not_found",
                }
        except (AttributeError, TypeError, ValueError):
            report_suppressed_exception(__name__, 'Fallback: current status value')
        return {"ok": False, "error": "no equity snapshot found in ledger"}

    eq_val = _extract_equity_from_ledger_record(best)
    actual_ts = float(best.get("ts", 0.0) or 0.0)
    event = str(best.get("event") or "UNKNOWN")

    return {
        "ok": True,
        "equity_usdt": float(eq_val or 0.0),
        "source_event": event,
        "target_ts": target_ts,
        "actual_ts": actual_ts,
        "diff_sec": int(abs(actual_ts - target_ts)),
    }




# ------------------------------------------------------------
# Empirical tuning (ledger-based)
# ------------------------------------------------------------
@router.get("/tuning/report")
def tuning_report(
    request: Request,
    hours: float = Query(24.0, ge=0.0, le=168.0),
    min_samples: int = Query(50, ge=1, le=10000),
) -> Dict[str, Any]:
    system = request.app.state.system
    rep = system.tuning_report(window_hours=hours, min_samples=min_samples)
    return {"ok": True, "report": rep}


@router.get("/tuning/export_env", response_class=PlainTextResponse)
def tuning_export_env(
    request: Request,
    hours: float = Query(24.0, ge=0.0, le=168.0),
    min_samples: int = Query(50, ge=1, le=10000),
) -> str:
    system = request.app.state.system
    return system.tuning_export_env(window_hours=hours, min_samples=min_samples)

# ------------------------------------------------------------
# Emergency
# ------------------------------------------------------------
@router.post("/emergency/stop")
def emergency_stop(request: Request, reason: str | None = None) -> Dict[str, Any]:
    system = request.app.state.system
    system.set_emergency_stop(True, reason=reason or "manual")
    return {"ok": True, "emergency_stop": True}


@router.post("/emergency/resume")
def emergency_resume(request: Request, reason: str | None = None) -> Dict[str, Any]:
    system = request.app.state.system
    system.emergency_manual_override = True
    system.set_emergency_stop(False, reason="manual_resume")
    return {"ok": True, "emergency_stop": False}


# ------------------------------------------------------------
# Reconcile
# ------------------------------------------------------------
@router.post("/reconcile")
def reconcile(request: Request, reason: str | None = None) -> Dict[str, Any]:
    system = request.app.state.system
    try:
        out = system.reconcile(reason=reason or "manual")
        return {"ok": True, "result": out}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("system_router.reconcile L557: %s", e)
        err_str = str(e).lower()
        if "ddos" in err_str or "rate" in err_str or "banned" in err_str or "418" in err_str:
            return {"ok": False, "error": "rate_limit", "message": "Exchange API rate limit exceeded. Please wait a few minutes."}
        return {"ok": False, "error": "reconcile_failed", "message": str(e)}

# ------------------------------------------------------------
# PnL Baseline Reset — one-click visual reset
# ★ [2026-05-31 owner] "A rice cake that looks good tastes good too" — capture the current
#   balance → start PnL from 0.
# ------------------------------------------------------------
@router.post("/pnl-baseline/reset")
def pnl_baseline_reset(
    request: Request,
    baseline: Optional[float] = Query(None, ge=0, description="Reference amount (USDT). Set directly to reflect deposits etc. If unset/0, uses current equity (legacy behavior)"),
) -> Dict[str, Any]:
    """Capture the PnL baseline → save to runtime/pnl_baseline.json.

    [2026-06-02 owner] Support direct baseline input — choose 'what amount' to reset from after a deposit.
      - baseline set (>0): use that amount as the reference point (reflects deposit)
      - unset/0: capture current equity (legacy 'previous amount' behavior = backward compatible)
    Effect: the PnL display at the top of the dashboard restarts from 0 (visual reset).
    No effect on bot behavior (entry sizing/guards are all based on the current balance only).
    """
    import json as _json
    import time as _time
    import os as _os
    system = request.app.state.system
    try:
        current_equity = float(getattr(system, "_last_equity_usdt", 0) or 0)
        # If a baseline is provided, use it (reflects deposit); otherwise use current equity (backward compatible)
        if baseline is not None and float(baseline) > 0:
            base_val = round(float(baseline), 2)
            _src = "manual_input"
        else:
            if current_equity <= 0:
                return {"ok": False, "error": "no_equity", "message": "Cannot measure current equity — retry shortly"}
            base_val = round(current_equity, 2)
            _src = "current_equity"
        _bp = _os.path.join("runtime", "pnl_baseline.json")
        _os.makedirs(_os.path.dirname(_bp), exist_ok=True)
        data = {
            "baseline": base_val,
            "reset_ts": _time.time(),
            "reset_iso": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime()),
            "source": _src,
        }
        with open(_bp, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2)
        logger.info("[PnL Baseline] Reset to $%.2f (%s)", base_val, _src)
        return {"ok": True, "baseline": data["baseline"], "reset_iso": data["reset_iso"], "source": _src}
    except Exception as e:
        logger.warning("system_router.pnl_baseline_reset failed: %s", e)
        return {"ok": False, "error": "reset_failed", "message": str(e)}


@router.get("/guards")
def guards_get(request: Request) -> Dict[str, Any]:
    system = request.app.state.system
    
    # [2026-02-02] Build coordinator data for Guard Matrix V2
    coordinator_data: Dict[str, Dict[str, Any]] = {}
    if hasattr(system, "coordinator") and hasattr(system.coordinator, "contexts"):
        # Guard Matrix is intended for currently managed positions only.
        # Restrict render scope to ACTIVE/RECOVERY to avoid WATCH history noise.
        markets_to_render: set[str] = set()
        try:
            if hasattr(system, "oma_registry"):
                for m in system.oma_registry.list_active():
                    markets_to_render.add(str(m))
                if hasattr(system.oma_registry, "list_recovery"):
                    for m in system.oma_registry.list_recovery():
                        markets_to_render.add(str(m))
        except (KeyError, AttributeError, TypeError):
            report_suppressed_exception(__name__, 'Restrict render scope to ACTIVE/RECOVERY to avoid WATCH history noise.')
        
        # Fallback: if registry is unavailable, keep only non-WATCH contexts.
        if not markets_to_render:
            try:
                for market, ctx in (system.coordinator.contexts or {}).items():
                    st_val = ""
                    if hasattr(system, "oma_registry"):
                        st = system.oma_registry.get_state(str(market))
                        st_val = (st.value if hasattr(st, "value") else str(st)).upper()
                    if st_val in ("ACTIVE", "RECOVERY") or bool(getattr(ctx, "recovery", False)):
                        markets_to_render.add(str(market))
            except (KeyError, AttributeError, TypeError, ValueError):
                report_suppressed_exception(__name__, 'Fallback: if registry is unavailable, keep only non-WATCH contexts.')

        for market in sorted(markets_to_render):
            try:
                ctx = system.coordinator.contexts.get(market)
                if ctx is None:
                    # If an ACTIVE/RECOVERY market is missing from contexts, create it so it shows in the Guard Matrix
                    ctx = system.coordinator.ensure_market(market)
            except (KeyError, AttributeError, TypeError):
                report_suppressed_exception(__name__, 'If an ACTIVE/RECOVERY market is missing from contexts, create it so it shows in the Guard Matrix; except-> continue')
                continue
            pos = getattr(ctx, "position", None)
            pos_qty = 0.0
            pos_entry = 0.0
            if pos:
                pos_qty = float(pos.get("qty") or 0.0) if isinstance(pos, dict) else float(getattr(pos, "qty", 0.0))
                pos_entry = float(pos.get("entry") or 0.0) if isinstance(pos, dict) else float(getattr(pos, "entry", 0.0))
            
            # Strategy info
            strat = getattr(ctx, "strategy_mode", None) or getattr(ctx, "strategy", None) or ""
            ctrls = getattr(ctx, "controls", None) or {}
            if not strat and isinstance(ctrls, dict):
                s_ctrl = ctrls.get("strategy", {})
                strat = s_ctrl.get("mode", "") if isinstance(s_ctrl, dict) else ""
            
            # Entry/Exit block state
            entry_blocked = False
            exit_blocked = False
            entry_reason = getattr(ctx, "entry_block_reason", None) or ""
            exit_reason = getattr(ctx, "exit_block_reason", None) or ""
            entry_until = getattr(ctx, "entry_block_until_ts", None)
            exit_until = getattr(ctx, "exit_block_until_ts", None)
            now_ts = time.time()
            if entry_until and float(entry_until) > now_ts:
                entry_blocked = True
            if exit_until and float(exit_until) > now_ts:
                exit_blocked = True
            
            # Recovery
            in_recovery = bool(getattr(ctx, "recovery", False))
            
            # Warmup status
            is_ready = getattr(ctx, "is_ready", lambda: True)()
            readiness = {}
            if hasattr(ctx, "readiness_status"):
                readiness = ctx.readiness_status()
            warmup_ticks = readiness.get("ticks", 0)
            min_ticks = readiness.get("min_ticks", 5)
            
            # Manual mode
            manual_mode = bool(getattr(ctx, "manual_mode", False))
            
            # LongHold
            long_hold = bool(getattr(ctx, "long_hold", False))
            
            # Order pending
            order_state = getattr(ctx, "order_state", None)
            has_order = order_state is not None
            
            # Market state from registry
            market_state = "WATCH"
            if hasattr(system, "oma_registry"):
                try:
                    st = system.oma_registry.get_state(market)
                    market_state = st.value if hasattr(st, "value") else str(st)
                except (KeyError, AttributeError, TypeError):
                    report_suppressed_exception(__name__, 'Market state from registry')
            
            # Strategy meta (last decision)
            last_meta = getattr(ctx, "last_strategy_meta", None) or {}
            last_signal = getattr(ctx, "last_signal", None) or ""
            
            # AI info
            current_ai = getattr(ctx, "current_ai", None) or {}
            ai_brain = current_ai.get("brain", {}) if isinstance(current_ai, dict) else {}
            ai_pred = float(ai_brain.get("ai_prediction", 0.5)) if ai_brain else 0.5
            
            coordinator_data[market] = {
                "market": market,
                "strategy": str(strat).upper(),
                "market_state": market_state.upper(),
                "position_qty": pos_qty,
                "position_entry": pos_entry,
                "entry_state": "BLOCKED" if entry_blocked else "OPEN",
                "exit_state": "BLOCKED" if exit_blocked else "OPEN",
                "entry_block_reason": entry_reason,
                "exit_block_reason": exit_reason,
                "entry_block_until_ts": float(entry_until or 0) * 1000 if entry_until else None,
                "exit_block_until_ts": float(exit_until or 0) * 1000 if exit_until else None,
                "recovery": in_recovery,
                "is_ready": is_ready,
                "warmup_ticks": warmup_ticks,
                "min_ticks": min_ticks,
                "manual_mode": manual_mode,
                "long_hold": long_hold,
                "order_state": str(order_state) if order_state else None,
                "last_signal": last_signal,
                "last_meta": last_meta,
                "ai_prediction": ai_pred,
                "controls": ctrls,
            }
    
    # Safety data
    safety_data: Dict[str, Any] = {}
    # Global entry cooldown
    global_cd_until = getattr(system, "global_entry_cooldown_until_ts", None)
    if global_cd_until:
        remaining = float(global_cd_until) - time.time()
        safety_data["global_entry_cooldown"] = {
            "until_ts": float(global_cd_until) * 1000,
            "remaining_sec": max(0, remaining),
        }
    # Order pressure
    pending_total = getattr(system, "pending_orders_count", 0)
    max_pending = int(getattr(system, "max_pending_orders_total", 0) or 0)
    safety_data["order_pressure"] = {
        "pending_orders_total": pending_total,
        "max_pending_orders_total": max_pending,
    }
    
    return {
        "ok": True,
        "coordinator": coordinator_data,
        "safety": safety_data,
        "guards": {
            # Global / safety switches
            "emergency_stop": bool(getattr(system, "emergency_stop", False)),

            # Informational / persistence
            "ui_settings_loaded": bool(getattr(system, "_ui_settings_loaded", False)),

            "exit_profit_guard": bool(getattr(system, "exit_profit_guard", False)),
            "entry_ob_guard_enabled": bool(getattr(system, "entry_ob_guard_enabled", False)),
            "entry_ceiling_guard": bool(getattr(system, "entry_ceiling_guard", False)),
            "entry_recent_high_guard": bool(getattr(system, "entry_recent_high_guard", False)),
            "entry_qty_guard": bool(getattr(system, "entry_qty_guard", False)),
            "drawdown_guard": bool(getattr(system, "drawdown_guard", False)),
            "btc_guard_enabled": bool(getattr(system, "btc_guard_enabled", True)),
            "btc_guard_mode": bool(getattr(system, "btc_guard_mode", False)),

            # optional but useful
            "tp_limit_exit_enabled": bool(getattr(system, "tp_limit_exit_enabled", False)),
            "wallet_mode": bool(getattr(system, "wallet_mode", False)),
            "reconcile_position_sync_mode": str(getattr(system, "reconcile_position_sync_mode", "OFF") or "OFF"),
            
            # Performance & Graduation
            "autopilot_perf_rebalance_enabled": bool(getattr(system, "autopilot_perf_rebalance_enabled", False)),
            "autopilot_perf_apply_auto": bool(getattr(system, "autopilot_perf_apply_auto", False)),
            "autopilot_graduation_enabled": bool(getattr(system, "autopilot_graduation_enabled", False)),
            "autopilot_grad_apply_auto": bool(getattr(system, "autopilot_grad_apply_auto", False)),
            
            # Scope Slot Rotation
            "autopilot_scope_rotation_enabled": bool(getattr(system, "autopilot_scope_rotation_enabled", True)),
            "autopilot_scope_idle_min": int(getattr(system, "autopilot_scope_idle_min", 2) or 2),
            "autopilot_scope_deploy_mode": str(getattr(system, "autopilot_scope_deploy_mode", "wait")),
            "autopilot_scope_trap_tp_timeout_hours": float(getattr(system, "autopilot_scope_trap_tp_timeout_hours", 4.0) or 0),
            "autopilot_scope_cooldown_min": int(getattr(system, "autopilot_scope_cooldown_min", 60) or 0),
            "autopilot_scope_adaptive_cd": bool(getattr(system, "autopilot_scope_adaptive_cd", True)),
            "autopilot_scope_target_n": max(0, int(getattr(system, "autopilot_scope_target_n", getattr(system, "reserved_sniper_n", 0)) or 0)),
            # LONG/SHORT (SNIPER(s) Scope) UI prefs
            "longshort_scope_power": bool(getattr(system, "longshort_scope_power", True)),
            "longshort_scope_auto_fire": bool(getattr(system, "longshort_scope_auto_fire", True)),
            "longshort_scope_assist_fire": bool(getattr(system, "longshort_scope_assist_fire", True)),
            "longshort_scope_assist_fire_auto": bool(getattr(system, "longshort_scope_assist_fire_auto", False)),
            "longshort_scope_slicing": bool(getattr(system, "longshort_scope_slicing", True)),
            "longshort_scope_random_active": bool(getattr(system, "longshort_scope_random_active", True)),
            "longshort_scope_random_interval_sec": int(getattr(system, "longshort_scope_random_interval_sec", 60) or 60),
            "longshort_scope_top_n": int(getattr(system, "longshort_scope_top_n", 5) or 5),
            "longshort_scope_budget_per_slot_usdt": int(getattr(system, "longshort_scope_budget_per_slot_usdt", 100) or 100),
            "longshort_scope_auto_scan": bool(getattr(system, "longshort_scope_auto_scan", True)),
            "longshort_scope_min_price": float(getattr(system, "longshort_scope_min_price", 0.0) or 0),
            "longshort_scope_max_price": float(getattr(system, "longshort_scope_max_price", 0.0) or 0),
            # Dust auto cleanup
            "dust_vacuum_enabled": bool(getattr(system, "dust_vacuum_enabled", False)),
            "dust_vacuum_daily_count": max(1, int(getattr(system, "dust_vacuum_daily_count", 1) or 1)),
            "dust_vacuum_threshold_usdt": float(getattr(system, "dust_vacuum_threshold_usdt", 5.0) or 5.0),

            # Risk & Smart Features
            "correlation_guard_enabled": bool(getattr(system, "correlation_guard_enabled", False)),
            "time_strategy_enabled": bool(getattr(system, "time_strategy_enabled", False)),
            "risk_budget_enabled": bool(getattr(system, "risk_budget_enabled", False)),
            "ai_position_sizing_enabled": bool(getattr(system, "ai_position_sizing_enabled", False)),
            "dynamic_stoploss_enabled": bool(getattr(system, "dynamic_stoploss_enabled", False)),
            "daily_loss_limit_pct": float(getattr(system, "daily_loss_limit_pct", 2.0) or 2.0),
            "circuit_breaker_loss_pct": float(getattr(system, "circuit_breaker_loss_pct", 10.0) or 10.0),
            "circuit_breaker_cooldown_min": float(getattr(system, "circuit_breaker_cooldown_min", 30.0) or 30.0),
            "max_same_sector": int(getattr(system, "max_same_sector", 2) or 2),
            "high_correlation_threshold": float(getattr(system, "high_correlation_threshold", 0.7) or 0.7),

            # -------- numeric / thresholds (env-default, dashboard-overridable) --------
            # Entry
            "min_order_usdt": float(getattr(system, "min_order_usdt", 0.0) or 0.0),

            "entry_global_gap_sec": float(getattr(system, "entry_global_gap_sec", 0.0) or 0.0),
            "max_pending_orders_total": int(getattr(system, "max_pending_orders_total", 0) or 0),

            "entry_ob_max_spread_bps": float(getattr(system, "entry_ob_max_spread_bps", 0.0) or 0.0),
            "entry_ob_depth_bps": float(getattr(system, "entry_ob_depth_bps", 0.0) or 0.0),
            "entry_ob_depth_factor": float(getattr(system, "entry_ob_depth_factor", 0.0) or 0.0),
            "entry_ob_stale_sec": float(getattr(system, "entry_ob_stale_sec", 0.0) or 0.0),

            "entry_max_qty": float(getattr(system, "entry_max_qty", 0.0) or 0.0),
            "entry_qty_cooldown_sec": float(getattr(system, "entry_qty_cooldown_sec", 0.0) or 0.0),

            "entry_ceiling_apply": str(getattr(system, "entry_ceiling_apply", "NON_BULL") or "NON_BULL"),
            "entry_ceiling_fee_rate": float(getattr(system, "entry_ceiling_fee_rate", 0.0) or 0.0),
            "entry_ceiling_slippage_guard_bps": float(getattr(system, "entry_ceiling_slippage_guard_bps", 0.0) or 0.0),
            "entry_ceiling_spread_guard_bps": float(getattr(system, "entry_ceiling_spread_guard_bps", 0.0) or 0.0),
            "entry_ceiling_extra_bps": float(getattr(system, "entry_ceiling_extra_bps", 0.0) or 0.0),
            "entry_ceiling_max_age_sec": float(getattr(system, "entry_ceiling_max_age_sec", 0.0) or 0.0),
            "entry_ceiling_decay_mode": str(getattr(system, "entry_ceiling_decay_mode", "") or ""),
            "entry_ceiling_decay_half_life_sec": float(getattr(system, "entry_ceiling_decay_half_life_sec", 0.0) or 0.0),
            "entry_ceiling_cooldown_sec": float(getattr(system, "entry_ceiling_cooldown_sec", 0.0) or 0.0),
            "entry_recent_high_apply": str(getattr(system, "entry_recent_high_apply", "NON_BULL") or "NON_BULL"),
            "entry_recent_high_lookback_hours": float(getattr(system, "entry_recent_high_lookback_hours", 24.0) or 24.0),
            "entry_recent_high_near_pct": float(getattr(system, "entry_recent_high_near_pct", 0.8) or 0.8),
            "entry_recent_high_cooldown_sec": float(getattr(system, "entry_recent_high_cooldown_sec", 10.0) or 10.0),
            "entry_recent_high_candle_unit_min": int(getattr(system, "entry_recent_high_candle_unit_min", 15) or 15),
            "entry_recent_high_cache_sec": float(getattr(system, "entry_recent_high_cache_sec", 30.0) or 30.0),
            "entry_recent_high_breakout_enabled": bool(getattr(system, "entry_recent_high_breakout_enabled", True)),
            "entry_recent_high_breakout_margin_pct": float(getattr(system, "entry_recent_high_breakout_margin_pct", 0.25) or 0.25),
            "entry_recent_high_breakout_require_bull": bool(getattr(system, "entry_recent_high_breakout_require_bull", True)),
            "entry_recent_high_breakout_min_regime_change_pct": float(getattr(system, "entry_recent_high_breakout_min_regime_change_pct", 0.35) or 0.35),
            "entry_recent_high_breakout_max_spread_bps": float(getattr(system, "entry_recent_high_breakout_max_spread_bps", 18.0) or 18.0),

            # Exit
            "exit_fee_rate": float(getattr(system, "exit_fee_rate", 0.0) or 0.0),
            "exit_slippage_guard_bps": float(getattr(system, "exit_slippage_guard_bps", 0.0) or 0.0),
            "exit_min_net_profit_pct": float(getattr(system, "exit_min_net_profit_pct", 0.0) or 0.0),
            "exit_min_net_profit_usdt": float(getattr(system, "exit_min_net_profit_usdt", 0.0) or 0.0),

            # TP limit exit
            "tp_limit_timeout_sec": float(getattr(system, "tp_limit_timeout_sec", 0.0) or 0.0),
            "tp_limit_max_retries": int(getattr(system, "tp_limit_max_retries", 0) or 0),

            # Entry limit buy
            "entry_limit_buy_enabled": bool(getattr(system, "entry_limit_buy_enabled", False)),
            "entry_limit_timeout_sec": float(getattr(system, "entry_limit_timeout_sec", 5.0) or 5.0),
            "entry_limit_cooldown_sec": float(getattr(system, "entry_limit_cooldown_sec", 60.0) or 60.0),
            "entry_limit_price_mode": str(getattr(system, "entry_limit_price_mode", "best_bid") or "best_bid"),

            # BTC guard
            "btc_guard_down_5m_pct": float(getattr(system, "btc_guard_down_5m_pct", 2.0) or 2.0),
            "btc_guard_down_15m_pct": float(getattr(system, "btc_guard_down_15m_pct", 5.0) or 5.0),
            "btc_guard_trail_tighten_ratio": float(getattr(system, "btc_guard_trail_tighten_ratio", 0.5) or 0.5),

            "ai_retrain_threshold": float(getattr(system, "ai_retrain_threshold", 0.6) or 0.6),
            
            # Smart Allocation
            "smart_alloc_enabled": bool(getattr(system, "smart_alloc_enabled", True)),
            "smart_alloc_w_profit": float(getattr(system, "smart_alloc_w_profit", 0.5)),
            "smart_alloc_w_ai": float(getattr(system, "smart_alloc_w_ai", 0.3)),
            "smart_alloc_w_risk": float(getattr(system, "smart_alloc_w_risk", 0.2)),
            "smart_alloc_w_momentum": float(getattr(system, "smart_alloc_w_momentum", 0.15)),
            "smart_alloc_w_kelly": float(getattr(system, "smart_alloc_w_kelly", 0.15)),
            "smart_alloc_w_liquidity": float(getattr(system, "smart_alloc_w_liquidity", 0.15)),
            "smart_alloc_min_mult": float(getattr(system, "smart_alloc_min_mult", 0.5)),
            "smart_alloc_max_mult": float(getattr(system, "smart_alloc_max_mult", 2.0)),
            "smart_alloc_corr_enabled": bool(getattr(system, "smart_alloc_corr_enabled", True)),
            "smart_alloc_corr_th": float(getattr(system, "smart_alloc_corr_th", 0.7) or 0.7),
            "smart_alloc_sector_enabled": bool(getattr(system, "smart_alloc_sector_enabled", True)),
        }
    }

@router.post("/guards")
def guards_set(
    request: Request,
    # On/off switches (optional; if None, keep current)
    exit_profit_guard: bool | None = None,
    entry_ob_guard_enabled: bool | None = None,
    entry_ceiling_guard: bool | None = None,
    entry_recent_high_guard: bool | None = None,
    entry_qty_guard: bool | None = None,
    drawdown_guard: bool | None = None,
    btc_guard_enabled: bool | None = None,

    tp_limit_exit_enabled: bool | None = None,
    entry_limit_buy_enabled: bool | None = None,
    wallet_mode: bool | None = None,
    reconcile_position_sync_mode: str | None = None,
    
    # Performance & Graduation
    autopilot_perf_rebalance_enabled: bool | None = None,
    autopilot_perf_apply_auto: bool | None = None,
    autopilot_graduation_enabled: bool | None = None,
    autopilot_grad_apply_auto: bool | None = None,
    
    # Scope Slot Rotation
    autopilot_scope_rotation_enabled: bool | None = None,
    autopilot_scope_idle_min: int | None = None,
    autopilot_scope_deploy_mode: str | None = None,
    autopilot_scope_trap_tp_timeout_hours: float | None = None,
    autopilot_scope_target_n: int | None = None,
    autopilot_scope_cooldown_min: int | None = None,
    autopilot_scope_adaptive_cd: bool | None = None,
    # LONG/SHORT (SNIPER(s) Scope) UI prefs
    longshort_scope_power: bool | None = None,
    longshort_scope_auto_fire: bool | None = None,
    longshort_scope_assist_fire: bool | None = None,
    longshort_scope_assist_fire_auto: bool | None = None,
    longshort_scope_slicing: bool | None = None,
    longshort_scope_random_active: bool | None = None,
    longshort_scope_random_interval_sec: int | None = None,
    longshort_scope_top_n: int | None = None,
    longshort_scope_budget_per_slot_usdt: int | None = None,
    longshort_scope_min_conf: float | None = None,
    longshort_scope_auto_scan: bool | None = None,
    longshort_scope_min_price: float | None = None,
    longshort_scope_max_price: float | None = None,
    # Dust auto cleanup
    dust_vacuum_enabled: bool | None = None,
    dust_vacuum_daily_count: int | None = None,
    dust_vacuum_threshold_usdt: float | None = None,

    # Risk & Smart Features
    correlation_guard_enabled: bool | None = None,
    time_strategy_enabled: bool | None = None,
    risk_budget_enabled: bool | None = None,
    ai_position_sizing_enabled: bool | None = None,
    dynamic_stoploss_enabled: bool | None = None,
    daily_loss_limit_pct: float | None = None,
    circuit_breaker_loss_pct: float | None = None,
    circuit_breaker_cooldown_min: float | None = None,
    max_same_sector: int | None = None,
    high_correlation_threshold: float | None = None,

    # Numeric thresholds (optional)
    # BTC guard
    btc_guard_down_5m_pct: float | None = None,
    btc_guard_down_15m_pct: float | None = None,
    btc_guard_trail_tighten_ratio: float | None = None,

    # Numeric thresholds (optional)
    # Exit profit guard
    exit_fee_rate: float | None = None,
    exit_slippage_guard_bps: float | None = None,
    exit_min_net_profit_pct: float | None = None,
    exit_min_net_profit_usdt: float | None = None,

    # Entry orderbook guard
    entry_ob_max_spread_bps: float | None = None,
    entry_ob_depth_bps: float | None = None,
    entry_ob_depth_factor: float | None = None,
    entry_ob_stale_sec: float | None = None,

    # Entry qty guard
    entry_max_qty: float | None = None,
    entry_qty_cooldown_sec: float | None = None,

    # Entry ceiling guard
    entry_ceiling_apply: str | None = None,
    entry_ceiling_fee_rate: float | None = None,
    entry_ceiling_slippage_guard_bps: float | None = None,
    entry_ceiling_spread_guard_bps: float | None = None,
    entry_ceiling_extra_bps: float | None = None,
    entry_ceiling_max_age_sec: float | None = None,
    entry_ceiling_decay_mode: str | None = None,
    entry_ceiling_decay_half_life_sec: float | None = None,
    entry_ceiling_cooldown_sec: float | None = None,
    entry_recent_high_apply: str | None = None,
    entry_recent_high_lookback_hours: float | None = None,
    entry_recent_high_near_pct: float | None = None,
    entry_recent_high_cooldown_sec: float | None = None,
    entry_recent_high_candle_unit_min: int | None = None,
    entry_recent_high_cache_sec: float | None = None,
    entry_recent_high_breakout_enabled: bool | None = None,
    entry_recent_high_breakout_margin_pct: float | None = None,
    entry_recent_high_breakout_require_bull: bool | None = None,
    entry_recent_high_breakout_min_regime_change_pct: float | None = None,
    entry_recent_high_breakout_max_spread_bps: float | None = None,

    # Global pressure guards
    min_order_usdt: float | None = None,
    entry_global_gap_sec: float | None = None,
    max_pending_orders_total: int | None = None,

    # TP limit exit
    tp_limit_timeout_sec: float | None = None,
    tp_limit_max_retries: int | None = None,

    # Entry limit buy
    entry_limit_timeout_sec: float | None = None,
    entry_limit_cooldown_sec: float | None = None,
    entry_limit_price_mode: str | None = None,

    ai_retrain_threshold: float | None = None,

    # Smart Allocation
    smart_alloc_enabled: bool | None = None,
    smart_alloc_w_profit: float | None = None,
    smart_alloc_w_ai: float | None = None,
    smart_alloc_w_risk: float | None = None,
    smart_alloc_w_momentum: float | None = None,
    smart_alloc_w_kelly: float | None = None,
    smart_alloc_w_liquidity: float | None = None,
    smart_alloc_min_mult: float | None = None,
    smart_alloc_max_mult: float | None = None,
    smart_alloc_corr_enabled: bool | None = None,
    smart_alloc_corr_th: float | None = None,
    smart_alloc_sector_enabled: bool | None = None,

    # Operational actions
    clear_global_entry_cooldown: bool = False,
) -> Dict[str, Any]:
    system = request.app.state.system

    # apply toggles
    if exit_profit_guard is not None:
        system.exit_profit_guard = bool(exit_profit_guard)
    if entry_ob_guard_enabled is not None:
        system.entry_ob_guard_enabled = bool(entry_ob_guard_enabled)
    if entry_ceiling_guard is not None:
        system.entry_ceiling_guard = bool(entry_ceiling_guard)
    if entry_recent_high_guard is not None:
        system.entry_recent_high_guard = bool(entry_recent_high_guard)
    if entry_qty_guard is not None:
        system.entry_qty_guard = bool(entry_qty_guard)
    if drawdown_guard is not None:
        system.drawdown_guard = bool(drawdown_guard)
    if btc_guard_enabled is not None:
        enabled = bool(btc_guard_enabled)
        system.btc_guard_enabled = enabled
        if not enabled:
            # Guard OFF: clear runtime mode + restore pre-guard state.
            try:
                if bool(getattr(system, "btc_guard_mode", False)):
                    pre = getattr(system, "_pre_guard_auto_approve", {}) or {}
                    if isinstance(pre, dict):
                        for _name, _val in pre.items():
                            setattr(system, str(_name), bool(_val))
            except (KeyError, AttributeError, TypeError):
                report_suppressed_exception(__name__, 'Guard OFF: clear runtime mode + restore pre-guard state.')
            try:
                setattr(system, "btc_guard_mode", False)
                setattr(system, "_pre_guard_auto_approve", {})
                if hasattr(system, "_restore_trailing_stops"):
                    try:
                        system._restore_trailing_stops()
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                        report_suppressed_exception(__name__, 'Guard OFF: clear runtime mode + restore pre-guard state.')
            except (KeyError, AttributeError, TypeError):
                report_suppressed_exception(__name__, 'Guard OFF: clear runtime mode + restore pre-guard state.')

    if tp_limit_exit_enabled is not None:
        system.tp_limit_exit_enabled = bool(tp_limit_exit_enabled)
    if entry_limit_buy_enabled is not None:
        system.entry_limit_buy_enabled = bool(entry_limit_buy_enabled)
    if wallet_mode is not None:
        system.wallet_mode = bool(wallet_mode)
    if entry_recent_high_breakout_enabled is not None:
        system.entry_recent_high_breakout_enabled = bool(entry_recent_high_breakout_enabled)
    if entry_recent_high_breakout_require_bull is not None:
        system.entry_recent_high_breakout_require_bull = bool(entry_recent_high_breakout_require_bull)

    # Performance & Graduation
    if autopilot_perf_rebalance_enabled is not None:
        system.autopilot_perf_rebalance_enabled = bool(autopilot_perf_rebalance_enabled)
    if autopilot_perf_apply_auto is not None:
        system.autopilot_perf_apply_auto = bool(autopilot_perf_apply_auto)
    if autopilot_graduation_enabled is not None:
        system.autopilot_graduation_enabled = bool(autopilot_graduation_enabled)
    if autopilot_grad_apply_auto is not None:
        system.autopilot_grad_apply_auto = bool(autopilot_grad_apply_auto)

    # Scope Slot Rotation
    if autopilot_scope_rotation_enabled is not None:
        system.autopilot_scope_rotation_enabled = bool(autopilot_scope_rotation_enabled)
    if autopilot_scope_idle_min is not None:
        system.autopilot_scope_idle_min = max(2, int(autopilot_scope_idle_min))
    if autopilot_scope_deploy_mode is not None:
        dm = str(autopilot_scope_deploy_mode).strip().lower()
        if dm in ("wait", "market", "trap"):
            system.autopilot_scope_deploy_mode = dm
    if autopilot_scope_trap_tp_timeout_hours is not None:
        system.autopilot_scope_trap_tp_timeout_hours = max(0.0, min(72.0, float(autopilot_scope_trap_tp_timeout_hours)))
    if autopilot_scope_target_n is not None:
        system.autopilot_scope_target_n = max(0, min(20, int(autopilot_scope_target_n)))
    if autopilot_scope_cooldown_min is not None:
        system.autopilot_scope_cooldown_min = max(0, int(autopilot_scope_cooldown_min))
    if autopilot_scope_adaptive_cd is not None:
        system.autopilot_scope_adaptive_cd = bool(autopilot_scope_adaptive_cd)
    if longshort_scope_power is not None:
        system.longshort_scope_power = bool(longshort_scope_power)
    if longshort_scope_auto_fire is not None:
        system.longshort_scope_auto_fire = bool(longshort_scope_auto_fire)
    if longshort_scope_assist_fire is not None:
        system.longshort_scope_assist_fire = bool(longshort_scope_assist_fire)
    if longshort_scope_assist_fire_auto is not None:
        system.longshort_scope_assist_fire_auto = bool(longshort_scope_assist_fire_auto)
    if longshort_scope_slicing is not None:
        system.longshort_scope_slicing = bool(longshort_scope_slicing)
    if longshort_scope_random_active is not None:
        system.longshort_scope_random_active = bool(longshort_scope_random_active)
    if longshort_scope_random_interval_sec is not None:
        system.longshort_scope_random_interval_sec = max(15, int(longshort_scope_random_interval_sec))
    if longshort_scope_top_n is not None:
        system.longshort_scope_top_n = max(1, min(30, int(longshort_scope_top_n)))
    if longshort_scope_budget_per_slot_usdt is not None:
        system.longshort_scope_budget_per_slot_usdt = max(5, int(longshort_scope_budget_per_slot_usdt))
    if longshort_scope_min_conf is not None:
        system.longshort_scope_min_conf = max(10.0, min(100.0, float(longshort_scope_min_conf)))
    if longshort_scope_auto_scan is not None:
        system.longshort_scope_auto_scan = bool(longshort_scope_auto_scan)
    if longshort_scope_min_price is not None:
        system.longshort_scope_min_price = max(0.0, float(longshort_scope_min_price))
    if longshort_scope_max_price is not None:
        system.longshort_scope_max_price = max(0.0, float(longshort_scope_max_price))
    if dust_vacuum_enabled is not None:
        system.dust_vacuum_enabled = bool(dust_vacuum_enabled)
    if dust_vacuum_daily_count is not None:
        # N/day, lower-bounded at 1 to avoid accidental disable-by-zero.
        system.dust_vacuum_daily_count = max(1, int(dust_vacuum_daily_count))
    if dust_vacuum_threshold_usdt is not None:
        system.dust_vacuum_threshold_usdt = max(0.0, float(dust_vacuum_threshold_usdt))

    # Risk & Smart Features
    if correlation_guard_enabled is not None:
        system.correlation_guard_enabled = bool(correlation_guard_enabled)
    if time_strategy_enabled is not None:
        system.time_strategy_enabled = bool(time_strategy_enabled)
    if risk_budget_enabled is not None:
        system.risk_budget_enabled = bool(risk_budget_enabled)
    if ai_position_sizing_enabled is not None:
        system.ai_position_sizing_enabled = bool(ai_position_sizing_enabled)
    if dynamic_stoploss_enabled is not None:
        system.dynamic_stoploss_enabled = bool(dynamic_stoploss_enabled)
    if daily_loss_limit_pct is not None:
        system.daily_loss_limit_pct = float(daily_loss_limit_pct)
    if circuit_breaker_loss_pct is not None:
        system.circuit_breaker_loss_pct = max(1.0, min(50.0, float(circuit_breaker_loss_pct)))
    if circuit_breaker_cooldown_min is not None:
        system.circuit_breaker_cooldown_min = max(1.0, min(1440.0, float(circuit_breaker_cooldown_min)))
    # [2026-03-23] Sync to PRM when guards are saved
    _prm = getattr(system, "portfolio_risk_manager", None)
    if _prm:
        _prm.sync_from_system(system)
    if max_same_sector is not None:
        system.max_same_sector = int(max_same_sector)
    if high_correlation_threshold is not None:
        x = float(high_correlation_threshold)
        system.high_correlation_threshold = x
        # Backward compatibility: map legacy Corr TH control to Smart Allocation TH too.
        if smart_alloc_corr_th is None:
            try:
                system.smart_alloc_corr_th = max(0.0, min(0.99, x))
            except (OverflowError, TypeError, ValueError):
                report_suppressed_exception(__name__, 'Backward compatibility: map legacy Corr TH control to Smart Allocation TH too.')

    if reconcile_position_sync_mode is not None:
        v = str(reconcile_position_sync_mode).strip().upper()
        if v in ("OFF", "ACTIVE", "ALL"):
            system.reconcile_position_sync_mode = v

    # apply numeric thresholds
    # (Type casting is lenient; invalid values are ignored rather than crashing the API.)
    def _set_float(name: str, v: float | None) -> None:
        if v is None:
            return
        try:
            setattr(system, name, float(v))
        except (AttributeError, OverflowError, TypeError, ValueError):
            report_suppressed_exception(__name__, 'system_router._set_float fallback')

    def _set_int(name: str, v: int | None) -> None:
        if v is None:
            return
        try:
            setattr(system, name, int(v))
        except (AttributeError, OverflowError, TypeError, ValueError):
            report_suppressed_exception(__name__, 'system_router._set_int fallback')

    def _set_str(name: str, v: str | None) -> None:
        if v is None:
            return
        try:
            setattr(system, name, str(v))
        except (AttributeError, TypeError):
            report_suppressed_exception(__name__, 'system_router._set_str fallback')

    _set_float("exit_fee_rate", exit_fee_rate)
    _set_float("exit_slippage_guard_bps", exit_slippage_guard_bps)
    _set_float("exit_min_net_profit_pct", exit_min_net_profit_pct)
    _set_float("exit_min_net_profit_usdt", exit_min_net_profit_usdt or exit_min_net_profit_usdt)

    _set_float("entry_ob_max_spread_bps", entry_ob_max_spread_bps)
    _set_float("entry_ob_depth_bps", entry_ob_depth_bps)
    _set_float("entry_ob_depth_factor", entry_ob_depth_factor)
    _set_float("entry_ob_stale_sec", entry_ob_stale_sec)

    _set_float("entry_max_qty", entry_max_qty)
    _set_float("entry_qty_cooldown_sec", entry_qty_cooldown_sec)

    _set_str("entry_ceiling_apply", entry_ceiling_apply)
    _set_float("entry_ceiling_fee_rate", entry_ceiling_fee_rate)
    _set_float("entry_ceiling_slippage_guard_bps", entry_ceiling_slippage_guard_bps)
    _set_float("entry_ceiling_spread_guard_bps", entry_ceiling_spread_guard_bps)
    _set_float("entry_ceiling_extra_bps", entry_ceiling_extra_bps)
    _set_float("entry_ceiling_max_age_sec", entry_ceiling_max_age_sec)
    _set_str("entry_ceiling_decay_mode", entry_ceiling_decay_mode)
    _set_float("entry_ceiling_decay_half_life_sec", entry_ceiling_decay_half_life_sec)
    _set_float("entry_ceiling_cooldown_sec", entry_ceiling_cooldown_sec)
    if entry_recent_high_apply is not None:
        mode = str(entry_recent_high_apply).strip().upper()
        if mode in ("BEAR", "NON_BULL", "ALWAYS"):
            system.entry_recent_high_apply = mode
    _set_float("entry_recent_high_lookback_hours", entry_recent_high_lookback_hours)
    _set_float("entry_recent_high_near_pct", entry_recent_high_near_pct)
    _set_float("entry_recent_high_cooldown_sec", entry_recent_high_cooldown_sec)
    _set_int("entry_recent_high_candle_unit_min", entry_recent_high_candle_unit_min)
    _set_float("entry_recent_high_cache_sec", entry_recent_high_cache_sec)
    _set_float("entry_recent_high_breakout_margin_pct", entry_recent_high_breakout_margin_pct)
    _set_float("entry_recent_high_breakout_min_regime_change_pct", entry_recent_high_breakout_min_regime_change_pct)
    _set_float("entry_recent_high_breakout_max_spread_bps", entry_recent_high_breakout_max_spread_bps)

    _set_float("min_order_usdt", min_order_usdt or min_order_usdt)
    _set_float("entry_global_gap_sec", entry_global_gap_sec)
    _set_int("max_pending_orders_total", max_pending_orders_total)

    _set_float("tp_limit_timeout_sec", tp_limit_timeout_sec)
    _set_int("tp_limit_max_retries", tp_limit_max_retries)

    _set_float("entry_limit_timeout_sec", entry_limit_timeout_sec)
    _set_float("entry_limit_cooldown_sec", entry_limit_cooldown_sec)
    if entry_limit_price_mode is not None:
        mode = str(entry_limit_price_mode).strip().lower()
        if mode in ("best_bid", "best_ask"):
            system.entry_limit_price_mode = mode

    # BTC Guard numeric settings
    if btc_guard_down_5m_pct is not None:
        try:
            x = abs(float(btc_guard_down_5m_pct))
            system.btc_guard_down_5m_pct = max(0.5, min(20.0, x))
        except (OverflowError, TypeError, ValueError):
            report_suppressed_exception(__name__, 'BTC Guard numeric settings')
    if btc_guard_down_15m_pct is not None:
        try:
            x = abs(float(btc_guard_down_15m_pct))
            system.btc_guard_down_15m_pct = max(1.0, min(40.0, x))
        except (OverflowError, TypeError, ValueError):
            report_suppressed_exception(__name__, 'BTC Guard numeric settings')
    if btc_guard_trail_tighten_ratio is not None:
        try:
            x = float(btc_guard_trail_tighten_ratio)
            system.btc_guard_trail_tighten_ratio = max(0.1, min(1.0, x))
        except (OverflowError, TypeError, ValueError):
            report_suppressed_exception(__name__, 'BTC Guard numeric settings')

    _set_float("ai_retrain_threshold", ai_retrain_threshold)

    # Smart Allocation
    if smart_alloc_enabled is not None:
        system.smart_alloc_enabled = bool(smart_alloc_enabled)
    _set_float("smart_alloc_w_profit", smart_alloc_w_profit)
    _set_float("smart_alloc_w_ai", smart_alloc_w_ai)
    _set_float("smart_alloc_w_risk", smart_alloc_w_risk)
    _set_float("smart_alloc_w_momentum", smart_alloc_w_momentum)
    _set_float("smart_alloc_w_kelly", smart_alloc_w_kelly)
    _set_float("smart_alloc_w_liquidity", smart_alloc_w_liquidity)
    _set_float("smart_alloc_min_mult", smart_alloc_min_mult)
    _set_float("smart_alloc_max_mult", smart_alloc_max_mult)
    if smart_alloc_corr_enabled is not None:
        system.smart_alloc_corr_enabled = bool(smart_alloc_corr_enabled)
    if smart_alloc_corr_th is not None:
        try:
            x = float(smart_alloc_corr_th)
            system.smart_alloc_corr_th = max(0.0, min(0.99, x))
        except (OverflowError, TypeError, ValueError):
            report_suppressed_exception(__name__, 'Smart Allocation')
    if smart_alloc_sector_enabled is not None:
        system.smart_alloc_sector_enabled = bool(smart_alloc_sector_enabled)

    # clear global BUY cooldown
    if bool(clear_global_entry_cooldown):
        try:
            system._global_entry_block_until_ts = 0.0
            system._global_entry_block_reason = ""
            system.ledger.append("GLOBAL_ENTRY_COOLDOWN_CLEARED")
        except AttributeError:
            report_suppressed_exception(__name__, 'clear global BUY cooldown')

    # Persist dashboard overrides (env defaults, dashboard is authoritative)
    try:
        if hasattr(system, "persist_ui_settings"):
            system.persist_ui_settings()
        elif hasattr(system, "ui_persist_guard_settings"):
            system.ui_persist_guard_settings()
        elif hasattr(system, "persist_runtime_settings"):
            system.persist_runtime_settings()
    except (KeyError, AttributeError, TypeError):
        report_suppressed_exception(__name__, 'Persist dashboard overrides (env defaults, dashboard is authoritative)')

    return {"ok": True, "guards": guards_get(request)["guards"]}


@router.post("/guards/save")
def guards_save(request: Request, body: Dict[str, Any]) -> Dict[str, Any]:
    """Save guard settings.

    If a per-strategy override is present, apply only to that strategy;
    otherwise apply as a global setting.
    """
    import json
    from pathlib import Path
    
    system = request.app.state.system
    guards = body.get("guards", {})
    strategy = body.get("strategy_override") or guards.get("strategy")
    
    guards_dir = Path("runtime/guards")
    guards_dir.mkdir(parents=True, exist_ok=True)
    
    if strategy:
        file_path = guards_dir / f"{strategy.lower()}_guards.json"
        existing = {}
        if file_path.exists():
            try:
                existing = json.loads(file_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                report_suppressed_exception(__name__, 'system_router.guards_save fallback')
        existing.update(guards)
        file_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        
        _apply_guards_to_system(system, guards, strategy)
        
        return {"ok": True, "strategy": strategy, "saved": len(guards)}
    else:
        for key, val in guards.items():
            if hasattr(system, key):
                setattr(system, key, val)

        # [2026-03-23] Smart risk: handle os.environ-based settings
        import os as _os
        if "size_mult_hi_pct" in guards:
            try:
                v = min(-0.1, float(guards["size_mult_hi_pct"]))
                _os.environ["OMA_SIZE_MULT_HI_PCT"] = str(v)
            except (OverflowError, TypeError, ValueError):
                report_suppressed_exception(__name__, '[2026-03-23] Smart risk: handle os.environ-based settings')
        if "size_mult_floor" in guards:
            try:
                v = max(0.1, min(0.9, float(guards["size_mult_floor"])))
                _os.environ["OMA_SIZE_MULT_FLOOR"] = str(v)
            except (OverflowError, TypeError, ValueError):
                report_suppressed_exception(__name__, '[2026-03-23] Smart risk: handle os.environ-based settings')
        if "concentration_limit_pct" in guards:
            try:
                v = max(5.0, min(50.0, float(guards["concentration_limit_pct"])))
                setattr(system, "concentration_limit_pct", v)
            except (AttributeError, OverflowError, TypeError, ValueError):
                report_suppressed_exception(__name__, '[2026-03-23] Smart risk: handle os.environ-based settings')

        try:
            system.persist_ui_settings()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            report_suppressed_exception(__name__, '[2026-03-23] Smart risk: handle os.environ-based settings')

        return {"ok": True, "applied": len(guards)}


@router.get("/guards/strategies")
def guards_strategies_get(request: Request) -> Dict[str, Any]:
    """Get per-strategy guard settings."""
    import json
    from pathlib import Path
    
    guards_dir = Path("runtime/guards")
    result = {}
    
    strategies = ["LADDER", "LIGHTNING", "GAZUA", "CONTRARIAN", "SNIPER"]
    for strat in strategies:
        file_path = guards_dir / f"{strat.lower()}_guards.json"
        if file_path.exists():
            try:
                result[strat] = json.loads(file_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                logger.warning("system_router.guards_strategies_get L1369 except", exc_info=True)
                result[strat] = {}
        else:
            result[strat] = {}
    
    return {"ok": True, "guards": result}


def _apply_guards_to_system(system, guards: Dict[str, Any], strategy: str) -> None:
    """Apply guard settings to the system in real time."""
    if hasattr(system, "_strategy_guards"):
        if strategy not in system._strategy_guards:
            system._strategy_guards[strategy] = {}
        system._strategy_guards[strategy].update(guards)
    else:
        system._strategy_guards = {strategy: guards}


# ------------------------------------------------------------
# PnL (session / active markets)
# ------------------------------------------------------------
@router.get("/pnl/baseline")
def pnl_baseline(request: Request) -> Dict[str, Any]:
    system = request.app.state.system
    ts = ledger_pnl.load_pnl_baseline_ts()
    # If baseline missing, default to process start time to avoid 'all history'
    if ts <= 0.0:
        ts = float(getattr(system, "_boot_ts", 0.0) or time.time())
        ledger_pnl.save_pnl_baseline_ts(ts)
    return {"ok": True, "baseline_ts": ts}

@router.post("/pnl/reset")
def pnl_reset(request: Request, reason: str | None = None) -> Dict[str, Any]:
    ts = time.time()
    ledger_pnl.save_pnl_baseline_ts(ts)
    # also write to ledger for audit if available
    system = request.app.state.system
    try:
        system.ledger.append("PNL_RESET", reason=reason or "manual", baseline_ts=ts)
    except (AttributeError, TypeError):
        report_suppressed_exception(__name__, 'also write to ledger for audit if available')
    return {"ok": True, "baseline_ts": ts}

@router.get("/pnl/markets")
def pnl_markets(
    request: Request,
    hours: float = Query(24.0, ge=0.0, le=168.0),
    min_trades: int = Query(1, ge=0, le=100000),
    since_reset: int = Query(1, ge=0, le=1),
    scope: str = Query("open"),  # open=ACTIVE+RECOVERY, active=ACTIVE only, all=registry all
    tail_lines: int = Query(20000, ge=1000, le=200000),
) -> Dict[str, Any]:
    system = request.app.state.system
    now = time.time()
    window_since = now - float(hours) * 3600.0

    base_ts = 0.0
    if since_reset:
        base_ts = ledger_pnl.load_pnl_baseline_ts()
        if base_ts <= 0.0:
            base_ts = float(getattr(system, "_boot_ts", 0.0) or now)
            ledger_pnl.save_pnl_baseline_ts(base_ts)

    # scope markets from registry
    reg = system.oma_registry
    s = (scope or "open").lower()
    if s == "active":
        markets = list(reg.list_active())
    elif s == "open":
        markets = list(reg.list_active()) + list(reg.list_recovery())
    else:
        # all known registry markets
        snap = reg.snapshot()
        markets = []
        for k in ("active","watch","recovery","disabled"):
            items = snap.get(k) or []
            for it in items:
                m = it.get("market") if isinstance(it, dict) else it
                if m:
                    markets.append(str(m))
        markets = sorted(set(markets))

    markets = sorted(set(m.upper() for m in markets))

    # compute since_ts per market: max(window_since, baseline, market active_since_ts)
    snap = reg.snapshot()
    active_since_map: Dict[str, float] = {}
    for it in (snap.get("active") or []) + (snap.get("recovery") or []):
        if isinstance(it, dict):
            m = str(it.get("market") or "").upper()
            ts0 = it.get("active_since_ts") or it.get("pnl_since_ts") or it.get("since_ts")
            try:
                tsf = float(ts0) if ts0 is not None else 0.0
            except (TypeError, ValueError):
                logger.warning("system_router.pnl_markets L1462 except", exc_info=True)
                tsf = 0.0
            if m:
                active_since_map[m] = tsf

    # Pull ledger tail and aggregate
    since_global = max(window_since, base_ts)
    records = system.ledger.tail_records(since_global, tail_lines) if hasattr(system.ledger, "tail_records") else system.ledger.tail(min(tail_lines, 2000))
    aggs = ledger_pnl.aggregate_fill_pnl(records, since_ts=since_global, until_ts=now, markets=markets)

    rows = []
    for m in markets:
        ctx = system.coordinator.contexts.get(m) if hasattr(system, "coordinator") else None
        allocated = float(getattr(ctx, "allocated_capital", 0.0) or 0.0) if ctx else 0.0
        cash = float(getattr(ctx, "usable_capital", 0.0) or 0.0) if ctx else 0.0
        qty = 0.0
        entry_price = 0.0  # fallback for equity calc
        if ctx and getattr(ctx, "position", None):
            try:
                pos = ctx.position
                if isinstance(pos, dict):
                    qty = float(pos.get("qty") or 0.0)
                    entry_price = float(pos.get("entry") or 0.0)
                else:
                    qty = float(getattr(pos, "qty", 0.0) or 0.0)
                    entry_price = float(getattr(pos, "entry", 0.0) or 0.0)
            except (AttributeError, TypeError, ValueError):
                logger.warning("system_router.pnl_markets L1488 except", exc_info=True)
                qty = 0.0
        # price store
        price = 0.0
        try:
            price = float(system.price_store.get_price(m) or 0.0)  # if system exposes
        except (AttributeError, TypeError, ValueError):
            try:
                from app.core.hyper_price_store import price_store
                price = float(price_store.get_price(m) or 0.0)
            except (AttributeError, TypeError, ValueError):
                logger.warning("system_router.pnl_markets L1498 except", exc_info=True)
                price = 0.0
        
        # [FIX] If price is missing, fall back to entry_price (average entry price)
        if price <= 0 and entry_price > 0:
            price = entry_price

        equity = cash + qty * price
        agg = aggs.get(m)
        trade_n = int(agg.trade_n) if agg else 0
        net_cash = float(agg.net_cash_usdt) if agg else 0.0

        # baseline for this market: max(global baseline, market active_since_ts)
        m_since = max(since_global, float(active_since_map.get(m, 0.0) or 0.0))
        # Note: we already filtered records by since_global; this m_since is informational.
        pnl = equity - allocated
        pnl_pct = (pnl / allocated * 100.0) if allocated > 0 else None

        if trade_n < int(min_trades):
            # still include if it has position or allocated > 0 (so "appears and disappears with coins")
            if (qty <= 0 and allocated <= 0):
                continue

        rows.append({
            "market": m,
            "state": reg.get_state(m).value if hasattr(reg, "get_state") else "",
            "strategy": (getattr(ctx, "strategy", None) or getattr(ctx, "strategy_name", None) or ""),
            "allocated_usdt": allocated,
            "equity_usdt": equity,
            "pnl_usdt": pnl,
            "pnl_pct": pnl_pct,
            "pos_qty": qty,
            "trade_n": trade_n,
            "net_cash_usdt": net_cash,
            "since_ts": m_since,
        })

    # sort worst first (most negative)
    rows.sort(key=lambda r: (r.get("pnl_usdt", 0.0)))
    return {"ok": True, "baseline_ts": base_ts if since_reset else None, "rows": rows}

@router.get("/pnl/strategies")
def pnl_strategies(
    request: Request,
    hours: float = Query(24.0, ge=0.0, le=168.0),
    min_trades: int = Query(1, ge=0, le=100000),
    since_reset: int = Query(1, ge=0, le=1),
    scope: str = Query("open"),
    tail_lines: int = Query(20000, ge=1000, le=200000),
) -> Dict[str, Any]:
    rep = pnl_markets(request, hours=hours, min_trades=min_trades, since_reset=since_reset, scope=scope, tail_lines=tail_lines)
    if not rep.get("ok"):
        return rep
    rows = rep.get("rows") or []
    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        k = str(r.get("strategy") or "UNKNOWN").upper()
        g = groups.get(k)
        if g is None:
            g = {"strategy": k, "markets": 0, "allocated_usdt": 0.0, "equity_usdt": 0.0, "pnl_usdt": 0.0}
            groups[k] = g
        g["markets"] += 1
        g["allocated_usdt"] += float(r.get("allocated_usdt") or 0.0)
        g["equity_usdt"] += float(r.get("equity_usdt") or 0.0)
        g["pnl_usdt"] += float(r.get("pnl_usdt") or 0.0)
    out = []
    for g in groups.values():
        denom = g["allocated_usdt"]
        g["pnl_pct"] = (g["pnl_usdt"] / denom * 100.0) if denom > 0 else None
        out.append(g)
    out.sort(key=lambda x: x.get("pnl_usdt", 0.0))
    return {"ok": True, "baseline_ts": rep.get("baseline_ts"), "rows": out}


@router.get(
    "/autopilot-stats",
    summary="Autopilot promotion stats",
    description="Per-strategy promotion success/failure stats (correlated with FILL_SELL PnL)",
)
def autopilot_stats(
    request: Request,
    hours: float = Query(168.0, ge=1.0, le=720.0),
    pnl_window_hours: float = Query(168.0, ge=1.0, le=720.0),
) -> Dict[str, Any]:
    """Return promotion stats per strategy: total, successful, failed, avg_pnl_after_promotion."""
    try:
        from app.manager.autopilot_tracker import autopilot_tracker
        system = request.app.state.system
        ledger_path = getattr(system.ledger, "path", getattr(system.ledger, "_path", None))
        stats = autopilot_tracker.get_strategy_promotion_stats(
            hours=hours,
            pnl_window_hours=pnl_window_hours,
            ledger_path=ledger_path,
        )
        return {"ok": True, "strategies": stats}
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("system_router.autopilot_stats L1593: %s", e)
        return {"ok": False, "error": str(e), "strategies": {}}


# ============================================================
# Delisting Check
# ============================================================

@router.get(
    "/delisting-check",
    summary="Check delisting markets",
    responses={
        200: {"description": "Delisting status of holdings"},
    },
)
def check_delisting_markets(request: Request):
    """
    Check held coins that are scheduled for delisting.

    Returns:
    - delisting_markets: list of markets scheduled for delisting
    - holdings_at_risk: markets that are held and scheduled for delisting

    Note: Checked via the Bybit delisting API.
    """
    return {
        "ok": True,
        "delisting_count": 0,
        "delisting_markets": {},
        "holdings_at_risk": [],
        "risk_count": 0,
        "note": "Exchange delisting check",
    }


@router.post("/auto-liquidate-delisting")
def set_auto_liquidate_delisting(
    request: Request,
    enabled: bool = Query(..., description="Enable auto liquidation of delisting markets")
) -> Dict[str, Any]:
    """Set the auto-liquidation option for markets scheduled for delisting."""
    import os
    os.environ["OMA_AUTO_LIQUIDATE_DELISTING"] = "1" if enabled else "0"
    return {"ok": True, "auto_liquidate_delisting": enabled}


@router.get("/auto-liquidate-delisting")
def get_auto_liquidate_delisting(request: Request) -> Dict[str, Any]:
    """Get the auto-liquidation option for markets scheduled for delisting."""
    from app.core.constants import env_bool
    return {"ok": True, "enabled": env_bool("OMA_AUTO_LIQUIDATE_DELISTING", default=False)}


@router.get(
    "/market-status-check",
    summary="Check market status changes",
    responses={
        200: {"description": "New listings, delisting alerts, preview markets"},
    },
)
def check_market_status_changes(request: Request):
    """
    Detect market status changes (new listings, scheduled delisting, pending listing).

    Returns:
    - new_listings: newly listed markets (PREVIEW → ACTIVE)
    - delisting_alerts: markets changed to scheduled-for-delisting
    - preview_markets: markets currently pending listing
    """
    from app.manager.market_status_monitor import check_market_status_changes
    
    system = request.app.state.system
    
    try:
        # Current active market list
        active_markets = set(system.oma_registry.list_active())
        
        result = check_market_status_changes(active_markets=active_markets)
        
        return {
            "ok": True,
            **result,
            "new_listings_count": len(result.get("new_listings", [])),
            "delisting_alerts_count": len(result.get("delisting_alerts", [])),
            "preview_count": len(result.get("preview_markets", [])),
        }
    
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("system_router.check_market_status_changes L1680: %s", e)
        return {"ok": False, "error": str(e)}


# ============================================================
# Sector Map API (Smart Allocation)
# ============================================================

@router.get(
    "/sector-map",
    summary="Get sector map",
    responses={200: {"description": "Current sector mapping info"}},
)
def get_sector_map(request: Request):
    """Get the sector mapping info used by Smart Allocation."""
    import json
    from pathlib import Path
    
    sector_file = Path(__file__).parent.parent / "data" / "sector_map.json"
    
    try:
        if sector_file.exists():
            with open(sector_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"sectors": {}, "default_sector": "OTHERS", "default_cap": 0.40}
        
        # Build a flat coin → sector map
        coin_to_sector = {}
        sector_caps = {}
        for sector_id, sector_info in data.get("sectors", {}).items():
            sector_caps[sector_id] = sector_info.get("cap", 0.40)
            for coin in sector_info.get("coins", []):
                coin_to_sector[coin] = sector_id

        return {
            "ok": True,
            "sectors": data.get("sectors", {}),
            "coin_to_sector": coin_to_sector,
            "sector_caps": sector_caps,
            "default_sector": data.get("default_sector", "OTHERS"),
            "default_cap": data.get("default_cap", 0.40),
        }
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.get_sector_map L1723: %s", e)
        return {"ok": False, "error": str(e)}


@router.post(
    "/sector-map",
    summary="Save sector map",
    responses={200: {"description": "Sector map saved"}},
)
def save_sector_map(request: Request, data: Dict[str, Any]):
    """Save the sector mapping info used by Smart Allocation."""
    import json
    from pathlib import Path
    
    sector_file = Path(__file__).parent.parent / "data" / "sector_map.json"
    
    try:
        from app.core.io_utils import safe_write_json
        safe_write_json(str(sector_file), data)

        # Apply to HyperSystem
        system = request.app.state.system

        # Build a flat coin → sector map
        coin_to_sector = {}
        sector_caps = {}
        for sector_id, sector_info in data.get("sectors", {}).items():
            sector_caps[sector_id] = sector_info.get("cap", 0.40)
            for coin in sector_info.get("coins", []):
                coin_to_sector[coin] = sector_id
        
        system.smart_alloc_sector_map = coin_to_sector
        system.smart_alloc_sector_caps = sector_caps
        system.smart_alloc_sector_default_cap = data.get("default_cap", 0.40)
        
        return {"ok": True, "message": "Sector map saved", "sectors_count": len(data.get("sectors", {}))}
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.save_sector_map L1759: %s", e)
        return {"ok": False, "error": str(e)}


@router.post(
    "/sector-map/coin",
    summary="Set coin sector",
    responses={200: {"description": "Coin sector set"}},
)
def set_coin_sector(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    sector: str = Query(..., description="Sector ID (e.g., L1, DEFI, MEME)"),
):
    """Set the sector of an individual coin."""
    import json
    from pathlib import Path
    
    sector_file = Path(__file__).parent.parent / "data" / "sector_map.json"
    
    try:
        # Load existing data
        if sector_file.exists():
            with open(sector_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"sectors": {}, "default_sector": "OTHERS", "default_cap": 0.40}

        # Remove the coin from its existing sector
        for s_id, s_info in data.get("sectors", {}).items():
            coins = s_info.get("coins", [])
            if market in coins:
                coins.remove(market)

        # Add the coin to the new sector
        if sector not in data["sectors"]:
            data["sectors"][sector] = {"name": sector, "cap": 0.20, "coins": []}

        if market not in data["sectors"][sector].get("coins", []):
            data["sectors"][sector].setdefault("coins", []).append(market)

        # Save
        from app.core.io_utils import safe_write_json
        safe_write_json(str(sector_file), data)

        # Apply to HyperSystem
        system = request.app.state.system
        if hasattr(system, "smart_alloc_sector_map"):
            system.smart_alloc_sector_map[market] = sector
        
        return {"ok": True, "market": market, "sector": sector}
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.set_coin_sector L1810: %s", e)
        return {"ok": False, "error": str(e)}


# ============================================================
# Fear & Greed Index API
# ============================================================

@router.get("/fear-greed", summary="Get Fear & Greed Index", description="Get the current market sentiment index and budget multiplier")
def get_fear_greed(request: Request) -> Dict[str, Any]:
    """Return the Fear & Greed Index information.

    Returns:
        value: 0-100 (0=extreme fear, 100=extreme greed)
        level: EXTREME_FEAR, FEAR, NEUTRAL, GREED, EXTREME_GREED
        budget_mult: budget multiplier (contrarian: fear→high, greed→low)
    """
    try:
        from app.core.fear_greed import get_fear_greed_index
        fg = get_fear_greed_index()
        return {"ok": True, **fg.to_dict()}
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("system_router.get_fear_greed L1831: %s", e)
        return {"ok": False, "error": str(e)}


@router.post("/fear-greed/refresh", summary="Refresh Fear & Greed Index", description="Fetch the latest data ignoring the cache")
def refresh_fear_greed(request: Request) -> Dict[str, Any]:
    """Force-refresh the Fear & Greed Index."""
    try:
        from app.core.fear_greed import get_fear_greed_index
        fg = get_fear_greed_index()
        info = fg.get_index(force_refresh=True)
        return {
            "ok": True,
            "value": info.value,
            "level": info.level.value,
            "classification": info.classification,
            "budget_mult": info.budget_mult,
            "cached": info.cached,
        }
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("system_router.refresh_fear_greed L1850: %s", e)
        return {"ok": False, "error": str(e)}


# ============================================================
# Daily PnL (trading journal) API
# [CREATED 2026-01-23]
# ============================================================

@router.get("/daily-pnl/today", summary="Get today's PnL", description="Summary of today's trading PnL")
def get_daily_pnl_today(request: Request) -> Dict[str, Any]:
    """Return today's PnL report."""
    try:
        from app.manager.daily_pnl import get_daily_pnl_manager
        system = request.app.state.system
        
        manager = get_daily_pnl_manager()
        # Only query records since today's midnight
        from datetime import datetime
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        records = system.ledger.tail_records(since_ts=today_start)
        report = manager.aggregate_from_ledger(records)
        
        return {"ok": True, "report": report.to_dict()}
    except (OSError, AttributeError, TypeError, ValueError, OverflowError) as e:
        logger.warning("system_router.get_daily_pnl_today L1874: %s", e)
        return {"ok": False, "error": str(e)}


@router.get("/daily-pnl/summary", summary="Daily PnL summary", description="PnL summary for the last N days")
def get_daily_pnl_summary(request: Request, days: int = 7) -> Dict[str, Any]:
    """Return the PnL summary for the last N days."""
    try:
        from app.manager.daily_pnl import get_daily_pnl_manager
        
        manager = get_daily_pnl_manager()
        summary = manager.get_summary(days=days)
        
        return {"ok": True, **summary}
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("system_router.get_daily_pnl_summary L1888: %s", e)
        return {"ok": False, "error": str(e)}


@router.get("/daily-pnl/list", summary="Daily PnL list", description="List of saved daily PnL entries")
def get_daily_pnl_list(request: Request, limit: int = 30) -> Dict[str, Any]:
    """Return the list of saved daily PnL dates."""
    try:
        from app.manager.daily_pnl import get_daily_pnl_manager
        
        manager = get_daily_pnl_manager()
        dates = manager.list_dates(limit=limit)
        
        return {"ok": True, "dates": dates, "count": len(dates)}
    except (AttributeError, TypeError) as e:
        logger.warning("system_router.get_daily_pnl_list L1902: %s", e)
        return {"ok": False, "error": str(e)}


@router.get("/daily-pnl/{date}", summary="Get PnL for a specific date", description="Trading PnL for a specific date")
def get_daily_pnl_date(request: Request, date: str) -> Dict[str, Any]:
    """Return the PnL report for a specific date. (format: 2026-01-23)"""
    try:
        from app.manager.daily_pnl import get_daily_pnl_manager
        
        manager = get_daily_pnl_manager()
        report = manager.load_report(date)
        
        if report is None:
            return {"ok": False, "error": f"No report for {date}"}
        
        return {"ok": True, "report": report.to_dict()}
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.get_daily_pnl_date L1919: %s", e)
        return {"ok": False, "error": str(e)}


@router.post("/daily-pnl/snapshot", summary="Save today's snapshot", description="Save today's PnL to a file")
def save_daily_pnl_snapshot(request: Request) -> Dict[str, Any]:
    """Save today's PnL snapshot."""
    try:
        from app.manager.daily_pnl import get_daily_pnl_manager
        from datetime import datetime
        system = request.app.state.system
        
        manager = get_daily_pnl_manager()
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        records = system.ledger.tail_records(since_ts=today_start)
        report = manager.snapshot_today(records)
        
        return {"ok": True, "report": report.to_dict()}
    except (OSError, AttributeError, TypeError, ValueError, OverflowError) as e:
        logger.warning("system_router.save_daily_pnl_snapshot L1937: %s", e)
        return {"ok": False, "error": str(e)}


# ============================================================
# Cross-exchange price comparison (reserved for future Bithumb integration)
# ============================================================

# ── Shared httpx client (TCP connection pooling) ──
# Creating a new httpx.Client() per request was the cause of the 10054 error
import httpx as _httpx
from app.core.constants import BYBIT_MARKET_KLINE, BYBIT_MARKET_TICKERS, parse_bybit_list

_bybit_ui_client: Optional[_httpx.Client] = None


def _get_bybit_ui_client() -> _httpx.Client:
    """Shared httpx client for the Bybit UI proxy (lazy init, connection pooling)."""
    global _bybit_ui_client
    if _bybit_ui_client is None:
        _bybit_ui_client = _httpx.Client(
            timeout=10.0,
            limits=_httpx.Limits(
                max_connections=4,
                max_keepalive_connections=2,
                keepalive_expiry=30,
            ),
        )
    return _bybit_ui_client


@router.get("/binance-klines", summary="Bybit candle data", description="Bybit V5 klines proxy (CORS bypass)")
def binance_klines(symbol: str = "BTCUSDT", interval: str = "15", limit: int = 100) -> Dict[str, Any]:
    """Bybit V5 kline API proxy.

    Args:
        symbol: trading pair symbol (e.g., BTCUSDT)
        interval: candle interval (1,3,5,15,30,60,120,240,360,720,D,W,M)
        limit: number of candles (max 1000)

    Returns:
        OHLCV candle data
    """
    try:
        # Map common Binance-style intervals to Bybit V5 format
        interval_map = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
                        "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
                        "1d": "D", "1w": "W", "1M": "M"}
        bybit_interval = interval_map.get(interval, interval)

        client = _get_bybit_ui_client()
        resp = client.get(BYBIT_MARKET_KLINE, params={
            "category": "spot", "symbol": symbol, "interval": bybit_interval, "limit": min(limit, 1000)
        })
        resp.raise_for_status()
        raw = parse_bybit_list(resp.json())

        # Convert to Binance-compatible kline format for frontend compatibility
        klines = []
        for k in raw:
            if isinstance(k, (list, tuple)) and len(k) >= 6:
                klines.append([int(k[0]), str(k[1]), str(k[2]), str(k[3]), str(k[4]), str(k[5]),
                               int(k[0]), str(float(k[4]) * float(k[5])), 0, "0", "0", "0"])

        return {"ok": True, "symbol": symbol, "interval": interval, "klines": klines}
    except Exception as e:
        logger.warning("system_router.binance_klines: %s", e)
        return {"ok": False, "error": str(e)}


@router.get("/binance-tickers", summary="Bybit all prices", description="Bybit V5 all ticker prices proxy (CORS bypass)")
def binance_tickers() -> Dict[str, Any]:
    """Bybit V5 all spot ticker prices proxy."""
    try:
        client = _get_bybit_ui_client()
        resp = client.get(BYBIT_MARKET_TICKERS, params={"category": "spot"})
        resp.raise_for_status()
        raw = parse_bybit_list(resp.json())

        tickers = []
        for t in raw:
            if isinstance(t, dict):
                tickers.append({
                    "symbol": t.get("symbol", ""),
                    "price": str(t.get("lastPrice", "0")),
                })

        return {"ok": True, "tickers": tickers}
    except Exception as e:
        logger.warning("system_router.binance_tickers: %s", e)
        return {"ok": False, "error": str(e)}


# exchange-rate endpoint removed (Bybit-only, Bybit USDT only)


# ============================================================
# Admin Authentication
# ============================================================

import hashlib
import secrets

# Session token store (in-memory, reset on server restart)
_admin_sessions: dict = {}

def _get_admin_password() -> str:
    """Get the admin password from the environment variable.

    Safe default for public deployment: if ADMIN_PASSWORD is unset, return an empty string →
    disable admin login entirely (never embed a guessable default password).
    """
    import os
    return os.getenv("ADMIN_PASSWORD", "")

def _verify_admin_token(token: str) -> bool:
    """Check whether the admin token is valid."""
    if not token:
        return False
    session = _admin_sessions.get(token)
    if not session:
        return False
    # 24-hour expiry
    import time
    if time.time() - session.get("created", 0) > 86400:
        del _admin_sessions[token]
        return False
    return True


@router.post("/admin/login", summary="Admin login", description="Log in with the admin password")
def admin_login(password: str = Query(...)) -> Dict[str, Any]:
    """Verify the admin password and issue a session token."""
    import time

    admin_pw = _get_admin_password()
    if not admin_pw:
        return {"ok": False, "error": "ADMIN_PASSWORD is not set, so admin features are disabled. (Set ADMIN_PASSWORD in .env)"}
    if password != admin_pw:
        return {"ok": False, "error": "Password does not match."}

    # Generate token
    token = secrets.token_hex(32)
    _admin_sessions[token] = {"created": time.time()}

    return {"ok": True, "token": token, "message": "Login successful"}


@router.get("/admin/verify", summary="Verify admin token", description="Check whether the admin token is valid")
def admin_verify(token: str = Query("")) -> Dict[str, Any]:
    """Check whether the admin token is valid."""
    if _verify_admin_token(token):
        return {"ok": True, "valid": True}
    return {"ok": True, "valid": False}


@router.post("/admin/logout", summary="Admin logout", description="End the admin session")
def admin_logout(token: str = Query("")) -> Dict[str, Any]:
    """End the admin session."""
    if token in _admin_sessions:
        del _admin_sessions[token]
    return {"ok": True, "message": "Logout complete"}


# ============================================================
# Exchange API Settings
# ============================================================

@router.get("/exchange-api/status", summary="Exchange API status", description="Check the Exchange API key configuration status")
def exchange_api_status() -> Dict[str, Any]:
    """Check the Exchange API key configuration status."""
    import os
    access_key = os.getenv("BYBIT_API_KEY", "")
    secret_key = os.getenv("BYBIT_API_SECRET", "")
    
    has_keys = bool(access_key and secret_key)
    # [2026-04-09 security] Expose only the first 2 chars (previously first 4 + last 4 → too much exposed)
    masked_access = access_key[:2] + "****" if len(access_key) > 4 else ("****" if access_key else "")
    
    return {
        "ok": True,
        "has_keys": has_keys,
        "access_key_masked": masked_access,
    }


@router.get("/exchange-api/detect-ip", summary="Detect server public IP", description="Detect the server's public IP address")
def detect_public_ip() -> Dict[str, Any]:
    """Detect the server's public IP address."""
    import requests

    try:
        # Try multiple IP detection services
        services = [
            "https://api.ipify.org?format=json",
            "https://httpbin.org/ip",
            "https://api.myip.com",
        ]
        
        for url in services:
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    ip = data.get("ip") or data.get("origin") or ""
                    if ip:
                        return {"ok": True, "ip": ip.split(",")[0].strip()}
            except Exception:
                report_suppressed_exception(__name__, 'Try multiple IP detection services; except-> continue')
                continue

        return {"ok": False, "error": "IP detection failed"}
    except Exception as e:
        logger.warning("system_router.detect_public_ip L2124: %s", e)
        return {"ok": False, "error": str(e)}


@router.post("/exchange-api/test", summary="Test Exchange API connection", description="Test the Exchange connection with the provided API keys")
def test_exchange_api(access_key: str = Body(..., embed=True), secret_key: str = Body(..., embed=True)) -> Dict[str, Any]:
    """Test the Exchange connection with the provided API keys."""
    try:
        from app.integrations.bybit_trade import BybitTradeClient as BybitTradeClient

        client = BybitTradeClient(api_key=access_key, api_secret=secret_key)
        accounts = client.get_accounts()
        
        if accounts is None:
            return {"ok": False, "error": "No API response"}

        # Balance summary
        quote_balance = 0
        coin_count = 0
        for acc in accounts:
            if acc.get("currency") == Q.symbol:
                quote_balance = float(acc.get("balance", 0))
            else:
                coin_count += 1
        
        return {
            "ok": True,
            "message": "Connection successful",
            "quote_balance": quote_balance,
            "coin_count": coin_count,
        }
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.test_exchange_api L2155: %s", e)
        error_msg = str(e)
        if "no_authorization_ip" in error_msg.lower() or "허용되지 않은" in error_msg:
            return {"ok": False, "error": "IP not allowed. Register the server IP on Bybit."}
        return {"ok": False, "error": error_msg}


@router.post("/exchange-api/save", summary="Save Exchange API keys", description="Save API keys to the .env file (admin auth required)")
def save_exchange_api(access_key: str = Body(..., embed=True), secret_key: str = Body(..., embed=True), admin_token: str = Body("", embed=True)) -> Dict[str, Any]:
    """Save the API keys to the .env file. (admin auth required)"""
    if not _verify_admin_token(admin_token):
        return {"ok": False, "error": "Admin authentication required."}
    import os
    from pathlib import Path

    try:
        env_path = Path(".env")

        # Read the existing .env file
        env_lines = []
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                env_lines = f.readlines()

        # Update BYBIT_API_KEY, BYBIT_API_SECRET
        new_lines = []
        access_found = False
        secret_found = False

        for line in env_lines:
            stripped = line.strip()
            if stripped.startswith("BYBIT_API_KEY="):
                new_lines.append(f"BYBIT_API_KEY={access_key}\n")
                access_found = True
            elif stripped.startswith("BYBIT_API_SECRET="):
                new_lines.append(f"BYBIT_API_SECRET={secret_key}\n")
                secret_found = True
            else:
                new_lines.append(line if line.endswith("\n") else line + "\n")

        # Add if missing
        if not access_found:
            new_lines.append(f"BYBIT_API_KEY={access_key}\n")
        if not secret_found:
            new_lines.append(f"BYBIT_API_SECRET={secret_key}\n")

        # Save the file
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        # Update environment variables (current process)
        os.environ["BYBIT_API_KEY"] = access_key
        os.environ["BYBIT_API_SECRET"] = secret_key

        return {"ok": True, "message": "Saved to the .env file. Fully applied after a server restart."}
    except (OSError, KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.save_exchange_api L2210: %s", e)
        return {"ok": False, "error": str(e)}


# ============================================================
# Telegram Settings
# ============================================================

@router.get("/telegram/status", summary="Telegram settings status", description="Check the Telegram notification settings status")
def telegram_status() -> Dict[str, Any]:
    """Check the Telegram settings status."""
    import os
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    
    has_config = bool(token and chat_id)
    masked_token = token[:10] + "****" if len(token) > 10 else ""
    
    return {
        "ok": True,
        "has_config": has_config,
        "token_masked": masked_token,
        "chat_id": chat_id if has_config else "",
    }


@router.post("/telegram/test", summary="Telegram test message", description="Send a test message with the provided settings")
def test_telegram(token: str = Query(...), chat_id: str = Query(...)) -> Dict[str, Any]:
    """Send a Telegram test message with the provided settings."""
    import requests
    from datetime import datetime

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        message = f"🤖 Autocoin OS test message\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n✅ Connection successful!"
        
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message},
            timeout=10
        )
        
        if resp.status_code == 200:
            return {"ok": True, "message": "Test message sent successfully"}
        else:
            data = resp.json()
            error_desc = data.get("description", "Unknown error")
            return {"ok": False, "error": error_desc}
    except Exception as e:
        logger.warning("system_router.test_telegram L2258: %s", e)
        return {"ok": False, "error": str(e)}


@router.post("/telegram/save", summary="Save Telegram settings", description="Save Telegram settings to the .env file (admin auth required)")
def save_telegram(token: str = Query(...), chat_id: str = Query(...), admin_token: str = Query("")) -> Dict[str, Any]:
    """Save the Telegram settings to the .env file. (admin auth required)"""
    if not _verify_admin_token(admin_token):
        return {"ok": False, "error": "Admin authentication required."}
    import os
    from pathlib import Path
    
    try:
        env_path = Path(".env")
        
        env_lines = []
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                env_lines = f.readlines()
        
        new_lines = []
        token_found = False
        chat_found = False
        
        for line in env_lines:
            stripped = line.strip()
            if stripped.startswith("TELEGRAM_TOKEN="):
                new_lines.append(f"TELEGRAM_TOKEN={token}\n")
                token_found = True
            elif stripped.startswith("TELEGRAM_CHAT_ID="):
                new_lines.append(f"TELEGRAM_CHAT_ID={chat_id}\n")
                chat_found = True
            else:
                new_lines.append(line if line.endswith("\n") else line + "\n")
        
        if not token_found:
            new_lines.append(f"TELEGRAM_TOKEN={token}\n")
        if not chat_found:
            new_lines.append(f"TELEGRAM_CHAT_ID={chat_id}\n")
        
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        
        os.environ["TELEGRAM_TOKEN"] = token
        os.environ["TELEGRAM_CHAT_ID"] = chat_id

        return {"ok": True, "message": "Saved to the .env file."}
    except (OSError, KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.save_telegram L2305: %s", e)
        return {"ok": False, "error": str(e)}


# ── [2026-06-01] Alert-type toggles (longhold/drawdown/exit_streak/daily/harpoon) ──
#   No changes to send sites: since the system attributes read at boot + the env-read
#   sites are checked as-is, updating the attribute + os.environ at runtime applies
#   immediately. Also update .env to persist across restarts.
_ALERT_FLAGS = {
    # ui_key: (env_name, system_attr or None, default)
    "longhold": ("OMA_LONGHOLD_ALERTS", "longhold_alerts_enabled", True),
    "drawdown": ("OMA_DRAWDOWN_NOTIFY", "drawdown_notify", True),
    "exit_profit_streak": ("OMA_EXIT_PROFIT_GUARD_STREAK_NOTIFY", "exit_profit_guard_streak_notify", False),
    "daily": ("DAILY_REPORT_TELEGRAM_ENABLED", None, True),
    "harpoon": ("OMA_HARPOON_ALERTS", None, True),
}


def _alert_truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _persist_env_keys(updates: Dict[str, str]) -> None:
    """Update/add multiple keys in .env (non-secret, alert flags)."""
    from pathlib import Path
    p = Path(".env")
    lines = []
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
    found = set()
    out = []
    for line in lines:
        key = line.strip().split("=", 1)[0] if "=" in line else ""
        if key in updates:
            out.append(f"{key}={updates[key]}\n")
            found.add(key)
        else:
            out.append(line if line.endswith("\n") else line + "\n")
    for k, v in updates.items():
        if k not in found:
            out.append(f"{k}={v}\n")
    with open(p, "w", encoding="utf-8") as f:
        f.writelines(out)


@router.get("/alerts", summary="Alert-type on/off status")
def get_alerts(request: Request) -> Dict[str, Any]:
    system = request.app.state.system
    out = {}
    for k, (env_name, attr, default) in _ALERT_FLAGS.items():
        if attr and hasattr(system, attr):
            out[k] = bool(getattr(system, attr))
        else:
            raw = os.getenv(env_name)
            out[k] = _alert_truthy(raw) if raw is not None else default
    return {"ok": True, "alerts": out}


@router.post("/alerts", summary="Set alert-type on/off (runtime + .env)")
def set_alerts(request: Request, longhold: str = Query(""), drawdown: str = Query(""),
               exit_profit_streak: str = Query(""), daily: str = Query(""), harpoon: str = Query("")) -> Dict[str, Any]:
    system = request.app.state.system
    incoming = {"longhold": longhold, "drawdown": drawdown, "exit_profit_streak": exit_profit_streak, "daily": daily, "harpoon": harpoon}
    env_updates: Dict[str, str] = {}
    for k, raw in incoming.items():
        if raw == "":   # unspecified = no change
            continue
        val = _alert_truthy(raw)
        env_name, attr, _ = _ALERT_FLAGS[k]
        os.environ[env_name] = "1" if val else "0"
        env_updates[env_name] = "1" if val else "0"
        if attr:
            try:
                setattr(system, attr, val)   # apply immediately at runtime (send sites check this attribute)
            except (AttributeError, TypeError):
                logger.warning("set_alerts: setattr %s failed", attr, exc_info=True)
    if env_updates:
        try:
            _persist_env_keys(env_updates)   # persist across restarts
        except OSError as e:
            return {"ok": False, "error": str(e)}
    return get_alerts(request)


# ============================================================
# Server Restart / Stop
# ============================================================

@router.post("/restart", summary="Restart server", description="Restart the server (requires run.ps1)")
async def restart_server(
    request: Request,
    delay_sec: int = Query(15, ge=1, le=60, description="Cleanup wait time (seconds)"),
    cleanup: int = Query(1, ge=0, le=1, description="Whether to run cleanup (1/0)")
) -> Dict[str, Any]:
    """Request a server restart.

    - delay_sec: cleanup wait time (default 15s, max 60s)
    - run.ps1 auto-restarts when it detects exit code 42.
    - Restart after performing a graceful shutdown
    """
    import asyncio
    import os
    import time
    
    system = request.app.state.system
    do_cleanup = bool(cleanup)
    
    async def graceful_shutdown():
        try:
            # 1) Stop the engine
            if hasattr(system, 'coordinator') and hasattr(system.coordinator, 'engine'):
                system.coordinator.engine.status.stop()
            # 1.5) Disable autopilot (prevent auto-promotion during cleanup)
            if hasattr(system, "autopilot_enabled"):
                system.autopilot_enabled = False

            # 2) Cleanup option
            start_ts = time.time()
            if do_cleanup:
                try:
                    checked = 0
                    cancelled = 0
                    errors = 0
                    order_fsm = getattr(system, "order_fsm", None)
                    contexts = {}
                    if hasattr(system, "coordinator") and hasattr(system.coordinator, "contexts"):
                        contexts = system.coordinator.contexts or {}
                    for market, ctx in (contexts.items() if isinstance(contexts, dict) else []):
                        if str(market).startswith("_"):
                            continue
                        checked += 1
                        if order_fsm:
                            try:
                                res = order_fsm.force_cancel_pending(ctx=ctx, market=market, reason="shutdown_cleanup")
                                if res.get("cancelled"):
                                    cancelled += 1
                            except (KeyError, AttributeError, TypeError):
                                logger.warning("system_router.save_telegram L2361 except", exc_info=True)
                                errors += 1
                    try:
                        if hasattr(system, "reconcile"):
                            system.reconcile(reason="shutdown_cleanup")
                    except (KeyError, AttributeError, TypeError):
                        logger.warning("system_router.save_telegram L2366 except", exc_info=True)
                        errors += 1
                    try:
                        system.ledger.append(
                            "SERVER_SHUTDOWN_CLEANUP",
                            checked=checked,
                            cancelled=cancelled,
                            errors=errors,
                            reason="restart"
                        )
                    except (AttributeError, TypeError):
                        report_suppressed_exception(__name__, '2) Cleanup option')
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    logger.warning("system_router.save_telegram L2378: %s", e)
                    print(f"[RESTART] Cleanup error: {e}")

            # 3) Cleanup wait (the tick loop keeps running and clears pending orders)
            elapsed = time.time() - start_ts
            remain = max(0.0, float(delay_sec) - elapsed)
            if remain > 0:
                await asyncio.sleep(remain)

            # 4) Save state
            await system.stop()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("system_router.save_telegram L2389: %s", e)
            print(f"[RESTART] Graceful shutdown error: {e}")
        finally:
            os._exit(42)  # Magic code for restart
    
    # Run in the current event loop to avoid cross-loop cancellation issues.
    asyncio.create_task(graceful_shutdown())
    return {
        "ok": True,
        "message": f"Server will restart in {delay_sec}s... (cleanup={1 if do_cleanup else 0})",
        "delay_sec": delay_sec,
        "cleanup": 1 if do_cleanup else 0
    }


@router.post("/stop", summary="Stop server", description="Stop the server")
async def stop_server(
    request: Request,
    delay_sec: int = Query(15, ge=1, le=60, description="Cleanup wait time (seconds)"),
    cleanup: int = Query(1, ge=0, le=1, description="Whether to run cleanup (1/0)")
) -> Dict[str, Any]:
    """Stop the server completely.

    - delay_sec: cleanup wait time (default 15s, max 60s)
    - Stop after performing a graceful shutdown
    """
    import asyncio
    import os
    import time
    
    system = request.app.state.system
    do_cleanup = bool(cleanup)
    
    async def graceful_shutdown():
        try:
            if hasattr(system, 'coordinator') and hasattr(system.coordinator, 'engine'):
                system.coordinator.engine.status.stop()
            if hasattr(system, "autopilot_enabled"):
                system.autopilot_enabled = False

            start_ts = time.time()
            if do_cleanup:
                try:
                    checked = 0
                    cancelled = 0
                    errors = 0
                    order_fsm = getattr(system, "order_fsm", None)
                    contexts = {}
                    if hasattr(system, "coordinator") and hasattr(system.coordinator, "contexts"):
                        contexts = system.coordinator.contexts or {}
                    for market, ctx in (contexts.items() if isinstance(contexts, dict) else []):
                        if str(market).startswith("_"):
                            continue
                        checked += 1
                        if order_fsm:
                            try:
                                res = order_fsm.force_cancel_pending(ctx=ctx, market=market, reason="shutdown_cleanup")
                                if res.get("cancelled"):
                                    cancelled += 1
                            except (KeyError, AttributeError, TypeError):
                                logger.warning("system_router.save_telegram L2448 except", exc_info=True)
                                errors += 1
                    try:
                        if hasattr(system, "reconcile"):
                            system.reconcile(reason="shutdown_cleanup")
                    except (KeyError, AttributeError, TypeError):
                        logger.warning("system_router.save_telegram L2453 except", exc_info=True)
                        errors += 1
                    try:
                        system.ledger.append(
                            "SERVER_SHUTDOWN_CLEANUP",
                            checked=checked,
                            cancelled=cancelled,
                            errors=errors,
                            reason="stop"
                        )
                    except (AttributeError, TypeError):
                        report_suppressed_exception(__name__, 'system_router fallback')
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    logger.warning("system_router.save_telegram L2465: %s", e)
                    print(f"[STOP] Cleanup error: {e}")

            # Cleanup wait (the tick loop keeps running and clears pending orders)
            elapsed = time.time() - start_ts
            remain = max(0.0, float(delay_sec) - elapsed)
            if remain > 0:
                await asyncio.sleep(remain)

            await system.stop()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("system_router.save_telegram L2475: %s", e)
            print(f"[STOP] Graceful shutdown error: {e}")
        finally:
            os._exit(0)
    
    # Run in the current event loop to avoid cross-loop cancellation issues.
    asyncio.create_task(graceful_shutdown())
    return {
        "ok": True,
        "message": f"Server will stop in {delay_sec}s... (cleanup={1 if do_cleanup else 0})",
        "delay_sec": delay_sec,
        "cleanup": 1 if do_cleanup else 0
    }


# ------------------------------------------------------------
# Ledger Validation
# ------------------------------------------------------------
@router.get("/validate/ledger", summary="Validate ledger", description="Validate Trade Ledger integrity")
def validate_ledger() -> Dict[str, Any]:
    """
    Trade Ledger validation:
    - Check BUY/SELL pairs
    - Detect duplicate trades
    - Validate chronological ordering
    - Validate negative values
    """
    try:
        from app.manager.ledger_validator import validate_ledger as do_validate
        result = do_validate()
        return result
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("system_router.validate_ledger L2506: %s", e)
        return {
            "ok": False,
            "error": str(e),
            "issues": [f"Validation failed: {e}"],
        }


@router.get("/validate/holding-sync", summary="Validate position sync", description="Compare Context vs Exchange balances")
def validate_holding_sync(request: Request) -> Dict[str, Any]:
    """
    Context.position vs Exchange.balance validation:
    - Detect position mismatches for active markets
    - Tolerance: 0.0001
    """
    try:
        from app.manager.ledger_validator import validate_holding_sync as do_validate
        system = request.app.state.system
        result = do_validate(system)
        return result
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("system_router.validate_holding_sync L2526: %s", e)
        return {
            "ok": False,
            "error": str(e),
            "issues": [f"Validation failed: {e}"],
        }


# ============================================================
# Night Mode API
# ============================================================
@router.get("/night-mode", summary="Get Night Mode settings")
def get_night_mode(request: Request):
    system = request.app.state.system
    return {"ok": True, **system.get_night_mode_config()}


@router.patch("/night-mode", summary="Change Night Mode settings")
def patch_night_mode(request: Request, body: Dict[str, Any]):
    """Change Night Mode settings.

    body example::

        {"enabled": true, "start_hour": 2, "end_hour": 9,
         "entry_score_boost_pct": 30, "sl_multiplier": 1.5}
    """
    system = request.app.state.system
    if "enabled" in body:
        system.night_mode_enabled = bool(body["enabled"])
    if "start_hour" in body:
        system.night_mode_start_hour = max(0, min(23, int(body["start_hour"])))
    if "end_hour" in body:
        system.night_mode_end_hour = max(0, min(23, int(body["end_hour"])))
    if "entry_score_boost_pct" in body:
        system.night_mode_entry_score_boost_pct = max(0.0, min(200.0, float(body["entry_score_boost_pct"])))
    if "sl_multiplier" in body:
        system.night_mode_sl_multiplier = max(1.0, min(5.0, float(body["sl_multiplier"])))

    # Save to ui_settings (restored on restart)
    try:
        g = getattr(system, '_ui_guard_overrides', {}) or {}
        g["night_mode_enabled"] = system.night_mode_enabled
        g["night_mode_start_hour"] = system.night_mode_start_hour
        g["night_mode_end_hour"] = system.night_mode_end_hour
        g["night_mode_entry_score_boost_pct"] = system.night_mode_entry_score_boost_pct
        g["night_mode_sl_multiplier"] = system.night_mode_sl_multiplier
        system._ui_guard_overrides = g
        system._save_ui_settings()
    except (KeyError, AttributeError, TypeError):
        report_suppressed_exception(__name__, 'Save to ui_settings (restored on restart)')

    return {"ok": True, **system.get_night_mode_config()}


# ============================================================
# Position Age Monitoring
# ============================================================

_STALE_THRESHOLDS_HOURS: Dict[str, float] = {
    "PINGPONG": 72.0,
    "AUTOLOOP": 96.0,
    "LIGHTNING": 48.0,
    "CONTRARIAN": 168.0,
    "SNIPER": 168.0,
    "LADDER": 336.0,
    "GAZUA": 336.0,
}

_DEFAULT_STALE_HOURS = 120.0


@router.get(
    "/position-ages",
    summary="Position holding-age monitoring",
    description="Return the holding time and long-hold warnings for all active positions.",
)
def position_ages(request: Request) -> Dict[str, Any]:
    system = request.app.state.system
    now = time.time()

    rows: List[Dict[str, Any]] = []

    active_markets: List[str] = []
    try:
        active_markets = list(system.oma_registry.list_active())
        if hasattr(system.oma_registry, "list_recovery"):
            active_markets += list(system.oma_registry.list_recovery())
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("system_router.position_ages L2613: %s", e)
        return {"ok": False, "error": f"registry unavailable: {e}"}

    for market in sorted(set(active_markets)):
        ctx = None
        try:
            ctx = system.coordinator.contexts.get(market)
        except (AttributeError, TypeError):
            report_suppressed_exception(__name__, 'system_router.position_ages except-> continue')
            continue
        if ctx is None:
            continue

        pos = getattr(ctx, "position", None)
        if not pos or not isinstance(pos, dict):
            continue
        pos_qty = float(pos.get("qty") or 0.0)
        if pos_qty <= 0:
            continue

        entry_ts = float(pos.get("entry_ts") or pos.get("ts") or 0)

        strategy = ""
        try:
            strategy = str(getattr(ctx, "selected_strategy", "") or "").strip().upper()
        except (AttributeError, TypeError):
            report_suppressed_exception(__name__, 'system_router.position_ages fallback')
        if not strategy:
            ctrls = getattr(ctx, "controls", None) or {}
            if isinstance(ctrls, dict):
                s_ctrl = ctrls.get("strategy", {})
                strategy = str(s_ctrl.get("mode", "") if isinstance(s_ctrl, dict) else "").strip().upper()

        age_hours = ((now - entry_ts) / 3600.0) if entry_ts > 0 else None

        entry_price = float(pos.get("entry") or 0.0)
        current_pnl_pct: Optional[float] = None
        if entry_price > 0:
            price = 0.0
            try:
                price = float(system.price_store.get_price(market) or 0.0)
            except (AttributeError, TypeError, ValueError):
                try:
                    from app.core.hyper_price_store import price_store
                    price = float(price_store.get_price(market) or 0.0)
                except (AttributeError, TypeError, ValueError):
                    report_suppressed_exception(__name__, 'system_router fallback')
            if price > 0:
                current_pnl_pct = (price - entry_price) / entry_price * 100.0

        threshold = _STALE_THRESHOLDS_HOURS.get(strategy, _DEFAULT_STALE_HOURS)
        is_stale = (age_hours is not None and age_hours > threshold)

        rows.append({
            "market": market,
            "strategy": strategy or "UNKNOWN",
            "entry_ts": entry_ts if entry_ts > 0 else None,
            "age_hours": round(age_hours, 2) if age_hours is not None else None,
            "stale_threshold_hours": threshold,
            "is_stale": is_stale,
            "current_pnl_pct": round(current_pnl_pct, 2) if current_pnl_pct is not None else None,
            "pos_qty": pos_qty,
            "entry_price": entry_price,
        })

    rows.sort(key=lambda r: (r.get("age_hours") or 0.0), reverse=True)
    stale_count = sum(1 for r in rows if r.get("is_stale"))

    return {
        "ok": True,
        "total_positions": len(rows),
        "stale_count": stale_count,
        "rows": rows,
    }


# ============================================================
# Paper Trading Mode — mode switching & status queries
# ============================================================

@router.post("/trading-mode", summary="Switch LIVE/PAPER trading mode")
def switch_trading_mode(request: Request, body: Dict[str, Any] = {}):
    """Switch between LIVE ↔ PAPER mode at runtime.

    PAPER mode: test strategies with simulated fills and no real orders.
    """
    mode = str(body.get("mode", "")).upper()
    if mode not in ("LIVE", "PAPER"):
        return {"ok": False, "error": "mode must be LIVE or PAPER"}

    system = request.app.state.system
    old_mode = str(getattr(system, "trading_mode", "LIVE")).upper()

    if old_mode == mode:
        return {"ok": True, "mode": mode, "changed": False}

    # API keys required when switching to LIVE
    if mode == "LIVE":
        ak = os.environ.get("BYBIT_API_KEY", "")
        sk = os.environ.get("BYBIT_API_SECRET", "")
        if not ak or not sk:
            return {"ok": False, "error": "BYBIT_API_KEY/SECRET required for LIVE mode"}

        from app.integrations.bybit_trade import BybitTradeClient
        from app.manager.order_state_machine import OrderStateMachine

        system.trade_client = BybitTradeClient(
            api_key=ak, api_secret=sk,
            timeout=float(os.getenv("BYBIT_TIMEOUT", "10")),
            category=getattr(system, "bybit_v5_category", "linear"),
        )
        system.order_fsm = OrderStateMachine(client=system.trade_client, ledger=system.ledger)
        system.order_fsm._sell_fill_callbacks.append(system._on_sell_filled)
        system.order_fsm._buy_fill_callbacks.append(system._on_buy_filled)

    elif mode == "PAPER":
        from app.integrations.paper_trade_client import PaperTradeClient
        from app.manager.order_state_machine import OrderStateMachine

        initial = float(os.getenv("DRY_INITIAL_USDT", "1000"))
        system.trade_client = PaperTradeClient(
            initial_usdt=initial,
            fee_rate=float(os.getenv("PAPER_FEE_RATE", "0.001")),
        )
        system.order_fsm = OrderStateMachine(client=system.trade_client, ledger=system.ledger)
        system.order_fsm._sell_fill_callbacks.append(system._on_sell_filled)
        system.order_fsm._buy_fill_callbacks.append(system._on_buy_filled)

    system.trading_mode = mode
    try:
        system.ledger.append("TRADING_MODE_SWITCH", old_mode=old_mode, new_mode=mode)
    except Exception:
        pass

    logger.info("[TradingMode] Switched: %s → %s", old_mode, mode)
    return {"ok": True, "mode": mode, "changed": True, "previous": old_mode}


@router.get("/paper/status", summary="Paper trading status")
def paper_status(request: Request):
    """Get the Paper-mode trading status."""
    system = request.app.state.system
    mode = str(getattr(system, "trading_mode", "LIVE")).upper()

    if mode != "PAPER" or not hasattr(system.trade_client, "get_summary"):
        return {"ok": False, "mode": mode, "error": "Not in PAPER mode"}

    return {"ok": True, **system.trade_client.get_summary()}


@router.post("/paper/reset", summary="Reset paper trading balance")
def paper_reset(request: Request, body: Dict[str, Any] = {}):
    """Reset the Paper balance."""
    system = request.app.state.system
    mode = str(getattr(system, "trading_mode", "LIVE")).upper()

    if mode != "PAPER" or not hasattr(system.trade_client, "reset"):
        return {"ok": False, "mode": mode, "error": "Not in PAPER mode"}

    initial = float(body.get("initial_usdt", 0) or os.getenv("DRY_INITIAL_USDT", "1000"))
    system.trade_client.reset(initial_usdt=initial)
    return {"ok": True, "reset_to": initial, **system.trade_client.get_summary()}


@router.post("/clean-slate", summary="Clean slate (paper): close all positions + wipe trade records")
def clean_slate(request: Request) -> Dict[str, Any]:
    """PAPER-ONLY clean slate for a fresh paper->live (or fresh paper) start:
      1. close every position on every engine (in-process — no external file lock),
      2. wipe all trade journals (each backed up first),
      3. reset the core paper balance (DRY_INITIAL_USDT) and the PnL baseline.
    Refuses in LIVE mode — never touches real positions. Restart afterwards for a clean baseline."""
    system = request.app.state.system
    mode = str(getattr(system, "trading_mode", "LIVE")).upper()
    if mode != "PAPER":
        return {"ok": False, "mode": mode, "error": "Clean Slate is paper-mode only; refusing in LIVE."}

    closed: Dict[str, int] = {}
    journals: Dict[str, int] = {}
    errors: list = []

    # 1. Spot managers (binance_spot is a SpotGazuaManager subclass) — uniform clean_slate()
    for name in ("upbit_gazua_manager", "bithumb_gazua_manager",
                 "bybit_spot_gazua_manager", "binance_spot_gazua_manager"):
        mgr = getattr(system, name, None)
        if mgr is None:
            continue
        try:
            r = mgr.clean_slate()
            closed[name] = int(r.get("closed", 0))
            journals[name] = int(r.get("journal_removed", 0))
        except Exception as exc:  # noqa: BLE001 — best-effort per engine, report and continue
            errors.append(f"{name}: {exc}")

    # 2. FOCUS + Binance futures — close positions, then clear the TradeJournal
    for name in ("focus_manager", "binance_futures_manager"):
        mgr = getattr(system, name, None)
        if mgr is None:
            continue
        try:
            n = 0
            for pos in list(getattr(mgr, "positions", []) or []):
                try:
                    if mgr._close_position(pos, reason="clean_slate"):
                        n += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{name} close {getattr(pos, 'market', '?')}: {exc}")
            legacy = getattr(mgr, "position", None)
            if legacy and getattr(legacy, "market", None):
                try:
                    if mgr._close_position(legacy, reason="clean_slate"):
                        n += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{name} close legacy: {exc}")
            closed[name] = n
            jrnl = getattr(mgr, "_journal", None)
            if jrnl is not None and hasattr(jrnl, "clear"):
                journals[name] = int(jrnl.clear())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")

    # 3. HARPOON — paper: flatten in-memory scalps (it shares the FOCUS journal, already cleared)
    hp = getattr(system, "harpoon_manager", None)
    if hp is not None:
        try:
            closed["harpoon_manager"] = len(getattr(hp, "active_scalps", []) or [])
            if hasattr(hp, "active_scalps"):
                hp.active_scalps = []
            if hasattr(hp, "current_scalp"):
                hp.current_scalp = None
        except Exception as exc:  # noqa: BLE001
            errors.append(f"harpoon_manager: {exc}")

    # 4. Reset core paper balance + PnL baseline (so the fresh start reads clean)
    try:
        tc = getattr(system, "trade_client", None)
        if tc is not None and hasattr(tc, "reset"):
            tc.reset(initial_usdt=float(os.getenv("DRY_INITIAL_USDT", "1000")))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"paper_reset: {exc}")
    try:
        _bp = os.path.join("runtime", "pnl_baseline.json")
        if os.path.exists(_bp):
            os.remove(_bp)  # absent -> re-baselines to current (flat) equity on next read/boot
    except OSError as exc:
        errors.append(f"baseline_reset: {exc}")

    logger.info("[CLEAN_SLATE] closed=%s journals=%s errors=%d", closed, journals, len(errors))
    return {
        "ok": True, "mode": mode, "closed": closed, "journals_cleared": journals,
        "errors": errors, "note": "Restart the bot to begin the clean post-fix baseline.",
    }
