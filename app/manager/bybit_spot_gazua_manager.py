# ============================================================
# Bybit spot FOCUS manager — inherits SpotGazuaManager
# ------------------------------------------------------------
# [2026-06-17 owner] "let's build the spot part for bybit first" — USDT-spot FOCUS baseline.
# SpotGazuaManager is exchange-agnostic (depends only on the client interface +
# state_path), so we reuse it as-is with a Bybit spot client + a separate state_path.
# The brain (scan/entry/exit/score/budget/hold) logic is not duplicated at all —
# only the client differs: BybitSpotTradeClient.
#
# Upbit(KRW) vs Bybit spot(USDT) differences:
#   - symbol 'BTCUSDT' (not KRW-BTC) → _normalize_market override.
#     * base_currency("BTCUSDT")=="BTC" already works (parent manager compatible).
#   - no market_warning (investment caution/alert) → client.get_market_warnings()={}.
#   - quote=USDT. USDT tuning such as fee_rate_pct is adjusted in the dashboard/runtime
#     (kept at defaults here).
#
# Isolation: state/journal are split into runtime/bybit_spot/ → capital and state are
#   independent from Upbit and futures.
#   * Capital cap: Bybit unified account (UTA) USDT is shared with futures → limit the
#     spot share via budget(=USDT).
# ============================================================
from __future__ import annotations

import os
from typing import Any, Optional

from app.manager.spot_gazua_manager import SpotGazuaManager


class BybitSpotGazuaManager(SpotGazuaManager):
    """Bybit spot (USDT) long-only FOCUS. Same logic as SpotGazuaManager; only client/state are Bybit spot."""

    _quote_currency = "USDT"   # quote currency USDT (balance/budget lookup key). Upbit/Bithumb=KRW.

    def __init__(self, system: Any = None, client: Any = None, *, state_path: Optional[str] = None):
        if client is None:
            from app.integrations.bybit_spot_trade import BybitSpotTradeClient
            # Wallet split (sub-account): if both BYBIT_SPOT_API_KEY/SECRET are set, use them.
            #   If unset, fall back to the main BYBIT_API_KEY (= legacy behavior, kept compatible).
            #   [2026-06-19 owner] On the Bybit Unified account, spot holdings were mistaken
            #   as orphans during futures reconcile (hs_mixin_reconcile) → isolate the wallet
            #   by splitting spot into a sub-account.
            _spot_key = os.getenv("BYBIT_SPOT_API_KEY", "").strip()
            _spot_sec = os.getenv("BYBIT_SPOT_API_SECRET", "").strip()
            if _spot_key and _spot_sec:
                _key, _sec, _acct = _spot_key, _spot_sec, "SUB(BYBIT_SPOT_*)"
            else:
                _key = os.getenv("BYBIT_API_KEY", "")
                _sec = os.getenv("BYBIT_API_SECRET", "")
                _acct = "MAIN(BYBIT_*) — fallback(wallet not split)"
            try:
                import logging
                logging.getLogger(__name__).info("[bybit_spot] trading account = %s", _acct)
            except Exception:
                pass
            client = BybitSpotTradeClient(_key, _sec)
        if state_path is None:
            try:
                from app.core.runtime_paths import RuntimePaths
                state_path = RuntimePaths(exchange="bybit_spot").custom("bybit_spot_focus_config.json")
            except Exception:
                state_path = os.path.join("runtime", "bybit_spot", "bybit_spot_focus_config.json")
                os.makedirs(os.path.dirname(state_path), exist_ok=True)
        super().__init__(system=system, client=client, state_path=state_path)
        # Journal also goes under the bybit_spot directory/name (capital/record isolation)
        self.journal_path = os.path.join(os.path.dirname(state_path), "bybit_spot_focus_journal.jsonl")

    def _normalize_market(self, market: str) -> str:
        """Normalize a manually entered market — Bybit spot: 'BTC'/'KRW-BTC'/'btcusdt' → 'BTCUSDT'."""
        m = str(market).upper().strip().replace("/", "")
        if m.startswith("KRW-"):
            m = m[4:]
        m = m.replace("-", "")
        if not m.endswith("USDT"):
            m = f"{m}USDT"
        return m
