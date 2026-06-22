# ============================================================
# File: autocoin/hyper/system/hyper_log.py
# Hyper Log – 통합 로깅 유틸리티 (Simple Edition)
# ============================================================

import datetime
import traceback


class HyperLog:
    """
    Autocoin 전체에서 사용할 공통 Logger.
    매우 단순하지만 확장 가능하도록 유지.
    """

    @staticmethod
    def info(msg: str):
        print(f"[INFO  {HyperLog._ts()}] {msg}")

    @staticmethod
    def warn(msg: str):
        print(f"[WARN  {HyperLog._ts()}] {msg}")

    @staticmethod
    def error(msg: str, exc: Exception | None = None):
        print(f"[ERROR {HyperLog._ts()}] {msg}")
        if exc:
            traceback.print_exc()

    @staticmethod
    def _ts():
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
