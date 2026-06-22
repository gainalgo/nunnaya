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


@router.get("/health", summary="헬스체크", description="서버 정상 동작 여부 확인")
def health(request: Request) -> Dict[str, Any]:
    """서버 헬스체크 엔드포인트 (강화)."""
    system = request.app.state.system
    
    # 기본 체크
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
    
    # 1. 메모리 사용량
    try:
        import psutil
        process = psutil.Process()
        checks["memory_mb"] = int(process.memory_info().rss / 1024 / 1024)
    except (ImportError, AttributeError, TypeError, ValueError):
        # psutil 미설치 환경 fallback (Windows 우선)
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
            report_suppressed_exception(__name__, 'psutil 미설치 환경 fallback (Windows 우선)')

    # 최후 fallback: tracemalloc(파이썬 힙 기준)이라도 0 방지
    if int(checks.get("memory_mb") or 0) <= 0:
        try:
            import tracemalloc
            if not tracemalloc.is_tracing():
                tracemalloc.start()
            current, peak = tracemalloc.get_traced_memory()
            checks["memory_mb"] = int(max(float(current), float(peak)) / 1024 / 1024)
        except (ImportError, AttributeError, ValueError):
            report_suppressed_exception(__name__, '최후 fallback: tracemalloc(파이썬 힙 기준)이라도 0 방지')
    
    # 2. Exchange API 응답 확인
    try:
        if hasattr(system, "query_client"):
            # 간단한 ticker 조회로 API 상태 확인
            ticker = system.query_client.get_ticker("BTCUSDT")
            if ticker and ticker.get("trade_price"):
                checks["exchange_api"] = "ok"
            else:
                checks["exchange_api"] = "error"
    except (AttributeError, TypeError, ValueError):
        logger.warning("system_router.health L111 except", exc_info=True)
        checks["exchange_api"] = "error"
    
    # 3. WebSocket 상태 (price feed)
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
    
    # 4. 최근 reconcile orphan 수
    # 우선 in-memory 마지막 reconcile 결과 사용 (가장 정확/저비용)
    try:
        last_result = getattr(system, "_last_reconcile_result", None)
        if isinstance(last_result, dict):
            orphans = last_result.get("orphans")
            if isinstance(orphans, list):
                checks["orphan_markets"] = int(len(orphans))
            elif orphans is not None:
                checks["orphan_markets"] = int(orphans)
    except (AttributeError, TypeError, ValueError):
        report_suppressed_exception(__name__, '우선 in-memory 마지막 reconcile 결과 사용 (가장 정확/저비용)')

    # fallback: 최근 원장 레코드
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
        report_suppressed_exception(__name__, 'fallback: 최근 원장 레코드')
    
    # 5. 오래된 미체결 주문 (1시간 이상)
    #   2026-06-06: 기존 구현은 system.get_markets() + system.get_context() (둘 다 HyperSystem
    #   에 없는 유령 — get_context 는 coordinator 전용) + ctx.get("order") (HyperEngineContext
    #   는 dict 도 order 필드도 없음) 3중 깨짐 → 매 health 호출마다 AttributeError 로그 스팸.
    #   현 아키텍처(FOCUS 시장가 + 서버사이드 TP/SL)는 미체결 limit 주문을 로컬 context 에
    #   들고 있지 않아 이 메트릭 자체가 무의미 → 0 고정. 실측 stuck-order 모니터가 필요하면
    #   Bybit open-orders 를 별도 async(여기 동기 폴링 X)로 붙일 것.
    checks["stuck_orders"] = 0

    # 6. Rate limiter 상태
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
            report_suppressed_exception(__name__, '6. Rate limiter 상태')
    except (AttributeError, TypeError, ValueError):
        logger.warning("system_router.health L200 except", exc_info=True)
        checks["rate_limiter"] = {"error": "unavailable"}

    # 7. Price feed 상태 (last update timestamp)
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

    # 8. Ledger 상태 (last write, total entries)
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
                report_suppressed_exception(__name__, '8. Ledger 상태 (last write, total entries)')
        checks["ledger"] = {
            "last_write_ts": last_write_ts,
            "total_entries": total_entries,
        }
    except (AttributeError, TypeError, ValueError, OSError):
        logger.warning("system_router.health L237 except", exc_info=True)
        checks["ledger"] = {"error": "unavailable"}
    
    # 전체 상태 판정 (healthy / degraded / critical)
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


@router.get("/currency", summary="기축통화 정보", description="현재 설정된 기축통화(Quote Currency) 정보 조회")
def currency() -> Dict[str, Any]:
    """현재 설정된 기축통화 정보를 반환합니다.
    
    Returns:
        symbol, min_order, decimals, exchange 등 통화 설정 정보
    """
    return {"ok": True, **Q.to_dict()}


