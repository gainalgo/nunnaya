"""
중앙 로깅 설정 모듈.

환경 변수(LOG_LEVEL, LOG_FORMAT)를 읽어 루트 로거를 구성합니다.
표준 포맷 또는 JSON 구조화 로깅을 지원합니다.
앱 시작 시 setup_logging()을 한 번 호출하여 사용합니다.
"""

import json
import logging
import os
from datetime import datetime


def _json_formatter(record: logging.LogRecord) -> str:
    """LogRecord를 JSON 문자열로 직렬화합니다."""
    data = {
        "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
        "level": record.levelname,
        "name": record.name,
        "message": record.getMessage(),
    }
    if record.exc_info:
        data["exception"] = logging.Formatter().formatException(record.exc_info)
    return json.dumps(data, ensure_ascii=False)


class JsonFormatter(logging.Formatter):
    """JSON 구조화 로깅용 포매터."""

    def format(self, record: logging.LogRecord) -> str:
        return _json_formatter(record)


def setup_logging() -> None:
    """
    환경 변수에 따라 루트 로거를 구성합니다.
    앱 시작 시 한 번만 호출합니다.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_format = os.environ.get("LOG_FORMAT", "standard").lower()

    root = logging.getLogger()
    root.setLevel(level)

    # 기존 핸들러 제거 (중복 방지)
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler()
    handler.setLevel(level)

    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))

    root.addHandler(handler)

    # ★ 파일 핸들러 추가 — runtime/focus.log (진단용)
    try:
        log_dir = os.path.join(os.getcwd(), "runtime")
        os.makedirs(log_dir, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(
            os.path.join(log_dir, "focus.log"),
            maxBytes=5_000_000,  # 5MB
            backupCount=2,
            encoding="utf-8",
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        # ★ 2-2: ERROR 이상 무조건 기록 + 주요 네임스페이스 INFO 기록
        _FILE_LOG_NAMESPACES = ("focus", "whale", "harpoon", "triage", "bybit", "recovery", "engine", "hyper")
        _FILE_LOG_TAGS = ("[FOCUS]", "[WHALE", "[HARPOON]", "[TRIAGE", "[BYBIT", "[RECOVERY", "[ENGINE", "[HYPER")

        def _file_log_filter(r: logging.LogRecord) -> bool:
            if r.levelno >= logging.ERROR:
                return True
            name_lower = r.name.lower()
            if any(ns in name_lower for ns in _FILE_LOG_NAMESPACES):
                return True
            msg = r.getMessage()
            if any(tag in msg for tag in _FILE_LOG_TAGS):
                return True
            return False

        fh.addFilter(_file_log_filter)
        root.addHandler(fh)
    except Exception:
        pass  # 파일 못 만들어도 서버 죽이지 않음

    # ★ Windows asyncio ProactorEventLoop 노이즈 억제 (기능 영향 없음)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
