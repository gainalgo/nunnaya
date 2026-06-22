# ============================================================
# Bithumb 현물 FOCUS 매니저 — SpotGazuaManager 상속
# ------------------------------------------------------------
# [2026-06-17 부모] 빗썸 계정으로 같은 봇 한 벌 더.
# SpotGazuaManager 는 거래소 무관(client + state_path 만 의존)이라
# 빗썸 client + 별도 state_path 로 그대로 재사용한다. 두뇌(스캔/진입/청산/
# 점수/예산/경고) 로직 일절 복제 안 함 — client 만 BithumbTradeClient.
#
# 격리: state/journal 은 runtime/bithumb/ 로 분리 → Upbit 과 자본·상태 완전 독립.
# ============================================================
from __future__ import annotations

import os
from typing import Any, Optional

from app.manager.spot_gazua_manager import SpotGazuaManager


class BithumbGazuaManager(SpotGazuaManager):
    """빗썸 현물 long-only FOCUS. SpotGazuaManager 와 동일 로직, client/state 만 빗썸."""

    def __init__(self, system: Any = None, client: Any = None, *, state_path: Optional[str] = None):
        if client is None:
            from app.integrations.bithumb_trade import BithumbTradeClient
            client = BithumbTradeClient(
                os.getenv("BITHUMB_ACCESS_KEY", ""), os.getenv("BITHUMB_SECRET_KEY", "")
            )
        if state_path is None:
            try:
                from app.core.runtime_paths import RuntimePaths
                state_path = RuntimePaths(exchange="bithumb").custom("bithumb_focus_config.json")
            except Exception:
                state_path = os.path.join("runtime", "bithumb", "bithumb_focus_config.json")
                os.makedirs(os.path.dirname(state_path), exist_ok=True)
        super().__init__(system=system, client=client, state_path=state_path)
        # 저널도 빗썸 디렉터리·이름으로 (Upbit 기본 이름 덮어쓰기 — 자본/기록 격리)
        self.journal_path = os.path.join(os.path.dirname(state_path), "bithumb_focus_journal.jsonl")