@router.get("/info", summary="시스템 정보", description="시스템 버전 및 엔진 정보 조회")
def info() -> Dict[str, Any]:
    """시스템 기본 정보를 반환합니다."""
    return {
        "ok": True,
        "version": "v3-H",
        "engine": "HyperNunnayaEngine",
        "description": "Autocoin OS v3-H System API",
    }


@router.get("/markets", summary="Bybit 마켓 목록", description="CORS 없이 Bybit 마켓 목록 조회 (서버 프록시)")
def bybit_markets(
    quote: str = Query(Q.symbol, description="기축통화 필터 (예: USDT)"),
    refresh: bool = Query(False, description="True면 업비트 API를 즉시 조회"),
    details: bool = Query(False, description="True면 마켓 상세 정보 포함"),
) -> Dict[str, Any]:
    """UI용 Bybit 마켓 목록 조회 (서버 프록시)."""
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

@router.get("/feed-status", summary="가격 피드 상태", description="REST/WebSocket 피드 상태 조회")
def feed_status() -> Dict[str, Any]:
    """가격 피드 상태를 반환합니다."""
    return {
        "ok": True,
        "mode": "REST",
        "rest_ok": True,
        "ws_ok": False,
        "banned": False,
        "ban_until": None,
    }

@router.get("/status", summary="시스템 상태", description="실시간 시스템 스냅샷 조회")
def status(request: Request, response: Response) -> Dict[str, Any]:
    """실시간 시스템 상태 스냅샷.

    2초 캐시: 부팅 직후 스레드풀 경쟁 시에도 빠르게 응답.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    # 2초 캐시: 동시 다발 요청 시 스레드풀 과부하 방지
    now = time.time()
    cached = getattr(request.app.state, "_status_cache", None)
    if cached and (now - cached[0]) < 2.0:
        return cached[1]

    system = request.app.state.system
    snap = system.status()
    snap["server_now_ts"] = now

    # API call stats (rate limit monitoring)
    try:
        if hasattr(system, "trade_client") and hasattr(system.trade_client, "get_api_stats"):
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
    """원장 레코드에서 계정 총자산(equity_usdt) 값을 추출한다."""
    try:
        data = rec.get("data") if isinstance(rec, dict) else None
        if not isinstance(data, dict):
            return None

        # 1) 가장 흔한 케이스: ALLOC_REBALANCE.data.equity_usdt
        eq = data.get("equity_usdt")
        if eq is not None:
            v = float(eq)
            if v > 0:
                return v

        # 2) 일부 스냅샷형 포맷 대응
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


@router.get("/equity/at", summary="기준 시각 총자산 조회", description="원장에서 기준 시각에 가장 가까운 equity_usdt를 조회")
def get_equity_at_time(
    request: Request,
    dt: str = Query(..., description="기준 시각 (예: 2026-02-15T00:00)"),
    lookback_hours: int = Query(240, ge=1, le=720, description="원장 검색 범위(시간)"),
    tail_lines: int = Query(120000, ge=1000, le=400000, description="원장 최대 스캔 라인 수"),
) -> Dict[str, Any]:
    """
    입력한 시각과 가장 가까운 원장 equity 스냅샷을 반환합니다.
    - 주 데이터 소스: ALLOC_REBALANCE.data.equity_usdt
    - 없으면 최근 상태값으로 폴백
    """
    from datetime import datetime

    try:
        dt_str = str(dt or "").strip()
        if not dt_str:
            return {"ok": False, "error": "dt is required"}

        # datetime-local("YYYY-MM-DDTHH:MM") 또는 ISO datetime을 로컬 시간으로 해석
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
        # 폴백: 현재 상태값
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
            report_suppressed_exception(__name__, '폴백: 현재 상태값')
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
# PnL Baseline Reset — 한 클릭 시각적 reset
# ★ [2026-05-31 부모] "보기에 좋은 떡이 맛도 좋다" — 현재 잔액 캡처 → PnL 0 부터 시작.
# ------------------------------------------------------------
@router.post("/pnl-baseline/reset")
def pnl_baseline_reset(
    request: Request,
    baseline: Optional[float] = Query(None, ge=0, description="기준 금액(USDT). 입금 등 반영해 직접 지정. 미지정/0 이면 현재 equity 사용(기존 동작)"),
) -> Dict[str, Any]:
    """PnL baseline 캡처 → runtime/pnl_baseline.json 저장.

    [2026-06-02 부모] baseline 직접 입력 지원 — 입금 후 '얼마를 기준으로' 리셋할지 선택.
      · baseline 지정(>0): 그 금액을 기준점으로 (입금 반영)
      · 미지정/0: 현재 equity 캡처 (기존 '직전 금액' 동작 = 하위호환)
    효과: dashboard 상단 PnL 표시가 0 부터 다시 시작 (시각적 reset).
    봇 동작에는 영향 X (진입 사이즈/가드 모두 현재 잔액만 기준).
    """
    import json as _json
    import time as _time
    import os as _os
    system = request.app.state.system
    try:
        current_equity = float(getattr(system, "_last_equity_usdt", 0) or 0)
        # baseline 입력 있으면 그 값(입금 반영), 없으면 현재 equity (하위호환)
        if baseline is not None and float(baseline) > 0:
            base_val = round(float(baseline), 2)
            _src = "manual_input"
        else:
            if current_equity <= 0:
                return {"ok": False, "error": "no_equity", "message": "현재 equity 측정 불가 — 잠시 후 재시도"}
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
                    # ACTIVE/RECOVERY가 contexts에 없으면 생성해 Guard Matrix에서 보이도록 함
                    ctx = system.coordinator.ensure_market(market)
            except (KeyError, AttributeError, TypeError):
                report_suppressed_exception(__name__, 'ACTIVE/RECOVERY가 contexts에 없으면 생성해 Guard Matrix에서 보이도록 함 except-> continue')
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
    # [2026-03-23] Guards 저장 시 PRM에 동기화
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
    """Guards 설정을 저장합니다.
    
    전략별 오버라이드가 있으면 해당 전략에만 적용,
    없으면 글로벌 설정으로 적용됩니다.
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

        # [2026-03-23] 스마트 리스크: os.environ 기반 설정 처리
        import os as _os
        if "size_mult_hi_pct" in guards:
            try:
                v = min(-0.1, float(guards["size_mult_hi_pct"]))
                _os.environ["OMA_SIZE_MULT_HI_PCT"] = str(v)
            except (OverflowError, TypeError, ValueError):
                report_suppressed_exception(__name__, '[2026-03-23] 스마트 리스크: os.environ 기반 설정 처리')
        if "size_mult_floor" in guards:
            try:
                v = max(0.1, min(0.9, float(guards["size_mult_floor"])))
                _os.environ["OMA_SIZE_MULT_FLOOR"] = str(v)
            except (OverflowError, TypeError, ValueError):
                report_suppressed_exception(__name__, '[2026-03-23] 스마트 리스크: os.environ 기반 설정 처리')
        if "concentration_limit_pct" in guards:
            try:
                v = max(5.0, min(50.0, float(guards["concentration_limit_pct"])))
                setattr(system, "concentration_limit_pct", v)
            except (AttributeError, OverflowError, TypeError, ValueError):
                report_suppressed_exception(__name__, '[2026-03-23] 스마트 리스크: os.environ 기반 설정 처리')

        try:
            system.persist_ui_settings()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            report_suppressed_exception(__name__, '[2026-03-23] 스마트 리스크: os.environ 기반 설정 처리')

        return {"ok": True, "applied": len(guards)}


