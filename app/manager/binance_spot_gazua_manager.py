# ============================================================
# Binance spot FOCUS manager — inherits SpotGazuaManager
# ------------------------------------------------------------
# [2026-06-23 owner] "Let's build a Binance one too? futures + spot" — spot (USDT) first.
# SpotGazuaManager is exchange-agnostic (depends only on the client interface +
# state_path), so it is reused as-is with a Binance spot client + a separate
# state_path. None of the brain logic (scan/entry/exit/scoring/budget/hold) is
# duplicated — only the client is BinanceSpotTradeClient.
# (Same structure as the Bybit spot BybitSpotGazuaManager — quote=USDT mirror.)
#
# Upbit(KRW) <-> Binance spot(USDT) differences:
#   - symbol 'BTCUSDT' (not KRW-BTC) -> _normalize_market override.
#   - no market_warning (investment caution/alert) -> client.get_market_warnings()={}.
#   - quote=USDT.
#
# Isolation: state/journal live under runtime/binance_spot/ -> capital and state
# are independent from Upbit, Bybit and futures.
# ============================================================
from __future__ import annotations

import os
from typing import Any, Optional

from app.manager.spot_gazua_manager import SpotGazuaManager


class BinanceSpotGazuaManager(SpotGazuaManager):
    """Binance spot (USDT) long-only FOCUS. Same logic as SpotGazuaManager, only client/state are Binance spot."""

    _quote_currency = "USDT"   # ★ quote currency USDT (balance/budget lookup key). Upbit/Bithumb=KRW.

    def __init__(self, system: Any = None, client: Any = None, *, state_path: Optional[str] = None):
        if client is None:
            from app.integrations.binance_spot_trade import BinanceSpotTradeClient
            client = BinanceSpotTradeClient(
                os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_API_SECRET", "")
            )
        if state_path is None:
            try:
                from app.core.runtime_paths import RuntimePaths
                state_path = RuntimePaths(exchange="binance_spot").custom("binance_spot_focus_config.json")
            except Exception:
                state_path = os.path.join("runtime", "binance_spot", "binance_spot_focus_config.json")
                os.makedirs(os.path.dirname(state_path), exist_ok=True)
        # ★ [2026-06-23 owner "remove the arbitrary force-lock"] paper is governed by the owner toggle —
        #   the automatic boot-time force is removed.
        #   Only on a new exchange's *first boot (no config file)* is paper=True applied once as a safe
        #   default (observe an unverified exchange first).
        #   After that the owner can freely toggle paper on/off in the UI (saved value is preserved and
        #   not re-locked on reboot).
        _fresh = not os.path.exists(state_path)
        super().__init__(system=system, client=client, state_path=state_path)
        # ★ [2026-06-23 audit low] the first boot is *always* paper (does not rely on the config.paper
        #   default — so a future default change cannot disable this force). Once the file exists the
        #   owner toggle governs (not re-locked).
        if _fresh and getattr(self.config, "paper", True) is not True:
            try:
                self.update_config(paper=True)
            except Exception:
                self.config.paper = True
            import logging as _lg
            _lg.getLogger(__name__).info("[binance_spot] first boot paper default (owner toggle free afterwards)")
        # journal also uses the binance_spot directory/name (capital/record isolation)
        self.journal_path = os.path.join(os.path.dirname(state_path), "binance_spot_focus_journal.jsonl")

    def _normalize_market(self, market: str) -> str:
        """Normalize a manually entered market — Binance spot: 'BTC'/'KRW-BTC'/'btcusdt' -> 'BTCUSDT'."""
        m = str(market).upper().strip().replace("/", "")
        if m.startswith("KRW-"):
            m = m[4:]
        m = m.replace("-", "")
        if not m.endswith("USDT"):
            m = f"{m}USDT"
        return m
