# ============================================================
# File: app/main.py
# Autocoin OS v3-H — FastAPI Main Entry (UI Static Mounted)
# ============================================================

import logging
import warnings
import os
import asyncio
import time

# ★ 로깅 초기화 — 반드시 다른 모듈 import 전에 호출
from app.core.logging_config import setup_logging
setup_logging()

logger = logging.getLogger(__name__)
from datetime import datetime, timedelta
from urllib.parse import quote

# sklearn.utils.parallel 경고 완전 억제
os.environ["PYTHONWARNINGS"] = "ignore::UserWarning"
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", message=".*sklearn.*")
warnings.filterwarnings("ignore", message=".*parallel.*")

# showwarning 오버라이드로 sklearn parallel 경고 완전 차단
_original_showwarning = warnings.showwarning
def _filtered_showwarning(message, category, filename, lineno, file=None, line=None):
    msg_str = str(message)
    if ("sklearn.utils.parallel" in msg_str) or ("delayed" in msg_str and "Parallel" in msg_str):
        return  # 완전히 무시
    if "sklearn" in filename:
        return  # sklearn 내부 경고 무시
    _original_showwarning(message, category, filename, lineno, file, line)
warnings.showwarning = _filtered_showwarning

import base64
import secrets
import hashlib
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, Response, HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager

# 세션 토큰 저장소 (메모리 기반, 서버 재시작 시 초기화)
_AUTH_SESSIONS: set[str] = set()
_AUTH_PASSWORD_HASH: str = ""  # 현재 비밀번호 해시 (변경 감지용)
_AUTH_FAILED: dict[str, dict[str, float]] = {}  # ip -> {count, first_ts, last_ts, blocked_until}

def _get_password_hash() -> str:
    """현재 환경변수 비밀번호의 해시값"""
    user = os.getenv("DASHBOARD_USER", "").strip()
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not user or not password:
        return ""
    return hashlib.sha256(f"{user}:{password}".encode()).hexdigest()[:16]

def _generate_session_token(user: str, password: str) -> str:
    """사용자 정보 기반 세션 토큰 생성 (비밀번호 해시 포함)"""
    pw_hash = _get_password_hash()
    data = f"{user}:{password}:{pw_hash}:{secrets.token_hex(8)}"
    return hashlib.sha256(data.encode()).hexdigest()[:32]

def _invalidate_sessions_if_password_changed():
    """비밀번호 변경 시 모든 세션 무효화"""
    global _AUTH_PASSWORD_HASH, _AUTH_SESSIONS
    current_hash = _get_password_hash()
    if _AUTH_PASSWORD_HASH and current_hash != _AUTH_PASSWORD_HASH:
        _AUTH_SESSIONS.clear()
        print("[AUTH] Password changed, all sessions invalidated")
    _AUTH_PASSWORD_HASH = current_hash


def _auth_bool_env(key: str, default: bool) -> bool:
    v = str(os.getenv(key, "1" if default else "0")).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _get_request_ip(request: Request) -> str:
    """Best-effort client IP extraction (Cloudflare/Proxy aware)."""
    try:
        # Cloudflare
        cf_ip = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf_ip:
            return cf_ip.split(",")[0].strip()
        # Generic proxy chain
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            return xff.split(",")[0].strip()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[MAIN] Generic proxy chain: %s", exc, exc_info=True)
    try:
        return str(request.client.host if request.client else "") or "unknown"
    except (AttributeError, TypeError, ValueError):
        logger.warning("[Auth] client host extraction failed", exc_info=True)
        return "unknown"


def _auth_rate_limit_config() -> tuple[bool, int, int, int]:
    enabled = _auth_bool_env("AUTH_LOGIN_RATE_LIMIT_ENABLED", True)
    try:
        max_fails = max(1, int(float(os.getenv("AUTH_LOGIN_MAX_FAILS", "6"))))
    except (TypeError, ValueError):
        logger.warning("[Auth] AUTH_LOGIN_MAX_FAILS parse failed, using default 6", exc_info=True)
        max_fails = 6
    try:
        window_sec = max(10, int(float(os.getenv("AUTH_LOGIN_WINDOW_SEC", "300"))))
    except (TypeError, ValueError):
        logger.warning("[Auth] AUTH_LOGIN_WINDOW_SEC parse failed, using default 300", exc_info=True)
        window_sec = 300
    try:
        block_sec = max(30, int(float(os.getenv("AUTH_LOGIN_BLOCK_SEC", "900"))))
    except (TypeError, ValueError):
        logger.warning("[Auth] AUTH_LOGIN_BLOCK_SEC parse failed, using default 900", exc_info=True)
        block_sec = 900
    return enabled, max_fails, window_sec, block_sec


def _auth_is_blocked(ip: str) -> tuple[bool, int]:
    enabled, _, _, _ = _auth_rate_limit_config()
    if not enabled:
        return False, 0
    now = time.time()
    rec = _AUTH_FAILED.get(ip)
    if not rec:
        return False, 0
    blocked_until = float(rec.get("blocked_until") or 0.0)
    if blocked_until > now:
        return True, int(max(1, blocked_until - now))
    return False, 0