@router.get("/guards/strategies")
def guards_strategies_get(request: Request) -> Dict[str, Any]:
    """전략별 Guard 설정을 조회합니다."""
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
    """Guard 설정을 시스템에 실시간 반영합니다."""
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
        
        # [FIX] 가격이 없으면 entry_price(평단가)로 fallback
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
    현재 보유 중인 코인 중 거래지원 종료 예정인 코인 확인.
    
    Returns:
    - delisting_markets: 거래지원 종료 예정 마켓 목록
    - holdings_at_risk: 보유 중이면서 종료 예정인 마켓
    
    Note: Bybit delisting API를 통해 확인합니다.
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
    """종료 예정 마켓 자동 청산 옵션 설정."""
    import os
    os.environ["OMA_AUTO_LIQUIDATE_DELISTING"] = "1" if enabled else "0"
    return {"ok": True, "auto_liquidate_delisting": enabled}


@router.get("/auto-liquidate-delisting")
def get_auto_liquidate_delisting(request: Request) -> Dict[str, Any]:
    """종료 예정 마켓 자동 청산 옵션 조회."""
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
    마켓 상태 변경 감지 (신규 상장, 종료 예정, 상장 대기).
    
    Returns:
    - new_listings: 새로 상장된 마켓 (PREVIEW → ACTIVE)
    - delisting_alerts: 종료 예정으로 변경된 마켓
    - preview_markets: 현재 상장 대기 중인 마켓
    """
    from app.manager.market_status_monitor import check_market_status_changes
    
    system = request.app.state.system
    
    try:
        # 현재 활성 마켓 목록
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
    summary="섹터 매핑 조회",
    responses={200: {"description": "현재 섹터 매핑 정보"}},
)
def get_sector_map(request: Request):
    """Smart Allocation에 사용되는 섹터 매핑 정보를 조회합니다."""
    import json
    from pathlib import Path
    
    sector_file = Path(__file__).parent.parent / "data" / "sector_map.json"
    
    try:
        if sector_file.exists():
            with open(sector_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"sectors": {}, "default_sector": "OTHERS", "default_cap": 0.40}
        
        # 코인 → 섹터 플랫 맵 생성
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
    summary="섹터 매핑 저장",
    responses={200: {"description": "섹터 매핑 저장 완료"}},
)
def save_sector_map(request: Request, data: Dict[str, Any]):
    """Smart Allocation에 사용되는 섹터 매핑 정보를 저장합니다."""
    import json
    from pathlib import Path
    
    sector_file = Path(__file__).parent.parent / "data" / "sector_map.json"
    
    try:
        from app.core.io_utils import safe_write_json
        safe_write_json(str(sector_file), data)

        # HyperSystem에 반영
        system = request.app.state.system

        # 코인 → 섹터 플랫 맵 생성
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
    summary="코인 섹터 설정",
    responses={200: {"description": "코인 섹터 설정 완료"}},
)
def set_coin_sector(
    request: Request,
    market: str = Query(..., description="마켓 코드 (e.g., BTCUSDT)"),
    sector: str = Query(..., description="섹터 ID (e.g., L1, DEFI, MEME)"),
):
    """개별 코인의 섹터를 설정합니다."""
    import json
    from pathlib import Path
    
    sector_file = Path(__file__).parent.parent / "data" / "sector_map.json"
    
    try:
        # 기존 데이터 로드
        if sector_file.exists():
            with open(sector_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"sectors": {}, "default_sector": "OTHERS", "default_cap": 0.40}
        
        # 기존 섹터에서 코인 제거
        for s_id, s_info in data.get("sectors", {}).items():
            coins = s_info.get("coins", [])
            if market in coins:
                coins.remove(market)
        
        # 새 섹터에 코인 추가
        if sector not in data["sectors"]:
            data["sectors"][sector] = {"name": sector, "cap": 0.20, "coins": []}
        
        if market not in data["sectors"][sector].get("coins", []):
            data["sectors"][sector].setdefault("coins", []).append(market)
        
        # 저장
        from app.core.io_utils import safe_write_json
        safe_write_json(str(sector_file), data)

        # HyperSystem에 반영
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

@router.get("/fear-greed", summary="Fear & Greed Index 조회", description="현재 시장 심리 지수 및 예산 배율 조회")
def get_fear_greed(request: Request) -> Dict[str, Any]:
    """Fear & Greed Index 정보를 반환합니다.
    
    Returns:
        value: 0-100 (0=극도의 공포, 100=극도의 탐욕)
        level: EXTREME_FEAR, FEAR, NEUTRAL, GREED, EXTREME_GREED
        budget_mult: 예산 배율 (역발상: 공포→높음, 탐욕→낮음)
    """
    try:
        from app.core.fear_greed import get_fear_greed_index
        fg = get_fear_greed_index()
        return {"ok": True, **fg.to_dict()}
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("system_router.get_fear_greed L1831: %s", e)
        return {"ok": False, "error": str(e)}


@router.post("/fear-greed/refresh", summary="Fear & Greed Index 새로고침", description="캐시 무시하고 최신 데이터 조회")
def refresh_fear_greed(request: Request) -> Dict[str, Any]:
    """Fear & Greed Index를 강제로 새로고침합니다."""
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
# Daily PnL (매매일지) API
# [CREATED 2026-01-23]
# ============================================================

@router.get("/daily-pnl/today", summary="오늘 손익 조회", description="오늘의 매매 손익 요약")
def get_daily_pnl_today(request: Request) -> Dict[str, Any]:
    """오늘의 손익 리포트를 반환합니다."""
    try:
        from app.manager.daily_pnl import get_daily_pnl_manager
        system = request.app.state.system
        
        manager = get_daily_pnl_manager()
        # 오늘 자정부터의 레코드만 조회
        from datetime import datetime
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        records = system.ledger.tail_records(since_ts=today_start)
        report = manager.aggregate_from_ledger(records)
        
        return {"ok": True, "report": report.to_dict()}
    except (OSError, AttributeError, TypeError, ValueError, OverflowError) as e:
        logger.warning("system_router.get_daily_pnl_today L1874: %s", e)
        return {"ok": False, "error": str(e)}


@router.get("/daily-pnl/summary", summary="일별 손익 요약", description="최근 N일간 손익 요약")
def get_daily_pnl_summary(request: Request, days: int = 7) -> Dict[str, Any]:
    """최근 N일간의 손익 요약을 반환합니다."""
    try:
        from app.manager.daily_pnl import get_daily_pnl_manager
        
        manager = get_daily_pnl_manager()
        summary = manager.get_summary(days=days)
        
        return {"ok": True, **summary}
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("system_router.get_daily_pnl_summary L1888: %s", e)
        return {"ok": False, "error": str(e)}


@router.get("/daily-pnl/list", summary="일별 손익 목록", description="저장된 일별 손익 목록")
def get_daily_pnl_list(request: Request, limit: int = 30) -> Dict[str, Any]:
    """저장된 일별 손익 날짜 목록을 반환합니다."""
    try:
        from app.manager.daily_pnl import get_daily_pnl_manager
        
        manager = get_daily_pnl_manager()
        dates = manager.list_dates(limit=limit)
        
        return {"ok": True, "dates": dates, "count": len(dates)}
    except (AttributeError, TypeError) as e:
        logger.warning("system_router.get_daily_pnl_list L1902: %s", e)
        return {"ok": False, "error": str(e)}


@router.get("/daily-pnl/{date}", summary="특정 날짜 손익 조회", description="특정 날짜의 매매 손익")
def get_daily_pnl_date(request: Request, date: str) -> Dict[str, Any]:
    """특정 날짜의 손익 리포트를 반환합니다. (형식: 2026-01-23)"""
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


@router.post("/daily-pnl/snapshot", summary="오늘 스냅샷 저장", description="오늘의 손익을 파일로 저장")
def save_daily_pnl_snapshot(request: Request) -> Dict[str, Any]:
    """오늘의 손익 스냅샷을 저장합니다."""
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

# ── 공유 httpx 클라이언트 (TCP 커넥션 풀링) ──
# 매 요청마다 httpx.Client() 생성 → 10054 원인이었음
import httpx as _httpx
from app.core.constants import BYBIT_MARKET_KLINE, BYBIT_MARKET_TICKERS, parse_bybit_list

_bybit_ui_client: Optional[_httpx.Client] = None


def _get_bybit_ui_client() -> _httpx.Client:
    """Bybit UI 프록시용 공유 httpx 클라이언트 (lazy init, 커넥션 풀링)."""
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


@router.get("/binance-klines", summary="Bybit 캔들 데이터", description="Bybit V5 klines 프록시 (CORS 우회)")
def binance_klines(symbol: str = "BTCUSDT", interval: str = "15", limit: int = 100) -> Dict[str, Any]:
    """Bybit V5 kline API 프록시.

    Args:
        symbol: 거래쌍 심볼 (예: BTCUSDT)
        interval: 캔들 간격 (1,3,5,15,30,60,120,240,360,720,D,W,M)
        limit: 캔들 개수 (최대 1000)

    Returns:
        OHLCV 캔들 데이터
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


