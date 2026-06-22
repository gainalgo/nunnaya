from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR
from typing import Any, Dict, List, Optional, Tuple

from app.manager.ladder_manager import LadderManager
from app.core.currency import Q

logger = logging.getLogger(__name__)

STATE_PATH = os.path.join("runtime", "ladder_grid_state.json")

_DEFAULT_MARKET_STATE: Dict[str, Any] = {
    "active_window_n": 2,
    "enabled": True,
    "last_rebalance_ts": 0.0,
    "rebalance_cooldown_sec": 10,
    "consecutive_down_buys": 0,
    "max_consecutive_down_buys": 2,
    # Additional hard guard: count buy fills until a sell occurs (price wiggles do not reset this).
    "consecutive_buys_without_sell": 0,
    "max_consecutive_buys_without_sell": 2,
    "last_buy_fill_price": 0.0,
    "consecutive_up_sells": 0,
    "max_consecutive_up_sells": 3,
    "last_sell_fill_price": 0.0,
    "blocked": False,
    "blocked_reason": "",
    "blocked_budget_usdt": 0,
    "blocked_budget_last_price": 0.0,
    "sell_blocked": False,
    "sell_blocked_reason": "",
    "blocked_sell_qty": 0.0,
    "blocked_sell_peak_price": 0.0,
    "sell_pullback_pct": 1.0,
    "max_buy_gap_pct": 20.0,
    # Auto-center anchor: recenter only after at least one spacing-step move.
    "auto_center_anchor_price": 0.0,
    # Fallback fill detector anchor (guards repeated buys when exchange fill polling misses).
    "last_seen_available_qty": 0.0,
    "last_buy_rearm_block_ts": 0.0,
    "downtrend_last_shift_ts": 0.0,
    "downtrend_last_shift_price": 0.0,
    # If a market keeps running without any active BUY line, follow current price and recover.
    "no_buy_since_ts": 0.0,
    "last_no_buy_follow_ts": 0.0,
    "no_buy_follow_sec": 900.0,
    "no_buy_demote_sec": 7200.0,
    "paused_steps": [],
    "skipped_steps": [],
    "fill_history": [],
}

_MAX_FILL_HISTORY = 200


