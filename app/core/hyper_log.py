# ============================================================
# File: autocoin/hyper/system/hyper_log.py
# Hyper Log – Unified logging utility (Simple Edition)
# ============================================================

import datetime
import traceback


class HyperLog:
    """
    Common logger used throughout Autocoin.
    Very simple, but kept extensible.
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