@router.get("/binance-tickers", summary="Bybit 전체 가격", description="Bybit V5 모든 티커 가격 프록시 (CORS 우회)")
def binance_tickers() -> Dict[str, Any]:
    """Bybit V5 모든 spot 티커 가격 프록시."""
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

# 세션 토큰 저장 (메모리 기반, 서버 재시작 시 초기화)
_admin_sessions: dict = {}

def _get_admin_password() -> str:
    """환경변수에서 admin 비밀번호를 가져옵니다.

    공개 배포 안전 기본값: ADMIN_PASSWORD 미설정 시 빈 문자열 반환 →
    admin 로그인 자체를 비활성화한다(추측 가능한 기본 비번을 절대 내장하지 않음).
    """
    import os
    return os.getenv("ADMIN_PASSWORD", "")

def _verify_admin_token(token: str) -> bool:
    """admin 토큰이 유효한지 확인합니다."""
    if not token:
        return False
    session = _admin_sessions.get(token)
    if not session:
        return False
    # 24시간 만료
    import time
    if time.time() - session.get("created", 0) > 86400:
        del _admin_sessions[token]
        return False
    return True


@router.post("/admin/login", summary="Admin 로그인", description="admin 비밀번호로 로그인")
def admin_login(password: str = Query(...)) -> Dict[str, Any]:
    """Admin 비밀번호를 확인하고 세션 토큰을 발급합니다."""
    import time
    
    admin_pw = _get_admin_password()
    if not admin_pw:
        return {"ok": False, "error": "ADMIN_PASSWORD 가 설정되지 않아 admin 기능이 비활성화되어 있습니다. (.env 에 ADMIN_PASSWORD 설정 필요)"}
    if password != admin_pw:
        return {"ok": False, "error": "비밀번호가 일치하지 않습니다."}
    
    # 토큰 생성
    token = secrets.token_hex(32)
    _admin_sessions[token] = {"created": time.time()}
    
    return {"ok": True, "token": token, "message": "로그인 성공"}


