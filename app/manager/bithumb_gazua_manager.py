# ============================================================
# Bithumb spot FOCUS manager — subclasses SpotGazuaManager
# ------------------------------------------------------------
# [2026-06-17 owner] Run one more copy of the same bot on a Bithumb account.
# SpotGazuaManager is exchange-agnostic (depends only on client + state_path),
# so reuse it as-is with a Bithumb client + separate state_path. None of the
# brain logic (scan/entry/exit/scoring/budget/alerts) is duplicated — only the
# client is BithumbTradeClient.
#
# Isolation: state/journal live under runtime/bithumb/ → fully independent
# capital and state from Upbit.
# ============================================================
from __future__ import annotations

import os
from typing import Any, Optional

from app.manager.spot_gazua_manager import SpotGazuaManager


class BithumbGazuaManager(SpotGazuaManager):
    """Bithumb spot long-only FOCUS. Same logic as SpotGazuaManager, only client/state are Bithumb."""

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
        # Journal also uses the Bithumb dir/name (override Upbit default name — capital/record isolation)
        self.journal_path = os.path.join(os.path.dirname(state_path), "bithumb_focus_journal.jsonl")
