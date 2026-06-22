# -*- coding: utf-8 -*-
"""
External Trade Sync - 외부 거래를 원장에 동기화.

File: app/manager/external_trade_sync.py

외부에서 실행된 거래(거래소 앱/웹에서 직접 실행)를 원장에 FILL_SYNC 이벤트로 기록하여
정확한 PnL 히스토리를 유지합니다.

Design:
- 거래소 API에서 체결 완료된 주문 목록 조회
- 원장에 없는 주문(uuid 기준)을 FILL_SYNC 이벤트로 추가
- append-only 원칙 유지
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
    """외부 거래 동기화 관리자."""

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
        """동기화 상태 로드 (이미 동기화된 uuid 목록)."""
        try:
            if os.path.exists(self._state_path):
                with open(self._state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._synced_uuids = set(data.get("synced_uuids", []))
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError):
            logger.warning("Failed to load external_trade_sync state from %s", self._state_path, exc_info=True)
            self._synced_uuids = set()

    def _save_state(self) -> None:
        """동기화 상태 저장."""
        from app.core.io_utils import safe_write_json
        try:
            safe_write_json(self._state_path, {
                "synced_uuids": list(self._synced_uuids),
                "last_sync_ts": time.time(),
            })
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[recovered] external_trade_sync state save: %s", e)

    def _get_ledger_uuids(self, since_ts: float = 0.0) -> Set[str]:
        """원장에서 기존 FILL_* 이벤트의 uuid 수집."""
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
        """특정 마켓의 외부 거래를 동기화.
        
        Returns:
            {
                "market": str,
                "synced": int,      # 새로 동기화된 거래 수
                "skipped": int,     # 이미 존재하여 스킵된 수
                "errors": int,      # 오류 수
                "orders_checked": int,
            }
        """
        synced = 0
        skipped = 0
        errors = 0

        try:
            # 거래소에서 체결 완료된 주문 조회
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

        # 원장에 있는 uuid 수집
        since_ts = time.time() - (lookback_days * 86400)
        ledger_uuids = self._get_ledger_uuids(since_ts=since_ts)
        all_known = ledger_uuids | self._synced_uuids

        for o in orders:
            try:
                uuid_ = _s(o.get("uuid"))
                if not uuid_:
                    continue

                # 이미 동기화된 경우 스킵
                if uuid_ in all_known:
                    skipped += 1
                    continue

                # 체결되지 않은 주문 스킵
                executed_vol = _f(o.get("executed_volume"))
                if executed_vol <= 0:
                    skipped += 1
                    continue

                side = _s(o.get("side"))  # bid or ask
                mkt = _s(o.get("market")) or market
                avg_price = _f(o.get("avg_price")) or _f(o.get("price"))
                paid_fee = _f(o.get("paid_fee"))
                funds = executed_vol * avg_price if avg_price > 0 else 0.0

                # FILL_SYNC 이벤트로 원장에 기록
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

        # 상태 저장
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
        """현재 보유 중인 모든 마켓의 외부 거래 동기화.
        
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
        """지정된 마켓들의 전체 거래 내역을 거래소에서 재구축.
        
        주의: 기존 원장 데이터와 중복될 수 있음. 
        초기 설정 또는 원장 손실 시에만 사용 권장.
        """
        results: Dict[str, Any] = {}
        for market in markets:
            results[market] = self.sync_market(
                market=market,
                max_pages=max_pages,
                lookback_days=365,  # 1년치
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