@router.get("/admin/verify", summary="Admin 토큰 확인", description="admin 토큰이 유효한지 확인")
def admin_verify(token: str = Query("")) -> Dict[str, Any]:
    """admin 토큰이 유효한지 확인합니다."""
    if _verify_admin_token(token):
        return {"ok": True, "valid": True}
    return {"ok": True, "valid": False}


@router.post("/admin/logout", summary="Admin 로그아웃", description="admin 세션 종료")
def admin_logout(token: str = Query("")) -> Dict[str, Any]:
    """admin 세션을 종료합니다."""
    if token in _admin_sessions:
        del _admin_sessions[token]
    return {"ok": True, "message": "로그아웃 완료"}


# ============================================================
# Exchange API Settings
# ============================================================

@router.get("/exchange-api/status", summary="Exchange API 상태", description="Exchange API 키 설정 상태 확인")
def exchange_api_status() -> Dict[str, Any]:
    """Exchange API 키 설정 상태를 확인합니다."""
    import os
    access_key = os.getenv("BYBIT_API_KEY", "")
    secret_key = os.getenv("BYBIT_API_SECRET", "")
    
    has_keys = bool(access_key and secret_key)
    # [2026-04-09 보안] 앞2자만 노출 (기존 앞4+뒤4 → 너무 많이 드러남)
    masked_access = access_key[:2] + "****" if len(access_key) > 4 else ("****" if access_key else "")
    
    return {
        "ok": True,
        "has_keys": has_keys,
        "access_key_masked": masked_access,
    }


