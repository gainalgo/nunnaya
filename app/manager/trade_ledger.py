# ============================================================
# File: app/manager/trade_ledger.py
# Autocoin OS v3-H — Trade Ledger (JSONL, Append-Only)
# ------------------------------------------------------------
# - 한 줄 = 한 JSON(record)
# - append-only (수정/삭제 금지)
# - 크래시/재부팅 이후 '무슨 일이 있었는가'를 복원할 근거
# ============================================================

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LedgerConfig:
    path: str = "runtime/trade_ledger.jsonl"
    max_bytes: int = 10 * 1024 * 1024
    keep: int = 10


class TradeLedger:
    def __init__(
        self,
        *,
        path: Optional[str] = None,
        max_bytes: Optional[int] = None,
        keep: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> None:
        cfg = LedgerConfig()
        self._path = str(path or cfg.path)
        self._max_bytes = int(max_bytes if max_bytes is not None else cfg.max_bytes)
        self._keep = int(keep if keep is not None else cfg.keep)

        # LEDGER_BAK_DIR 환경변수로 bak 저장 위치 분리 가능 (기본: 원장과 같은 디렉토리)
        _bak_env = os.environ.get("LEDGER_BAK_DIR", "").strip()
        self._bak_dir: Optional[str] = _bak_env if _bak_env else None

        self.run_id = run_id or str(uuid.uuid4())
        self.host = socket.gethostname()

        self._lock = Lock()

        d = os.path.dirname(self._path)
        if d:
            os.makedirs(d, exist_ok=True)
        if self._bak_dir:
            os.makedirs(self._bak_dir, exist_ok=True)

    @property
    def path(self) -> str:
        return self._path

    # --------------------------------------------------------
    # Core: append record
    # --------------------------------------------------------
    def append(
        self,
        event: str,
        *,
        market: Optional[str] = None,
        level: str = "INFO",
        **data: Any,
    ) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "ts": time.time(),
            "event": str(event),
            "level": str(level).upper(),
            "run_id": self.run_id,
            "host": self.host,
        }
        if market:
            rec["market"] = str(market)
        if data:
            rec["data"] = data

        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))

        with self._lock:
            self._rotate_if_needed()
            try:
                self._append_line(line)
            except PermissionError:
                # Fallback to a per-run ledger if the default file is locked/denied.
                self._switch_to_fallback_path()
                try:
                    self._append_line(line)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    # Ledger is best-effort; never break trading path.
                    logger.warning("[LEDGER] Fallback to a per-run ledger if the default file is locked/denied: %s", exc, exc_info=True)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                # Ledger is best-effort; never break trading path.
                logger.warning("[LEDGER] Ledger is best-effort; never break trading path: %s", exc, exc_info=True)

        return rec

    def _append_line(self, line: str) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _switch_to_fallback_path(self) -> None:
        base = os.path.basename(self._path)
        d = os.path.dirname(self._path) or "."
        root, ext = os.path.splitext(base)
        if self.run_id in base:
            return
        self._path = os.path.join(d, f"{root}.{self.run_id}{ext or '.jsonl'}")

    # --------------------------------------------------------
    # Rotation
    # --------------------------------------------------------
    def _bak_path(self, ts: str) -> str:
        """bak 파일 경로: LEDGER_BAK_DIR 설정 시 해당 디렉토리, 아니면 원장과 같은 위치."""
        base = os.path.basename(self._path)
        bak_name = f"{base}.{ts}.bak"
        d = self._bak_dir if self._bak_dir else (os.path.dirname(self._path) or ".")
        return os.path.join(d, bak_name)

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.exists(self._path) and os.path.getsize(self._path) >= self._max_bytes:
                ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                bak = self._bak_path(ts)
                if self._bak_dir:
                    os.makedirs(self._bak_dir, exist_ok=True)
                os.replace(self._path, bak)
                self._cleanup_old_backups()
        except (OSError, TypeError, ValueError) as exc:
            # 원장 로깅은 trading path를 죽이지 않도록 'best-effort'
            logger.warning("[LEDGER] _rotate_if_needed: %s", exc, exc_info=True)
            return

    def _cleanup_old_backups(self) -> None:
        try:
            base = os.path.basename(self._path)
            d = self._bak_dir if self._bak_dir else (os.path.dirname(self._path) or ".")
            files = [
                os.path.join(d, fn)
                for fn in os.listdir(d)
                if fn.startswith(base + ".") and fn.endswith(".bak")
            ]
            files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for p in files[self._keep :]:
                try:
                    os.remove(p)
                except (OSError, TypeError, ValueError) as exc:
                    logger.warning("[LEDGER] _cleanup_old_backups fallback: %s", exc, exc_info=True)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[LEDGER] _cleanup_old_backups: %s", exc, exc_info=True)
            return

    # --------------------------------------------------------
    # Read helpers
    # --------------------------------------------------------

    def tail_records(self, since_ts: float, tail_lines: int = 20000) -> List[Dict[str, Any]]:
        """Return recent ledger records filtered by ts >= since_ts.

        Scans current file AND recent backups to ensure continuity across rotations.
        """
        since_ts = float(since_ts or 0.0)
        tail_lines = max(100, int(tail_lines or 20000))

        # 1. Collect files: current + backups (newest first)
        files = [self._path]
        base = os.path.basename(self._path)
        # bak 탐색 디렉토리: LEDGER_BAK_DIR 설정 시 해당 디렉토리도 포함
        bak_dirs = set()
        bak_dirs.add(os.path.dirname(self._path) or ".")
        if self._bak_dir:
            bak_dirs.add(self._bak_dir)
        try:
            baks = []
            for d in bak_dirs:
                baks += [os.path.join(d, fn) for fn in os.listdir(d) if fn.startswith(base + ".") and fn.endswith(".bak")]
            baks.sort(reverse=True)  # Newest backups first
            files.extend(baks)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[LEDGER] bak dir scan: %s", exc, exc_info=True)

        out: List[Dict[str, Any]] = []
        count = 0

        for fp in files:
            if count >= tail_lines:
                break
            
            lines = self._read_lines_safe(fp)
            # Process in reverse (newest lines first) to find relevant window
            chunk = []
            for line in reversed(lines):
                if count >= tail_lines:
                    break
                try:
                    rec = json.loads(line)
                    ts = float(rec.get("ts", 0.0) or 0.0)
                    if ts < since_ts:
                        continue
                    chunk.append(rec)
                    count += 1
                except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[LEDGER] tail_records parse: %s", exc, exc_info=True)
                    continue
            
            # Prepend this file's chunk to result (to maintain chronological order)
            out = list(reversed(chunk)) + out

        return out

    def _read_lines_safe(self, path: str) -> List[str]:
        """Safely read lines from a file (best-effort)."""
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().splitlines()
        except (OSError, TypeError, ValueError):
            logger.warning("[TradeLedger] _read_lines_safe: failed to read %s", path, exc_info=True)
            return []

    def tail(self, n: int = 200) -> List[Dict[str, Any]]:
        n = max(1, int(n))
        if not os.path.exists(self._path):
            return []

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, TypeError, ValueError):
            logger.warning("[TradeLedger] tail: failed to read ledger", exc_info=True)
            return []

        out: List[Dict[str, Any]] = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning("[LEDGER] tail parse: %s", exc, exc_info=True)
                continue
        return out