def _auth_register_failure(ip: str) -> None:
    enabled, max_fails, window_sec, block_sec = _auth_rate_limit_config()
    if not enabled:
        return
    now = time.time()
    rec = dict(_AUTH_FAILED.get(ip) or {})
    first_ts = float(rec.get("first_ts") or 0.0)
    count = int(rec.get("count") or 0)

    # window rollover
    if first_ts <= 0.0 or (now - first_ts) > float(window_sec):
        first_ts = now
        count = 0

    count += 1
    blocked_until = float(rec.get("blocked_until") or 0.0)
    if count >= max_fails:
        blocked_until = now + float(block_sec)

    _AUTH_FAILED[ip] = {
        "count": float(count),
        "first_ts": float(first_ts),
        "last_ts": float(now),
        "blocked_until": float(blocked_until),
    }


def _auth_clear_failures(ip: str) -> None:
    try:
        _AUTH_FAILED.pop(ip, None)
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[MAIN] main._auth_clear_failures fallback: %s", exc, exc_info=True)


# ============================================================
# Basic Auth + Session Cookie Middleware (외부 접속 보호)
# ============================================================
class BasicAuthMiddleware(BaseHTTPMiddleware):
    """
    환경변수 DASHBOARD_USER / DASHBOARD_PASSWORD 설정 시 Basic Auth 적용.
    한 번 인증하면 세션 쿠키로 유지되어 페이지 이동 시 재인증 불필요.
    """
    async def dispatch(self, request: Request, call_next):
        user = os.getenv("DASHBOARD_USER", "").strip()
        password = os.getenv("DASHBOARD_PASSWORD", "").strip()
        
        # 인증 미설정 시 통과
        if not user or not password:
            return await call_next(request)
        
        # [2026-02-02] 비밀번호 변경 시 기존 세션 모두 무효화
        _invalidate_sessions_if_password_changed()
        
        # [2026-04-09 보안 강화] localhost 인증 우회 제거.
        # 같은 서버의 다른 프로세스가 무인증 접근하는 것을 방지.
        # 헬스체크(/health)는 위에서 이미 우회 허용됨.
        
        # WebSocket은 WS 핸들러 내부에서 인증 처리 (쿠키/Basic Auth)
        # [2026-04-09] 미들웨어에서 무조건 우회하지 않고 핸들러에 위임
        if request.url.path.startswith("/ws"):
            return await call_next(request)  # WS 핸들러가 자체 인증 수행
        
        # 인증 폼 경로는 우회 (브라우저 Basic Auth 프롬프트 미표시 환경 대응)
        if request.url.path in ("/auth/login", "/auth/login-submit", "/auth/logout"):
            return await call_next(request)

        # 헬스체크 경로는 인증 없이 통과 (Cloudflare 등 외부 모니터링)
        if request.url.path in ("/health", "/api/system/health"):
            return await call_next(request)

        # [2026-06-05] Peer Brief — Basic Auth 우회 (peer 간 polling 용).
        #   라우터 내부에서 PEER_BRIEF_TOKEN 으로 자체 인증.
        #   token 미설정 시 무인증 = 폐쇄망 전용. 외부 노출 환경은 token 필수.
        if request.url.path.startswith("/peer/"):
            return await call_next(request)
        
        # 정적 리소스 (CSS, JS, 이미지, 폰트)는 인증 없이 통과
        static_exts = ('.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.woff', '.woff2', '.ttf', '.svg')
        if request.url.path.startswith("/ui/") and request.url.path.lower().endswith(static_exts):
            response = await call_next(request)
            # JS/CSS는 Cloudflare 등 CDN 캐시 방지 (코드 배포 즉시 반영)
            if request.url.path.lower().endswith(('.js', '.css')):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
                response.headers["Pragma"] = "no-cache"
            return response
        
        # 1. 세션 쿠키 확인 (이미 인증된 경우)
        session_token = request.cookies.get("autocoin_session")
        if session_token and session_token in _AUTH_SESSIONS:
            return await call_next(request)
        
        # 2. Basic Auth 헤더 확인
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                encoded = auth_header[6:]
                decoded = base64.b64decode(encoded).decode("utf-8")
                req_user, req_pass = decoded.split(":", 1)
                if secrets.compare_digest(req_user, user) and secrets.compare_digest(req_pass, password):
                    # 인증 성공 → 세션 쿠키 발급
                    new_token = _generate_session_token(user, password)
                    _AUTH_SESSIONS.add(new_token)
                    response = await call_next(request)
                    response.set_cookie(
                        key="autocoin_session",
                        value=new_token,
                        httponly=True,
                        samesite="lax",
                        max_age=86400  # 1일 유지 [2026-04-09 보안: 7일→1일 단축]
                    )
                    return response
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[MAIN] 인증 성공 → 세션 쿠키 발급: %s", exc, exc_info=True)
        
        # 인증 실패:
        # - HTML 페이지 접근은 로그인 화면으로 리다이렉트
        # - API/비HTML 접근은 기존 401 + WWW-Authenticate 유지
        accept = (request.headers.get("accept", "") or "").lower()
        wants_html = ("text/html" in accept) or ("*/*" in accept)
        if request.method == "GET" and wants_html and not request.url.path.startswith("/api"):
            next_path = request.url.path or "/"
            if request.url.query:
                next_path = f"{next_path}?{request.url.query}"
            login_url = f"/auth/login?next={quote(next_path, safe='')}"
            return RedirectResponse(url=login_url, status_code=307)

        # 인증 실패 → 401 + WWW-Authenticate 헤더
        return Response(
            content="Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Autocoin"'}
        )

