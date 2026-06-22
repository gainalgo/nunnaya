"""GateLedger — 진입 게이트 통과/거절 집계 장부 (해결안 B, master 설계도 §9.2-B / F3).

부모님 북극성 "봇=차림, 사람=수확"(INV-6)은 *후보 가시화*가 핵심인데, 지금은
"차린 것(near-miss)"만 보이고 "왜 못 차렸나(게이트 병목)"가 안 보인다. 이 장부가
그 빈칸을 채운다 — 진입 게이트가 reject 할 때 던지는 reason 을 코인×게이트 매트릭스로
*세기만* 한다(새 판단 0). _record_skip / _record_near_miss 가 이미 만든 reason 을 받아
in-memory 카운터로 누적, 주기적으로 runtime/focus_gate_stats.json 에 영속.

★ 진입 로직 1바이트도 안 건드린다(관측만). record() 의 모든 예외는 삼켜서 진입 흐름에
  절대 전파되지 않는다(INV-2 기존 불침). default OFF — FOCUS_GATE_LEDGER_ENABLED 로만 켬.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from typing import Dict


def _atomic_write_json(path: str, data: dict) -> None:
    """tmp 쓰고 os.replace 로 교체 = 원자적(app.core.io_utils.safe_write_json 과 동일 패턴).
    ★ 자체 구현 — 무거운 app.core import 체인(fastapi 등)에 의존 안 함(관측 도구가 본체를 깨면 안 됨)."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# 방어적 상한 — 코드의 reason/코인 집합은 유한하지만 이상 입력에 대비해 카디널리티 캡
_MAX_GATES = 300
_MAX_MARKETS_PER_GATE = 50


class GateLedger:
    """틱당 in-memory 카운터 + 주기 flush. record() 가 lazy flush 를 내장하므로
    호출자는 결선 지점에서 record() 1줄만 부르면 된다(별도 tick 훅 불필요)."""

    def __init__(self, flush_path: str = "runtime/focus_gate_stats.json",
                 flush_sec: float = 60.0) -> None:
        self.flush_path = flush_path
        self.flush_sec = max(5.0, float(flush_sec or 60.0))
        self._date: str = self._today()
        # {gate: {"pass": int, "reject": int, "markets": Counter}}
        self._gates: Dict[str, Dict] = {}
        self._total_scanned: int = 0
        self._last_flush: float = 0.0

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _roll_if_new_day(self) -> None:
        today = self._today()
        if today != self._date:
            self._date = today
            self._gates = {}
            self._total_scanned = 0

    def record(self, market: str, gate: str, passed: bool, direction: str = "-",
               detail: str = "") -> None:
        """진입 게이트 1건 통과/거절 집계. 기존 reject/entry 지점에서 1줄 호출.
        예외는 절대 전파 금지 — 관측이 진입 흐름을 깨면 안 됨."""
        try:
            self._roll_if_new_day()
            g = str(gate or "-").strip()[:80] or "-"
            mkt = str(market or "-").strip().upper()[:20] or "-"
            slot = self._gates.get(g)
            if slot is None:
                if len(self._gates) >= _MAX_GATES:
                    return  # 카디널리티 폭주 방어 — 새 게이트 무시(기존 집계는 계속)
                slot = {"pass": 0, "reject": 0, "markets": Counter()}
                self._gates[g] = slot
            if passed:
                slot["pass"] += 1
            else:
                slot["reject"] += 1
            mc: Counter = slot["markets"]
            if mkt in mc or len(mc) < _MAX_MARKETS_PER_GATE:
                mc[mkt] += 1
            self._total_scanned += 1
            self.maybe_flush(time.time())
        except Exception:  # noqa: BLE001 — 관측 실패는 무해, 진입 불침
            pass

    def snapshot(self) -> dict:
        """peer_brief / UI 노출용. {"date", "gates": {<gate>: {pass, reject, top_markets}}, "total_scanned"}."""
        try:
            gates_out: Dict[str, Dict] = {}
            for g, slot in self._gates.items():
                mc: Counter = slot["markets"]
                gates_out[g] = {
                    "pass": int(slot["pass"]),
                    "reject": int(slot["reject"]),
                    "top_markets": [m for m, _ in mc.most_common(5)],
                }
            return {"date": self._date, "gates": gates_out, "total_scanned": int(self._total_scanned)}
        except Exception:  # noqa: BLE001
            return {"date": self._date, "gates": {}, "total_scanned": 0}

    def maybe_flush(self, now: float) -> None:
        """flush_sec 경과 시 원자적 쓰기로 영속. 실패해도 무해."""
        try:
            if (now - self._last_flush) < self.flush_sec:
                return
            self._last_flush = now
            _atomic_write_json(self.flush_path, self.snapshot())
        except Exception:  # noqa: BLE001
            pass