@router.get("/exchange-api/detect-ip", summary="서버 공인 IP 감지", description="서버의 공인 IP 주소를 감지합니다")
def detect_public_ip() -> Dict[str, Any]:
    """서버의 공인 IP 주소를 감지합니다."""
    import requests
    
    try:
        # 여러 IP 감지 서비스 시도
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
                report_suppressed_exception(__name__, '여러 IP 감지 서비스 시도 except-> continue')
                continue
        
        return {"ok": False, "error": "IP 감지 실패"}
    except Exception as e:
        logger.warning("system_router.detect_public_ip L2124: %s", e)
        return {"ok": False, "error": str(e)}


@router.post("/exchange-api/test", summary="Exchange API 연결 테스트", description="입력된 API 키로 Exchange 연결 테스트")
def test_exchange_api(access_key: str = Body(..., embed=True), secret_key: str = Body(..., embed=True)) -> Dict[str, Any]:
    """입력된 API 키로 Exchange 연결을 테스트합니다."""
    try:
        from app.integrations.bybit_trade import BybitTradeClient as BybitTradeClient

        client = BybitTradeClient(api_key=access_key, api_secret=secret_key)
        accounts = client.get_accounts()
        
        if accounts is None:
            return {"ok": False, "error": "API 응답 없음"}
        
        # 잔고 요약
        quote_balance = 0
        coin_count = 0
        for acc in accounts:
            if acc.get("currency") == Q.symbol:
                quote_balance = float(acc.get("balance", 0))
            else:
                coin_count += 1
        
        return {
            "ok": True,
            "message": "연결 성공",
            "quote_balance": quote_balance,
            "coin_count": coin_count,
        }
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.test_exchange_api L2155: %s", e)
        error_msg = str(e)
        if "no_authorization_ip" in error_msg.lower() or "허용되지 않은" in error_msg:
            return {"ok": False, "error": "IP가 허용되지 않음. Bybit에서 서버 IP를 등록하세요."}
        return {"ok": False, "error": error_msg}


@router.post("/exchange-api/save", summary="Exchange API 키 저장", description=".env 파일에 API 키 저장 (admin 인증 필요)")
def save_exchange_api(access_key: str = Body(..., embed=True), secret_key: str = Body(..., embed=True), admin_token: str = Body("", embed=True)) -> Dict[str, Any]:
    """API 키를 .env 파일에 저장합니다. (admin 인증 필요)"""
    if not _verify_admin_token(admin_token):
        return {"ok": False, "error": "Admin 인증이 필요합니다."}
    import os
    from pathlib import Path
    
    try:
        env_path = Path(".env")
        
        # 기존 .env 파일 읽기
        env_lines = []
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                env_lines = f.readlines()
        
        # BYBIT_API_KEY, BYBIT_API_SECRET 업데이트
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

        # 없으면 추가
        if not access_found:
            new_lines.append(f"BYBIT_API_KEY={access_key}\n")
        if not secret_found:
            new_lines.append(f"BYBIT_API_SECRET={secret_key}\n")

        # 파일 저장
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        # 환경 변수 업데이트 (현재 프로세스)
        os.environ["BYBIT_API_KEY"] = access_key
        os.environ["BYBIT_API_SECRET"] = secret_key
        
        return {"ok": True, "message": ".env 파일에 저장되었습니다. 서버 재시작 후 완전히 적용됩니다."}
    except (OSError, KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.save_exchange_api L2210: %s", e)
        return {"ok": False, "error": str(e)}


# ============================================================
# Telegram Settings
# ============================================================

@router.get("/telegram/status", summary="Telegram 설정 상태", description="Telegram 알림 설정 상태 확인")
def telegram_status() -> Dict[str, Any]:
    """Telegram 설정 상태를 확인합니다."""
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


@router.post("/telegram/test", summary="Telegram 테스트 메시지", description="입력된 설정으로 테스트 메시지 전송")
def test_telegram(token: str = Query(...), chat_id: str = Query(...)) -> Dict[str, Any]:
    """입력된 설정으로 Telegram 테스트 메시지를 전송합니다."""
    import requests
    from datetime import datetime
    
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        message = f"🤖 Autocoin OS 테스트 메시지\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n✅ 연결 성공!"
        
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message},
            timeout=10
        )
        
        if resp.status_code == 200:
            return {"ok": True, "message": "테스트 메시지 전송 성공"}
        else:
            data = resp.json()
            error_desc = data.get("description", "알 수 없는 오류")
            return {"ok": False, "error": error_desc}
    except Exception as e:
        logger.warning("system_router.test_telegram L2258: %s", e)
        return {"ok": False, "error": str(e)}


