# -*- coding: utf-8 -*-
"""Market candidate scanner for Reserved Queue.

This scanner is intentionally read-only:
- It queries Bybit public endpoints (ticker + orderbook) to compute suitability.
- It writes ONLY to ReservedQueueStore (runtime/reserved_queue.json).
- It does not change OMA state nor place orders.

Selection philosophy
- PINGPONG: requires tight spread + sufficient depth for target alloc.
- AUTOLOOP: liquidity-first; can be slightly more tolerant.

To avoid mismatch with runtime guards, the scanner uses the *same* guard parameters
as HyperSystem by default, and optionally applies strategy-specific multipliers.
"""

from __future__ import annotations
import math
import os
import time
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.core.rate_limiter import bybit_get
from app.manager.reserved_queue import ReservedQueueStore
from app.core.constants import (
    BYBIT_MARKET_TICKERS,
    BYBIT_MARKET_ORDERBOOK,
    bybit_v5_rest_category,
    parse_bybit_list,
    normalize_bybit_ticker,
)
from app.core.currency import Q
from app.manager.reserved_selector_utils import _finalize_usdt_notional

BYBIT_TICKERS_URL = BYBIT_MARKET_TICKERS
BYBIT_ORDERBOOK_URL = BYBIT_MARKET_ORDERBOOK

from app.core.constants import env_bool as _env_bool, env_int as _env_int, env_float as _env_float

_log = logging.getLogger(__name__)


def _chunks(xs: List[str], n: int) -> List[List[str]]:
    out: List[List[str]] = []
    cur: List[str] = []
    for x in xs:
        if not x:
            continue
        cur.append(x)
        if len(cur) >= n:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        if math.isfinite(v):
            return v
        return float(default)
    except (TypeError, ValueError):
        _log.warning("[CandidateScanner] _safe_float: conversion failed for %r", x, exc_info=True)
        return float(default)


