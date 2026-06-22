# ============================================================
# File: app/manager/ledger_recovery_reactor.py
# Autocoin OS v3-H — Ledger Recovery Reactor
# ------------------------------------------------------------
# - OMA_LEDGER_PATH(JSONL)을 tail 하며 이벤트를 읽고
#   RecoveryPolicyEngine에 전달한다.
# - 원장 이벤트 기반으로만 동작하므로 기존 엔진/전략 로직을 크게 건드리지 않는다.
# ============================================================

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

from app.manager.recovery_policy import RecoveryPolicyEngine


class LedgerRecoveryReactor:
    """JSONL ledger를 tail 하여 RECOVERY 정책을 실행한다."""

    def __init__(self, system: Any):
        self.system = system
        self.policy = RecoveryPolicyEngine()

        self.ledger_path = os.getenv("OMA_LEDGER_PATH", "runtime/trade_ledger.jsonl")
        self.offset_path = os.getenv("OMA_LEDGER_OFFSET_PATH", "runtime/ledger_reactor.offset")

        self.poll_sec = float(os.getenv("OMA_LEDGER_REACTOR_POLL_SEC", "0.2"))
        self.tick_sec = float(os.getenv("OMA_LEDGER_REACTOR_POLICY_TICK_SEC", "1.0"))

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

        self._last_policy_tick = 0.0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ledger_recovery_reactor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=3.0)
        except Exception:
            logger.warning("ledger_recovery_reactor stop wait_for failed, cancelling task", exc_info=True)
            self._task.cancel()
        finally:
            self._task = None

    # --------------------------------------------------------
    # Internal
    # --------------------------------------------------------
    def _load_offset(self) -> int:
        try:
            with open(self.offset_path, "r", encoding="utf-8") as f:
                s = f.read().strip()
                return int(s) if s else 0
        except (OSError, TypeError, ValueError):
            logger.warning("[LedgerRecoveryReactor] _load_offset failed", exc_info=True)
            return 0

    def _save_offset(self, offset: int) -> None:
        os.makedirs(os.path.dirname(self.offset_path) or ".", exist_ok=True)
        try:
            with open(self.offset_path, "w", encoding="utf-8") as f:
                f.write(str(int(offset)))
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[LEDGER_REACTOR] _save_offset fallback: %s", exc, exc_info=True)

    def _process_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            record = json.loads(line)
            self.policy.on_ledger_event(self.system, record)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("[LEDGER_REACTOR] _process_line fallback: %s", exc, exc_info=True)

    def _process_rotated_tail(self, offset: int) -> None:
        """파일 회전 감지 시, 직전 백업 파일의 나머지 부분을 처리한다."""
        d = os.path.dirname(self.ledger_path) or "."
        base = os.path.basename(self.ledger_path)
        try:
            # 가장 최근 백업 파일 찾기
            baks = [os.path.join(d, fn) for fn in os.listdir(d) if fn.startswith(base + ".") and fn.endswith(".bak")]
            if not baks:
                return
            latest_bak = max(baks, key=os.path.getmtime)
            
            with open(latest_bak, "r", encoding="utf-8") as f:
                f.seek(offset)
                for line in f:
                    self._process_line(line)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[LEDGER_REACTOR] rotated tail backup scan: %s", exc, exc_info=True)

    async def _run(self) -> None:
        # 기본 동작: 오프셋 파일이 없으면 "현재 파일 끝"에서 시작(과거 이벤트 재처리 방지)
        offset = self._load_offset()
        if offset == 0:
            try:
                if os.path.exists(self.ledger_path):
                    offset = os.path.getsize(self.ledger_path)
            except (OSError, TypeError, ValueError):
                logger.warning("[LedgerRecoveryReactor] getsize failed for ledger", exc_info=True)
                offset = 0

        while not self._stop.is_set():
            now = time.time()

            # 정책의 시간 기반 조건(conditional max_hold/stoploss 등) 정기 평가
            if now - self._last_policy_tick >= self.tick_sec:
                try:
                    self.policy.periodic(self.system)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[LEDGER_REACTOR] periodic policy eval: %s", exc, exc_info=True)
                self._last_policy_tick = now

            try:
                if not os.path.exists(self.ledger_path):
                    await asyncio.sleep(self.poll_sec)
                    continue

                # rotation / truncate 대비
                try:
                    size = os.path.getsize(self.ledger_path)
                    if size < offset:
                        # Rotation detected: finish reading the old file first
                        self._process_rotated_tail(offset)
                        offset = 0
                except (OSError, TypeError, ValueError) as exc:
                    logger.warning("[LEDGER_REACTOR] rotation detect: %s", exc, exc_info=True)

                with open(self.ledger_path, "r", encoding="utf-8") as f:
                    f.seek(offset)
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        # Incomplete line check (tailing race condition)
                        if not line.endswith("\n"):
                            break

                        offset = f.tell()
                        self._process_line(line)

                self._save_offset(offset)

            except (OSError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[LEDGER_REACTOR] tail read: %s", exc, exc_info=True)

            await asyncio.sleep(self.poll_sec)