from app.core.hyper_system import HyperSystem
from app.manager.ledger_recovery_reactor import LedgerRecoveryReactor

from app.api.system_router import router as system_router
from app.api.engine_router import router as engine_router
from app.api.strategy_router import router as strategy_router
from app.api.manager_router import router as manager_router, legacy_markets_router, legacy_market_router, legacy_longhold_router
from app.api.ui_router import router as ui_router
from app.api.ladder_router import router as ladder_router
from app.api.reserved_router import router as reserved_router
from app.api.ai_router import router as ai_router
from app.api.websocket_router import router as websocket_router
from app.api.websocket_router import stop_broadcast_task
from app.api.quick_trade_router import router as quick_trade_router
from app.api.performance_router import router as performance_router
from app.api.portfolio_risk_router import router as portfolio_risk_router
from app.api.triage_router import router as triage_router
from app.api.smart_alerts_router import router as smart_alerts_router
from app.api.backtest_router import router as backtest_router
from app.api.market_signals_router import router as market_signals_router
from app.api.recommend_router import router as recommend_router
from app.api.am_performance_router import router as am_performance_router
from app.api.strategy_focus_router import router as focus_router
from app.api.upbit_gazua_router import router as upbit_gazua_router
from app.api.bithumb_gazua_router import router as bithumb_gazua_router
from app.api.bybit_spot_gazua_router import router as bybit_spot_gazua_router
from app.api.binance_spot_gazua_router import router as binance_spot_gazua_router
from app.api.binance_futures_router import router as binance_futures_router
from app.api.spot_gazua_cross_router import router as spot_gazua_cross_router
from app.api.strategy_harpoon_router import router as harpoon_router
from app.api.news_sentiment_router import router as news_sentiment_router
from app.api.peer_brief_router import router as peer_brief_router
from app.core.constants import env_bool, env_int


