# ============================================================
# File: app/manager/ladder_grid_v3.py
# ICAG Grid Engine — Inventory-Controlled Adaptive Grid
# ------------------------------------------------------------
# Replaces ladder_grid_v2's fixed-center grid with:
#   anchor = weighted(avg_price, VWAP) + EMA smoothing
#   step   = ATR-based dynamic spacing
#   bias   = inventory-ratio driven (BUY/SELL/BALANCED)
#
# Reuses LadderManager's order execution infrastructure.
# ============================================================
from __future__ import annotations

import logging
import os
import time
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

from app.manager.ladder_manager import LadderManager
from app.strategy.ladder_icag.config import ICAGConfig
from app.strategy.ladder_icag.engine import ICAGEngine
from app.strategy.ladder_icag.state import (
    ICAGMarketState,
    ICAGPortfolioState,
    load_icag_states,
    save_icag_states,
)
from app.strategy.ladder_icag.portfolio import ICAGPortfolioGuard
from app.strategy.ladder_icag.atr import get_current_price as icag_get_price
from app.core.constants import env_float, env_bool

logger = logging.getLogger(__name__)

# Entry timeout: auto-disable markets with no position after this period
ENTRY_TIMEOUT_SEC = env_float("OMA_LADDER_ENTRY_TIMEOUT_SEC", default=180.0)

# Limit order mode: True=지정가 그리드(피뢰침), False=시장가 fallback(기존 방식)
LADDER_LIMIT_ORDERS = env_bool("OMA_LADDER_LIMIT_ORDERS", default=True)


