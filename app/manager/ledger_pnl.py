"""Ledger-based PnL utilities.

File: app/manager/ledger_pnl.py

This module computes simple cash-based PnL metrics from the trade ledger
(FILL_BUY / FILL_SELL events).

Design goals:
- Do not rely on in-memory state (works after restart)
- Be cheap enough to call from a dashboard (tail-based)
- Be explicit about what is and isn't included (fees are included if present)

Important:
- The primary metric here is `net_cash_usdt`, which is the net USDT cash delta
  from executed fills: (sell_funds - sell_fee) - (buy_funds + buy_fee).
- If a market currently holds an open position, `net_cash_usdt` will typically be
  negative (cash converted into inventory). A dashboard should show position
  value separately (mark-to-market) if needed.
"""

from __future__ import annotations

import json
import logging
import os
import time

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        logger.warning("[LedgerPnl] _f() conversion failed for %r", x, exc_info=True)
        return default

def _s(x: Any, default: str = "") -> str:
    try:
        if x is None:
            return default
        return str(x)
    except (AttributeError, TypeError, ValueError):
        logger.warning("[LedgerPnl] _s() conversion failed for %r", x, exc_info=True)
        return default

@dataclass
class MarketFillAgg:
    market: str
    buy_n: int = 0
    sell_n: int = 0
    buy_funds_usdt: float = 0.0
    sell_funds_usdt: float = 0.0
    buy_fee_usdt: float = 0.0
    sell_fee_usdt: float = 0.0
    first_ts: float = 0.0
    last_ts: float = 0.0

    def update(self, *, ts: float, side: str, funds_usdt: float, fee_usdt: float) -> None:
        if ts <= 0:
            return
        if self.first_ts <= 0 or ts < self.first_ts:
            self.first_ts = ts
        if ts > self.last_ts:
            self.last_ts = ts

        if side == "BUY":
            self.buy_n += 1
            self.buy_funds_usdt += max(0.0, funds_usdt)
            self.buy_fee_usdt += max(0.0, fee_usdt)
        elif side == "SELL":
            self.sell_n += 1
            self.sell_funds_usdt += max(0.0, funds_usdt)
            self.sell_fee_usdt += max(0.0, fee_usdt)

    @property
    def trade_n(self) -> int:
        return int(self.buy_n + self.sell_n)

    @property
    def fees_usdt(self) -> float:
        return float(self.buy_fee_usdt + self.sell_fee_usdt)

    @property
    def net_cash_usdt(self) -> float:
        """Net USDT cash delta (fees included if present in ledger)."""
        return float((self.sell_funds_usdt - self.sell_fee_usdt) - (self.buy_funds_usdt + self.buy_fee_usdt))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market": self.market,
            "buy_n": int(self.buy_n),
            "sell_n": int(self.sell_n),
            "trade_n": int(self.trade_n),
            "buy_funds_usdt": float(self.buy_funds_usdt),
            "sell_funds_usdt": float(self.sell_funds_usdt),
            "buy_fee_usdt": float(self.buy_fee_usdt),
            "sell_fee_usdt": float(self.sell_fee_usdt),
            "fees_usdt": float(self.fees_usdt),
            "net_cash_usdt": float(self.net_cash_usdt),
            "first_ts": float(self.first_ts),
            "last_ts": float(self.last_ts),
        }

def aggregate_fill_pnl(
    records: Iterable[Dict[str, Any]],
    *,
    since_ts: float,
    until_ts: float,
    markets: Optional[Iterable[str]] = None,
) -> Dict[str, MarketFillAgg]:
    """Aggregate ledger FILL_* events into per-market cash metrics."""
    allow = None
    if markets is not None:
        allow = set(str(m) for m in markets)

    out: Dict[str, MarketFillAgg] = {}
    for rec in records:
        try:
            ts = _f(rec.get("ts"), 0.0)
            if ts <= 0:
                continue
            if ts < since_ts or ts > until_ts:
                continue

            ev = _s(rec.get("event"))
            # Support both regular fills and synced external fills
            if ev not in ("FILL_BUY", "FILL_SELL", "FILL_SYNC_BUY", "FILL_SYNC_SELL"):
                continue

            market = _s(rec.get("market"))
            if not market:
                # Some legacy records might store market in data
                data = rec.get("data") or {}
                market = _s(data.get("market"))
            if not market:
                continue
            if allow is not None and market not in allow:
                continue

            data = rec.get("data") or {}
            funds = _f(data.get("funds"), 0.0)
            fee = _f(data.get("paid_fee"), 0.0)

            agg = out.get(market)
            if agg is None:
                agg = MarketFillAgg(market=market)
                out[market] = agg

            side = "BUY" if ev in ("FILL_BUY", "FILL_SYNC_BUY") else "SELL"
            agg.update(ts=ts, side=side, funds_usdt=funds, fee_usdt=fee)
        except (KeyError, AttributeError, TypeError) as exc:
            # Best-effort: never let a single malformed record break dashboard.
            logger.warning("[ledger_pnl] %s: %s", 'Some legacy records might store market in data except-> continue', exc, exc_info=True)
            continue

    return out
# -----------------------------
# PnL baseline (reset) store
# -----------------------------

DEFAULT_PNL_BASELINE_PATH = "runtime/pnl_baseline.json"

def load_pnl_baseline_ts(path: str = DEFAULT_PNL_BASELINE_PATH) -> float:
    """Load baseline timestamp (epoch seconds). Returns 0.0 if missing/invalid."""
    try:
        if not path:
            return 0.0
        if not os.path.exists(path):
            return 0.0
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        ts = float(obj.get("baseline_ts", 0.0) or 0.0)
        # guard against future timestamps / nonsense
        now = time.time()
        if ts < 0.0 or ts > now + 3600.0:
            return 0.0
        return ts
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
        # quarantine the file if it's unreadable, but don't crash the server
        try:
            bad = f"{path}.bad.{int(time.time())}"
            os.replace(path, bad)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[ledger_pnl] %s: %s", "quarantine the file if it's unreadable, but don't crash the server", exc, exc_info=True)
        return 0.0

def save_pnl_baseline_ts(ts: float, path: str = DEFAULT_PNL_BASELINE_PATH) -> None:
    """Persist baseline timestamp atomically."""
    from app.core.io_utils import safe_write_json
    if not path:
        return
    ts = float(ts or 0.0)
    safe_write_json(path, {
        "ts": time.time(),
        "baseline_ts": ts,
    })