async def _recommend_snapshot_worker(system: HyperSystem) -> None:
    from app.api.recommend_router import compute_snapshot, _parse_basis_kst, KST

    enabled = env_bool("OMA_RESERVED_SNAPSHOT_ENABLED", default=True)
    if not enabled:
        print("[SNAPSHOT] Reserved snapshot scheduler disabled")
        return

    basis_kst = os.getenv("OMA_RESERVED_SNAPSHOT_BASIS_KST", "07:00").strip() or "07:00"
    strategies_csv = os.getenv("OMA_RESERVED_SNAPSHOT_STRATEGIES", "").strip()
    n = env_int("OMA_RESERVED_SNAPSHOT_N", default=5)

    try:
        while True:
            try:
                hour, minute, basis_norm = _parse_basis_kst(basis_kst)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[MAIN] Invalid basis_kst=%r: %s", basis_kst, exc, exc_info=True)
                await asyncio.sleep(3600)
                continue

            now_kst = datetime.now(KST)
            target = now_kst.replace(hour=hour, minute=minute, second=5, microsecond=0)
            if target <= now_kst:
                target += timedelta(days=1)

            wait_sec = max(1.0, (target - now_kst).total_seconds())
            await asyncio.sleep(wait_sec)

            try:
                compute_snapshot(
                    system,
                    basis_kst=basis_norm,
                    n=n,
                    strategies=strategies_csv or None,
                    force=True,
                    now_kst=datetime.now(KST),
                )
                print(f"[SNAPSHOT] Saved reserved snapshot {basis_norm} (n={n})")
            except (OSError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                logger.warning("[SNAPSHOT] Snapshot failed: %s", exc, exc_info=True)
                await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("[SNAPSHOT] shutdown")
        return


def _boot_auto_start_engine(system: HyperSystem) -> bool:
    """Start the engine status on boot only when the persisted toggle is enabled."""
    if not bool(getattr(system, "auto_engine_start", False)):
        print("[BOOT] Auto Engine Start: disabled")
        return False

    try:
        coordinator = getattr(system, "coordinator", None)
        engine = getattr(coordinator, "engine", None)
        status = getattr(engine, "status", None)
        if status is None or not hasattr(status, "start"):
            print("[BOOT] Auto Engine Start skipped: engine unavailable")
            return False

        status.start()
        try:
            system.ledger.append("AUTO_ENGINE_START", reason="auto_engine_start_on_boot")
        except (AttributeError, TypeError) as exc:
            logger.warning("[MAIN] main._boot_auto_start_engine fallback: %s", exc, exc_info=True)
        print("[BOOT] Auto Engine Start: enabled")
        return True
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[BOOT] Auto Engine Start failed: %s", exc, exc_info=True)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    import time
    # [PERF] GC 튜닝: gen0 임계값 올려서 GC 빈도 감소 → tick 스파이크 완화
    import gc
    gc.set_threshold(50000, 30, 20)  # 기본 (700, 10, 10) → 훨씬 덜 자주 GC

    # [2026-03-30] default executor 스레드 상한 — to_thread 스레드 무한 증가 방지
    import concurrent.futures
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="asyncio_default"))

    # [2026-02-04] Runtime 상태 검증 및 자동 수정
    try:
        from app.core.runtime_validator import validate_on_startup
        validation_result = validate_on_startup(auto_fix=True)
        if validation_result["issues"]:
            print(f"[BOOT] Runtime validation: {len(validation_result['issues'])} issues found")
        if validation_result["fixes"]:
            print(f"[BOOT] Runtime validation: {len(validation_result['fixes'])} fixes applied")
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[BOOT] Runtime validation failed: %s", exc, exc_info=True)
    
    # [2026-04-09 보안] 인증 미설정 시 강력 경고
    _du = os.getenv("DASHBOARD_USER", "").strip()
    _dp = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not _du or not _dp:
        logger.critical("=" * 60)
        logger.critical("[SECURITY] DASHBOARD_USER/PASSWORD not set!")
        logger.critical("[SECURITY] ALL API endpoints are UNPROTECTED.")
        logger.critical("[SECURITY] Set DASHBOARD_USER and DASHBOARD_PASSWORD in .env")
        logger.critical("=" * 60)

    # ★ 3-3: 테스트 환경에서는 HyperSystem 생성 + 백그라운드 루프 건너뜀
    if os.getenv("AUTOCOIN_TESTING") == "1":
        logger.info("[BOOT] AUTOCOIN_TESTING=1 — skipping HyperSystem and background loops")
        from unittest.mock import MagicMock
        mock_sys = MagicMock()
        # ★ iterable 반환 메서드는 빈 리스트로 — MagicMock __iter__ 무한루프 방지
        mock_sys.get_markets.return_value = []
        mock_sys.ledger.tail_records.return_value = []
        mock_sys.focus_manager = MagicMock()
        mock_sys.harpoon_manager = MagicMock()
        app.state.system = mock_sys
        yield
        return

    system = HyperSystem()
    app.state.system = system

    # 1) 시스템 start
    await system.start()

    # 2) 원장 이벤트 기반 RECOVERY Reactor start (최소 침습)
    reactor = LedgerRecoveryReactor(system)
    app.state.ledger_recovery_reactor = reactor
    await reactor.start()

    # [2026-02-02] Auto Engine Start on Boot
    _boot_auto_start_engine(system)

    # [2026-03-30] 이벤트 루프 하트비트 진단 (hang 감지용)
    async def _heartbeat():
        import threading
        _hb_count = 0
        while True:
            await asyncio.sleep(30)
            _hb_count += 1
            print(f"[HEARTBEAT] #{_hb_count} threads={threading.active_count()}")
    app.state._heartbeat_task = asyncio.create_task(_heartbeat())

    # [2026-02-10] Reserved Snapshot Scheduler (KST)
    try:
        app.state.recommend_snapshot_task = asyncio.create_task(_recommend_snapshot_worker(system))
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[BOOT] Snapshot scheduler failed: %s", exc, exc_info=True)

    # [2026-06-05] Peer Brief polling — 옆 서버 가드
    try:
        from app.core.peer_brief import start_poll_loop as _peer_start
        app.state.peer_brief_task = _peer_start()
    except (KeyError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[BOOT] Peer Brief poll loop failed: %s", exc, exc_info=True)
    
    # [2026-02-03] Server Startup Telegram 알림
    try:
        from app.notify.telegram import send_telegram
        import socket
        hostname = socket.gethostname()
        mode = "LIVE" if getattr(system, "is_live", False) else "DRY"
        send_telegram(
            f"🚀 [Autocoin Server Started]\n"
            f"Mode: {mode}\n"
            f"Host: {hostname}\n"
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            cooldown_key=None  # 서버 시작은 항상 알림
        )
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[MAIN] [2026-02-03] Server Startup Telegram 알림: %s", exc, exc_info=True)

    yield

    # shutdown: websocket -> reactor -> system 순
    try:
        await stop_broadcast_task()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[MAIN] shutdown: websocket -> reactor -> system 순: %s", exc, exc_info=True)

    try:
        task = getattr(app.state, "recommend_snapshot_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # normal shutdown
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[MAIN] shutdown: websocket -> reactor -> system 순: %s", exc, exc_info=True)

    try:
        await reactor.stop()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("[MAIN] shutdown: websocket -> reactor -> system 순: %s", exc, exc_info=True)

    await system.stop()


app = FastAPI(
    title="Autocoin OS v3-H",
    description="""
## 암호화폐 자동매매 시스템 API

Autocoin OS v3-H는 Bybit 기반 자동매매 시스템입니다.

### 주요 기능
- **시스템 관리**: 시스템 상태 조회, 비상 정지/재개
- **엔진 제어**: 매매 엔진 시작/정지, 수동 주문
- **전략 관리**: PINGPONG, AUTOLOOP, LADDER 등 전략 설정
- **마켓 관리**: OMA 마켓 등록/해제, 예산 설정
- **예약 관리**: 후보 마켓 조회, 자동 승인 설정

### 대시보드
- `/ui/dashboard_v2.html` - 메인 대시보드 (V2)
- `/ui/market_detail.html` - 마켓 상세
    """,
    version="3.0.0",
    docs_url="/docs" if os.getenv("ENABLE_API_DOCS") == "1" else None,
    redoc_url="/redoc" if os.getenv("ENABLE_API_DOCS") == "1" else None,
    openapi_url="/openapi.json" if os.getenv("ENABLE_API_DOCS") == "1" else None,
    openapi_tags=[
        {"name": "system", "description": "시스템 상태 및 제어"},
        {"name": "engine", "description": "매매 엔진 제어"},
        {"name": "strategy", "description": "전략 조회 및 설정"},
        {"name": "manager", "description": "마켓 관리 (OMA)"},
        {"name": "reserved", "description": "예약 후보 관리"},
        {"name": "ladder", "description": "래더 전략 관리"},
        {"name": "ai", "description": "AI 분석 및 학습"},
    ],
    lifespan=lifespan
)

# ✅ GZip 압축 — 외부/모바일 접속 응답 속도 개선 (1KB 이상 응답 자동 압축)
app.add_middleware(GZipMiddleware, minimum_size=1024)

# ✅ 스캐너/백업파일 탐색 차단 미들웨어
# 한국 IP 대역 (KT/SKT/LGU+/KORNET 주요 대역)
_KR_IP_PREFIXES = (
    "1.11.", "1.176.", "1.177.", "1.178.", "1.179.",
    "14.32.", "14.33.", "14.34.", "14.35.", "14.36.", "14.37.",
    "27.96.", "27.115.", "27.116.", "27.117.",
    "39.7.", "49.1.", "49.142.", "49.143.", "49.144.", "49.145.",
    "58.120.", "58.121.", "58.122.", "58.123.", "58.124.", "58.125.",
    "59.1.", "59.2.", "59.3.", "59.4.", "59.5.", "59.6.", "59.7.",
    "61.32.", "61.33.", "61.34.", "61.35.", "61.36.", "61.37.", "61.38.", "61.39.",
    "61.72.", "61.73.", "61.74.", "61.75.", "61.76.", "61.77.", "61.78.", "61.79.",
    "110.0.", "110.10.", "110.11.", "110.12.", "110.13.", "110.14.", "110.15.",
    "110.70.", "110.71.",
    "112.148.", "112.149.", "112.150.", "112.151.", "112.152.", "112.153.", "112.154.", "112.217.",
    "114.200.", "114.201.", "114.202.", "114.203.", "114.204.", "114.205.",
    "115.136.", "115.137.", "115.138.", "115.139.", "115.140.", "115.141.",
    "116.36.", "116.37.", "116.38.", "116.39.", "116.40.", "116.41.", "116.42.", "116.43.",
    "118.36.", "118.37.", "118.38.", "118.39.", "118.40.", "118.41.",
    "119.64.", "119.65.", "119.66.", "119.67.",
    "121.128.", "121.129.", "121.130.", "121.131.", "121.132.", "121.133.", "121.134.", "121.135.",
    "121.160.", "121.161.", "121.162.", "121.163.", "121.164.", "121.165.",
    "123.212.", "123.213.", "123.214.", "123.215.",
    "124.49.", "124.50.", "124.51.", "124.52.", "124.53.", "124.54.", "124.55.",
    "125.128.", "125.129.", "125.130.", "125.131.", "125.132.", "125.133.", "125.179.",
    "175.193.", "175.194.", "175.195.", "175.196.", "175.197.", "175.198.", "175.199.",
    "180.64.", "180.65.", "180.66.", "180.67.", "180.68.", "180.69.", "180.70.", "180.71.",
    "182.208.", "182.209.", "182.210.", "182.211.",
    "183.96.", "183.97.", "183.98.", "183.99.", "183.100.", "183.101.", "183.102.", "183.103.",
    "203.226.", "203.227.", "203.228.", "203.229.",
    "210.90.", "210.91.", "210.92.", "210.93.", "210.94.", "210.95.",
    "211.36.", "211.37.", "211.38.", "211.39.", "211.40.", "211.41.", "211.42.", "211.43.",
    "220.64.", "220.65.", "220.66.", "220.67.", "220.68.", "220.69.", "220.70.", "220.71.",
    "221.140.", "221.141.", "221.142.", "221.143.", "221.144.", "221.145.", "221.146.", "221.147.",
    "222.96.", "222.97.", "222.98.", "222.99.", "222.100.", "222.101.",
    # Cloudflare (모든 국가 경유 — 허용) — https://www.cloudflare.com/ips-v4/
    "172.64.", "172.65.", "172.66.", "172.67.", "172.68.", "172.69.", "172.70.", "172.71.",
    "104.16.", "104.17.", "104.18.", "104.19.", "104.20.", "104.21.", "104.22.", "104.23.", "104.24.", "104.25.", "104.26.", "104.27.", "104.28.",
    "141.101.", "162.158.", "162.159.", "188.114.", "190.93.", "197.234.", "198.41.",
    "173.245.", "103.21.", "103.22.", "103.31.", "108.162.", "131.0.72.", "131.0.73.", "131.0.74.", "131.0.75.",
    # 로컬/내부
    "127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
)

class BlockScannerMiddleware(BaseHTTPMiddleware):
    """스캐너·백업파일·해외 직접접속 차단."""
    _BAD_EXT = (".php", ".asp", ".aspx", ".jsp", ".cgi",
                ".7z", ".rar", ".tar", ".gz", ".zip", ".bak", ".backup", ".sql", ".db")
    _BAD_PATH = ("/wp-", "/wordpress", "/xmlrpc", "/phpmyadmin", "/admin.php",
                 "/.env", "/.git", "/shell", "/backdoor", "/webshell", "/cgibin")

    async def dispatch(self, request: Request, call_next):
        # ★ 3-2: 테스트 환경에서 우회
        if os.getenv("AUTOCOIN_TESTING") == "1":
            return await call_next(request)
        # 해외 직접접속 차단 (Cloudflare/KR/로컬 외)
        client_ip = request.client.host if request.client else ""
        if client_ip and not any(client_ip.startswith(p) for p in _KR_IP_PREFIXES):
            return Response(status_code=444)
        # 스캐너 패턴 차단
        path = request.url.path.lower()
        if path.endswith(self._BAD_EXT) or any(p in path for p in self._BAD_PATH):
            return Response(status_code=444)
        return await call_next(request)

app.add_middleware(BlockScannerMiddleware)

# ✅ Basic Auth 미들웨어 적용 (DASHBOARD_USER/PASSWORD 설정 시 활성화)
app.add_middleware(BasicAuthMiddleware)


# ============================================================
# [2026-04-09] Security Headers Middleware
# ============================================================
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """모든 HTTP 응답에 보안 헤더 추가."""
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response

# ✅ UI 정적 파일 서빙 (핵심)
app.mount("/ui", StaticFiles(directory="app/ui"), name="ui")


def _serve_html_no_cache(filepath: str):
    """HTML 파일을 항상 no-cache로 서빙 (배포 즉시 반영)."""
    with open(filepath, encoding="utf-8") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

@app.get("/ui/dashboard_v2.html", include_in_schema=False)
async def dashboard_html():
    """V2 자리 = Upbit 현물 대시보드 (레거시 dashboard_v2.html 은 디스크 보존, 미서빙)."""
    return _serve_html_no_cache("app/ui/dashboard_upbit.html")

@app.get("/ui/dashboard_upbit.html", include_in_schema=False)
async def dashboard_upbit_html():
    """Upbit FOCUS 현물 대시보드 — 항상 no-cache."""
    return _serve_html_no_cache("app/ui/dashboard_upbit.html")

@app.get("/ui/dashboard_upbit_v3.html", include_in_schema=False)
async def dashboard_upbit_v3_html():
    """Upbit FOCUS v3 리본 대시보드 (개편 중 — dashboard_upbit.html 과 병행, 완성 후 교체)."""
    return _serve_html_no_cache("app/ui/dashboard_upbit_v3.html")

@app.get("/ui/dashboard_bithumb_v3.html", include_in_schema=False)
async def dashboard_bithumb_v3_html():
    """Bithumb FOCUS v3 리본 대시보드 (Upbit v3 미러 — 빗썸 전용 API)."""
    return _serve_html_no_cache("app/ui/dashboard_bithumb_v3.html")

@app.get("/ui/dashboard_bybit_spot_v3.html", include_in_schema=False)
async def dashboard_bybit_spot_v3_html():
    """Bybit 현물(USDT) FOCUS v3 리본 대시보드 (Upbit v3 미러 — Bybit 현물 전용 API)."""
    return _serve_html_no_cache("app/ui/dashboard_bybit_spot_v3.html")

@app.get("/ui/dashboard_binance_spot_v3.html", include_in_schema=False)
async def dashboard_binance_spot_v3_html():
    """Binance 현물(USDT) FOCUS v3 리본 대시보드 (Bybit 현물 v3 미러 — Binance 현물 전용 API)."""
    return _serve_html_no_cache("app/ui/dashboard_binance_spot_v3.html")

@app.get("/ui/focus.html", include_in_schema=False)
async def focus_html():
    """FOCUS 대시보드 HTML — 항상 no-cache."""
    return _serve_html_no_cache("app/ui/focus.html")

@app.get("/ui/js/dashboard_v2.js", include_in_schema=False)
async def dashboard_js():
    """대시보드 JS — 항상 no-cache (배포 즉시 반영)."""
    with open("app/ui/js/dashboard_v2.js", encoding="utf-8") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/")
async def root():
    """루트 경로 → V3 대시보드로 리다이렉트 (★2026-06-02 부모님: v3 기본 통일, 헷갈림 방지)."""
    return RedirectResponse(url="/ui/dashboard_v3.html")


@app.get("/auth/login")
async def auth_login(request: Request):
    """브라우저용 로그인 폼 (세션 쿠키 발급)."""
    user = os.getenv("DASHBOARD_USER", "").strip()
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    next_target = str(request.query_params.get("next") or "/ui/dashboard_v3.html")
    if not next_target.startswith("/") or next_target.startswith("//"):
        next_target = "/ui/dashboard_v3.html"

    if not user or not password:
        return RedirectResponse(url=next_target)

    html = f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Autocoin Login</title>
  <style>
    body {{ font-family: Arial, sans-serif; background: #0b1220; color: #e6edf3; margin: 0; }}
    .wrap {{ max-width: 420px; margin: 10vh auto; padding: 24px; background: #101a2d; border-radius: 12px; }}
    h1 {{ margin: 0 0 16px; font-size: 20px; }}
    input {{ width: 100%; padding: 10px; margin: 8px 0; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #e6edf3; }}
    button {{ width: 100%; padding: 10px; margin-top: 10px; border: 0; border-radius: 8px; background: #2563eb; color: white; cursor: pointer; }}
    .err {{ color: #ef4444; min-height: 20px; margin-top: 10px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Autocoin 로그인</h1>
    <input id="u" placeholder="아이디" autocomplete="username" />
    <input id="p" type="password" placeholder="비밀번호" autocomplete="current-password" />
    <button id="btn">로그인</button>
    <div id="err" class="err"></div>
  </div>
  <script>
    const next = {next_target!r};
    const btn = document.getElementById('btn');
    const err = document.getElementById('err');
    async function login() {{
      err.textContent = '';
      const username = document.getElementById('u').value || '';
      const password = document.getElementById('p').value || '';
      try {{
        const res = await fetch('/auth/login-submit', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          credentials: 'include',
          body: JSON.stringify({{ username, password, next }})
        }});
        const data = await res.json();
        if (!res.ok || !data.ok) {{
          err.textContent = (data && data.error) ? data.error : '로그인 실패';
          return;
        }}
        window.location.href = data.redirect || '/ui/dashboard_v3.html';
      }} catch (e) {{
        err.textContent = '로그인 요청 실패';
      }}
    }}
    btn.addEventListener('click', login);
    document.addEventListener('keydown', (e) => {{ if (e.key === 'Enter') login(); }});
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html, status_code=200)


@app.post("/auth/login-submit")
async def auth_login_submit(request: Request):
    """로그인 처리 후 세션 쿠키 발급."""
    client_ip = _get_request_ip(request)
    blocked, remain_sec = _auth_is_blocked(client_ip)
    if blocked:
        return JSONResponse(
            {"ok": False, "error": f"로그인 시도 제한. {remain_sec}초 후 다시 시도하세요."},
            status_code=429,
        )

    user = os.getenv("DASHBOARD_USER", "").strip()
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not user or not password:
        return JSONResponse({"ok": False, "error": "auth_not_configured"}, status_code=400)

    try:
        body = await request.json()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
        logger.warning("[Auth] request JSON parse failed", exc_info=True)
        body = {}
    req_user = str((body or {}).get("username") or "")
    req_pass = str((body or {}).get("password") or "")
    next_target = str((body or {}).get("next") or "/ui/dashboard_v3.html")
    if not next_target.startswith("/") or next_target.startswith("//"):
        next_target = "/ui/dashboard_v3.html"

    if not (secrets.compare_digest(req_user, user) and secrets.compare_digest(req_pass, password)):
        _auth_register_failure(client_ip)
        return JSONResponse({"ok": False, "error": "아이디/비밀번호 불일치"}, status_code=401)

    _invalidate_sessions_if_password_changed()
    new_token = _generate_session_token(user, password)
    _AUTH_SESSIONS.add(new_token)
    _auth_clear_failures(client_ip)
    response = JSONResponse({"ok": True, "redirect": next_target}, status_code=200)
    response.set_cookie(
        key="autocoin_session",
        value=new_token,
        httponly=True,
        samesite="lax",
        max_age=86400 * 7,
    )
    return response


@app.get("/auth/logout")
async def auth_logout(request: Request):
    """세션 쿠키 삭제."""
    token = request.cookies.get("autocoin_session")
    if token and token in _AUTH_SESSIONS:
        try:
            _AUTH_SESSIONS.discard(token)
        except (AttributeError, TypeError) as exc:
            logger.warning("[MAIN] main.auth_logout fallback: %s", exc, exc_info=True)
    response = RedirectResponse(url="/auth/login", status_code=307)
    response.delete_cookie("autocoin_session")
    return response


@app.get("/health")
async def health_check():
    """헬스체크 엔드포인트 - 서버 상태, 마지막 거래 시간, Autopilot 상태 체크."""
    import time
    
    now = time.time()
    system = getattr(app.state, "system", None)
    
    # 기본 상태
    status = {
        "ok": True,
        "timestamp": now,
        "uptime_sec": 0,
        "engine_status": "unknown",
        "autopilot_enabled": False,
        "autopilot_last_run": 0,
        "autopilot_idle_sec": 0,
        "last_fill_ts": 0,
        "last_fill_idle_sec": 0,
        "active_markets": 0,
        "warnings": [],
    }
    
    if not system:
        status["ok"] = False
        status["warnings"].append("system_not_initialized")
        return status
    
    # 엔진 상태
    try:
        engine_status = getattr(system.coordinator.engine.status, "state", "unknown")
        status["engine_status"] = str(engine_status)
    except (KeyError, AttributeError, TypeError):
        logger.warning("[Health] engine_status check failed", exc_info=True)
        status["warnings"].append("engine_status_unavailable")
    
    # Autopilot 상태
    try:
        autopilot_mgr = getattr(system, "autopilot_manager", None)
        status["autopilot_enabled"] = bool(getattr(system, "autopilot_enabled", False))
        status["autopilot_last_run"] = float(getattr(autopilot_mgr, "last_run_ts", 0) or 0) if autopilot_mgr is not None else 0.0
        status["autopilot_idle_sec"] = int(now - status["autopilot_last_run"]) if status["autopilot_last_run"] > 0 else 0
        
        # Autopilot 10분 이상 idle 경고
        if status["autopilot_enabled"] and status["autopilot_idle_sec"] > 600:
            status["warnings"].append(f"autopilot_idle_{status['autopilot_idle_sec']}sec")
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[Health] autopilot_status check failed", exc_info=True)
        status["warnings"].append("autopilot_status_unavailable")
    
    # 마지막 거래 시간 (FILL 이벤트)
    try:
        records = system.ledger.tail_records(since_ts=now - 3600, tail_lines=1000)
        last_fill = 0.0
        for rec in reversed(records):
            ev = str(rec.get("event") or "")
            if ev in ("FILL_BUY", "FILL_SELL"):
                last_fill = float(rec.get("ts") or 0.0)
                break
        status["last_fill_ts"] = last_fill
        status["last_fill_idle_sec"] = int(now - last_fill) if last_fill > 0 else 0
        
        # 30분 이상 거래 없으면 경고
        if status["engine_status"] == "RUNNING" and status["last_fill_idle_sec"] > 1800:
            status["warnings"].append(f"no_trades_{status['last_fill_idle_sec']}sec")
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[Health] last_fill check failed", exc_info=True)
        status["warnings"].append("last_fill_unavailable")
    
    # Active 마켓 수
    try:
        snap = system.oma_registry.snapshot()
        status["active_markets"] = len(snap.get("active") or [])
        
        # Active 마켓 0개면 경고
        if status["engine_status"] == "RUNNING" and status["active_markets"] == 0:
            status["warnings"].append("no_active_markets")
    except (KeyError, AttributeError, TypeError):
        logger.warning("[Health] active_markets check failed", exc_info=True)
        status["warnings"].append("active_markets_unavailable")
    
    # 경고가 있으면 ok=False
    if status["warnings"]:
        status["ok"] = False
    
    return status

# Routers
app.include_router(system_router)
app.include_router(engine_router)
app.include_router(strategy_router)
app.include_router(manager_router)
app.include_router(ui_router)
app.include_router(ladder_router)
app.include_router(reserved_router)
app.include_router(ai_router)
app.include_router(websocket_router)
app.include_router(quick_trade_router)
app.include_router(performance_router)
app.include_router(portfolio_risk_router)
app.include_router(triage_router)
app.include_router(smart_alerts_router)
app.include_router(backtest_router)
app.include_router(market_signals_router)
app.include_router(recommend_router)
app.include_router(am_performance_router)
app.include_router(legacy_markets_router)
app.include_router(legacy_market_router)
app.include_router(legacy_longhold_router)
app.include_router(focus_router)
app.include_router(upbit_gazua_router)
app.include_router(bithumb_gazua_router)
app.include_router(bybit_spot_gazua_router)
app.include_router(binance_spot_gazua_router)
app.include_router(binance_futures_router)
app.include_router(spot_gazua_cross_router)
app.include_router(harpoon_router)
app.include_router(news_sentiment_router)
app.include_router(peer_brief_router)
