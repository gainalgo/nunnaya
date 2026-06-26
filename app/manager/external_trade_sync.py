# -*- coding: utf-8 -*-
"""
External Trade Sync - sync externally executed trades into the ledger.

File: app/manager/external_trade_sync.py

Records trades executed outside the bot (directly on the exchange app/web) into
the ledger as FILL_SYNC events to keep an accurate PnL history.

Design:
- Fetch the list of filled orders from the exchange API
- Append orders not yet in the ledger (by uuid) as FILL_SYNC events
- Keep the append-only principle
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Set

import logging

from app.manager.trade_ledger import TradeLedger

logger = logging.getLogger(__name__)

def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        logger.warning("[ExternalTradeSync] _f() conversion failed for %r", x, exc_info=True)
        return default

def _s(x: Any, default: str = "") -> str:
    try:
        if x is None:
            return default
        return str(x)
    except (TypeError, ValueError):
        logger.warning("[ExternalTradeSync] _s() conversion failed for %r", x, exc_info=True)
        return default

class ExternalTradeSync:
    """Manager for syncing external trades."""

    def __init__(
        self,
        trade_client: Any,
        ledger: TradeLedger,
        state_path: str = "runtime/external_sync_state.json",
    ) -> None:
        self._client = trade_client
        self._ledger = ledger
        self._state_path = state_path
        self._synced_uuids: Set[str] = set()
        self._load_state()

    def _load_state(self) -> None:
        """Load sync state (list of already-synced uuids)."""
        try:
            if os.path.exists(self._state_path):
                with open(self._state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._synced_uuids = set(data.get("synced_uuids", []))
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError):
            logger.warning("Failed to load external_trade_sync state from %s", self._state_path, exc_info=True)
            self._synced_uuids = set()

    def _save_state(self) -> None:
        """Save sync state."""
        from app.core.io_utils import safe_write_json
        try:
            safe_write_json(self._state_path, {
                "synced_uuids": list(self._synced_uuids),
                "last_sync_ts": time.time(),
            })
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[recovered] external_trade_sync state save: %s", e)

    def _get_ledger_uuids(self, since_ts: float = 0.0) -> Set[str]:
        """Collect uuids of existing FILL_* events from the ledger."""
        uuids: Set[str] = set()
        try:
            records = self._ledger.tail_records(since_ts=since_ts, tail_lines=50000)
            for rec in records:
                event = _s(rec.get("event"))
                if event.startswith("FILL_"):
                    data = rec.get("data") or {}
                    uuid_ = _s(data.get("uuid"))
                    if uuid_:
                        uuids.add(uuid_)
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.warning("[recovered] ledger UUID scan: %s", e)
        return uuids

    def sync_market(
        self,
        market: str,
        max_pages: int = 5,
        lookback_days: int = 30,
    ) -> Dict[str, Any]:
        """Sync external trades for a specific market.

        Returns:
            {
                "market": str,
                "synced": int,      # number of newly synced trades
                "skipped": int,     # number skipped (already present)
                "errors": int,      # number of errors
                "orders_checked": int,
            }
        """
        synced = 0
        skipped = 0
        errors = 0

        try:
            # Fetch filled orders from the exchange
            orders = self._client.list_done_orders(market=market, max_pages=max_pages)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("[ExternalTradeSync] sync_market(%s) failed: %s", market, e, exc_info=True)
            return {
                "market": market,
                "synced": 0,
                "skipped": 0,
                "errors": 1,
                "orders_checked": 0,
                "error_msg": str(e),
            }

        # Collect uuids already in the ledger
        since_ts = time.time() - (lookback_days * 86400)
        ledger_uuids = self._get_ledger_uuids(since_ts=since_ts)
        all_known = ledger_uuids | self._synced_uuids

        for o in orders:
            try:
                uuid_ = _s(o.get("uuid"))
                if not uuid_:
                    continue

                # Skip if already synced
                if uuid_ in all_known:
                    skipped += 1
                    continue

                # Skip unfilled orders
                executed_vol = _f(o.get("executed_volume"))
                if executed_vol <= 0:
                    skipped += 1
                    continue

                side = _s(o.get("side"))  # bid or ask
                mkt = _s(o.get("market")) or market
                avg_price = _f(o.get("avg_price")) or _f(o.get("price"))
                paid_fee = _f(o.get("paid_fee"))
                funds = executed_vol * avg_price if avg_price > 0 else 0.0

                # Record into the ledger as a FILL_SYNC event
                event = "FILL_SYNC_BUY" if side == "bid" else "FILL_SYNC_SELL"
                self._ledger.append(
                    event,
                    market=mkt,
                    uuid=uuid_,
                    side=side,
                    qty=executed_vol,
                    funds=funds,
                    avg_price=avg_price,
                    paid_fee=paid_fee,
                    source="external_sync",
                    ord_type=_s(o.get("ord_type")),
                    created_at=_s(o.get("created_at")),
                    strategy="unknown",
                )

                self._synced_uuids.add(uuid_)
                synced += 1

            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[external_trade_sync] %s: %s", "external_trade_sync.sync_recent_fills continue", exc, exc_info=True)
                errors += 1
                continue

        # Save state
        if synced > 0:
            self._save_state()

        return {
            "market": market,
            "synced": synced,
            "skipped": skipped,
            "errors": errors,
            "orders_checked": len(orders),
        }

    def sync_all_holdings(
        self,
        holdings: Dict[str, Dict[str, Any]],
        max_pages: int = 5,
        lookback_days: int = 30,
    ) -> Dict[str, Any]:
        """Sync external trades for all currently held markets.

        Args:
            holdings: {market: {qty, avg_buy_price, ...}}
        
        Returns:
            {
                "total_synced": int,
                "total_skipped": int,
                "total_errors": int,
                "markets": {market: result, ...}
            }
        """
        total_synced = 0
        total_skipped = 0
        total_errors = 0
        market_results: Dict[str, Any] = {}

        for market in holdings.keys():
            result = self.sync_market(
                market=market,
                max_pages=max_pages,
                lookback_days=lookback_days,
            )
            market_results[market] = result
            total_synced += result.get("synced", 0)
            total_skipped += result.get("skipped", 0)
            total_errors += result.get("errors", 0)

        return {
            "total_synced": total_synced,
            "total_skipped": total_skipped,
            "total_errors": total_errors,
            "markets": market_results,
        }

    def rebuild_from_exchange(
        self,
        markets: List[str],
        max_pages: int = 10,
    ) -> Dict[str, Any]:
        """Rebuild the full trade history for the given markets from the exchange.

        Note: may duplicate existing ledger data.
        Recommended only for initial setup or after ledger loss.
        """
        results: Dict[str, Any] = {}
        for market in markets:
            results[market] = self.sync_market(
                market=market,
                max_pages=max_pages,
                lookback_days=365,  # one year
            )
        return results

# Singleton instance (lazy init)
_sync_instance: Optional[ExternalTradeSync] = None

def get_external_sync(trade_client: Any, ledger: TradeLedger) -> ExternalTradeSync:
    """Get or create ExternalTradeSync singleton."""
    global _sync_instance
    if _sync_instance is None:
        _sync_instance = ExternalTradeSync(trade_client=trade_client, ledger=ledger)
    return _sync_instance