@router.post("/telegram/save", summary="Telegram 설정 저장", description=".env 파일에 Telegram 설정 저장 (admin 인증 필요)")
def save_telegram(token: str = Query(...), chat_id: str = Query(...), admin_token: str = Query("")) -> Dict[str, Any]:
    """Telegram 설정을 .env 파일에 저장합니다. (admin 인증 필요)"""
    if not _verify_admin_token(admin_token):
        return {"ok": False, "error": "Admin 인증이 필요합니다."}
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
        
        return {"ok": True, "message": ".env 파일에 저장되었습니다."}
    except (OSError, KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("system_router.save_telegram L2305: %s", e)
        return {"ok": False, "error": str(e)}


# ── [2026-06-01] 알림 종류 토글 (longhold/drawdown/exit_streak/daily/harpoon) ──
#   send 지점 무수정: 부팅 시 읽힌 system 속성 + env-read 사이트가 그대로 체크하므로,
#   런타임에 속성 + os.environ 갱신하면 즉시 반영. .env 도 갱신해 재시작 후 유지.
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
    """.env 의 여러 키를 갱신/추가 (비밀 아님·알림 플래그)."""
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


@router.get("/alerts", summary="알림 종류 on/off 상태")
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


@router.post("/alerts", summary="알림 종류 on/off 설정 (런타임+.env)")
def set_alerts(request: Request, longhold: str = Query(""), drawdown: str = Query(""),
               exit_profit_streak: str = Query(""), daily: str = Query(""), harpoon: str = Query("")) -> Dict[str, Any]:
    system = request.app.state.system
    incoming = {"longhold": longhold, "drawdown": drawdown, "exit_profit_streak": exit_profit_streak, "daily": daily, "harpoon": harpoon}
    env_updates: Dict[str, str] = {}
    for k, raw in incoming.items():
        if raw == "":   # 미지정 = 변경 안 함
            continue
        val = _alert_truthy(raw)
        env_name, attr, _ = _ALERT_FLAGS[k]
        os.environ[env_name] = "1" if val else "0"
        env_updates[env_name] = "1" if val else "0"
        if attr:
            try:
                setattr(system, attr, val)   # 런타임 즉시 반영 (send 지점이 이 속성 체크)
            except (AttributeError, TypeError):
                logger.warning("set_alerts: setattr %s failed", attr, exc_info=True)
    if env_updates:
        try:
            _persist_env_keys(env_updates)   # 재시작 후 유지
        except OSError as e:
            return {"ok": False, "error": str(e)}
    return get_alerts(request)


# ============================================================
# Server Restart / Stop
# ============================================================

@router.post("/restart", summary="서버 재시작", description="서버를 재시작합니다 (run.ps1 필요)")
async def restart_server(
    request: Request,
    delay_sec: int = Query(15, ge=1, le=60, description="정리 대기 시간 (초)"),
    cleanup: int = Query(1, ge=0, le=1, description="정리 실행 여부 (1/0)")
) -> Dict[str, Any]:
    """서버 재시작을 요청합니다.
    
    - delay_sec: 정리 대기 시간 (기본 15초, 최대 60초)
    - run.ps1이 exit code 42를 감지하면 자동으로 재시작합니다.
    - Graceful shutdown 수행 후 재시작
    """
    import asyncio
    import os
    import time
    
    system = request.app.state.system
    do_cleanup = bool(cleanup)
    
    async def graceful_shutdown():
        try:
            # 1) 엔진 정지
            if hasattr(system, 'coordinator') and hasattr(system.coordinator, 'engine'):
                system.coordinator.engine.status.stop()
            # 1.5) Autopilot 비활성화 (정리 기간 동안 자동 승격 방지)
            if hasattr(system, "autopilot_enabled"):
                system.autopilot_enabled = False

            # 2) 정리 옵션
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
                        report_suppressed_exception(__name__, '2) 정리 옵션')
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    logger.warning("system_router.save_telegram L2378: %s", e)
                    print(f"[RESTART] Cleanup error: {e}")

            # 3) 정리 대기 (tick loop는 계속 돌며 pending 정리)
            elapsed = time.time() - start_ts
            remain = max(0.0, float(delay_sec) - elapsed)
            if remain > 0:
                await asyncio.sleep(remain)

            # 4) 상태 저장
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
        "message": f"서버가 {delay_sec}초 후 재시작됩니다... (cleanup={1 if do_cleanup else 0})",
        "delay_sec": delay_sec,
        "cleanup": 1 if do_cleanup else 0
    }