class LadderGridV2:

    def __init__(self, mgr: LadderManager) -> None:
        self.mgr = mgr
        self._state_cache: Optional[Dict[str, Any]] = None  # [FIX H3] 인메모리 캐시 — tick마다 반복 파일 I/O 방지

    # --------------------------------------------------------
    # Tick size helper
    # --------------------------------------------------------
    def get_tick_size(self, price: float) -> float:
        """Bybit USDT 마켓 공식 호가 단위."""
        try:
            p = Decimal(str(price))
            if p <= 0:
                return 0.001
            if p >= Decimal("2000000"):
                return 1000.0
            if p >= Decimal("1000000"):
                return 500.0
            if p >= Decimal("500000"):
                return 100.0
            if p >= Decimal("100000"):
                return 50.0
            if p >= Decimal("10000"):
                return 10.0
            if p >= Decimal("1000"):
                return 5.0
            if p >= Decimal("100"):
                return 1.0
            if p >= Decimal("10"):
                return 0.1
            if p >= Decimal("1"):
                return 0.01
            return 0.001
        except (TypeError, ValueError, OverflowError):
            logger.warning("LadderGridV2.get_tick_size suppressed exception", exc_info=True)
            return 0.001

    # --------------------------------------------------------
    # Core: sync active window
    # --------------------------------------------------------
    def sync_active_window(self, market: str) -> Dict[str, Any]:
        mstate = self._get_market_state(market)
        if not mstate.get("enabled", True):
            return {"market": market, "skipped": True, "reason": "disabled"}

        # Guard: do not run ladder sync when market mode is explicitly conflicting.
        try:
            self.mgr.validate_exclusive_mode(market)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("LadderGridV2.sync_active_window except: %s", e, exc_info=True)
            return {
                "market": market,
                "skipped": True,
                "reason": "mode_conflict",
                "detail": str(e),
            }

        # Guard: if core OSM has a pending order state, skip ladder rebalancing to avoid order-path collisions.
        try:
            ctx_guard = self.mgr.system.coordinator.contexts.get(market)
        except AttributeError:
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            ctx_guard = None
        if ctx_guard is not None and getattr(ctx_guard, "order_state", None):
            return {"market": market, "skipped": True, "reason": "order_state_busy"}

        now = time.time()
        cooldown = float(mstate.get("rebalance_cooldown_sec", 10))
        last_ts = float(mstate.get("last_rebalance_ts", 0.0))
        if (now - last_ts) < cooldown:
            return {
                "market": market,
                "skipped": True,
                "reason": "cooldown",
                "next_sec": round(cooldown - (now - last_ts), 1),
            }

        current_price = self.mgr.get_current_price(market)
        if not current_price or current_price <= 0:
            return {"market": market, "skipped": True, "reason": "no_price"}

        cfg = self.mgr.get_config(market)
        cfg_dirty = False
        try:
            ctx = self.mgr.system.coordinator.contexts.get(market)
        except AttributeError:
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            ctx = None
        strategy_params = {}
        try:
            if ctx is not None:
                ctrls = getattr(ctx, "controls", None) or {}
                if isinstance(ctrls, dict):
                    strategy_params = dict((ctrls.get("strategy") or {}).get("params") or {})
        except (AttributeError, TypeError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            strategy_params = {}

        # Recover broken config values from strategy params / budget fallback.
        try:
            max_levels = int(cfg.get("max_levels") or 0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            max_levels = 0
        if max_levels <= 0:
            try:
                max_levels = int(strategy_params.get("max_steps") or strategy_params.get("steps") or 10)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
                max_levels = 10
            max_levels = max(1, max_levels)
            cfg = dict(cfg)
            cfg["max_levels"] = max_levels
            cfg_dirty = True

        try:
            order_usdt = int(cfg.get("order_usdt") or 0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            order_usdt = 0
        min_order_buffer_usdt = 10
        try:
            min_order_buffer_usdt = max(
                1, int(float(os.getenv("OMA_LADDER_MIN_ORDER_BUFFER_USDT", "10") or 10))
            )
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            min_order_buffer_usdt = 10

        min_order_usdt = 5
        try:
            sys_min = int(float(getattr(self.mgr.system, "min_order_usdt", 5) or 5))
            min_order_usdt = max(min_order_usdt, sys_min)
        except (TypeError, ValueError, AttributeError) as exc:
            logger.warning("[GRID] Recover broken config values from strategy params / budget fallback: %s", exc, exc_info=True)
        try:
            min_order_usdt = max(
                min_order_usdt,
                int(float(strategy_params.get("min_order_usdt") or min_order_usdt)),
            )
        except (TypeError, ValueError) as exc:
            logger.warning("[GRID] Recover broken config values from strategy params / budget fallback: %s", exc, exc_info=True)
        min_order_usdt = int(min_order_usdt) + int(min_order_buffer_usdt)
        min_order_usdt = max(5, min_order_usdt)
        if order_usdt > 0 and order_usdt < min_order_usdt:
            order_usdt = min_order_usdt
            cfg = dict(cfg)
            cfg["order_usdt"] = order_usdt
            cfg_dirty = True
        if order_usdt <= 0:
            fallback_order = 0
            try:
                fixed = int(cfg.get("ladder_fixed_order_usdt") or 0)
                if fixed > 0:
                    fallback_order = fixed
            except (TypeError, ValueError):
                logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
                fallback_order = 0
            if fallback_order <= 0:
                budget_cap = self._get_budget_cap(market, cfg)
                if budget_cap > 0 and max_levels > 0:
                    fallback_order = int(float(budget_cap) / float(max_levels))
            order_usdt = max(min_order_usdt, int(fallback_order or 0))
            cfg = dict(cfg)
            cfg["order_usdt"] = order_usdt
            cfg_dirty = True

        try:
            spacing_mode = str(cfg.get("spacing_mode") or "PERCENT").upper()
            spacing_value = float(cfg.get("spacing_value") or 0.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            spacing_mode = "PERCENT"
            spacing_value = 0.0
        if spacing_value <= 0:
            try:
                spacing_value = float(
                    strategy_params.get("spacing_value")
                    or strategy_params.get("step_pct")
                    or 1.0
                )
            except (TypeError, ValueError):
                logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
                spacing_value = 1.0
            cfg = dict(cfg)
            cfg["spacing_mode"] = spacing_mode if spacing_mode in ("PERCENT", "FIXED") else "PERCENT"
            cfg["spacing_value"] = max(0.0001, spacing_value)
            cfg_dirty = True

        if cfg_dirty:
            try:
                self.mgr.save_config(cfg)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.error("[GRID] save_config failed: %s", exc, exc_info=True)
        try:
            env_max_down = int(float(os.getenv("OMA_GRID_MAX_CONSECUTIVE_DOWN_BUYS", "2") or 2))
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            env_max_down = 2
        env_max_down = max(1, env_max_down)
        try:
            env_max_buy_wo_sell = int(
                float(
                    os.getenv(
                        "OMA_GRID_MAX_CONSECUTIVE_BUYS_WITHOUT_SELL",
                        str(env_max_down),
                    )
                    or env_max_down
                )
            )
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            env_max_buy_wo_sell = env_max_down
        env_max_buy_wo_sell = max(1, env_max_buy_wo_sell)
        try:
            cfg_max_down = int(
                cfg.get("max_down_buys")
                or cfg.get("max_consecutive_down_buys")
                or env_max_down
            )
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            cfg_max_down = env_max_down
        # Hard cap by global guard so runtime config drift cannot silently loosen risk.
        cfg_max_down = max(1, min(cfg_max_down, env_max_down))
        mstate["max_consecutive_down_buys"] = cfg_max_down
        try:
            cfg_max_buy_wo_sell = int(
                cfg.get("max_buy_fills_without_sell")
                or cfg.get("max_consecutive_buys_without_sell")
                or cfg_max_down
            )
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            cfg_max_buy_wo_sell = cfg_max_down
        cfg_max_buy_wo_sell = max(1, min(cfg_max_buy_wo_sell, env_max_buy_wo_sell))
        mstate["max_consecutive_buys_without_sell"] = cfg_max_buy_wo_sell
        try:
            cfg_max_up = int(
                cfg.get("max_up_sells")
                or cfg.get("max_consecutive_up_sells")
                or 0
            )
            if cfg_max_up > 0:
                mstate["max_consecutive_up_sells"] = cfg_max_up
        except (TypeError, ValueError) as exc:
            logger.warning("[GRID] Hard cap by global guard — config drift for max_consecutive_up_sells: %s", exc, exc_info=True)
        try:
            cfg_pullback = float(
                cfg.get("sell_pullback_pct")
                or cfg.get("sell_rebound_pullback_pct")
                or 0.0
            )
            if cfg_pullback > 0:
                mstate["sell_pullback_pct"] = cfg_pullback
        except (TypeError, ValueError) as exc:
            logger.warning("[GRID] Hard cap by global guard — config drift for sell_pullback_pct: %s", exc, exc_info=True)
        if cfg.get("auto_center") and current_price > 0:
            mode = str(cfg.get("spacing_mode") or "PERCENT").upper()
            spacing_val = float(cfg.get("spacing_value") or 0.5)
            max_levels = int(cfg.get("max_levels") or 10)
            if spacing_val > 0 and max_levels > 0:
                pre_lower = float(cfg.get("lower_bound") or 0.0)
                pre_upper = float(cfg.get("upper_bound") or 0.0)
                try:
                    anchor_price = float(mstate.get("auto_center_anchor_price") or 0.0)
                except (TypeError, ValueError):
                    logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
                    anchor_price = 0.0
                try:
                    min_steps = float(os.getenv("OMA_GRID_AUTO_CENTER_MIN_STEPS", "1.0") or 1.0)
                except (TypeError, ValueError):
                    logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
                    min_steps = 1.0
                if min_steps <= 0:
                    min_steps = 1.0
                if mode == "FIXED":
                    move_unit = spacing_val
                else:
                    base_for_step = anchor_price if anchor_price > 0 else current_price
                    move_unit = base_for_step * (spacing_val / 100.0)
                move_threshold = max(1e-9, float(move_unit) * float(min_steps))
                needs_recenter = (
                    anchor_price <= 0
                    or abs(float(current_price) - float(anchor_price)) >= move_threshold
                    or pre_lower <= 0
                    or pre_upper <= 0
                    or current_price < pre_lower
                    or current_price > pre_upper
                )
                if needs_recenter:
                    per_side = max(1, max_levels // 2)
                    if mode == "FIXED":
                        lower_center = current_price - (spacing_val * per_side)
                        upper_center = current_price + (spacing_val * per_side)
                    else:
                        lower_center = current_price * (1.0 - (spacing_val / 100.0) * per_side)
                        upper_center = current_price * (1.0 + (spacing_val / 100.0) * per_side)
                    if lower_center > 0 and upper_center > lower_center:
                        cfg = dict(cfg)
                        cfg["lower_bound"] = lower_center
                        cfg["upper_bound"] = upper_center
                        mstate["auto_center_anchor_price"] = float(current_price)
                        try:
                            self.mgr.save_config(cfg)
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                            logger.error("[GRID] save_config failed during auto_center: %s", exc, exc_info=True)
        lower = float(cfg.get("lower_bound") or 0.0)
        upper = float(cfg.get("upper_bound") or 0.0)

        if lower > 0 and upper > 0 and (current_price < lower * 0.5 or current_price > upper * 2.0):
            canceled = self._cancel_all_orders(market)
            logger.warning(
                "GridV2 %s price %.2f outside grid range [%.2f ~ %.2f] — canceled %d orders, skipping sync",
                market, current_price, lower, upper, canceled,
            )
            return {
                "market": market,
                "skipped": True,
                "reason": "price_out_of_range",
                "current_price": current_price,
                "lower_bound": lower,
                "upper_bound": upper,
                "canceled": canceled,
            }

        bounds_stale = (lower <= 0 or upper <= 0
                        or current_price < lower
                        or current_price > upper)
        if bounds_stale:
            cfg = self._auto_reconfigure(market, current_price, cfg)
            lower = float(cfg.get("lower_bound") or 0.0)
            upper = float(cfg.get("upper_bound") or 0.0)
            if lower <= 0 or upper <= 0:
                return {"market": market, "skipped": True, "reason": "auto_reconfig_failed"}

        levels = self.mgr.calc_levels(cfg)
        if not levels:
            return {"market": market, "skipped": True, "reason": "no_levels"}

        levels = [p for p in levels if lower <= p <= upper]

        max_buy_gap_pct = float(mstate.get("max_buy_gap_pct", 20.0))
        try:
            max_buy_gap_cap = float(os.getenv("OMA_GRID_MAX_BUY_GAP_PCT_CAP", "20") or 20.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            max_buy_gap_cap = 20.0
        if max_buy_gap_cap > 0:
            max_buy_gap_pct = min(max_buy_gap_pct, max_buy_gap_cap)
            mstate["max_buy_gap_pct"] = max_buy_gap_pct
        min_buy_price = current_price * (1.0 - max_buy_gap_pct / 100.0)

        has_holding = self._has_holding(market)
        _raw_avail = self._get_available_qty(market)
        if _raw_avail < 0:
            logger.warning("[GRID] %s: balance query failed, skipping tick", market)
            return {}
        available_qty = _raw_avail
        active_buy_count = 0
        try:
            reg_qty = self.mgr._read_order_registry()
            m_qty = reg_qty.get(market, {})
            if isinstance(m_qty, dict):
                active_buy_count = sum(
                    1
                    for meta in m_qty.values()
                    if isinstance(meta, dict)
                    and str(meta.get("side") or "").lower() == "buy"
                    and str(meta.get("status") or "").lower() in ("active", "open")
                )
        except (AttributeError, KeyError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            active_buy_count = 0
        try:
            prev_seen_qty = float(mstate.get("last_seen_available_qty", 0.0) or 0.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            prev_seen_qty = 0.0
        mstate["last_seen_available_qty"] = float(available_qty or 0.0)
        qty_delta = float(available_qty or 0.0) - float(prev_seen_qty)
        # Guard: first baseline snapshot (prev_seen_qty<=0) is not a fill signal.
        # Otherwise any initial holding can be misread as a new BUY fill.
        if prev_seen_qty > 0 and qty_delta > 0 and active_buy_count > 0:
            est_fill_usdt = float(qty_delta) * float(current_price or 0.0)
            fill_detect_threshold = max(1000.0, float(min_order_usdt) * 0.5)
            try:
                cfg_order_usdt = float(cfg.get("order_usdt") or 0.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
                cfg_order_usdt = 0.0
            max_reasonable_fill_usdt = max(
                float(min_order_usdt) * 6.0,
                (cfg_order_usdt * 4.0) if cfg_order_usdt > 0 else 0.0,
            )
            if fill_detect_threshold <= est_fill_usdt <= max_reasonable_fill_usdt:
                last_fill = float(mstate.get("last_buy_fill_price", 0.0) or 0.0)
                consec = int(mstate.get("consecutive_down_buys", 0) or 0)
                max_consec = int(mstate.get("max_consecutive_down_buys", 2) or 2)
                buy_run = int(mstate.get("consecutive_buys_without_sell", 0) or 0) + 1
                max_buy_run = int(
                    mstate.get("max_consecutive_buys_without_sell", max_consec) or max_consec
                )
                if last_fill > 0:
                    if current_price <= last_fill:
                        consec += 1
                    else:
                        consec = 0
                else:
                    consec = 1
                mstate["consecutive_down_buys"] = consec
                mstate["consecutive_buys_without_sell"] = buy_run
                mstate["last_buy_fill_price"] = float(current_price)
                if (consec >= max_consec or buy_run >= max_buy_run) and not mstate.get("blocked", False):
                    mstate["blocked"] = True
                    if buy_run >= max_buy_run:
                        mstate["blocked_reason"] = (
                            f"qty_delta_buy_run={buy_run} >= {max_buy_run}"
                        )
                    else:
                        mstate["blocked_reason"] = (
                            f"qty_delta_down_buys={consec} >= {max_consec}"
                        )
                    self._cancel_all_buys(market)
                    logger.warning(
                        "GridV2 BLOCKED(by qty delta) %s: down=%d/%d buy_run=%d/%d",
                        market, consec, max_consec, buy_run, max_buy_run,
                    )
            elif est_fill_usdt > max_reasonable_fill_usdt:
                logger.debug(
                    "GridV2 qty_delta ignored as abnormal jump: %s est_fill_usdt=%.0f max_reasonable=%.0f",
                    market, est_fill_usdt, max_reasonable_fill_usdt,
                )

        buy_levels_all = sorted(
            [self.mgr.round_to_tick(p, side="buy") for p in levels
             if p < current_price and p >= min_buy_price],
            reverse=True,
        )
        sell_levels_all = sorted(
            [self.mgr.round_to_tick(p, side="sell") for p in levels if p > current_price],
        )

        # If no holding and no buy levels are generated, recenter bounds around current price
        # to ensure there is always at least one BUY below the market.
        if not has_holding and not buy_levels_all:
            mode = str(cfg.get("spacing_mode") or "PERCENT").upper()
            spacing_val = float(cfg.get("spacing_value") or 0.0)
            max_levels = int(cfg.get("max_levels") or 0)
            if current_price > 0 and spacing_val > 0 and max_levels > 0:
                per_side = max(1, max_levels // 2)
                if mode == "FIXED":
                    lower_center = current_price - (spacing_val * per_side)
                    upper_center = current_price + (spacing_val * per_side)
                else:
                    lower_center = current_price * (1.0 - (spacing_val / 100.0) * per_side)
                    upper_center = current_price * (1.0 + (spacing_val / 100.0) * per_side)
                if lower_center > 0 and upper_center > lower_center:
                    cfg["lower_bound"] = round(lower_center, 2)
                    cfg["upper_bound"] = round(upper_center, 2)
                    try:
                        self.mgr.save_config(cfg)
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.error("[GRID] save_config failed ensuring BUY below market: %s", exc, exc_info=True)
                    lower = float(cfg.get("lower_bound") or lower)
                    upper = float(cfg.get("upper_bound") or upper)
                    levels = self.mgr.calc_levels(cfg)
                    levels = [p for p in levels if lower <= p <= upper]
                    buy_levels_all = sorted(
                        [self.mgr.round_to_tick(p, side="buy") for p in levels
                         if p < current_price and p >= min_buy_price],
                        reverse=True,
                    )
                    sell_levels_all = sorted(
                        [self.mgr.round_to_tick(p, side="sell") for p in levels if p > current_price],
                    )

        # Safety cap: keep active window compact to avoid over-reserving budget.
        try:
            n_raw = int(mstate.get("active_window_n", 2))
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            n_raw = 2
        n = max(1, min(3, n_raw))
        if n != n_raw:
            mstate["active_window_n"] = n
        paused_set = set(float(p) for p in mstate.get("paused_steps", []))
        skipped_set = set(float(p) for p in mstate.get("skipped_steps", []))
        excluded = paused_set | skipped_set

        sell_lock_mode = str(cfg.get("sell_lock_mode") or "TRAIL_UP").upper()
        sell_lock_enabled = sell_lock_mode not in ("OFF", "DISABLED", "NONE", "0", "FALSE")
        sell_lock_trailing = sell_lock_mode in ("TRAIL", "TRAIL_UP", "UP")
        sell_blocked = bool(mstate.get("sell_blocked", False))

        target_buys = [p for p in buy_levels_all if p not in excluded][:n]
        target_sells = [p for p in sell_levels_all if p not in excluded][:n] if not sell_lock_enabled else []
        if sell_blocked:
            target_sells = []

        # Global cap (2~3): keep reservations compact, but retain one buffer-buy slot on stress.
        try:
            max_active_orders = int(cfg.get("max_active_orders_total") or 3)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.sync_active_window suppressed exception", exc_info=True)
            max_active_orders = 3
        max_active_orders = max(2, min(3, max_active_orders))

        def _apply_target_caps(
            buys: List[float],
            sells: List[float],
            *,
            prefer_buffer_buy: bool,
        ) -> Tuple[List[float], List[float], int]:
            eff_max = max_active_orders
            # In stress mode, force one extra BUY slot (2 BUY + 1 SELL = 3).
            if prefer_buffer_buy and eff_max < 3:
                eff_max = 3

            if sell_lock_enabled:
                buy_cap = eff_max - 1 if (has_holding and available_qty > 0) else eff_max
                buy_cap = max(1, buy_cap)
                if prefer_buffer_buy:
                    buy_cap = max(2, buy_cap)
                return buys[:buy_cap], [], eff_max

            if has_holding and available_qty > 0:
                sell_cap = 1
                buy_cap = max(1, eff_max - sell_cap)
                if prefer_buffer_buy:
                    buy_cap = max(2, buy_cap)
                return buys[:buy_cap], sells[:sell_cap], eff_max

            return buys[:eff_max], [], eff_max

        target_buys, target_sells, effective_max_active_orders = _apply_target_caps(
            target_buys,
            target_sells,
            prefer_buffer_buy=False,
        )

        ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
        rid = str(ids.get("rid") or "")

        reg = self.mgr._read_order_registry()
        market_reg = reg.get(market)
        if not isinstance(market_reg, dict):
            market_reg = {}

        live_uuids = self._get_live_order_uuids(market)

        registry_dirty = False
        for uuid_, meta in list(market_reg.items()):
            if not isinstance(meta, dict):
                continue
            if meta.get("status") in ("filled", "deleted"):
                continue
            if uuid_ not in live_uuids:
                meta["status"] = "deleted"
                registry_dirty = True
                logger.info("GridV2 registry cleanup: %s %s price=%.2f (not on exchange)", market, uuid_[:12], float(meta.get("price") or 0))

        if sell_lock_enabled:
            for uuid_, meta in list(market_reg.items()):
                if not isinstance(meta, dict):
                    continue
                if meta.get("status") in ("filled", "deleted"):
                    continue
                if meta.get("side") == "sell" and not meta.get("lock_sell"):
                    price = float(meta.get("price") or 0)
                    if price > 0:
                        meta["lock_sell"] = True
                        meta.setdefault("min_sell_price", price)
                        meta.setdefault("entry_price", 0.0)
                        registry_dirty = True

        if registry_dirty:
            reg[market] = market_reg
            self.mgr._write_order_registry(reg)

        active_meta: Dict[str, Dict[str, Any]] = {}
        active_prices: set = set()
        for uuid_, meta in market_reg.items():
            if not isinstance(meta, dict):
                continue
            status = meta.get("status", "active")
            if status in ("filled", "deleted"):
                continue
            price = float(meta.get("price") or 0)
            if price > 0:
                active_meta[uuid_] = meta
                active_prices.add(price)

        if sell_lock_trailing and active_meta:
            for uuid_, meta in list(active_meta.items()):
                if meta.get("side") != "sell" or not meta.get("lock_sell"):
                    continue
                out = self._maybe_raise_locked_sell(market, uuid_, meta, current_price, cfg)
                if out:
                    new_uuid, new_meta = out
                    active_meta.pop(uuid_, None)
                    active_meta[new_uuid] = new_meta
                    try:
                        active_prices.discard(float(meta.get("price") or 0))
                        active_prices.add(float(new_meta.get("price") or 0))
                    except (TypeError, ValueError) as exc:
                        logger.warning("[GRID] active price update failed: %s", exc, exc_info=True)

        # Emergency last-step (optional): insert a closer buy when sell gap is too wide.
        emergency_buy_price = None
        try:
            # Default ON when key is missing: helps fill widened buy/sell gap on fast markets.
            emergency_enabled = bool(cfg.get("emergency_last_step_enabled", True))
            mode = str(cfg.get("spacing_mode") or "PERCENT").upper()
            spacing_val = float(cfg.get("spacing_value") or 0.0)
            gap_mult = float(cfg.get("emergency_last_step_gap_mult") or 2.0)
            buy_mult = float(cfg.get("emergency_last_step_buy_mult") or 0.5)
            if emergency_enabled and spacing_val > 0 and gap_mult > 0 and buy_mult > 0:
                nearest_sell = None
                if sell_lock_enabled:
                    for meta in active_meta.values():
                        if not isinstance(meta, dict):
                            continue
                        if str(meta.get("side") or "").lower() != "sell":
                            continue
                        if not meta.get("lock_sell"):
                            continue
                        p = float(meta.get("price") or 0.0)
                        if p > 0:
                            nearest_sell = p if nearest_sell is None else min(nearest_sell, p)
                else:
                    if target_sells:
                        nearest_sell = min(target_sells)
                if nearest_sell and current_price > 0:
                    if mode == "FIXED":
                        gap_usdt = nearest_sell - current_price
                        if gap_usdt >= (spacing_val * gap_mult):
                            emergency_buy = current_price - (spacing_val * buy_mult)
                        else:
                            emergency_buy = 0.0
                    else:
                        gap_pct = (nearest_sell / current_price - 1.0) * 100.0
                        if gap_pct >= (spacing_val * gap_mult):
                            emergency_buy = current_price * (1.0 - (spacing_val * buy_mult) / 100.0)
                        else:
                            emergency_buy = 0.0
                    if emergency_buy > 0:
                            emergency_buy = self.mgr.round_to_tick(emergency_buy, side="buy")
                            if emergency_buy >= current_price:
                                fallback_price = (
                                    current_price - spacing_val
                                    if mode == "FIXED"
                                    else current_price * (1.0 - (spacing_val / 100.0))
                                )
                                emergency_buy = self.mgr.round_to_tick(
                                    fallback_price,
                                    side="buy",
                                )
                            if emergency_buy > 0:
                                emergency_buy = max(emergency_buy, min_buy_price)
                                if emergency_buy < current_price:
                                    if target_buys:
                                        lowest = min(target_buys)
                                        if emergency_buy > lowest:
                                            target_buys = list(target_buys[:-1]) + [emergency_buy]
                                            target_buys = sorted(set(target_buys), reverse=True)
                                            emergency_buy_price = emergency_buy
                                    else:
                                        target_buys = [emergency_buy]
                                        emergency_buy_price = emergency_buy
        except (TypeError, ValueError, ZeroDivisionError):
            logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
            emergency_buy_price = None

        downtrend_shift = None
        shifted_sell_to_buy = False
        try:
            downtrend = bool(self.mgr.is_downtrend(market))
        except (AttributeError, TypeError):
            logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
            downtrend = False
        if downtrend:
            # Guard: avoid rapid sell->buy churn on tiny movements.
            try:
                shift_enabled_raw = str(os.getenv("OMA_GRID_DOWNTREND_SHIFT_ENABLED", "1")).strip().lower()
                shift_enabled = shift_enabled_raw not in ("0", "false", "off", "no")
            except (TypeError, AttributeError):
                logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                shift_enabled = True
            try:
                shift_cooldown_sec = float(os.getenv("OMA_GRID_DOWNTREND_SHIFT_COOLDOWN_SEC", "90") or 90.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                shift_cooldown_sec = 90.0
            if shift_cooldown_sec < 0:
                shift_cooldown_sec = 0.0
            try:
                shift_min_steps = float(os.getenv("OMA_GRID_DOWNTREND_SHIFT_MIN_STEPS", "1.0") or 1.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                shift_min_steps = 1.0
            if shift_min_steps <= 0:
                shift_min_steps = 1.0
            try:
                last_shift_ts = float(mstate.get("downtrend_last_shift_ts", 0.0) or 0.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                last_shift_ts = 0.0
            try:
                last_shift_price = float(mstate.get("downtrend_last_shift_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                last_shift_price = 0.0
            try:
                mode_dt = str(cfg.get("spacing_mode") or "PERCENT").upper()
                spacing_dt = float(cfg.get("spacing_value") or 0.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                mode_dt = "PERCENT"
                spacing_dt = 0.0
            if mode_dt == "FIXED":
                step_unit = spacing_dt
            else:
                base_for_step = last_shift_price if last_shift_price > 0 else float(current_price or 0.0)
                step_unit = base_for_step * (spacing_dt / 100.0)
            move_threshold = max(1e-9, float(step_unit) * float(shift_min_steps))
            cooldown_ok = (now - last_shift_ts) >= shift_cooldown_sec
            move_ok = (last_shift_price <= 0) or (abs(float(current_price or 0.0) - last_shift_price) >= move_threshold)
            shift_allowed = shift_enabled and cooldown_ok and move_ok
            if not shift_allowed:
                downtrend = False

        if downtrend:
            sell_uuid = ""
            sell_meta = None
            sell_price = 0.0
            for uuid_, meta in active_meta.items():
                if not isinstance(meta, dict):
                    continue
                if str(meta.get("side") or "").lower() != "sell":
                    continue
                try:
                    price = float(meta.get("price") or 0.0)
                except (TypeError, ValueError):
                    logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                    price = 0.0
                if price <= 0:
                    continue
                if price > sell_price:
                    sell_price = price
                    sell_uuid = uuid_
                    sell_meta = meta

            buy_price = 0.0
            if buy_levels_all:
                for price in reversed(buy_levels_all):
                    if price in excluded or price in active_prices:
                        continue
                    buy_price = float(price)
                    break

            if sell_uuid and sell_meta and buy_price > 0:
                try:
                    qty = float(sell_meta.get("qty") or 0.0)
                except (TypeError, ValueError):
                    logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                    qty = 0.0
                usdt_total = buy_price * qty if qty > 0 else 0.0
                if qty > 0 and usdt_total >= 5:
                    blocked = bool(mstate.get("blocked", False))
                    cb_tripped = self.check_circuit_breaker()
                    budget_ok = True
                    budget_cap = self._get_budget_cap(market, cfg)
                    if budget_cap > 0:
                        reserved_buy_usdt = 0.0
                        try:
                            for meta in active_meta.values():
                                if not isinstance(meta, dict):
                                    continue
                                if str(meta.get("side") or "").lower() != "buy":
                                    continue
                                p = float(meta.get("price") or 0.0)
                                q = float(meta.get("qty") or 0.0)
                                if p > 0 and q > 0:
                                    reserved_buy_usdt += p * q
                        except (TypeError, ValueError):
                            logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                            reserved_buy_usdt = reserved_buy_usdt or 0.0
                        holding_qty = self._get_position_qty(market)
                        holding_usdt = float(holding_qty) * float(current_price or 0.0)
                        buy_reserve_usdt = self._get_buy_budget_reserve(
                            market=market,
                            cfg=cfg,
                            budget_cap=float(budget_cap),
                            min_order_usdt=float(min_order_usdt),
                            has_holding=has_holding,
                        )
                        remaining_budget = float(budget_cap) - float(reserved_buy_usdt) - float(holding_usdt)
                        effective_remaining_budget = float(remaining_budget) - float(buy_reserve_usdt)
                        if effective_remaining_budget < usdt_total:
                            budget_ok = False

                    if budget_ok and not blocked and not cb_tripped:
                        try:
                            self.mgr._cancel_order(uuid_=sell_uuid)
                            self.mgr.update_order_status(market, sell_uuid, "deleted")
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                            logger.error("[GRID] cancel order %s failed: %s", sell_uuid, exc, exc_info=True)
                        active_meta.pop(sell_uuid, None)
                        active_prices.discard(float(sell_price or 0.0))
                        try:
                            logger.info(
                                "GridV2 DOWNTREND SHIFT: %s sell->buy %.2f->%.2f qty=%.8f",
                                market, sell_price, buy_price, qty,
                            )
                            resp = self.mgr._place_limit_buy_qty(market=market, price=buy_price, qty=qty)
                            ou = str(resp.get("uuid") or "")
                            if ou:
                                self.mgr._register_order_uuid(
                                    market=market, rid=rid, uuid_=ou,
                                    side="buy", price=buy_price, seq=0, qty=qty,
                                )
                                active_meta[ou] = {
                                    "side": "buy",
                                    "price": float(buy_price),
                                    "qty": float(qty),
                                    "status": "active",
                                }
                                active_prices.add(float(buy_price))
                                shifted_sell_to_buy = True
                                downtrend_shift = {
                                    "from_sell": float(sell_price),
                                    "to_buy": float(buy_price),
                                    "qty": float(qty),
                                }
                                if buy_price not in target_buys:
                                    target_buys = list(target_buys) + [buy_price]
                                    target_buys = sorted(set(target_buys), reverse=True)
                                    while len(target_buys) > n:
                                        target_buys = target_buys[1:]
                                if sell_price in target_sells:
                                    target_sells = [p for p in target_sells if p != sell_price]
                                mstate["downtrend_last_shift_ts"] = float(now)
                                mstate["downtrend_last_shift_price"] = float(current_price or 0.0)
                            else:
                                logger.warning(
                                    "GridV2 DOWNTREND SHIFT BUY no uuid: %s price=%.2f qty=%.8f resp=%s",
                                    market, buy_price, qty, resp,
                                )
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
                            logger.error(
                                "GridV2 DOWNTREND SHIFT BUY FAILED: %s price=%.2f qty=%.8f — %s",
                                market, buy_price, qty, e,
                            )

        # Final cap pass after emergency/downtrend adjustments.
        stress_mode = bool(downtrend or emergency_buy_price is not None)
        if not stress_mode and target_buys:
            try:
                mode2 = str(cfg.get("spacing_mode") or "PERCENT").upper()
                spacing2 = float(cfg.get("spacing_value") or 0.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                mode2 = "PERCENT"
                spacing2 = 0.0
            if spacing2 > 0 and current_price > 0:
                nearest_buy = max(target_buys)
                if mode2 == "FIXED":
                    buy_gap = current_price - nearest_buy
                    stress_mode = buy_gap >= (spacing2 * 1.8)
                else:
                    buy_gap_pct = (current_price / nearest_buy - 1.0) * 100.0 if nearest_buy > 0 else 0.0
                    stress_mode = buy_gap_pct >= (spacing2 * 1.8)

        if stress_mode:
            # Refill BUY candidates that may have been trimmed by the first cap pass.
            desired_stress_buys = 2 if (has_holding and available_qty > 0) else 3
            if len(target_buys) < desired_stress_buys:
                for p in buy_levels_all:
                    if p in excluded or p in active_prices:
                        continue
                    if p in target_buys:
                        continue
                    target_buys = sorted(set(list(target_buys) + [p]), reverse=True)
                    if len(target_buys) >= desired_stress_buys:
                        break

        target_buys, target_sells, effective_max_active_orders = _apply_target_caps(
            target_buys,
            target_sells,
            prefer_buffer_buy=stress_mode,
        )

        target_buy_set = set(target_buys)
        target_sell_set = set(target_sells)
        target_all = target_buy_set | target_sell_set

        canceled = 0
        cancel_failed: List[Dict[str, Any]] = []
        for uuid_, meta in list(active_meta.items()):
            price = float(meta.get("price") or 0)
            if price <= 0:
                continue
            if meta.get("side") == "sell" and meta.get("lock_sell"):
                # available_qty excludes locked qty, so locked sells can look like 0.
                # Keep lock-sell lines while a position exists to prevent cancel/recreate churn.
                if has_holding and not sell_blocked:
                    continue
            if price not in target_all:
                try:
                    self.mgr._cancel_order(uuid_=uuid_)
                    self.mgr.update_order_status(market, uuid_, "deleted")
                    canceled += 1
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                    logger.warning("LadderGridV2._apply_target_caps except: %s", e, exc_info=True)
                    cancel_failed.append({"uuid": uuid_, "price": price, "error": str(e)})

        blocked = bool(mstate.get("blocked", False))
        cb_tripped = self.check_circuit_breaker()
        order_usdt = int(cfg.get("order_usdt") or 0)
        if sell_blocked and available_qty > 0:
            try:
                blocked_sell_qty = float(mstate.get("blocked_sell_qty") or 0.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                blocked_sell_qty = 0.0
            if available_qty > blocked_sell_qty:
                mstate["blocked_sell_qty"] = float(available_qty)
            try:
                peak = float(mstate.get("blocked_sell_peak_price") or 0.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                peak = 0.0
            if current_price > peak:
                mstate["blocked_sell_peak_price"] = float(current_price)
        budget_cap = self._get_budget_cap(market, cfg)
        budget_active = budget_cap > 0
        if budget_active and order_usdt > 0:
            # Budget can change via allocator; clamp oversized per-order USDT so at least one/two buys can stay active.
            affordable_unit = int(float(budget_cap) / float(max(1, n)))
            if affordable_unit >= min_order_usdt and order_usdt > affordable_unit:
                old_order_usdt = order_usdt
                order_usdt = affordable_unit
                cfg = dict(cfg)
                cfg["order_usdt"] = order_usdt
                cfg["ladder_fixed_order_usdt"] = order_usdt
                try:
                    self.mgr.save_config(cfg)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.error("[GRID] save_config failed during budget clamp: %s", exc, exc_info=True)
                logger.warning(
                    "GridV2 auto-resized order_usdt for %s: %d -> %d (budget_cap=%.0f, window_n=%d)",
                    market, old_order_usdt, order_usdt, budget_cap, n,
                )
        reserved_buy_usdt = 0.0
        if budget_active:
            try:
                for meta in active_meta.values():
                    if not isinstance(meta, dict):
                        continue
                    if str(meta.get("side") or "").lower() != "buy":
                        continue
                    p = float(meta.get("price") or 0.0)
                    q = float(meta.get("qty") or 0.0)
                    if p > 0 and q > 0:
                        reserved_buy_usdt += p * q
            except (TypeError, ValueError):
                logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
                reserved_buy_usdt = reserved_buy_usdt or 0.0
        holding_qty = self._get_position_qty(market) if budget_active else 0.0
        holding_usdt = float(holding_qty) * float(current_price or 0.0) if budget_active else 0.0
        remaining_budget = float(budget_cap) - float(reserved_buy_usdt) - float(holding_usdt)
        buy_reserve_usdt = self._get_buy_budget_reserve(
            market=market,
            cfg=cfg,
            budget_cap=float(budget_cap),
            min_order_usdt=float(min_order_usdt),
            has_holding=has_holding,
        ) if budget_active else 0.0

        placed_buy = 0
        placed_sell = 0
        place_failed: List[Dict[str, Any]] = []

        try:
            rearm_min_steps = float(
                cfg.get("buy_rearm_min_steps")
                or os.getenv("OMA_GRID_BUY_REARM_MIN_STEPS", "1.0")
                or 1.0
            )
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
            rearm_min_steps = 1.0
        if rearm_min_steps <= 0:
            rearm_min_steps = 1.0
        try:
            last_buy_fill_price = float(mstate.get("last_buy_fill_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
            last_buy_fill_price = 0.0
        try:
            spacing_mode_local = str(cfg.get("spacing_mode") or "PERCENT").upper()
            spacing_value_local = float(cfg.get("spacing_value") or 0.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
            spacing_mode_local = "PERCENT"
            spacing_value_local = 0.0
        if spacing_mode_local == "FIXED":
            rearm_step_unit = spacing_value_local
        else:
            base_price = last_buy_fill_price if last_buy_fill_price > 0 else float(current_price or 0.0)
            rearm_step_unit = base_price * (spacing_value_local / 100.0)
        rearm_gap_required = max(1e-9, float(rearm_step_unit) * float(rearm_min_steps))
        buy_rearm_blocked = bool(
            last_buy_fill_price > 0
            and current_price > 0
            and rearm_step_unit > 0
            and float(current_price) > (float(last_buy_fill_price) - float(rearm_gap_required))
        )
        if buy_rearm_blocked:
            last_block_ts = float(mstate.get("last_buy_rearm_block_ts", 0.0) or 0.0)
            if (now - last_block_ts) >= 20.0:
                logger.warning(
                    "GridV2 BUY_REARM_BLOCKED %s current=%.2f last_buy=%.2f req_gap=%.6f",
                    market, current_price, last_buy_fill_price, rearm_gap_required,
                )
                mstate["last_buy_rearm_block_ts"] = float(now)

        # Legacy states may carry oversized blocked_budget from older buggy accumulation.
        try:
            blocked_budget_now = int(mstate.get("blocked_budget_usdt", 0) or 0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._apply_target_caps suppressed exception", exc_info=True)
            blocked_budget_now = 0
        if budget_active and blocked_budget_now > int(float(budget_cap)):
            mstate["blocked_budget_usdt"] = int(float(budget_cap))
            blocked_budget_now = int(float(budget_cap))

        def _accum_blocked_budget_once(price: float, reason: str) -> None:
            nonlocal blocked_budget_now
            if order_usdt <= 0:
                return
            try:
                last_accum_price = float(mstate.get("blocked_budget_last_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2._accum_blocked_budget_once suppressed exception", exc_info=True)
                last_accum_price = 0.0
            price_tick = max(self.get_tick_size(price), 1e-9)
            step_for_accum = max(price_tick, float(rearm_step_unit) if rearm_step_unit > 0 else price_tick)
            # Same ladder level should be counted once until price moves by roughly one step.
            if last_accum_price > 0 and abs(float(price) - last_accum_price) < (step_for_accum * 0.9):
                return
            blocked_budget_now += int(max(order_usdt, min_order_usdt))
            if budget_active:
                blocked_budget_now = min(blocked_budget_now, int(float(budget_cap)))
            mstate["blocked_budget_usdt"] = int(max(0, blocked_budget_now))
            mstate["blocked_budget_last_price"] = float(price)
            mstate["blocked_reason"] = str(reason or "blocked")

        for price in target_buys:
            if price in active_prices:
                continue
            if buy_rearm_blocked:
                place_failed.append(
                    {
                        "side": "buy",
                        "price": price,
                        "error": "buy_rearm_guard",
                        "last_buy_fill_price": round(float(last_buy_fill_price), 8),
                        "required_gap": round(float(rearm_gap_required), 8),
                    }
                )
                continue
            order_usdt_use = order_usdt
            if blocked or cb_tripped:
                _accum_blocked_budget_once(float(price), "blocked_or_circuit_breaker")
                continue
            if budget_active:
                effective_remaining_budget = float(remaining_budget) - float(buy_reserve_usdt)
                if effective_remaining_budget < float(min_order_usdt):
                    place_failed.append({
                        "side": "buy",
                        "price": price,
                        "error": "budget_cap",
                        "remaining_budget_usdt": round(float(remaining_budget), 2),
                        "buy_reserve_usdt": round(float(buy_reserve_usdt), 2),
                        "effective_remaining_budget_usdt": round(float(effective_remaining_budget), 2),
                    })
                    continue
                order_usdt_use = min(int(float(effective_remaining_budget)), int(order_usdt))
            if order_usdt_use <= 0 or price <= 0:
                continue
            # 주문 직전 가격을 한 번 더 호가 단위로 보정 (BUY는 floor)
            price_tick = self.mgr.round_to_tick(price, side="buy")
            if price_tick <= 0:
                continue
            price_dec = Decimal(str(price_tick))
            qty_dec = (Decimal(str(order_usdt_use)) / price_dec).quantize(
                Decimal("0.00000001"),
                rounding=ROUND_DOWN,
            )
            if qty_dec <= 0:
                continue
            # Bybit는 volume을 소수점 8자리 내림 처리하므로, 그 결과 기준으로 최소 주문금액을 재검증한다.
            usdt_total = float(price_dec * qty_dec)
            if usdt_total < float(min_order_usdt):
                qty_dec = qty_dec + Decimal("0.00000001")
                usdt_total = float(price_dec * qty_dec)
            if usdt_total < float(min_order_usdt):
                place_failed.append(
                    {
                        "side": "buy",
                        "price": price_tick,
                        "error": f"order_usdt < {int(min_order_usdt)} minimum",
                    }
                )
                continue
            qty = float(qty_dec)
            try:
                logger.info("GridV2 PLACING BUY: %s price=%.2f qty=%.8f usdt=%d", market, price_tick, qty, order_usdt_use)
                resp = self.mgr._place_limit_buy_qty(market=market, price=price_tick, qty=qty)
                ou = str(resp.get("uuid") or "")
                if ou:
                    self.mgr._register_order_uuid(
                        market=market, rid=rid, uuid_=ou,
                        side="buy", price=price_tick, seq=0, qty=qty,
                    )
                    logger.info("GridV2 BUY OK: %s uuid=%s price=%.2f", market, ou, price_tick)
                else:
                    logger.warning("GridV2 BUY no uuid: %s resp=%s", market, resp)
                placed_buy += 1
                if budget_active:
                    remaining_budget -= float(order_usdt_use)
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.error("GridV2 BUY FAILED: %s price=%.2f — %s", market, price_tick, e)
                place_failed.append({"side": "buy", "price": price_tick, "error": str(e)})

        if not sell_lock_enabled and not sell_blocked:
            new_sells = [p for p in target_sells if p not in active_prices]
            sell_qty_each = 0.0
            if new_sells and has_holding and available_qty > 0:
                sell_qty_each = available_qty / len(new_sells)

            for price in target_sells:
                if price in active_prices:
                    continue
                if not has_holding or sell_qty_each <= 0:
                    continue
                if price <= 0:
                    continue
                qty = sell_qty_each
                try:
                    logger.info("GridV2 PLACING SELL: %s price=%.2f qty=%.8f (avail=%.8f)", market, price, qty, available_qty)
                    resp = self.mgr._place_limit_sell_qty(market=market, price=price, qty=qty)
                    ou = str(resp.get("uuid") or "")
                    if ou:
                        self.mgr._register_order_uuid(
                            market=market, rid=rid, uuid_=ou,
                            side="sell", price=price, seq=0, qty=qty,
                        )
                        logger.info("GridV2 SELL OK: %s uuid=%s price=%.2f", market, ou, price)
                    placed_sell += 1
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    logger.error("GridV2 SELL FAILED: %s price=%.2f — %s", market, price, e)
                    place_failed.append({"side": "sell", "price": price, "error": str(e)})
        elif (
            has_holding
            and available_qty > 0
            and not shifted_sell_to_buy
            and not downtrend
            and not sell_blocked
        ):
            entry_ref = self._guess_entry_price(cfg, mstate, current_price)
            lock_price = self._calc_locked_sell_price(entry_ref, cfg)
            est_usdt = lock_price * available_qty if lock_price > 0 else 0.0
            if lock_price > 0 and est_usdt >= 5.0:
                out = self._place_locked_sell(
                    market=market,
                    qty=available_qty,
                    price=lock_price,
                    cfg=cfg,
                    entry_price=entry_ref,
                    parent_buy_uuid="",
                )
                if out:
                    placed_sell += 1
                else:
                    place_failed.append({"side": "sell", "price": lock_price, "error": "locked_sell_failed"})

        active_buy_count_after = self._count_active_orders(market, side="buy")
        no_buy_follow_triggered = False
        no_buy_demoted_watch = False
        no_buy_elapsed_sec = 0.0

        no_buy_follow_enabled = self._as_bool(cfg.get("no_buy_follow_enabled"), default=True)
        no_buy_demote_enabled = self._as_bool(cfg.get("no_buy_demote_enabled"), default=True)
        no_buy_follow_sec = self._coerce_positive_float(
            cfg.get("no_buy_follow_sec"),
            default=float(mstate.get("no_buy_follow_sec", 900.0) or 900.0),
        )
        no_buy_demote_sec = self._coerce_positive_float(
            cfg.get("no_buy_demote_sec"),
            default=float(mstate.get("no_buy_demote_sec", 7200.0) or 7200.0),
        )
        mstate["no_buy_follow_sec"] = no_buy_follow_sec
        mstate["no_buy_demote_sec"] = no_buy_demote_sec

        if not has_holding and active_buy_count_after <= 0:
            no_buy_since_ts = float(mstate.get("no_buy_since_ts", 0.0) or 0.0)
            if no_buy_since_ts <= 0:
                no_buy_since_ts = now
                mstate["no_buy_since_ts"] = no_buy_since_ts
            no_buy_elapsed_sec = max(0.0, now - no_buy_since_ts)

            if no_buy_follow_enabled and no_buy_follow_sec > 0:
                last_follow_ts = float(mstate.get("last_no_buy_follow_ts", 0.0) or 0.0)
                follow_due = no_buy_elapsed_sec >= no_buy_follow_sec
                follow_cooldown_ok = (now - last_follow_ts) >= no_buy_follow_sec
                if follow_due and follow_cooldown_ok:
                    new_cfg = self._auto_reconfigure(market, current_price, cfg)
                    # Force next loop to pick new bounds quickly.
                    mstate["last_rebalance_ts"] = 0.0
                    mstate["last_no_buy_follow_ts"] = now
                    no_buy_follow_triggered = (new_cfg is not None)
                    logger.warning(
                        "GridV2 no-buy follow/reseed: %s elapsed=%.1fs follow_sec=%.1fs",
                        market, no_buy_elapsed_sec, no_buy_follow_sec,
                    )

            if (
                no_buy_demote_enabled
                and no_buy_demote_sec > 0
                and no_buy_elapsed_sec >= no_buy_demote_sec
                and not no_buy_follow_triggered
            ):
                no_buy_demoted_watch = self._demote_market_to_watch(
                    market,
                    reason=f"ladder_no_buy_timeout:{int(no_buy_elapsed_sec)}s",
                )
                if no_buy_demoted_watch:
                    mstate["enabled"] = False
                    mstate["blocked_reason"] = f"demoted_no_buy_timeout:{int(no_buy_elapsed_sec)}s"
                    logger.warning(
                        "GridV2 no-buy demoted to WATCH: %s elapsed=%.1fs demote_sec=%.1fs",
                        market, no_buy_elapsed_sec, no_buy_demote_sec,
                    )
        else:
            mstate["no_buy_since_ts"] = 0.0
            mstate["last_no_buy_follow_ts"] = 0.0

        mstate["last_rebalance_ts"] = 0.0 if no_buy_follow_triggered else time.time()
        self._set_market_state(market, mstate)

        active_orders_out = {}
        try:
            for uuid_, meta in active_meta.items():
                if not isinstance(meta, dict):
                    continue
                p = float(meta.get("price") or 0)
                if p > 0:
                    active_orders_out[str(p)] = uuid_[:12]
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._accum_blocked_budget_once suppressed exception", exc_info=True)
            active_orders_out = {}

        return {
            "market": market,
            "current_price": current_price,
            "window_n": n,
            "target_buys": target_buys,
            "target_sells": target_sells,
            "emergency_buy": emergency_buy_price,
            "downtrend_shift": downtrend_shift,
            "downtrend": downtrend,
            "stress_mode": stress_mode,
            "max_active_orders_total": effective_max_active_orders,
            "active_orders": active_orders_out,
            "has_holding": has_holding,
            "available_qty": available_qty,
            "live_uuids_count": len(live_uuids),
            "canceled": canceled,
            "cancel_failed": cancel_failed,
            "placed_buy": placed_buy,
            "placed_sell": placed_sell,
            "place_failed": place_failed,
            "blocked": blocked,
            "buy_rearm_blocked": buy_rearm_blocked,
            "buy_rearm_required_gap": rearm_gap_required,
            "circuit_breaker_tripped": cb_tripped,
            "budget_cap": budget_cap,
            "reserved_buy_usdt": reserved_buy_usdt,
            "holding_usdt": holding_usdt,
            "buy_reserve_usdt": buy_reserve_usdt,
            "remaining_budget_usdt": remaining_budget,
            "active_buy_orders_after": active_buy_count_after,
            "no_buy_elapsed_sec": round(float(no_buy_elapsed_sec), 1),
            "no_buy_follow_triggered": no_buy_follow_triggered,
            "no_buy_demoted_watch": no_buy_demoted_watch,
        }

    # --------------------------------------------------------
    # Fill handling
    # --------------------------------------------------------
    def on_fill(self, market: str, uuid_: str, fill_price: float, side: str) -> Dict[str, Any]:
        mstate = self._get_market_state(market)
        result: Dict[str, Any] = {
            "market": market,
            "uuid": uuid_,
            "fill_price": fill_price,
            "side": side,
        }

        fill_entry = {
            "uuid": uuid_,
            "price": fill_price,
            "side": side,
            "ts": time.time(),
        }
        history: List[Dict[str, Any]] = mstate.get("fill_history", [])
        history.append(fill_entry)
        if len(history) > _MAX_FILL_HISTORY:
            history = history[-_MAX_FILL_HISTORY:]
        mstate["fill_history"] = history

        if side == "sell":
            mstate["consecutive_buys_without_sell"] = 0
            rebuy_price = self._calc_rebuy_price(fill_price, market=market)
            result["rebuy_price"] = rebuy_price

            current_price = self.mgr.get_current_price(market)
            max_gap_pct = float(mstate.get("max_buy_gap_pct", 20.0))
            try:
                max_gap_cap = float(os.getenv("OMA_GRID_MAX_BUY_GAP_PCT_CAP", "20") or 20.0)
            except (TypeError, ValueError):
                logger.warning("LadderGridV2.on_fill suppressed exception", exc_info=True)
                max_gap_cap = 20.0
            if max_gap_cap > 0:
                max_gap_pct = min(max_gap_pct, max_gap_cap)
            min_valid = (current_price * (1.0 - max_gap_pct / 100.0)) if current_price and current_price > 0 else 0.0

            if rebuy_price > 0 and (min_valid <= 0 or rebuy_price >= min_valid):
                cfg = self.mgr.get_config(market)
                order_usdt = int(cfg.get("order_usdt") or 0)
                ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
                rid = str(ids.get("rid") or "")
                budget_cap = self._get_budget_cap(market, cfg)
                if budget_cap > 0:
                    reserved_buy_usdt = 0.0
                    try:
                        reg = self.mgr._read_order_registry()
                        m = reg.get(market, {})
                        if isinstance(m, dict):
                            for meta in m.values():
                                if not isinstance(meta, dict):
                                    continue
                                if str(meta.get("side") or "").lower() != "buy":
                                    continue
                                if meta.get("status") not in ("active", "open"):
                                    continue
                                p = float(meta.get("price") or 0.0)
                                q = float(meta.get("qty") or 0.0)
                                if p > 0 and q > 0:
                                    reserved_buy_usdt += p * q
                    except (TypeError, ValueError):
                        logger.warning("LadderGridV2.on_fill suppressed exception", exc_info=True)
                        reserved_buy_usdt = reserved_buy_usdt or 0.0
                    holding_qty = self._get_position_qty(market)
                    holding_usdt = float(holding_qty) * float(current_price or 0.0)
                    buy_reserve_usdt = self._get_buy_budget_reserve(
                        market=market,
                        cfg=cfg,
                        budget_cap=float(budget_cap),
                        min_order_usdt=5.0,
                        has_holding=self._has_holding(market),
                    )
                    remaining_budget = float(budget_cap) - float(reserved_buy_usdt) - float(holding_usdt)
                    if (remaining_budget - buy_reserve_usdt) < float(order_usdt):
                        result["rebuy_placed"] = False
                        result["rebuy_reason"] = "budget_cap"
                        self._set_market_state(market, mstate)
                        return result
                if order_usdt > 0:
                    qty = float(order_usdt) / rebuy_price
                    if qty > 0:
                        try:
                            resp = self.mgr._place_limit_buy_qty(
                                market=market, price=rebuy_price, qty=qty,
                            )
                            ou = str(resp.get("uuid") or "")
                            if ou:
                                self.mgr._register_order_uuid(
                                    market=market, rid=rid, uuid_=ou,
                                    side="buy", price=rebuy_price, seq=0, qty=qty,
                                )
                            result["rebuy_placed"] = True
                            result["rebuy_uuid"] = ou
                            logger.info(
                                "GridV2 rebuy placed: %s @ %.2f (sell filled @ %.2f)",
                                market, rebuy_price, fill_price,
                            )
                        except (KeyError, AttributeError, TypeError) as e:
                            result["rebuy_placed"] = False
                            result["rebuy_error"] = str(e)
                            logger.warning(
                                "GridV2 rebuy failed: %s @ %.2f — %s",
                                market, rebuy_price, e,
                            )
            elif rebuy_price > 0:
                result["rebuy_placed"] = False
                result["rebuy_reason"] = "price_too_far_from_current"
                logger.warning(
                    "GridV2 rebuy BLOCKED %s: rebuy=%.2f too far from current=%.2f (max_gap=%.0f%%)",
                    market, rebuy_price, current_price or 0, max_gap_pct,
                )

            last_sell_fill = float(mstate.get("last_sell_fill_price", 0.0))
            up_consec = int(mstate.get("consecutive_up_sells", 0))
            max_up_consec = int(mstate.get("max_consecutive_up_sells", 3))
            if last_sell_fill > 0 and fill_price > last_sell_fill:
                up_consec += 1
            else:
                up_consec = 0
            mstate["consecutive_up_sells"] = up_consec
            mstate["last_sell_fill_price"] = fill_price
            result["consecutive_up_sells"] = up_consec

            if up_consec >= max_up_consec and not mstate.get("sell_blocked", False):
                mstate["sell_blocked"] = True
                mstate["sell_blocked_reason"] = (
                    f"consecutive_up_sells={up_consec} >= {max_up_consec}"
                )
                current_peak = self.mgr.get_current_price(market) or fill_price
                mstate["blocked_sell_peak_price"] = max(float(current_peak), float(fill_price))
                try:
                    avail_qty_now = float(self._get_available_qty(market) or 0.0)
                except (TypeError, ValueError):
                    logger.warning("LadderGridV2.on_fill suppressed exception", exc_info=True)
                    avail_qty_now = 0.0
                blocked_sell_qty = float(mstate.get("blocked_sell_qty") or 0.0)
                if avail_qty_now > blocked_sell_qty:
                    mstate["blocked_sell_qty"] = avail_qty_now
                self._cancel_all_sells(market)
                result["sell_blocked"] = True
                logger.warning(
                    "GridV2 SELL_BLOCKED %s: %d consecutive up sells",
                    market, up_consec,
                )

        elif side == "buy":
            last_fill = float(mstate.get("last_buy_fill_price", 0.0))
            consec = int(mstate.get("consecutive_down_buys", 0))
            max_consec = int(mstate.get("max_consecutive_down_buys", 2) or 2)
            buy_run = int(mstate.get("consecutive_buys_without_sell", 0) or 0) + 1
            max_buy_run = int(mstate.get("max_consecutive_buys_without_sell", max_consec) or max_consec)

            if last_fill > 0 and fill_price <= last_fill:
                consec += 1
            else:
                consec = 0 if last_fill > 0 else 1

            mstate["consecutive_down_buys"] = consec
            mstate["consecutive_buys_without_sell"] = buy_run
            mstate["last_buy_fill_price"] = fill_price
            result["consecutive_down_buys"] = consec
            result["consecutive_buys_without_sell"] = buy_run

            cfg = self.mgr.get_config(market)
            sell_lock_mode = str(cfg.get("sell_lock_mode") or "TRAIL_UP").upper()
            sell_lock_enabled = sell_lock_mode not in ("OFF", "DISABLED", "NONE", "0", "FALSE")
            if sell_lock_enabled and not self._has_locked_sell_for_parent(market, uuid_):
                qty = self._get_order_qty(market, uuid_)
                if qty > 0:
                    lock_price = self._calc_locked_sell_price(fill_price, cfg)
                    if lock_price > 0 and (lock_price * qty) >= 5.0:
                        ok = self._place_locked_sell(
                            market=market,
                            qty=qty,
                            price=lock_price,
                            cfg=cfg,
                            entry_price=fill_price,
                            parent_buy_uuid=uuid_,
                        )
                        result["paired_sell"] = ok
                    else:
                        result["paired_sell"] = False
                else:
                    result["paired_sell"] = False

            if (consec >= max_consec or buy_run >= max_buy_run) and not mstate.get("blocked", False):
                mstate["blocked"] = True
                if buy_run >= max_buy_run:
                    mstate["blocked_reason"] = (
                        f"consecutive_buys_without_sell={buy_run} >= {max_buy_run}"
                    )
                else:
                    mstate["blocked_reason"] = (
                        f"consecutive_down_buys={consec} >= {max_consec}"
                    )
                self._cancel_all_buys(market)
                result["blocked"] = True
                logger.warning(
                    "GridV2 BLOCKED %s: down=%d/%d buy_run=%d/%d",
                    market, consec, max_consec, buy_run, max_buy_run,
                )

        self._set_market_state(market, mstate)
        return result

    # --------------------------------------------------------
    # Enhanced poll that detects fills and handles them
    # --------------------------------------------------------
    def poll_and_sync(self, market: str) -> Dict[str, Any]:
        fills_detected: List[Dict[str, Any]] = []

        reg = self.mgr._read_order_registry()
        m = reg.get(market)
        if not isinstance(m, dict):
            m = {}

        u: Any = None
        try:
            u = self.mgr.get_trade_client()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.error("GridV2 poll_and_sync: cannot get exchange client — %s", e)
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
                state = order_info.get("state") if order_info else None
                if state == "done":
                    m[uuid_]["status"] = "filled"
                    updated = True
                    m[uuid_]["filled_ts"] = time.time()
                    try:
                        m[uuid_]["qty"] = float(order_info.get("executed_volume") or 0)
                        m[uuid_]["volume"] = m[uuid_]["qty"]
                        avg_p = float(order_info.get("avg_price") or order_info.get("price") or 0)
                        m[uuid_]["avg_price"] = avg_p
                        m[uuid_]["fee"] = float(order_info.get("paid_fee") or 0)
                    except (TypeError, ValueError) as exc:
                        logger.warning("[GRID] poll_and_sync price/fee parse failed: %s", exc, exc_info=True)

                    fill_price = float(order_info.get("avg_price") or order_info.get("price") or step.get("price") or 0)
                    side_raw = str(step.get("side") or "")
                    fill_result = self.on_fill(market, uuid_, fill_price, side_raw)
                    fills_detected.append(fill_result)
            except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
                logger.warning("[GRID] poll order %s error: %s", uuid_, e, exc_info=True)
                continue

        if updated:
            reg[market] = m
            self.mgr._write_order_registry(reg)

        if self.check_rebound(market):
            rebound_result = self.batch_buy_on_rebound(market)
            fills_detected.append({"rebound": True, "batch_buy": rebound_result})
        if self.check_sell_pullback(market):
            pullback_result = self.batch_sell_on_pullback(market)
            fills_detected.append({"sell_pullback": True, "batch_sell": pullback_result})

        sync_result = self.sync_active_window(market)

        return {
            "market": market,
            "fills": fills_detected,
            "sync": sync_result,
        }

    # --------------------------------------------------------
    # Step management (with exchange sync)
    # --------------------------------------------------------
    def pause_step(self, market: str, price: float) -> bool:
        mstate = self._get_market_state(market)
        paused: List[float] = mstate.get("paused_steps", [])
        if price in paused:
            return True

        uuid_ = self._find_uuid_for_price(market, price)
        if uuid_:
            try:
                self.mgr._cancel_order(uuid_=uuid_)
                self.mgr.update_order_status(market, uuid_, "paused")
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                logger.warning("GridV2 pause_step cancel failed: %s — %s", price, e)

        paused.append(price)
        mstate["paused_steps"] = paused
        self._set_market_state(market, mstate)
        logger.info("GridV2 step paused: %s @ %.2f", market, price)
        return True

    def resume_step(self, market: str, price: float) -> bool:
        mstate = self._get_market_state(market)
        paused: List[float] = mstate.get("paused_steps", [])
        if price not in paused:
            return False

        paused = [p for p in paused if p != price]
        mstate["paused_steps"] = paused
        self._set_market_state(market, mstate)
        logger.info("GridV2 step resumed: %s @ %.2f", market, price)
        return True

    def skip_step(self, market: str, price: float, skip_until_ts: float = 0) -> bool:
        mstate = self._get_market_state(market)
        skipped: List[float] = mstate.get("skipped_steps", [])
        if price in skipped:
            return True

        uuid_ = self._find_uuid_for_price(market, price)
        if uuid_:
            try:
                self.mgr._cancel_order(uuid_=uuid_)
                self.mgr.update_order_status(market, uuid_, "deleted")
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                logger.warning("GridV2 skip_step cancel failed: %s — %s", price, e)

        skipped.append(price)
        mstate["skipped_steps"] = skipped
        self._set_market_state(market, mstate)
        logger.info("GridV2 step skipped: %s @ %.2f (until_ts=%.0f)", market, price, skip_until_ts)
        return True

    def edit_step_price(self, market: str, old_price: float, new_price: float) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "market": market,
            "old_price": old_price,
            "new_price": new_price,
        }

        uuid_ = self._find_uuid_for_price(market, old_price)
        if uuid_:
            try:
                self.mgr._cancel_order(uuid_=uuid_)
                self.mgr.update_order_status(market, uuid_, "deleted")
                result["old_canceled"] = True
            except (KeyError, AttributeError, TypeError) as e:
                result["old_canceled"] = False
                result["cancel_error"] = str(e)
                logger.warning("GridV2 edit_step cancel failed: %s — %s", old_price, e)
                return result

        reg = self.mgr._read_order_registry()
        market_reg = reg.get(market) or {}
        side = "buy"
        rid = ""
        if uuid_ and isinstance(market_reg.get(uuid_), dict):
            side = market_reg[uuid_].get("side", "buy")
            rid = market_reg[uuid_].get("rid", "")

        if not rid:
            cfg = self.mgr.get_config(market)
            ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
            rid = str(ids.get("rid") or "")

        cfg = self.mgr.get_config(market)
        order_usdt = int(cfg.get("order_usdt") or 0)
        if order_usdt <= 0 or new_price <= 0:
            result["placed"] = False
            result["reason"] = "invalid_order_usdt_or_price"
            return result

        qty = float(order_usdt) / new_price
        if qty <= 0:
            result["placed"] = False
            result["reason"] = "zero_qty"
            return result

        try:
            if side == "sell":
                resp = self.mgr._place_limit_sell_qty(market=market, price=new_price, qty=qty)
            else:
                resp = self.mgr._place_limit_buy_qty(market=market, price=new_price, qty=qty)
            ou = str(resp.get("uuid") or "")
            if ou:
                self.mgr._register_order_uuid(
                    market=market, rid=rid, uuid_=ou,
                    side=side, price=new_price, seq=0,
                )
            result["placed"] = True
            result["new_uuid"] = ou
        except (KeyError, AttributeError, TypeError) as e:
            result["placed"] = False
            result["place_error"] = str(e)
            logger.warning("GridV2 edit_step place failed: %s @ %.2f — %s", market, new_price, e)

        mstate = self._get_market_state(market)
        for key in ("paused_steps", "skipped_steps"):
            lst: List[float] = mstate.get(key, [])
            if old_price in lst:
                lst = [p if p != old_price else new_price for p in lst]
                mstate[key] = lst
        self._set_market_state(market, mstate)

        return result

    def delete_step(self, market: str, price: float) -> bool:
        uuid_ = self._find_uuid_for_price(market, price)
        if uuid_:
            try:
                self.mgr._cancel_order(uuid_=uuid_)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                logger.warning("GridV2 delete_step cancel failed: %s — %s", price, e)
            self.mgr.delete_order(market, uuid_)

        mstate = self._get_market_state(market)
        mstate["paused_steps"] = [p for p in mstate.get("paused_steps", []) if p != price]
        mstate["skipped_steps"] = [p for p in mstate.get("skipped_steps", []) if p != price]
        self._set_market_state(market, mstate)
        logger.info("GridV2 step deleted: %s @ %.2f", market, price)
        return True

    # --------------------------------------------------------
    # Protection: rebound detection and batch buy
    # --------------------------------------------------------
    def check_rebound(self, market: str) -> bool:
        mstate = self._get_market_state(market)
        if not mstate.get("blocked", False):
            return False

        last_fill = float(mstate.get("last_buy_fill_price", 0.0))
        if last_fill <= 0:
            return False

        current_price = self.mgr.get_current_price(market)
        if not current_price or current_price <= 0:
            return False

        rebound_threshold = last_fill * 1.01
        if current_price >= rebound_threshold:
            logger.info(
                "GridV2 rebound detected: %s price=%.2f > threshold=%.2f",
                market, current_price, rebound_threshold,
            )
            return True
        return False

    def batch_buy_on_rebound(self, market: str) -> Dict[str, Any]:
        mstate = self._get_market_state(market)
        budget = int(mstate.get("blocked_budget_usdt", 0))
        result: Dict[str, Any] = {"market": market, "budget": budget}

        if budget <= 0:
            mstate["blocked"] = False
            mstate["blocked_reason"] = ""
            mstate["consecutive_down_buys"] = 0
            mstate["consecutive_buys_without_sell"] = 0
            mstate["blocked_budget_usdt"] = 0
            mstate["blocked_budget_last_price"] = 0.0
            self._set_market_state(market, mstate)
            result["placed"] = False
            result["reason"] = "no_budget"
            return result

        # Keep blocked state until rebound batch buy is actually placed.
        mstate["blocked"] = True
        if not str(mstate.get("blocked_reason") or "").strip():
            mstate["blocked_reason"] = "awaiting_rebound_batch_buy"

        current_price = self.mgr.get_current_price(market)
        if not current_price or current_price <= 0:
            self._set_market_state(market, mstate)
            result["placed"] = False
            result["reason"] = "no_price"
            return result

        price = self.mgr.round_to_tick(current_price, side="buy")
        if price <= 0:
            self._set_market_state(market, mstate)
            result["placed"] = False
            result["reason"] = "invalid_tick_price"
            return result

        qty = float(budget) / price
        if qty <= 0:
            self._set_market_state(market, mstate)
            result["placed"] = False
            result["reason"] = "zero_qty"
            return result

        cfg = self.mgr.get_config(market)
        ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
        rid = str(ids.get("rid") or "")
        result["price"] = price
        result["qty"] = qty
        try:
            resp = self.mgr._place_limit_buy_qty(market=market, price=price, qty=qty)
            ou = str(resp.get("uuid") or "")
            if ou:
                self.mgr._register_order_uuid(
                    market=market, rid=rid, uuid_=ou,
                    side="buy", price=price, seq=0, qty=qty,
                )
            result["placed"] = True
            result["uuid"] = ou
            mstate["blocked"] = False
            mstate["blocked_reason"] = ""
            mstate["consecutive_down_buys"] = 0
            mstate["consecutive_buys_without_sell"] = 0
            mstate["blocked_budget_usdt"] = 0
            mstate["blocked_budget_last_price"] = 0.0
            logger.info(
                "GridV2 batch buy on rebound: %s budget=%d @ %.2f qty=%.8f",
                market, budget, price, qty,
            )
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            result["placed"] = False
            result["error"] = str(e)
            mstate["blocked"] = True
            mstate["blocked_reason"] = "rebound_buy_failed"
            mstate["blocked_budget_usdt"] = int(max(0, budget))
            mstate["blocked_budget_last_price"] = float(price)
            logger.warning("GridV2 batch buy failed: %s — %s", market, e)

        self._set_market_state(market, mstate)
        return result

    def check_sell_pullback(self, market: str) -> bool:
        mstate = self._get_market_state(market)
        if not mstate.get("sell_blocked", False):
            return False

        current_price = self.mgr.get_current_price(market)
        if not current_price or current_price <= 0:
            return False

        peak_price = float(mstate.get("blocked_sell_peak_price", 0.0))
        if peak_price <= 0 or current_price > peak_price:
            mstate["blocked_sell_peak_price"] = float(current_price)
            self._set_market_state(market, mstate)
            return False

        pullback_pct = float(mstate.get("sell_pullback_pct", 1.0))
        if pullback_pct <= 0:
            pullback_pct = 1.0
        try:
            min_pullback_pct = float(os.getenv("OMA_GRID_MIN_SELL_PULLBACK_PCT", "0.8") or 0.8)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2.check_sell_pullback suppressed exception", exc_info=True)
            min_pullback_pct = 0.8
        if min_pullback_pct > 0:
            pullback_pct = max(pullback_pct, min_pullback_pct)
        trigger_price = peak_price * (1.0 - pullback_pct / 100.0)
        if current_price <= trigger_price:
            logger.info(
                "GridV2 sell pullback detected: %s price=%.2f <= trigger=%.2f (peak=%.2f, pullback=%.2f%%)",
                market, current_price, trigger_price, peak_price, pullback_pct,
            )
            return True
        return False

    def batch_sell_on_pullback(self, market: str) -> Dict[str, Any]:
        mstate = self._get_market_state(market)
        blocked_qty = float(mstate.get("blocked_sell_qty", 0.0))
        available_qty = float(self._get_available_qty(market) or 0.0)
        qty = min(blocked_qty, available_qty) if blocked_qty > 0 else available_qty
        result: Dict[str, Any] = {"market": market, "qty": qty}

        if qty <= 0:
            mstate["blocked_sell_qty"] = 0.0
            mstate["blocked_sell_peak_price"] = 0.0
            mstate["sell_blocked"] = False
            mstate["sell_blocked_reason"] = ""
            mstate["consecutive_up_sells"] = 0
            self._set_market_state(market, mstate)
            result["placed"] = False
            result["reason"] = "no_qty"
            return result

        current_price = self.mgr.get_current_price(market)
        if not current_price or current_price <= 0:
            self._set_market_state(market, mstate)
            result["placed"] = False
            result["reason"] = "no_price"
            return result

        price = self.mgr.round_to_tick(current_price, side="sell")
        if price <= 0:
            self._set_market_state(market, mstate)
            result["placed"] = False
            result["reason"] = "invalid_tick_price"
            return result
        est_usdt = float(price) * float(qty)
        if est_usdt < Q.min_order:
            mstate["blocked_sell_qty"] = 0.0
            mstate["blocked_sell_peak_price"] = 0.0
            mstate["sell_blocked"] = False
            mstate["sell_blocked_reason"] = ""
            mstate["consecutive_up_sells"] = 0
            self._set_market_state(market, mstate)
            result["price"] = price
            result["placed"] = False
            result["reason"] = "order_under_min_usdt"
            return result

        # Snapshot existing sell reservations before mass cancel.
        prev_sells = self._snapshot_active_sells(market)
        canceled_before = self._cancel_all_sells(market)

        cfg = self.mgr.get_config(market)
        ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
        rid = str(ids.get("rid") or "")

        result["price"] = price
        result["est_usdt"] = est_usdt
        result["canceled_before"] = int(canceled_before)
        result["snapshot_sells"] = len(prev_sells)
        try:
            resp = self.mgr._place_limit_sell_qty(market=market, price=price, qty=qty)
            ou = str(resp.get("uuid") or "")
            if ou:
                self.mgr._register_order_uuid(
                    market=market,
                    rid=rid,
                    uuid_=ou,
                    side="sell",
                    price=price,
                    seq=0,
                    qty=qty,
                )
            result["placed"] = True
            result["uuid"] = ou
            mstate["sell_blocked"] = False
            mstate["sell_blocked_reason"] = ""
            mstate["consecutive_up_sells"] = 0
            mstate["blocked_sell_qty"] = 0.0
            mstate["blocked_sell_peak_price"] = 0.0
            logger.info(
                "GridV2 batch sell on pullback: %s qty=%.8f @ %.2f",
                market, qty, price,
            )
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.warning("LadderGridV2.batch_sell_on_pullback except: %s", e, exc_info=True)
            result["placed"] = False
            result["error"] = str(e)
            restore = self._restore_sell_orders(
                market=market,
                rid=rid,
                sells=prev_sells,
                max_qty=float(self._get_available_qty(market) or 0.0),
            )
            result["restored"] = int(len(restore.get("restored") or []))
            result["restore_failed"] = restore.get("failed") or []
            if result["restored"] > 0:
                # Recovery success: keep running with restored reservations.
                mstate["sell_blocked"] = False
                mstate["sell_blocked_reason"] = ""
                mstate["consecutive_up_sells"] = 0
                mstate["blocked_sell_qty"] = 0.0
                mstate["blocked_sell_peak_price"] = 0.0
                logger.warning(
                    "GridV2 batch sell failed: %s — restored %d previous sell orders",
                    market, result["restored"],
                )
            else:
                # Recovery failed: keep blocked state to retry on next pullback cycle.
                mstate["sell_blocked"] = True
                mstate["sell_blocked_reason"] = "pullback_sell_failed"
                mstate["blocked_sell_qty"] = max(float(mstate.get("blocked_sell_qty") or 0.0), float(qty))
                mstate["blocked_sell_peak_price"] = max(
                    float(mstate.get("blocked_sell_peak_price") or 0.0),
                    float(current_price or 0.0),
                )
                logger.warning("GridV2 batch sell failed: %s — no sell order restored (%s)", market, e)

        self._set_market_state(market, mstate)
        return result

    # --------------------------------------------------------
    # Circuit Breaker (global buy block)
    # --------------------------------------------------------
    _DEFAULT_CB: Dict[str, Any] = {
        "enabled": True,
        "threshold": 2.0,
        "tripped": False,
        "tripped_ts": 0.0,
        "tripped_reason": "",
    }

    def get_circuit_breaker_status(self) -> Dict[str, Any]:
        state = self._load_state()
        markets: Dict[str, int] = {}
        total = 0
        count = 0
        for key, val in state.items():
            if key == "__circuit_breaker__" or not isinstance(val, dict):
                continue
            cdb = int(val.get("consecutive_down_buys", 0))
            markets[key] = cdb
            total += cdb
            count += 1

        avg = total / count if count > 0 else 0.0

        cb_raw = state.get("__circuit_breaker__")
        cb = dict(self._DEFAULT_CB)
        if isinstance(cb_raw, dict):
            cb.update(cb_raw)

        tripped = bool(cb["enabled"]) and avg >= float(cb["threshold"])

        return {
            "enabled": cb["enabled"],
            "threshold": cb["threshold"],
            "tripped": tripped,
            "avg_consecutive_down_buys": avg,
            "market_count": count,
            "tripped_ts": cb["tripped_ts"],
            "tripped_reason": cb["tripped_reason"],
            "markets": markets,
        }

    def check_circuit_breaker(self) -> bool:
        status = self.get_circuit_breaker_status()
        tripped_now = status["tripped"]

        state = self._load_state()
        cb_raw = state.get("__circuit_breaker__")
        cb = dict(self._DEFAULT_CB)
        if isinstance(cb_raw, dict):
            cb.update(cb_raw)

        was_tripped = bool(cb.get("tripped", False))

        if tripped_now and not was_tripped:
            cb["tripped"] = True
            cb["tripped_ts"] = time.time()
            cb["tripped_reason"] = (
                f"avg_consecutive_down_buys={status['avg_consecutive_down_buys']:.2f}"
                f" >= threshold={status['threshold']}"
            )
            state["__circuit_breaker__"] = cb
            self._save_state(state)
            logger.warning(
                "GridV2 CIRCUIT BREAKER TRIPPED: %s (%d markets)",
                cb["tripped_reason"], status["market_count"],
            )
        elif not tripped_now and was_tripped:
            cb["tripped"] = False
            cb["tripped_ts"] = 0.0
            cb["tripped_reason"] = ""
            state["__circuit_breaker__"] = cb
            self._save_state(state)
            logger.info(
                "GridV2 circuit breaker cleared: avg=%.2f < threshold=%.1f",
                status["avg_consecutive_down_buys"], status["threshold"],
            )

        return tripped_now

    def set_circuit_breaker_enabled(self, enabled: bool) -> None:
        state = self._load_state()
        cb_raw = state.get("__circuit_breaker__")
        cb = dict(self._DEFAULT_CB)
        if isinstance(cb_raw, dict):
            cb.update(cb_raw)

        cb["enabled"] = enabled
        if not enabled and cb.get("tripped", False):
            cb["tripped"] = False
            cb["tripped_ts"] = 0.0
            cb["tripped_reason"] = ""

        state["__circuit_breaker__"] = cb
        self._save_state(state)

    def set_circuit_breaker_threshold(self, threshold: float) -> None:
        state = self._load_state()
        cb_raw = state.get("__circuit_breaker__")
        cb = dict(self._DEFAULT_CB)
        if isinstance(cb_raw, dict):
            cb.update(cb_raw)

        cb["threshold"] = max(1.0, threshold)
        state["__circuit_breaker__"] = cb
        self._save_state(state)

    # --------------------------------------------------------
    # State I/O
    # --------------------------------------------------------
    def _load_state(self) -> Dict[str, Any]:
        if self._state_cache is not None:  # [FIX H3] 캐시 히트 — 파일 I/O 생략
            return dict(self._state_cache)
        try:
            if not os.path.exists(STATE_PATH):
                self._state_cache = {}
                return {}
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                result = data if isinstance(data, dict) else {}
                self._state_cache = result  # [FIX H3] 최초 로딩 시 캐시 저장
                return dict(result)
        except (OSError, json.JSONDecodeError, ValueError):
            logger.warning("LadderGridV2._load_state suppressed exception", exc_info=True)
            return {}

    def _save_state(self, state: Dict[str, Any]) -> None:
        self._state_cache = dict(state)  # [FIX H3] 캐시 즉시 갱신 — 다음 _load_state가 파일 안 읽어도 됨
        try:
            from app.core.io_utils import safe_write_json
            safe_write_json(STATE_PATH, state)
        except OSError as e:
            logger.error("GridV2 _save_state failed: %s", e)

    def _get_market_state(self, market: str) -> Dict[str, Any]:
        state = self._load_state()
        mstate = state.get(market)
        if not isinstance(mstate, dict):
            mstate = dict(_DEFAULT_MARKET_STATE)
        else:
            merged = dict(_DEFAULT_MARKET_STATE)
            merged.update(mstate)
            mstate = merged
        return mstate

    def _set_market_state(self, market: str, mstate: Dict[str, Any]) -> None:
        state = self._load_state()
        state[market] = mstate
        self._save_state(state)

    # --------------------------------------------------------
    # Internal helpers
    # --------------------------------------------------------
    def _calc_rebuy_price(self, fill_price: float, market: str = "") -> float:
        cfg = self.mgr.get_config(market) if market else {}
        mode = str(cfg.get("spacing_mode") or "PERCENT").upper()
        spacing_val = float(cfg.get("spacing_value") or 0.0)
        if mode == "FIXED":
            raw = fill_price - spacing_val
        else:
            raw = fill_price * (1.0 - spacing_val / 100.0)
        if raw <= 0:
            return 0.0
        return self.mgr.round_to_tick(raw, side="buy")

    def _calc_locked_sell_price(self, entry_price: float, cfg: Dict[str, Any]) -> float:
        if entry_price <= 0:
            return 0.0
        mode = str(cfg.get("spacing_mode") or "PERCENT").upper()
        spacing_val = float(cfg.get("spacing_value") or 0.0)
        if mode == "PERCENT":
            base = entry_price * (1.0 + spacing_val / 100.0)
        else:
            base = entry_price + spacing_val

        risk = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
        fee_bps = float(risk.get("fee_bps_roundtrip", 0.0))
        slip_bps = float(risk.get("slippage_bps_est", 0.0))
        risk_min_pct = (fee_bps + slip_bps) / 100.0
        min_profit_pct = float(cfg.get("sell_lock_min_profit_pct") or 0.0)
        if min_profit_pct <= 0:
            min_profit_pct = risk_min_pct
        if min_profit_pct > 0:
            base = max(base, entry_price * (1.0 + min_profit_pct / 100.0))

        if base <= 0:
            return 0.0
        return self.mgr.round_to_tick(base, side="sell")

    def _guess_entry_price(self, cfg: Dict[str, Any], mstate: Dict[str, Any], current_price: float) -> float:
        try:
            avg_buy = float(cfg.get("avg_buy_price") or 0.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._guess_entry_price suppressed exception", exc_info=True)
            avg_buy = 0.0
        if avg_buy > 0:
            return avg_buy
        try:
            last_buy = float(mstate.get("last_buy_fill_price") or 0.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._guess_entry_price suppressed exception", exc_info=True)
            last_buy = 0.0
        if last_buy > 0:
            return last_buy
        try:
            last_buy_cfg = float(cfg.get("ladder_last_buy_price") or 0.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._guess_entry_price suppressed exception", exc_info=True)
            last_buy_cfg = 0.0
        if last_buy_cfg > 0:
            return last_buy_cfg
        return float(current_price or 0.0)

    def _get_order_qty(self, market: str, uuid_: str) -> float:
        reg = self.mgr._read_order_registry()
        m = reg.get(market)
        if not isinstance(m, dict):
            return 0.0
        meta = m.get(uuid_)
        if not isinstance(meta, dict):
            return 0.0
        try:
            return float(meta.get("qty") or meta.get("volume") or meta.get("executed_volume") or 0.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._get_order_qty suppressed exception", exc_info=True)
            return 0.0

    def _has_locked_sell_for_parent(self, market: str, parent_uuid: str) -> bool:
        if not parent_uuid:
            return False
        reg = self.mgr._read_order_registry()
        m = reg.get(market)
        if not isinstance(m, dict):
            return False
        for meta in m.values():
            if not isinstance(meta, dict):
                continue
            if meta.get("status") in ("filled", "deleted"):
                continue
            if meta.get("side") != "sell":
                continue
            if not meta.get("lock_sell"):
                continue
            if str(meta.get("parent_buy_uuid") or "") == parent_uuid:
                return True
        return False

    def _place_locked_sell(
        self,
        *,
        market: str,
        qty: float,
        price: float,
        cfg: Dict[str, Any],
        entry_price: float,
        parent_buy_uuid: str,
    ) -> bool:
        if qty <= 0 or price <= 0:
            return False
        ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
        rid = str(ids.get("rid") or "")
        try:
            resp = self.mgr._place_limit_sell_qty(market=market, price=price, qty=qty)
            ou = str(resp.get("uuid") or "")
            if not ou:
                return False
            extra = {
                "lock_sell": True,
                "entry_price": float(entry_price or 0.0),
                "min_sell_price": float(price),
                "parent_buy_uuid": str(parent_buy_uuid or ""),
                "last_reprice_ts": 0.0,
            }
            self.mgr._register_order_uuid(
                market=market, rid=rid, uuid_=ou,
                side="sell", price=price, seq=0, qty=qty,
                extra=extra,
            )
            logger.info("GridV2 LOCKED SELL OK: %s uuid=%s price=%.2f qty=%.8f", market, ou, price, qty)
            return True
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error("GridV2 LOCKED SELL FAILED: %s price=%.2f — %s", market, price, e)
            return False

    def _maybe_raise_locked_sell(
        self,
        market: str,
        uuid_: str,
        meta: Dict[str, Any],
        current_price: float,
        cfg: Dict[str, Any],
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            cur_price = float(meta.get("price") or 0.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._maybe_raise_locked_sell suppressed exception", exc_info=True)
            cur_price = 0.0
        if cur_price <= 0 or current_price <= 0:
            return None

        lock_price = float(meta.get("min_sell_price") or cur_price)
        activate_pct = float(cfg.get("sell_lock_activate_pct") or 0.4)
        trail_pct = float(cfg.get("sell_lock_trail_pct") or 0.3)
        min_raise_pct = float(cfg.get("sell_lock_reprice_min_pct") or 0.05)
        cooldown_sec = float(cfg.get("sell_lock_reprice_cooldown_sec") or 15)

        if current_price < lock_price * (1.0 + activate_pct / 100.0):
            return None

        target = max(lock_price, current_price * (1.0 - trail_pct / 100.0))
        target = self.mgr.round_to_tick(target, side="sell")
        if target <= cur_price:
            return None

        if min_raise_pct > 0:
            raise_pct = (target - cur_price) / cur_price * 100.0
            if raise_pct < min_raise_pct:
                return None

        last_ts = float(meta.get("last_reprice_ts") or 0.0)
        if cooldown_sec > 0 and (time.time() - last_ts) < cooldown_sec:
            return None

        try:
            qty = float(meta.get("qty") or 0.0)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._maybe_raise_locked_sell suppressed exception", exc_info=True)
            qty = 0.0
        if qty <= 0:
            return None

        try:
            self.mgr._cancel_order(uuid_=uuid_)
            self.mgr.update_order_status(market, uuid_, "deleted")
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.error("[GRID] cancel order failed during _cancel_and_remove: %s", exc, exc_info=True)

        ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
        rid = str(ids.get("rid") or "")
        try:
            resp = self.mgr._place_limit_sell_qty(market=market, price=target, qty=qty)
            new_uuid = str(resp.get("uuid") or "")
            if not new_uuid:
                return None
            extra = {
                "lock_sell": True,
                "entry_price": float(meta.get("entry_price") or 0.0),
                "min_sell_price": float(target),
                "parent_buy_uuid": str(meta.get("parent_buy_uuid") or ""),
                "last_reprice_ts": time.time(),
            }
            self.mgr._register_order_uuid(
                market=market, rid=rid, uuid_=new_uuid,
                side="sell", price=target, seq=0, qty=qty,
                extra=extra,
            )
            logger.info("GridV2 LOCKED SELL RAISE: %s %s->%s %.2f->%.2f", market, uuid_[:10], new_uuid[:10], cur_price, target)
            new_meta = dict(meta)
            new_meta["price"] = float(target)
            new_meta["lock_sell"] = True
            new_meta["entry_price"] = extra["entry_price"]
            new_meta["min_sell_price"] = extra["min_sell_price"]
            new_meta["qty"] = qty
            new_meta["status"] = "active"
            new_meta["last_reprice_ts"] = extra["last_reprice_ts"]
            return new_uuid, new_meta
        except (OSError, KeyError, IndexError, AttributeError, TypeError, ValueError, OverflowError) as e:
            logger.error("GridV2 LOCKED SELL RAISE FAILED: %s price=%.2f — %s", market, target, e)
            return None

    def _find_uuid_for_price(self, market: str, price: float) -> Optional[str]:
        reg = self.mgr._read_order_registry()
        m = reg.get(market)
        if not isinstance(m, dict):
            return None
        for uuid_, meta in m.items():
            if not isinstance(meta, dict):
                continue
            if meta.get("status") in ("filled", "deleted"):
                continue
            if float(meta.get("price") or 0) == price:
                return uuid_
        return None

    def _cancel_all_buys(self, market: str) -> int:
        reg = self.mgr._read_order_registry()
        m = reg.get(market)
        if not isinstance(m, dict):
            return 0
        canceled = 0
        for uuid_, meta in list(m.items()):
            if not isinstance(meta, dict):
                continue
            if meta.get("status") in ("filled", "deleted"):
                continue
            if meta.get("side") != "buy":
                continue
            try:
                self.mgr._cancel_order(uuid_=uuid_)
                m[uuid_]["status"] = "deleted"
                canceled += 1
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning("GridV2 _cancel_all_buys %s error: %s", uuid_, e)
        reg[market] = m
        self.mgr._write_order_registry(reg)
        return canceled

    def _cancel_all_sells(self, market: str) -> int:
        reg = self.mgr._read_order_registry()
        m = reg.get(market)
        if not isinstance(m, dict):
            return 0
        canceled = 0
        for uuid_, meta in list(m.items()):
            if not isinstance(meta, dict):
                continue
            if meta.get("status") in ("filled", "deleted"):
                continue
            if meta.get("side") != "sell":
                continue
            try:
                self.mgr._cancel_order(uuid_=uuid_)
                m[uuid_]["status"] = "deleted"
                canceled += 1
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning("GridV2 _cancel_all_sells %s error: %s", uuid_, e)
        reg[market] = m
        self.mgr._write_order_registry(reg)
        return canceled

    def _snapshot_active_sells(self, market: str) -> List[Dict[str, Any]]:
        reg = self.mgr._read_order_registry()
        m = reg.get(market)
        if not isinstance(m, dict):
            return []
        out: List[Dict[str, Any]] = []
        for uuid_, meta in m.items():
            if not isinstance(meta, dict):
                continue
            if str(meta.get("side") or "").lower() != "sell":
                continue
            status = str(meta.get("status") or "").lower()
            if status in ("filled", "deleted"):
                continue
            try:
                price = float(meta.get("price") or 0.0)
                qty = float(meta.get("qty") or 0.0)
            except (TypeError, ValueError) as exc:
                logger.warning("[GRID] _snapshot_active_sells parse failed: %s", exc, exc_info=True)
                continue
            if price <= 0 or qty <= 0:
                continue
            out.append({"uuid": str(uuid_), "price": price, "qty": qty})
        out.sort(key=lambda x: float(x.get("price") or 0.0))
        return out

    def _restore_sell_orders(
        self,
        *,
        market: str,
        rid: str,
        sells: List[Dict[str, Any]],
        max_qty: float,
    ) -> Dict[str, Any]:
        restored: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        remain = max(0.0, float(max_qty or 0.0))
        for item in sells:
            if remain <= 0:
                break
            try:
                price = float(item.get("price") or 0.0)
                req_qty = float(item.get("qty") or 0.0)
            except (TypeError, ValueError) as exc:
                logger.warning("[GRID] _restore_sell_orders parse failed: %s", exc, exc_info=True)
                continue
            qty = min(req_qty, remain)
            if price <= 0 or qty <= 0:
                continue
            if (price * qty) < Q.min_order:
                continue
            try:
                resp = self.mgr._place_limit_sell_qty(market=market, price=price, qty=qty)
                ou = str(resp.get("uuid") or "")
                if ou:
                    self.mgr._register_order_uuid(
                        market=market,
                        rid=rid,
                        uuid_=ou,
                        side="sell",
                        price=price,
                        seq=0,
                        qty=qty,
                    )
                restored.append({"uuid": ou, "price": price, "qty": qty})
                remain -= qty
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning("LadderGridV2._restore_sell_orders except: %s", e, exc_info=True)
                failed.append({"price": price, "qty": qty, "error": str(e)})
        return {"restored": restored, "failed": failed, "remain_qty": remain}

    def _cancel_all_orders(self, market: str) -> int:
        reg = self.mgr._read_order_registry()
        m = reg.get(market)
        if not isinstance(m, dict):
            return 0
        canceled = 0
        for uuid_, meta in list(m.items()):
            if not isinstance(meta, dict):
                continue
            if meta.get("status") in ("filled", "deleted"):
                continue
            try:
                self.mgr._cancel_order(uuid_=uuid_)
                m[uuid_]["status"] = "deleted"
                canceled += 1
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning("GridV2 _cancel_all_orders %s error: %s", uuid_, e)
        reg[market] = m
        self.mgr._write_order_registry(reg)
        return canceled

    def _auto_reconfigure(self, market: str, current_price: float, old_cfg: Dict[str, Any]) -> Dict[str, Any]:
        try:
            stats = self.mgr.get_market_stats(market)
            hi = float(stats.get("hi_24h") or 0)
            lo = float(stats.get("lo_24h") or 0)

            lower = lo * 0.97 if lo > 0 else current_price * 0.95
            upper = hi * 1.03 if hi > 0 else current_price * 1.05
            lower = min(lower, current_price * 0.98)
            upper = max(upper, current_price * 1.02)

            manual_lock = str(old_cfg.get("tune_mode") or "").upper() == "MANUAL"
            spacing_mode = str(old_cfg.get("spacing_mode") or "PERCENT").upper()
            if manual_lock:
                spacing_value = float(old_cfg.get("spacing_value") or 0.5)
                max_levels = int(old_cfg.get("max_levels") or 10)
            else:
                if spacing_mode == "FIXED" and float(old_cfg.get("spacing_value") or 0) > 0:
                    spacing_value = float(old_cfg.get("spacing_value") or 0.0)
                    max_levels = self.mgr._suggest_max_levels(
                        upper, lower, "FIXED", spacing_value, cap=40
                    )
                else:
                    spacing_mode = "PERCENT"
                    try:
                        spacing_value = self.mgr.auto_set_spacing_value(market)
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                        logger.warning("LadderGridV2._auto_reconfigure suppressed exception", exc_info=True)
                        spacing_value = float(old_cfg.get("spacing_value") or 0.5)
                    max_levels = self.mgr._suggest_max_levels(
                        upper, lower, "PERCENT", spacing_value, cap=40
                    )
            order_usdt = int(old_cfg.get("order_usdt") or 10)

            new_cfg = dict(old_cfg)
            new_cfg.update({
                "market": market,
                "enabled": True,
                "lower_bound": round(lower, 2),
                "upper_bound": round(upper, 2),
                "spacing_mode": spacing_mode,
                "spacing_value": spacing_value,
                "max_levels": max_levels,
                "order_usdt": order_usdt,
            })
            if not new_cfg.get("tune_mode"):
                new_cfg["tune_mode"] = "AUTO" if not manual_lock else "MANUAL"
            self.mgr.save_config(new_cfg)
            logger.info(
                "GridV2 auto-reconfigured %s: bounds=[%.2f ~ %.2f] spacing=%s%s levels=%d%s",
                market, lower, upper,
                f"{spacing_value:.3f}" if spacing_mode == "PERCENT" else f"{spacing_value:.0f}",
                "%" if spacing_mode == "PERCENT" else "FIXED",
                max_levels,
                " (manual_lock)" if manual_lock else "",
            )
            return new_cfg
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.warning("GridV2 _auto_reconfigure %s failed: %s", market, e)
            return old_cfg

    def _get_live_order_uuids(self, market: str) -> set:
        try:
            u = self.mgr.get_trade_client()
            orders = u.list_orders(state="wait", market=market)
            return {str(o.get("uuid") or "") for o in orders if o.get("uuid")}
        except (KeyError, AttributeError, TypeError) as e:
            logger.warning("GridV2 _get_live_order_uuids %s error: %s", market, e)
            return set()

    def _get_available_qty(self, market: str) -> float:
        try:
            u = self.mgr.get_trade_client()
            currency = market.replace("USDT", "") if market.endswith("USDT") else market
            return u.get_balance(currency, include_locked=False)
        except Exception as exc:
            logger.error("[GRID] _get_available_qty FAILED for %s: %s", market, exc, exc_info=True)
            return -1.0  # 호출자가 음수를 보고 에러 상황임을 인지

    def _get_position_qty(self, market: str) -> float:
        try:
            ctx = self.mgr.system.coordinator.contexts.get(market)
        except AttributeError:
            logger.warning("LadderGridV2._get_position_qty suppressed exception", exc_info=True)
            return 0.0
        if ctx is None:
            return 0.0
        pos = None
        try:
            if isinstance(ctx, dict):
                pos = ctx.get("position")
            else:
                pos = getattr(ctx, "position", None)
        except AttributeError:
            logger.warning("LadderGridV2._get_position_qty suppressed exception", exc_info=True)
            return 0.0
        if pos is None:
            return 0.0
        try:
            if isinstance(pos, dict):
                qty = float(pos.get("qty") or pos.get("volume") or pos.get("balance") or 0)
            else:
                qty = float(
                    getattr(pos, "qty", None)
                    or getattr(pos, "volume", None)
                    or getattr(pos, "balance", None)
                    or 0
                )
            return max(0.0, qty)
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._get_position_qty suppressed exception", exc_info=True)
            return 0.0

    def _has_holding(self, market: str) -> bool:
        try:
            ctx = self.mgr.system.coordinator.contexts.get(market)
        except AttributeError:
            logger.warning("LadderGridV2._has_holding suppressed exception", exc_info=True)
            return False
        if ctx is None:
            return False
        pos = None
        try:
            if isinstance(ctx, dict):
                pos = ctx.get("position")
            else:
                pos = getattr(ctx, "position", None)
        except AttributeError:
            logger.warning("LadderGridV2._has_holding suppressed exception", exc_info=True)
            return False
        if pos is None:
            return False
        try:
            if isinstance(pos, dict):
                qty = float(pos.get("qty") or pos.get("volume") or pos.get("balance") or 0)
            else:
                qty = float(
                    getattr(pos, "qty", None)
                    or getattr(pos, "volume", None)
                    or getattr(pos, "balance", None)
                    or 0
                )
            return qty > 0
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._has_holding suppressed exception", exc_info=True)
            return False

    def _get_budget_cap(self, market: str, cfg: Dict[str, Any]) -> float:
        def _coerce_positive_budget(value: Any) -> float:
            if isinstance(value, bool):
                return 0.0
            if isinstance(value, (int, float)):
                out = float(value)
                return out if out > 0 else 0.0
            if isinstance(value, str):
                try:
                    out = float(value.strip())
                    return out if out > 0 else 0.0
                except (ValueError, AttributeError):
                    logger.warning("LadderGridV2._coerce_positive_budget suppressed exception", exc_info=True)
                    return 0.0
            return 0.0

        try:
            b = _coerce_positive_budget(cfg.get("budget_usdt"))
            if b > 0:
                return b
        except (TypeError, ValueError, AttributeError) as exc:
            logger.warning("[GRID] _coerce_positive_budget primary fallback: %s", exc, exc_info=True)
        try:
            reg = getattr(self.mgr.system, "oma_registry", None)
            if reg and hasattr(reg, "get_budget_usdt"):
                b = _coerce_positive_budget(reg.get_budget_usdt(market))
                if b > 0:
                    return b
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[GRID] _coerce_positive_budget registry fallback: %s", exc, exc_info=True)
        try:
            ctx = self.mgr.system.coordinator.contexts.get(market)
            if ctx is not None:
                alloc = None
                if isinstance(ctx, dict):
                    alloc = ctx.get("allocated_capital")
                else:
                    alloc = getattr(ctx, "allocated_capital", None)
                b = _coerce_positive_budget(alloc)
                if b > 0:
                    return b
        except AttributeError as exc:
            logger.warning("[GRID] _coerce_positive_budget context fallback: %s", exc, exc_info=True)
        return 0.0

    def _count_active_orders(self, market: str, side: Optional[str] = None) -> int:
        try:
            reg = self.mgr._read_order_registry()
        except (OSError, json.JSONDecodeError, AttributeError):
            logger.warning("LadderGridV2._count_active_orders suppressed exception", exc_info=True)
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

    def _coerce_positive_float(self, value: Any, default: float) -> float:
        try:
            out = float(value)
            if out > 0:
                return out
        except (TypeError, ValueError) as exc:
            logger.warning("[GRID] _coerce_positive_float fallback: %s", exc, exc_info=True)
        return float(default)

    def _get_buy_budget_reserve(
        self,
        *,
        market: str,
        cfg: Dict[str, Any],
        budget_cap: float,
        min_order_usdt: float,
        has_holding: bool,
    ) -> float:
        # Keep a small reserve while holding to avoid all-in averaging and repeated buy-line disappearance.
        if budget_cap <= 0:
            return 0.0
        if not has_holding:
            return 0.0
        try:
            ratio = float(
                cfg.get("buy_budget_reserve_ratio")
                or os.getenv("OMA_GRID_BUY_BUDGET_RESERVE_RATIO", "0.10")
                or 0.10
            )
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._get_buy_budget_reserve suppressed exception", exc_info=True)
            ratio = 0.10
        ratio = max(0.0, min(ratio, 0.90))
        try:
            reserve_abs = float(
                cfg.get("buy_budget_reserve_usdt")
                or os.getenv("OMA_GRID_BUY_BUDGET_RESERVE_USDT", "0")
                or 0.0
            )
        except (TypeError, ValueError):
            logger.warning("LadderGridV2._get_buy_budget_reserve suppressed exception", exc_info=True)
            reserve_abs = 0.0
        reserve_abs = max(0.0, reserve_abs)
        reserve = max(float(budget_cap) * ratio, reserve_abs)
        if reserve > 0:
            reserve = max(reserve, float(min_order_usdt))
        max_allowed = max(0.0, float(budget_cap) - float(min_order_usdt))
        reserve = min(reserve, max_allowed)
        if reserve > 0:
            logger.debug(
                "GridV2 buy reserve %s: reserve=%.0f budget_cap=%.0f ratio=%.3f",
                market, reserve, budget_cap, ratio,
            )
        return float(reserve)

    def _as_bool(self, value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "yes", "y", "on"):
                return True
            if v in ("0", "false", "no", "n", "off"):
                return False
        return bool(default)

    def _demote_market_to_watch(self, market: str, reason: str) -> bool:
        try:
            system = getattr(self.mgr, "system", None)
            if system is None or not hasattr(system, "oma_set_market"):
                return False
            from app.manager.oma_market_registry import MarketState

            system.oma_set_market(
                market=market,
                state=MarketState.WATCH,
                reason=[reason],
            )
            return True
        except (KeyError, AttributeError, TypeError) as e:
            logger.warning("GridV2 demote watch failed: %s — %s", market, e)
            return False