class CandidateScanner:
    def __init__(self, *, system: Any, store: ReservedQueueStore) -> None:
        self.system = system
        self.store = store

    # ------------------------------------------------------------
    # Bybit public fetchers
    # ------------------------------------------------------------
    def _fetch_tickers(self, markets: List[str], *, timeout: float = 5.0) -> List[Dict[str, Any]]:
        from app.core.rate_limiter import rate_limiter

        if rate_limiter.is_banned():
            _log.warning("[CandidateScanner] REST API banned, skipping _fetch_tickers")
            return []

        out: List[Dict[str, Any]] = []
        market_set = set(m.upper() for m in markets)
        try:
            r = bybit_get(BYBIT_TICKERS_URL, params={"category": bybit_v5_rest_category()}, timeout=timeout)
            r.raise_for_status()
            data = parse_bybit_list(r.json())
            for t in data:
                if not isinstance(t, dict):
                    continue
                t = normalize_bybit_ticker(t)
                if t.get("market") in market_set:
                    out.append(t)
            rate_limiter.record_success()
        except requests.RequestException as e:
            _log.warning("[CandidateScanner] fetch_tickers request failed", exc_info=True)
            rate_limiter.handle_api_error(str(e))
            raise
        return out

    def _fetch_orderbooks(self, markets: List[str], *, timeout: float = 5.0) -> Dict[str, Dict[str, Any]]:
        from app.core.rate_limiter import rate_limiter

        if rate_limiter.is_banned():
            _log.warning("[CandidateScanner] REST API banned, skipping _fetch_orderbooks")
            return {}

        out: Dict[str, Dict[str, Any]] = {}
        try:
            for sym in markets:
                r = bybit_get(BYBIT_ORDERBOOK_URL, params={"category": bybit_v5_rest_category(), "symbol": sym, "limit": 15}, timeout=timeout)
                r.raise_for_status()
                body = r.json()
                # Bybit V5 orderbook: result is the orderbook object (not result.list)
                ob = body.get("result") if isinstance(body, dict) else None
                if not isinstance(ob, dict):
                    _log.warning("[CandidateScanner] %s: unexpected orderbook response", sym)
                    continue
                bids = ob.get("b", [])
                asks = ob.get("a", [])
                if not bids or not asks:
                    continue
                units = []
                for i in range(min(len(bids), len(asks), 15)):
                    units.append({
                        "bid_price": float(bids[i][0]),
                        "bid_size": float(bids[i][1]),
                        "ask_price": float(asks[i][0]),
                        "ask_size": float(asks[i][1]),
                    })
                out[sym] = {"market": sym, "orderbook_units": units}
            rate_limiter.record_success()
        except requests.RequestException as e:
            _log.warning("[CandidateScanner] fetch_orderbooks request failed", exc_info=True)
            rate_limiter.handle_api_error(str(e))
            raise
        return out

    # ------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------
    def _ob_metrics(
        self,
        ob: Dict[str, Any],
        *,
        depth_bps: float,
    ) -> Optional[Dict[str, float]]:
        units = ob.get("orderbook_units")
        if not isinstance(units, list):
            units = ob.get("units")
        if not isinstance(units, list) or not units:
            return None

        try:
            best_ask = _safe_float(units[0].get("ask_price"), 0.0)
            best_bid = _safe_float(units[0].get("bid_price"), 0.0)
        except (KeyError, IndexError, AttributeError, TypeError):
            _log.warning("[CandidateScanner] _build_snapshot: orderbook parse failed", exc_info=True)
            return None

        if best_ask <= 0 or best_bid <= 0:
            return None

        mid = (best_ask + best_bid) / 2.0
        spread_bps = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 else 999999.0

        ask_lim = best_ask * (1.0 + float(depth_bps) / 10000.0) if depth_bps > 0 else best_ask
        bid_lim = best_bid * (1.0 - float(depth_bps) / 10000.0) if depth_bps > 0 else best_bid

        ask_notional = 0.0
        bid_notional = 0.0
        for u in units:
            if not isinstance(u, dict):
                continue
            ap = _safe_float(u.get("ask_price"), 0.0)
            asz = _safe_float(u.get("ask_size"), 0.0)
            bp = _safe_float(u.get("bid_price"), 0.0)
            bsz = _safe_float(u.get("bid_size"), 0.0)
            if ap > 0 and asz > 0 and ap <= ask_lim:
                ask_notional += ap * asz
            if bp > 0 and bsz > 0 and bp >= bid_lim:
                bid_notional += bp * bsz

        return {
            "best_bid": float(best_bid),
            "best_ask": float(best_ask),
            "spread_bps": float(spread_bps),
            "ask_notional_usdt": float(ask_notional),
            "bid_notional_usdt": float(bid_notional),
        }

    # ------------------------------------------------------------
    # Config / capital model
    # ------------------------------------------------------------
    def _effective_equity_usdt(self) -> float:
        mode = str(getattr(self.system, "trading_mode", "") or "").upper()
        if mode == "PAPER":
            return _safe_float(os.getenv("DRY_INITIAL_USDT", "1000"), 1000.0)
        eq = _safe_float(getattr(self.system, "_last_equity_usdt", 0.0), 0.0)
        cash = _safe_float(getattr(self.system, "_last_cash_usdt", 0.0), 0.0)
        return eq if eq > 0 else cash

    def _slot_base_usdt(self, *, n_pp: int, n_al: int) -> float:
        equity = self._effective_equity_usdt()
        deploy_ratio = _safe_float(getattr(self.system, "deploy_ratio", 0.0), 0.0)
        deployable = max(0.0, float(equity) * float(deploy_ratio))

        # Consider current ACTIVE markets to avoid suggesting budgets that cannot fit.
        try:
            active_n = len(list(getattr(self.system.oma_registry, "list_active")()))  # type: ignore[attr-defined]
        except (KeyError, AttributeError, TypeError):
            _log.warning("[CandidateScanner] _base_budget: list_active failed", exc_info=True)
            active_n = 0

        slots = max(1, int(active_n) + int(n_pp) + int(n_al))
        base = deployable / float(slots) if deployable > 0 else 0.0

        # Keep budget suggestions conservative.
        cap_mult = _env_float("OMA_RESERVED_SLOT_BASE_MULT", 1.0)
        return max(0.0, float(base) * float(cap_mult))

    def _strategy_thresholds(self, strategy: str) -> Dict[str, float]:
        s = str(strategy or "").strip().upper()

        # Pull defaults from HyperSystem
        max_spread_bps = _safe_float(getattr(self.system, "entry_ob_max_spread_bps", 0.0), 0.0)
        depth_bps = _safe_float(getattr(self.system, "entry_ob_depth_bps", 0.0), 0.0)
        depth_factor = _safe_float(getattr(self.system, "entry_ob_depth_factor", 0.0), 0.0)
        max_qty = _safe_float(getattr(self.system, "entry_max_qty", 0.0), 0.0)

        if s == "AUTOLOOP":
            max_spread_bps *= _env_float("OMA_RESERVED_AUTOLOOP_SPREAD_MULT", 1.5)
            depth_factor *= _env_float("OMA_RESERVED_AUTOLOOP_DEPTH_FACTOR_MULT", 0.9)
            max_qty *= _env_float("OMA_RESERVED_AUTOLOOP_MAX_QTY_MULT", 1.5)

        # Safety: never zero-out depth_factor unless explicitly set
        if depth_factor <= 0:
            depth_factor = _safe_float(getattr(self.system, "entry_ob_depth_factor", 1.0), 1.0)

        return {
            "max_spread_bps": float(max_spread_bps),
            "depth_bps": float(depth_bps),
            "depth_factor": float(depth_factor),
            "max_qty": float(max_qty),
        }

    # ------------------------------------------------------------
    # Candidate selection
    # ------------------------------------------------------------
    def _recommend_alloc(
        self,
        *,
        base_slot: float,
        trade_value_24h: float,
        trade_value_median: float,
        price: float,
        min_order: float,
        max_qty: float,
        depth_factor: float,
        ask_notional: float,
        bid_notional: float,
    ) -> Tuple[Optional[float], str]:
        if base_slot <= 0:
            base_slot = min_order

        med = trade_value_median if trade_value_median > 0 else max(1.0, trade_value_24h)
        vol_factor = (trade_value_24h / med) ** 0.5 if med > 0 else 1.0
        vol_factor = max(0.5, min(2.0, float(vol_factor)))

        alloc = float(base_slot) * float(vol_factor)

        # qty guard cap
        if max_qty > 0 and price > 0:
            alloc = min(alloc, float(price) * float(max_qty) * 0.95)

        # depth cap: ensure alloc * depth_factor <= min(depth)
        if depth_factor > 0 and (ask_notional > 0 or bid_notional > 0):
            cap = min(float(ask_notional), float(bid_notional)) / float(depth_factor) * 0.95
            if cap > 0:
                alloc = min(alloc, cap)

        finalized = _finalize_usdt_notional(alloc, float(min_order))
        if finalized is None:
            return None, "below_min_order"
        return float(finalized), "ok"

    def _score_pingpong(self, *, v24: float, spread_bps: float, depth_min: float, change_rate: float) -> float:
        # Liquidity dominant, punish spread, slightly punish large trend (too directional)
        return (
            50.0 * math.log10(v24 + 1.0)
            + 8.0 * math.log10(depth_min + 1.0)
            - 1.5 * float(spread_bps)
            - 8.0 * abs(float(change_rate))
        )

    def _score_autoloop(self, *, v24: float, spread_bps: float, depth_min: float, change_rate: float) -> float:
        # Liquidity dominant, smaller spread penalty
        return (
            60.0 * math.log10(v24 + 1.0)
            + 4.0 * math.log10(depth_min + 1.0)
            - 0.7 * float(spread_bps)
            - 3.0 * abs(float(change_rate))
        )

    def _pick_stable(
        self,
        *,
        ranked: List[Dict[str, Any]],
        current: List[Dict[str, Any]],
        n: int,
        keep_k: int,
    ) -> List[Dict[str, Any]]:
        if n <= 0:
            return []
        if not ranked:
            return []

        top_keep = {str(it.get("market") or "").upper() for it in ranked[: max(1, keep_k)] if isinstance(it, dict)}
        out: List[Dict[str, Any]] = []
        seen = set()

        # keep existing candidates if still in top_keep
        for it in current or []:
            if not isinstance(it, dict):
                continue
            m = str(it.get("market") or "").upper()
            if m and m in top_keep and m not in seen:
                out.append(it)
                seen.add(m)
            if len(out) >= n:
                return out

        # fill with best new
        for it in ranked:
            if not isinstance(it, dict):
                continue
            m = str(it.get("market") or "").upper()
            if not m or m in seen:
                continue
            out.append(it)
            seen.add(m)
            if len(out) >= n:
                break

        return out

    def scan_and_update(self) -> Dict[str, Any]:
        """Scan Bybit and update ReservedQueueStore.

        Returns a short summary dict for logging/telemetry.
        """

        enabled = _env_bool("OMA_RESERVED_ENABLED", True)
        if not enabled:
            return {"ok": True, "enabled": False}

        n_pp = max(0, _env_int("OMA_RESERVED_PINGPONG_N", 3))
        n_al = max(0, _env_int("OMA_RESERVED_AUTOLOOP_N", 3))
        preselect_k = max(10, _env_int("OMA_RESERVED_PRESELECT_K", 40))
        keep_k = max(3, _env_int("OMA_RESERVED_KEEP_K", 9))
        timeout = max(1.0, _env_float("OMA_RESERVED_API_TIMEOUT", 5.0))

        # Market universe
        try:
            known = list(getattr(self.system, "_known_quote_markets", set()) or [])
        except (KeyError, AttributeError, TypeError):
            _log.warning("[CandidateScanner] scan: _known_quote_markets read failed", exc_info=True)
            known = []
        markets = [str(m).upper() for m in known if isinstance(m, str) and str(m).upper().startswith(Q.config.market_prefix)]

        # Exclusions
        exclude = set()
        try:
            exclude |= set([m.upper() for m in (getattr(self.system.oma_registry, "list_active")() or [])])  # type: ignore[attr-defined]
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            _log.warning("[SCANNER] active exclusion list: %s", exc, exc_info=True)
        try:
            rec = getattr(self.system.oma_registry, "list_recovery")()  # type: ignore[attr-defined]
            exclude |= set([m.upper() for m in (rec or [])])
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            _log.warning("[SCANNER] recovery exclusion list: %s", exc, exc_info=True)

        # Apply cooldown exclusions (reject/approve)
        now_ts = time.time()
        markets2 = [m for m in markets if (m not in exclude) and (not self.store.is_in_cooldown(m, now=now_ts))]

        if not markets2:
            self.store.set_pools(pingpong=[], autoloop=[], config={"enabled": True}, last_scan_ts=now_ts)
            self.store.save()
            return {"ok": True, "enabled": True, "markets": 0}

        # Pull tickers (single source for volume/price)
        tickers = self._fetch_tickers(markets2, timeout=timeout)
        tmap: Dict[str, Dict[str, Any]] = {str(t.get("market") or "").upper(): t for t in tickers if isinstance(t, dict)}

        def trade_value_24h(m: str) -> float:
            t = tmap.get(m) or {}
            return _safe_float(t.get("acc_trade_price_24h"), 0.0)

        # Preselect by volume (USDT notional)
        ranked_by_v = sorted(markets2, key=lambda m: trade_value_24h(m), reverse=True)
        pre_pp = ranked_by_v[: max(preselect_k, n_pp * 10)]
        pre_al = ranked_by_v[: max(preselect_k, n_al * 10)]

        # Orderbook fetch for union preselection
        union = sorted(set(pre_pp + pre_al))
        ob_map = self._fetch_orderbooks(union, timeout=timeout)

        min_order = float(self.system.effective_min_order_usdt())
        base_slot = self._slot_base_usdt(n_pp=n_pp, n_al=n_al)

        # trade value median (for scaling)
        v_list = [trade_value_24h(m) for m in union if trade_value_24h(m) > 0]
        v_list_sorted = sorted(v_list)
        med_v = v_list_sorted[len(v_list_sorted) // 2] if v_list_sorted else 0.0

        def build_pool(strategy: str, pre: List[str]) -> List[Dict[str, Any]]:
            thr = self._strategy_thresholds(strategy)
            max_spread_bps = float(thr["max_spread_bps"])
            depth_bps = float(thr["depth_bps"])
            depth_factor = float(thr["depth_factor"])
            max_qty = float(thr["max_qty"])

            items: List[Dict[str, Any]] = []
            for m in pre:
                t = tmap.get(m)
                if not isinstance(t, dict):
                    continue

                price = _safe_float(t.get("trade_price"), 0.0)
                v24 = _safe_float(t.get("acc_trade_price_24h"), 0.0)
                chg = _safe_float(t.get("signed_change_rate"), 0.0)

                if price <= 0 or v24 <= 0:
                    continue

                ob = ob_map.get(m)
                if not isinstance(ob, dict):
                    continue

                obm = self._ob_metrics(ob, depth_bps=depth_bps)
                if not isinstance(obm, dict):
                    continue

                spread = float(obm.get("spread_bps") or 999999.0)
                ask_n = float(obm.get("ask_notional_usdt") or 0.0)
                bid_n = float(obm.get("bid_notional_usdt") or 0.0)
                depth_min = min(ask_n, bid_n)

                # spread suitability
                if max_spread_bps > 0 and spread > max_spread_bps:
                    continue

                alloc, why = self._recommend_alloc(
                    base_slot=base_slot,
                    trade_value_24h=v24,
                    trade_value_median=med_v,
                    price=price,
                    min_order=min_order,
                    max_qty=max_qty,
                    depth_factor=depth_factor,
                    ask_notional=ask_n,
                    bid_notional=bid_n,
                )
                if alloc is None:
                    continue

                # depth suitability for recommended alloc
                req = float(alloc) * float(depth_factor) if depth_factor > 0 else 0.0
                if req > 0 and depth_min > 0 and depth_min < req:
                    continue

                if strategy.upper() == "PINGPONG":
                    score = self._score_pingpong(v24=v24, spread_bps=spread, depth_min=depth_min, change_rate=chg)
                else:
                    score = self._score_autoloop(v24=v24, spread_bps=spread, depth_min=depth_min, change_rate=chg)

                guards_suggested = {
                    "entry_ob_max_spread_bps": max_spread_bps,
                    "entry_ob_depth_bps": depth_bps,
                    "entry_ob_depth_factor": depth_factor,
                    "entry_max_qty": max_qty,
                }

                items.append(
                    {
                        "market": m,
                        "strategy": strategy.upper(),
                        "score": float(score),
                        "alloc_usdt": float(alloc),
                        "metrics": {
                            "price": float(price),
                            "volume_24h_usdt": float(v24),
                            "signed_change_rate": float(chg),
                            "spread_bps": float(spread),
                            "depth_bps": float(depth_bps),
                            "depth_ask_usdt": float(ask_n),
                            "depth_bid_usdt": float(bid_n),
                        },
                        "guards_suggested": guards_suggested,
                        "ts": now_ts,
                    }
                )

            items.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
            return items

        ranked_pp = build_pool("PINGPONG", pre_pp)
        ranked_al = build_pool("AUTOLOOP", pre_al)

        # Stability: keep existing candidates if they are still in top keep_k.
        snap = self.store.snapshot()
        cur_pp = snap.get("pingpong") if isinstance(snap.get("pingpong"), list) else []
        cur_al = snap.get("autoloop") if isinstance(snap.get("autoloop"), list) else []

        out_pp = self._pick_stable(ranked=ranked_pp, current=cur_pp, n=n_pp, keep_k=keep_k)
        out_al = self._pick_stable(ranked=ranked_al, current=cur_al, n=n_al, keep_k=keep_k)

        cfg = {
            "enabled": True,
            "n_pingpong": n_pp,
            "n_autoloop": n_al,
            "preselect_k": preselect_k,
            "keep_k": keep_k,
            "min_order_usdt": min_order,
            "base_slot_usdt": base_slot,
            "median_volume_24h_usdt": med_v,
            "autoloop_multipliers": {
                "spread_mult": _env_float("OMA_RESERVED_AUTOLOOP_SPREAD_MULT", 1.5),
                "depth_factor_mult": _env_float("OMA_RESERVED_AUTOLOOP_DEPTH_FACTOR_MULT", 0.9),
                "max_qty_mult": _env_float("OMA_RESERVED_AUTOLOOP_MAX_QTY_MULT", 1.5),
            },
        }

        self.store.set_pools(pingpong=out_pp, autoloop=out_al, config=cfg, last_scan_ts=now_ts)
        self.store.add_history(
            "SCAN",
            {
                "pingpong_n": len(out_pp),
                "autoloop_n": len(out_al),
                "universe": len(markets2),
                "preselect": len(union),
            },
        )
        self.store.save()

        return {
            "ok": True,
            "enabled": True,
            "pingpong": len(out_pp),
            "autoloop": len(out_al),
            "universe": len(markets2),
        }