class LadderGridV3:
    """ICAG-based grid engine — default engine for all LADDER markets.

    Replaces LadderGridV2 with inventory-controlled adaptive grid:
    anchor (EMA-smoothed) + ATR-based spacing + bias (inventory-driven).
    """

    def __init__(self, mgr: LadderManager) -> None:
        self.mgr = mgr
        self.icag_cfg = ICAGConfig()
        self.engine = ICAGEngine(self.icag_cfg)
        self.portfolio_guard = ICAGPortfolioGuard(self.icag_cfg)
        self.states: Dict[str, ICAGMarketState] = load_icag_states()
        self.portfolio = ICAGPortfolioState()
        self._last_save_ts: float = 0.0

    # ============================================================
    # State helpers
    # ============================================================
    def _get_state(self, market: str) -> ICAGMarketState:
        if market not in self.states:
            self.states[market] = ICAGMarketState(symbol=market)
        return self.states[market]

    def _save_states(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_save_ts) < 5.0:
            return
        save_icag_states(self.states)
        self._last_save_ts = now

    # ============================================================
    # Position / budget helpers (mirror grid_v2 approach)
    # ============================================================
    def _get_position_info(self, market: str) -> Tuple[float, float]:
        """Return (qty, avg_price) from system context."""
        try:
            ctx = self.mgr.system.coordinator.contexts.get(market)
        except (KeyError, AttributeError, TypeError):
            logger.warning("LadderGridV3._get_position_info suppressed exception", exc_info=True)
            return 0.0, 0.0
        if ctx is None:
            return 0.0, 0.0
        pos = None
        try:
            pos = ctx.get("position") if isinstance(ctx, dict) else getattr(ctx, "position", None)
        except (KeyError, AttributeError, TypeError):
            logger.warning("LadderGridV3._get_position_info suppressed exception", exc_info=True)
            return 0.0, 0.0
        if pos is None:
            return 0.0, 0.0
        try:
            if isinstance(pos, dict):
                qty = float(pos.get("qty") or pos.get("volume") or pos.get("balance") or 0)
                avg = float(pos.get("avg_buy_price") or pos.get("avg_price") or 0)
            else:
                qty = float(getattr(pos, "qty", None) or getattr(pos, "volume", None) or 0)
                avg = float(getattr(pos, "avg_buy_price", None) or getattr(pos, "avg_price", None) or 0)
            return max(0.0, qty), max(0.0, avg)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("LadderGridV3._get_position_info suppressed exception", exc_info=True)
            return 0.0, 0.0

    def _get_available_qty(self, market: str) -> float:
        try:
            u = self.mgr.get_trade_client()
            currency = market.split("-")[-1] if "-" in market else market
            return u.get_balance(currency, include_locked=False)
        except Exception:
            logger.warning("LadderGridV3._get_available_qty suppressed exception", exc_info=True)
            return 0.0

    def _get_budget_cap(self, market: str, cfg: Dict[str, Any]) -> float:
        for key in ("budget_usdt", "allocated_capital"):
            v = cfg.get(key)
            if v is not None:
                try:
                    b = float(v)
                    if b > 0:
                        return b
                except (TypeError, ValueError) as exc:
                    logger.warning("[GRID_V3] ladder_grid_v3._get_budget_cap fallback: %s", exc, exc_info=True)
        try:
            ctx = self.mgr.system.coordinator.contexts.get(market)
            if ctx is not None:
                alloc = ctx.get("allocated_capital") if isinstance(ctx, dict) else getattr(ctx, "allocated_capital", None)
                if alloc is not None:
                    b = float(alloc)
                    if b > 0:
                        return b
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[GRID_V3] ladder_grid_v3._get_budget_cap fallback: %s", exc, exc_info=True)
        return 0.0

    def _count_active_orders(self, market: str, side: Optional[str] = None) -> int:
        try:
            reg = self.mgr._read_order_registry()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("LadderGridV3._count_active_orders suppressed exception", exc_info=True)
            return 0
        m = reg.get(market)
        if not isinstance(m, dict):
            return 0
        side_norm = str(side or "").lower()
        out = 0
        for meta in m.values():
            if not isinstance(meta, dict):
                continue
            status = str(meta.get("status") or "active").lower()
            if status in ("filled", "deleted"):
                continue
            if side_norm and str(meta.get("side") or "").lower() != side_norm:
                continue
            out += 1
        return out

    def _get_active_order_prices(self, market: str) -> Dict[str, List[float]]:
        """Return {'buy': [prices...], 'sell': [prices...]} of active orders."""
        result: Dict[str, List[float]] = {"buy": [], "sell": []}
        try:
            reg = self.mgr._read_order_registry()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("LadderGridV3._get_active_order_prices suppressed exception", exc_info=True)
            return result
        m = reg.get(market)
        if not isinstance(m, dict):
            return result
        for meta in m.values():
            if not isinstance(meta, dict):
                continue
            if str(meta.get("status") or "active").lower() in ("filled", "deleted"):
                continue
            side = str(meta.get("side") or "").lower()
            price = float(meta.get("price") or 0)
            if side in ("buy", "bid") and price > 0:
                result["buy"].append(price)
            elif side in ("sell", "ask") and price > 0:
                result["sell"].append(price)
        return result

    def _safe_write_order_registry(self, reg: dict) -> None:
        """Write order registry with retry for Windows file lock contention."""
        for attempt in range(3):
            try:
                self.mgr._write_order_registry(reg)
                return
            except OSError:
                logger.warning("_write_order_registry retry %d/3", attempt + 1, exc_info=True)
                if attempt < 2:
                    time.sleep(0.1 * (attempt + 1))
                else:
                    raise

    def _cancel_order_by_uuid(self, market: str, uuid_: str) -> bool:
        try:
            self.mgr._cancel_order(uuid_=uuid_)
            return True
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("V3 cancel failed: %s %s — %s", market, uuid_[:12], e)
            return False

    def _cancel_all_orders(self, market: str) -> int:
        """Cancel all active ICAG orders for a market."""
        try:
            reg = self.mgr._read_order_registry()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("LadderGridV3._cancel_all_orders suppressed exception", exc_info=True)
            return 0
        m = reg.get(market)
        if not isinstance(m, dict):
            return 0
        canceled = 0
        for uuid_, meta in list(m.items()):
            if not isinstance(meta, dict):
                continue
            if str(meta.get("status") or "").lower() in ("filled", "deleted"):
                continue
            if self._cancel_order_by_uuid(market, uuid_):
                m[uuid_]["status"] = "deleted"
                canceled += 1
        reg[market] = m
        self._safe_write_order_registry(reg)
        return canceled

    # ============================================================
    # TICK SIZE (delegate to v2 or internal)
    # ============================================================
    def _round_price(self, price: float, side: str = "buy") -> float:
        try:
            from app.integrations.bybit_trade import adjust_price_to_tick
            return adjust_price_to_tick(price, side="bid" if side == "buy" else "ask")
        except (ImportError, AttributeError, TypeError):
            logger.warning("LadderGridV3._round_price suppressed exception", exc_info=True)
            return float(int(price)) if price >= 100 else round(price, 4)

    # ============================================================
    # CORE: sync_active_window (ICAG version)
    # ============================================================
    def sync_active_window(self, market: str) -> Dict[str, Any]:
        """ICAG tick: compute targets → reconcile with live orders."""
        state = self._get_state(market)
        cfg = self.mgr.get_config(market)

        if not cfg.get("enabled", False):
            return {"market": market, "skipped": True, "reason": "disabled"}

        # cooldown
        now = time.time()
        cooldown = float(self.icag_cfg.requote_cooldown_sec)
        if (now - state.last_tick_ts) < cooldown and state.last_tick_ts > 0:
            return {"market": market, "skipped": True, "reason": "cooldown"}

        # current price
        price = self.mgr.get_current_price(market)
        if not price or price <= 0:
            price = icag_get_price(market)
        if not price or price <= 0:
            return {"market": market, "skipped": True, "reason": "no_price"}

        # activation timestamp (first tick for this market)
        if state.activation_ts <= 0:
            state.activation_ts = now

        # position sync
        qty, avg_price = self._get_position_info(market)
        state.position_qty = qty
        if avg_price > 0:
            state.position_avg_price = avg_price
        if qty > 0 and state.position_entry_ts <= 0:
            state.position_entry_ts = now

        # entry timeout: no position after ENTRY_TIMEOUT_SEC → auto-disable
        if qty <= 0 and ENTRY_TIMEOUT_SEC > 0 and (now - state.activation_ts) > ENTRY_TIMEOUT_SEC:
            logger.warning(
                "ICAG entry timeout %s: no position after %.0fs → auto-disable",
                market, now - state.activation_ts,
            )
            self._auto_disable_market(market, "entry_timeout_no_position")
            return {"market": market, "skipped": True, "reason": "entry_timeout"}

        # budget sync
        budget_cap = self._get_budget_cap(market, cfg)
        if budget_cap > 0:
            state.budget_allocated = budget_cap
        state.budget_used = qty * avg_price if qty > 0 and avg_price > 0 else 0.0

        # order_usdt from config
        order_usdt = float(cfg.get("order_usdt") or cfg.get("ladder_fixed_order_usdt") or 0)
        if order_usdt <= 0 and budget_cap > 0:
            max_levels = int(cfg.get("max_levels") or 10)
            order_usdt = budget_cap / max_levels if max_levels > 0 else 10.0
        min_order_usdt = max(5.0, float(getattr(self.mgr.system, "min_order_usdt", 5.0) or 5.0))
        order_usdt = max(min_order_usdt, order_usdt)

        # portfolio update (lightweight)
        self.portfolio_guard.update(self.states, self.portfolio, btc_change_5m=self._get_btc_change())

        # === ICAG ENGINE TICK ===
        targets = self.engine.on_tick(state, price, order_usdt, self.portfolio)

        buy_targets: List[Tuple[float, float]] = targets.get("buy_targets", [])
        sell_targets: List[Tuple[float, float]] = targets.get("sell_targets", [])
        diag = targets.get("diagnostics", {})

        # budget guard: don't exceed budget_cap
        if budget_cap > 0:
            reserved_buy_usdt = sum(p * q for p, q in self._iter_active_buy_orders(market))
            holding_usdt = qty * price
            remaining = budget_cap - reserved_buy_usdt - holding_usdt
            capped_buys: List[Tuple[float, float]] = []
            for bp, bq in buy_targets:
                cost = bp * bq
                if remaining >= cost:
                    capped_buys.append((bp, bq))
                    remaining -= cost
                else:
                    break
            buy_targets = capped_buys

        # === RECONCILE: diff current orders vs targets ===
        placed_buy, placed_sell = 0, 0
        canceled = 0
        failed: List[Dict[str, Any]] = []

        # round targets to tick
        buy_targets_rounded = [(self._round_price(p, "buy"), q) for p, q in buy_targets]
        sell_targets_rounded = [(self._round_price(p, "sell"), q) for p, q in sell_targets]

        # OMA_LADDER_LIMIT_ORDERS toggle: skip order placement when disabled
        if not LADDER_LIMIT_ORDERS:
            state.open_buy_count = 0
            state.open_sell_count = 0
            self._save_states()
            return {
                "market": market, "engine": "v3_icag", "current_price": price,
                "anchor": diag.get("anchor"), "zone": diag.get("zone"),
                "bias": diag.get("bias"), "step": diag.get("step"),
                "step_pct": diag.get("step_pct"), "atr": diag.get("atr"),
                "inv_ratio": diag.get("inv_ratio"),
                "underwater_mode": diag.get("underwater_mode"),
                "buy_targets": len(buy_targets_rounded),
                "sell_targets": len(sell_targets_rounded),
                "placed_buy": 0, "placed_sell": 0, "canceled": 0,
                "failed": [], "budget_cap": budget_cap,
                "order_usdt": order_usdt, "position_qty": qty,
                "position_avg": avg_price, "limit_orders": False,
            }

        active = self._get_active_order_prices(market)
        active_buy_set = set(active["buy"])
        active_sell_set = set(active["sell"])

        target_buy_prices = {p for p, _ in buy_targets_rounded}
        target_sell_prices = {p for p, _ in sell_targets_rounded}

        # price-drift tolerance: don't cancel if existing order is within 0.3% of a target
        _DRIFT_TOL = 0.003

        def _near_any(op: float, targets: set) -> bool:
            for t in targets:
                if t > 0 and abs(op - t) / t < _DRIFT_TOL:
                    return True
            return False

        # cancel orders that are no longer near any targets
        try:
            reg = self.mgr._read_order_registry()
            m = reg.get(market, {})
            if isinstance(m, dict):
                for uuid_, meta in list(m.items()):
                    if not isinstance(meta, dict):
                        continue
                    if str(meta.get("status") or "").lower() in ("filled", "deleted"):
                        continue
                    op = float(meta.get("price") or 0)
                    side = str(meta.get("side") or "").lower()
                    should_cancel = False
                    if side in ("buy", "bid") and not _near_any(op, target_buy_prices):
                        should_cancel = True
                    elif side in ("sell", "ask") and not _near_any(op, target_sell_prices):
                        should_cancel = True
                    if should_cancel:
                        if self._cancel_order_by_uuid(market, uuid_):
                            m[uuid_]["status"] = "deleted"
                            canceled += 1
                reg[market] = m
                self._safe_write_order_registry(reg)
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.warning("V3 cancel reconcile error: %s — %s", market, e)

        # place new orders
        ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
        rid = str(ids.get("rid") or "")

        for bp, bq in buy_targets_rounded:
            if _near_any(bp, active_buy_set):
                continue  # already exists (or close enough)
            try:
                resp = self.mgr._place_limit_buy_qty(market=market, price=bp, qty=bq)
                ou = str(resp.get("uuid") or "")
                if ou:
                    self.mgr._register_order_uuid(
                        market=market, rid=rid, uuid_=ou,
                        side="buy", price=bp, seq=0, qty=bq,
                    )
                    placed_buy += 1
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning("LadderGridV3._near_any except: %s", e, exc_info=True)
                failed.append({"side": "buy", "price": bp, "error": str(e)})

        for sp, sq in sell_targets_rounded:
            if _near_any(sp, active_sell_set):
                continue
            if sq <= 0:
                continue
            try:
                resp = self.mgr._place_limit_sell_qty(market=market, price=sp, qty=sq)
                ou = str(resp.get("uuid") or "")
                if ou:
                    self.mgr._register_order_uuid(
                        market=market, rid=rid, uuid_=ou,
                        side="sell", price=sp, seq=0, qty=sq,
                    )
                    placed_sell += 1
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning("LadderGridV3._near_any except: %s", e, exc_info=True)
                failed.append({"side": "sell", "price": sp, "error": str(e)})

        # save state
        state.open_buy_count = self._count_active_orders(market, "buy")
        state.open_sell_count = self._count_active_orders(market, "sell")
        self._save_states()

        return {
            "market": market,
            "engine": "v3_icag",
            "current_price": price,
            "anchor": diag.get("anchor"),
            "zone": diag.get("zone"),
            "bias": diag.get("bias"),
            "step": diag.get("step"),
            "step_pct": diag.get("step_pct"),
            "atr": diag.get("atr"),
            "inv_ratio": diag.get("inv_ratio"),
            "underwater_mode": diag.get("underwater_mode"),
            "buy_targets": len(buy_targets_rounded),
            "sell_targets": len(sell_targets_rounded),
            "placed_buy": placed_buy,
            "placed_sell": placed_sell,
            "canceled": canceled,
            "failed": failed,
            "budget_cap": budget_cap,
            "order_usdt": order_usdt,
            "position_qty": qty,
            "position_avg": avg_price,
        }

    # ============================================================
    # Fill handling
    # ============================================================
    def on_fill(self, market: str, uuid_: str, fill_price: float, side: str) -> Dict[str, Any]:
        """Handle a fill event from the exchange."""
        state = self._get_state(market)
        cfg = self.mgr.get_config(market)
        ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
        rid = str(ids.get("rid") or "")

        # get fill qty from registry
        fill_qty = 0.0
        try:
            reg = self.mgr._read_order_registry()
            m = reg.get(market, {})
            meta = m.get(uuid_, {})
            fill_qty = float(meta.get("qty") or meta.get("volume") or 0)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[GRID_V3] get fill qty from registry: %s", exc, exc_info=True)
        if fill_qty <= 0:
            fill_qty = float(cfg.get("order_usdt", 10)) / fill_price if fill_price > 0 else 0.0

        result: Dict[str, Any] = {
            "market": market,
            "engine": "v3_icag",
            "uuid": uuid_,
            "fill_price": fill_price,
            "side": side,
        }

        # record to global trade ledger
        self._record_to_ledger(market, side, fill_price, fill_qty, uuid_)

        if side == "buy":
            pair = self.engine.on_buy_fill(state, fill_price, fill_qty)
            result.update(pair)

            # place paired sell
            sell_price = self._round_price(pair["sell_price"], "sell")
            sell_qty = pair["sell_qty"]
            if sell_price > 0 and sell_qty > 0:
                try:
                    resp = self.mgr._place_limit_sell_qty(
                        market=market, price=sell_price, qty=sell_qty,
                    )
                    ou = str(resp.get("uuid") or "")
                    if ou:
                        self.mgr._register_order_uuid(
                            market=market, rid=rid, uuid_=ou,
                            side="sell", price=sell_price, seq=0, qty=sell_qty,
                        )
                        result["paired_sell_uuid"] = ou
                        result["paired_sell_placed"] = True
                except (KeyError, AttributeError, TypeError) as e:
                    logger.warning("LadderGridV3.on_fill except: %s", e, exc_info=True)
                    result["paired_sell_placed"] = False
                    result["paired_sell_error"] = str(e)

        elif side == "sell":
            pair = self.engine.on_sell_fill(state, fill_price, fill_qty)
            result.update(pair)

            # place paired buy (only if bias allows)
            if state.bias != "SELL" and state.zone != "RISK_CUT":
                buy_price = self._round_price(pair["buy_price"], "buy")
                buy_qty = pair["buy_qty"]
                if buy_price > 0 and buy_qty > 0:
                    try:
                        resp = self.mgr._place_limit_buy_qty(
                            market=market, price=buy_price, qty=buy_qty,
                        )
                        ou = str(resp.get("uuid") or "")
                        if ou:
                            self.mgr._register_order_uuid(
                                market=market, rid=rid, uuid_=ou,
                                side="buy", price=buy_price, seq=0, qty=buy_qty,
                            )
                            result["paired_buy_uuid"] = ou
                            result["paired_buy_placed"] = True
                    except (KeyError, AttributeError, TypeError) as e:
                        logger.warning("LadderGridV3.on_fill except: %s", e, exc_info=True)
                        result["paired_buy_placed"] = False
                        result["paired_buy_error"] = str(e)

        self._save_states(force=True)
        return result

    # ============================================================
    # Poll and sync (fill detection + grid sync)
    # ============================================================
    def poll_and_sync(self, market: str) -> Dict[str, Any]:
        """Detect fills then sync grid. Mirror of GridV2.poll_and_sync."""
        fills_detected: List[Dict[str, Any]] = []

        try:
            reg = self.mgr._read_order_registry()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("LadderGridV3.poll_and_sync suppressed exception", exc_info=True)
            reg = {}
        m = reg.get(market)
        if not isinstance(m, dict):
            m = {}

        u = None
        try:
            u = getattr(self.mgr, 'trade_client', None) or getattr(getattr(self.mgr, 'system', None), 'trade_client', None)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("LadderGridV3.poll_and_sync: trade_client unavailable: %s", e)
            return {"market": market, "error": str(e)}

        updated = False
        for uuid_ in list(m.keys()):
            step = m[uuid_]
            if not isinstance(step, dict):
                continue
            if step.get("status") in ("filled", "deleted"):
                continue
            try:
                order_info = u.get_order(uuid_)
                order_state = order_info.get("state") if order_info else None
                if order_state == "done":
                    m[uuid_]["status"] = "filled"
                    m[uuid_]["filled_ts"] = time.time()
                    updated = True
                    try:
                        m[uuid_]["qty"] = float(order_info.get("executed_volume") or 0)
                        m[uuid_]["volume"] = m[uuid_]["qty"]
                        m[uuid_]["avg_price"] = float(
                            order_info.get("avg_price") or order_info.get("price") or 0
                        )
                        m[uuid_]["fee"] = float(order_info.get("paid_fee") or 0)
                    except (TypeError, ValueError) as exc:
                        logger.warning("[GRID_V3] ladder_grid_v3.poll_and_sync fallback: %s", exc, exc_info=True)

                    fill_price = float(
                        order_info.get("avg_price")
                        or order_info.get("price")
                        or step.get("price")
                        or 0
                    )
                    side_raw = str(step.get("side") or "")
                    fill_result = self.on_fill(market, uuid_, fill_price, side_raw)
                    fills_detected.append(fill_result)

                elif order_state == "cancel":
                    m[uuid_]["status"] = "deleted"
                    updated = True
            except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
                logger.warning("V3 poll order %s error: %s", uuid_[:12], e)

        if updated:
            reg[market] = m
            self._safe_write_order_registry(reg)

        sync_result = self.sync_active_window(market)

        return {
            "market": market,
            "engine": "v3_icag",
            "fills": fills_detected,
            "sync": sync_result,
        }

    # ============================================================
    # Helpers
    # ============================================================
    def _iter_active_buy_orders(self, market: str):
        """Yield (price, qty) for active buy orders."""
        try:
            reg = self.mgr._read_order_registry()
            m = reg.get(market, {})
            if isinstance(m, dict):
                for meta in m.values():
                    if not isinstance(meta, dict):
                        continue
                    if str(meta.get("status") or "").lower() in ("filled", "deleted"):
                        continue
                    if str(meta.get("side") or "").lower() not in ("buy", "bid"):
                        continue
                    p = float(meta.get("price") or 0)
                    q = float(meta.get("qty") or 0)
                    if p > 0 and q > 0:
                        yield p, q
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[GRID_V3] ladder_grid_v3._iter_active_buy_orders except-> return: %s", exc, exc_info=True)
            return

    def _record_to_ledger(
        self, market: str, side: str, price: float, qty: float, uuid_: str,
    ) -> None:
        """Write fill event to global TradeLedger for PnL tracking."""
        try:
            ledger = getattr(self.mgr.system, "trade_ledger", None)
            if ledger is None:
                return
            event = "FILL_BUY" if side == "buy" else "FILL_SELL"
            ledger.append(
                event,
                market=market,
                price=price,
                qty=qty,
                volume=price * qty,
                fee=price * qty * self.icag_cfg.fee_rate,
                uuid=uuid_,
                engine="icag_v3",
                strategy="LADDER",
            )
        except (KeyError, AttributeError, TypeError) as e:
            logger.warning("V3 ledger write failed: %s — %s", market, e)

    def _get_btc_change(self) -> float:
        """Get BTC 5-minute price change % (for correlation guard)."""
        try:
            from app.strategy.ladder_icag.atr import _fetch_candles
            candles = _fetch_candles("BTCUSDT", 5, count=2)
            if candles and len(candles) >= 2:
                cur = float(candles[0].get("trade_price") or 0)
                prev = float(candles[1].get("trade_price") or 0)
                if prev > 0:
                    return (cur - prev) / prev * 100.0
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[GRID_V3] ladder_grid_v3._get_btc_change fallback: %s", exc, exc_info=True)
        return 0.0

    def _auto_disable_market(self, market: str, reason: str) -> None:
        """Disable a LADDER market: config off + OMA → WATCH + cancel orders."""
        try:
            cfg = self.mgr.get_config(market)
            cfg["enabled"] = False
            self.mgr.save_config(cfg)
        except (KeyError, AttributeError, TypeError) as e:
            logger.warning("auto_disable config %s: %s", market, e)

        try:
            from app.manager.oma_market_registry import MarketState
            self.mgr.system.oma_set_market(
                market, MarketState.WATCH, reason=[reason],
            )
        except (ImportError, AttributeError, TypeError) as e:
            logger.warning("auto_disable OMA %s: %s", market, e)

        self._cancel_all_orders(market)

        state = self._get_state(market)
        state.activation_ts = 0.0
        self._save_states(force=True)
        logger.info("ICAG auto-disabled %s: %s", market, reason)

    def get_diagnostics(self, market: str) -> Dict[str, Any]:
        """Return current ICAG state for UI/debugging."""
        state = self._get_state(market)
        return {
            "market": market,
            "engine": "v3_icag",
            **state.to_dict(),
            "portfolio": self.portfolio.to_dict(),
        }

    # ============================================================
    # Bootstrap: scan exchange positions → register as LADDER
    # ============================================================
    def bootstrap_from_positions(
        self,
        default_budget_usdt: float = 100,
        default_order_usdt: float = 10,
        default_max_levels: int = 10,
        min_position_usdt: float = 5.0,
    ) -> Dict[str, Any]:
        """Scan exchange balances → ONLY coins with positions become LADDER.

        1. Disable ALL existing LADDER configs
        2. Demote ALL LADDER markets (ACTIVE/RECOVERY/WATCH) to WATCH + clear strategy
        3. Purge stale ICAG states from memory & disk
        4. Register ONLY coins with actual positions
        """
        from app.manager.oma_market_registry import MarketState
        system = self.mgr.system

        # --- Phase 1: Disable ALL existing ladder configs ---
        disabled_markets: List[str] = []
        try:
            all_configs = self.mgr.list_configs()
            for cfg in all_configs:
                if not isinstance(cfg, dict):
                    continue
                mk = cfg.get("market", "")
                if mk and cfg.get("enabled"):
                    cfg["enabled"] = False
                    self.mgr.save_config(cfg)
                    disabled_markets.append(mk)
        except (KeyError, AttributeError, TypeError) as e:
            logger.warning("Bootstrap phase1 (disable all) failed: %s", e)

        # --- Phase 2: Demote ALL LADDER markets (not just ACTIVE) ---
        demoted: List[str] = []
        try:
            all_oma = system.oma_registry.list_all()
            for mk in all_oma:
                try:
                    ctx = system.coordinator.contexts.get(mk)
                    if ctx is None:
                        continue
                    ctrl = getattr(ctx, "controls", None) or {}
                    strat = ctrl.get("strategy", {}) if isinstance(ctrl, dict) else {}
                    if str(strat.get("mode", "")).upper() == "LADDER":
                        # Clear LADDER strategy from context to prevent re-promotion
                        try:
                            strat["enabled"] = False
                            strat["mode"] = ""
                            if hasattr(ctx, "update_controls"):
                                ctx.update_controls({"strategy": strat})
                        except (KeyError, AttributeError, TypeError) as exc:
                            logger.warning("[GRID_V3] Clear LADDER strategy from context to prevent re-promotion: %s", exc, exc_info=True)
                        cur_state = system.oma_registry.get_state(mk)
                        if cur_state != MarketState.WATCH:
                            system.oma_set_market(mk, MarketState.WATCH, reason=["icag_bootstrap_cleanup"])
                        demoted.append(mk)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[GRID_V3] Clear LADDER strategy from context to prevent re-promotion: %s", exc, exc_info=True)
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.warning("Bootstrap phase2 (demote) failed: %s", e)

        # --- Phase 2.5: Purge stale ICAG states ---
        purged_states = list(self.states.keys())
        self.states.clear()
        self._save_states(force=True)
        logger.info("Bootstrap: purged %d stale ICAG states", len(purged_states))

        # --- Phase 3: Scan exchange → register ONLY coins with positions ---
        registered: List[Dict[str, Any]] = []
        try:
            u = self.mgr.get_trade_client()
            accounts = u.accounts(skip_currencies=["USDT"])
        except (KeyError, AttributeError, TypeError) as e:
            logger.warning("LadderGridV3.bootstrap_from_positions except: %s", e, exc_info=True)
            return {"ok": False, "error": f"Exchange accounts failed: {e}",
                    "disabled": disabled_markets, "demoted": demoted}

        for acc in accounts:
            currency = acc.get("currency", "")
            if not currency:
                continue
            balance = float(acc.get("balance") or 0)
            locked = float(acc.get("locked") or 0)
            avg_price = float(acc.get("avg_buy_price") or 0)
            total_qty = balance + locked
            if total_qty <= 0:
                continue

            market = f"{currency}USDT"

            cur_price = self.mgr.get_current_price(market) or avg_price
            if cur_price <= 0:
                cur_price = icag_get_price(market)
            if cur_price <= 0:
                continue

            position_usdt = total_qty * cur_price
            if position_usdt < min_position_usdt:
                continue

            budget = max(default_budget_usdt, position_usdt * 2)
            order_usdt = max(default_order_usdt, int(budget / default_max_levels * 100) / 100)
            order_usdt = max(5, order_usdt)

            # ladder_config
            existing = self.mgr.get_config(market)
            existing["market"] = market
            existing["enabled"] = True
            existing["grid_auto_sync"] = True
            existing["budget_usdt"] = budget
            existing["order_usdt"] = order_usdt
            existing["max_levels"] = default_max_levels
            if not existing.get("ladder_fixed_order_usdt"):
                existing["ladder_fixed_order_usdt"] = order_usdt
            self.mgr.save_config(existing)

            # OMA ACTIVE + LADDER
            # NOTE: Bootstrap은 사용자 수동 Deploy로만 호출됨 (confidence gate N/A)
            try:
                system.oma_set_market(
                    market, MarketState.ACTIVE,
                    reason=["icag_bootstrap"], budget_usdt=budget,
                )
                ctx = system.coordinator.ensure_market(market)
                ctx.update_controls({
                    "strategy": {
                        "enabled": True,
                        "mode": "LADDER",
                        "params": {
                            "grid_auto_sync": True,
                            "max_steps": default_max_levels,
                            "step_pct": 1.0,
                            "tp": 3.0,
                            "sl": -5.0,
                        },
                    }
                })
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning("Bootstrap OMA/context failed for %s: %s", market, e)

            # ICAG state
            state = self._get_state(market)
            state.position_qty = total_qty
            state.position_avg_price = avg_price
            state.budget_allocated = budget

            registered.append({
                "market": market,
                "position_qty": round(total_qty, 6),
                "position_usdt": round(position_usdt),
                "avg_price": avg_price,
                "budget_usdt": budget,
                "order_usdt": order_usdt,
            })

        self._save_states(force=True)
        try:
            system._save_context_state()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[GRID_V3] ICAG state: %s", exc, exc_info=True)

        return {
            "ok": True,
            "disabled_old": len(disabled_markets),
            "demoted_to_watch": len(demoted),
            "purged_states": len(purged_states),
            "registered": len(registered),
            "markets": registered,
        }