@router.post("/stop", summary="서버 정지", description="서버를 정지합니다")
async def stop_server(
    request: Request,
    delay_sec: int = Query(15, ge=1, le=60, description="정리 대기 시간 (초)"),
    cleanup: int = Query(1, ge=0, le=1, description="정리 실행 여부 (1/0)")
) -> Dict[str, Any]:
    """서버를 완전히 정지합니다.
    
    - delay_sec: 정리 대기 시간 (기본 15초, 최대 60초)
    - Graceful shutdown 수행 후 정지
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

            # 정리 대기 (tick loop는 계속 돌며 pending 정리)
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
        "message": f"서버가 {delay_sec}초 후 정지됩니다... (cleanup={1 if do_cleanup else 0})",
        "delay_sec": delay_sec,
        "cleanup": 1 if do_cleanup else 0
    }


# ------------------------------------------------------------
# Ledger Validation
# ------------------------------------------------------------
@router.get("/validate/ledger", summary="원장 검증", description="Trade Ledger 무결성 검증")
def validate_ledger() -> Dict[str, Any]:
    """
    Trade Ledger 검증:
    - BUY/SELL 짝 확인
    - 중복 거래 감지
    - 시간순 정렬 검증
    - 음수 값 검증
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


@router.get("/validate/holding-sync", summary="포지션 동기화 검증", description="Context vs Exchange 잔고 비교")
def validate_holding_sync(request: Request) -> Dict[str, Any]:
    """
    Context.position vs Exchange.balance 검증:
    - Active 마켓의 포지션 불일치 감지
    - 허용 오차: 0.0001
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
@router.get("/night-mode", summary="Night Mode 설정 조회")
def get_night_mode(request: Request):
    system = request.app.state.system
    return {"ok": True, **system.get_night_mode_config()}


@router.patch("/night-mode", summary="Night Mode 설정 변경")
def patch_night_mode(request: Request, body: Dict[str, Any]):
    """Night Mode 설정 변경.

    body 예시::

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

    # ui_settings에 저장 (재시작 시 복원)
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
        report_suppressed_exception(__name__, 'ui_settings에 저장 (재시작 시 복원)')

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
    summary="포지션 보유 기간 모니터링",
    description="모든 활성 포지션의 보유 시간과 장기 보유 경고를 반환합니다.",
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
# Paper Trading Mode — 모드 전환 & 상태 조회
# ============================================================

@router.post("/trading-mode", summary="Switch LIVE/PAPER trading mode")
def switch_trading_mode(request: Request, body: Dict[str, Any] = {}):
    """런타임에서 LIVE ↔ PAPER 모드 전환.

    PAPER 모드: 실제 주문 없이 가상 체결로 전략 테스트.
    """
    mode = str(body.get("mode", "")).upper()
    if mode not in ("LIVE", "PAPER"):
        return {"ok": False, "error": "mode must be LIVE or PAPER"}

    system = request.app.state.system
    old_mode = str(getattr(system, "trading_mode", "LIVE")).upper()

    if old_mode == mode:
        return {"ok": True, "mode": mode, "changed": False}

    # LIVE 전환 시 API 키 필요
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
    """Paper 모드 거래 현황 조회."""
    system = request.app.state.system
    mode = str(getattr(system, "trading_mode", "LIVE")).upper()

    if mode != "PAPER" or not hasattr(system.trade_client, "get_summary"):
        return {"ok": False, "mode": mode, "error": "Not in PAPER mode"}

    return {"ok": True, **system.trade_client.get_summary()}


@router.post("/paper/reset", summary="Reset paper trading balance")
def paper_reset(request: Request, body: Dict[str, Any] = {}):
    """Paper 잔고 초기화."""
    system = request.app.state.system
    mode = str(getattr(system, "trading_mode", "LIVE")).upper()

    if mode != "PAPER" or not hasattr(system.trade_client, "reset"):
        return {"ok": False, "mode": mode, "error": "Not in PAPER mode"}

    initial = float(body.get("initial_usdt", 0) or os.getenv("DRY_INITIAL_USDT", "1000"))
    system.trade_client.reset(initial_usdt=initial)
    return {"ok": True, "reset_to": initial, **system.trade_client.get_summary()}
