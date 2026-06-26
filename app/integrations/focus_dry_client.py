"""FOCUS-only dry-run wrapper — perpetual virtual trading.

[2026-05-25 owner] For paper validation. FOCUS is perpetual (LONG/SHORT/leverage),
but PaperTradeClient is a spot model so it doesn't fit → added a FOCUS-only dry wrapper.

Design:
  - Quotes (get_kline, _linear_last_price): delegate to real Bybit → accurate market data
  - Orders (place_order, set_trading_stop): virtual → *never* sent to the real exchange
  - Balance (get_balance): virtual (virtual_usdt fixed)
  - Exchange positions (get_positions): empty list (FOCUS self.positions is the source of truth)
  - Other methods: delegated to the real client via __getattr__

FOCUS performs its own position tracking / PnL calculation using self.positions + real quotes,
so handling only orders virtually still validates the entry/exit flow + volatility gates intact.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class FocusDryClient:
    """Dry-run wrapper that wraps the real Bybit client and handles only orders virtually."""

    def __init__(self, real_client: Any, virtual_usdt: float = 1000.0, slippage_bps: float = 0.0):
        # Set __dict__ directly (─ prevents __getattr__ infinite loop)
        object.__setattr__(self, "_real", real_client)
        object.__setattr__(self, "_virtual_usdt", float(virtual_usdt))
        object.__setattr__(self, "_slip_bps", max(0.0, float(slippage_bps)))
        logger.info("[FOCUS-DRY] 🧪 Dry-run client active — quotes=real / orders=virtual / balance=$%.2f / slippage=%.0fbp",
                    float(virtual_usdt), max(0.0, float(slippage_bps)))

    # ── Quotes: delegate to real Bybit (accurate data required) ──
    def get_kline(self, *args, **kwargs):
        return self._real.get_kline(*args, **kwargs)

    def _linear_last_price(self, *args, **kwargs):
        return self._real._linear_last_price(*args, **kwargs)

    # ── Balance: fixed virtual ──
    def get_balance(self, currency: str, *, include_locked: bool = False) -> float:
        return self._virtual_usdt

    # ── Orders: virtual (real trades always blocked) ──
    def place_order(self, *args, **kwargs) -> Dict[str, Any]:
        oid = f"DRY-{uuid.uuid4().hex[:12]}"
        _mkt = kwargs.get("market") or (args[0] if args else "?")
        _side = str(kwargs.get("side", "")).lower()
        _qty = kwargs.get("volume", "")
        # ★ [2026-06-24] paper slippage — set the fill price unfavorably (buy higher / sell lower) and return it as avg_price.
        #   FocusManager._extract_fill uses avg_price as the entry/exit price (same path as live=real avg_price),
        #   so this single spot covers paper slippage on both entry and exit. If slippage_bps=0, avg_price is not returned = old behavior.
        _fill = 0.0
        if self._slip_bps > 0:
            try:
                _px = float(self._real._linear_last_price(_mkt) or 0)
                if _px > 0:
                    _is_buy = _side in ("buy", "bid", "long")
                    _s = self._slip_bps / 10000.0
                    _fill = _px * (1.0 + _s) if _is_buy else _px * (1.0 - _s)
            except Exception:
                _fill = 0.0
        logger.info("[FOCUS-DRY] 🧪 place_order virtual (no real trade): %s %s qty=%s slip=%.0fbp", _mkt, _side, _qty, self._slip_bps)
        res: Dict[str, Any] = {"ok": True, "orderId": oid, "result": {"orderId": oid}, "_dry": True, "state": "done"}
        if _fill > 0:
            res["avg_price"] = _fill
            res["avgPrice"] = _fill
            res["result"]["avgPrice"] = _fill
        return res

    def set_trading_stop(self, *args, **kwargs) -> Dict[str, Any]:
        logger.debug("[FOCUS-DRY] 🧪 set_trading_stop virtual (no real trade)")
        return {"ok": True, "_dry": True}

    def cancel_order(self, *args, **kwargs) -> Dict[str, Any]:
        logger.debug("[FOCUS-DRY] 🧪 cancel_order virtual")
        return {"ok": True, "_dry": True}

    def set_leverage(self, *args, **kwargs) -> Dict[str, Any]:
        # Virtual — does not set leverage on the real exchange
        return {"ok": True, "_dry": True}

    def switch_position_mode(self, *args, **kwargs) -> Dict[str, Any]:
        # ★ [2026-06-23 audit] block paper leak — without override, __getattr__ would send the
        #   account-wide position mode (dualSidePosition) change API on the real account (called right before entry).
        return {"ok": True, "_dry": True}

    # ── Exchange positions: empty list (FOCUS self.positions is the source of truth) ──
    def get_positions(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return []

    # ── [2026-06-23 audit] block paper leak — the two below were not overridden, so __getattr__
    #   caused an asymmetric leak that sent real exchange authenticated APIs (positionRisk·account). Sealed with explicit virtual values.
    def list_open_positions(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return []  # paper = no real account position queries (self.positions is the source of truth)

    def get_available_margin(self, *args, **kwargs) -> float:
        return self._virtual_usdt  # paper = virtual balance (no real account queries)

    # ── Others: delegate to real client ──
    def __getattr__(self, name: str):
        # _real / _virtual_usdt are in __dict__ so they don't reach here.
        # Any other attribute/method not found is delegated to the real client.
        if name in ("_real", "_virtual_usdt", "_slip_bps"):
            raise AttributeError(name)
        return getattr(self._real, name)
