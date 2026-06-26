# ============================================================
# File: app/manager/ladder_manager.py
# Ladder Manager (MVP) — Bybit Reservation Grid
#
# MVP Scope:
# - config save/load (runtime/ladder_config.json)
# - RID-based identification for reconcile/cancel across restarts
#   NOTE: Bybit REST orders API does NOT accept arbitrary client tags.
#         Therefore MVP keeps a local uuid->meta registry file:
#           runtime/ladder_orders.json
#         and reconciles ladder orders by matching UUIDs.
# - level calculation (range + spacing) + safety max_levels
# - open-orders reconcile (Bybit wait orders filtered by UUID registry)
# - seed: BUY limit orders only (below current price within [lower, upper])
# - cancel: cancel ladder orders only (UUID registry + rid match)
#
# NOTE:
# - This manager intentionally does NOT interact with Hyper Engine signals.
# - A market must be exclusive: AUTOLOOP/PINGPONG vs LADDER (no overlap).
# - Tick-size rounding follows Bybit tick-size rules (Decimal-safe).
#   (Previously float(int(p)) caused 0.xx USDT pairs to break; fixed.)
# ============================================================

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, getcontext
import os
import json
import time
import uuid
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.parse

log = logging.getLogger(__name__)
logger = log  # Pylance compat: unify logger/log usage within the file

from app.core.constants import BYBIT_MARKET_TICKERS, BYBIT_MARKET_KLINE
from app.core.currency import Q

CONFIG_PATH = os.path.join("runtime", "ladder_config.json")
ORDERS_PATH = os.path.join("runtime", "ladder_orders.json")  # uuid registry for ladder orders
# ------------------------------------------------------------
# LongHold (LADDER/GAZUA advisory) — notify-only watchlist
# - Operator manually buys/sells; OMA only tracks and notifies via Telegram.
# - Config lives in runtime/longhold_config.json
# ------------------------------------------------------------
LONGHOLD_PATH = os.path.join("runtime", "longhold_config.json")

LONGHOLD_STORE_DEFAULTS = {
    "version": 1,
    "defaults": {
        "enabled": True,
        "strategy": "LADDER",          # LADDER | GAZUA
        "target_profit_pct": 50.0,     # [2026-02-04] LongHold default profit target 50%
        "notify_cooldown_sec": 3600,   # anti-spam cooldown
        "auto_sell_check_interval_min": 10,  # [2026-02-04] auto-sell check interval (minutes)
        "min_position_usdt": 10,
        "budget_usdt": 0,      # ignore tiny dust unless overridden
        "repeat": True,                # allow repeated alerts after cooldown
        # [2026-02-04] Hybrid Auto Sell settings
        "trailing_stop_pct": 2.0,      # Trailing Stop distance (%) (default 2.0%)
        "limit_order_timeout_sec": 30, # limit order wait time (seconds)
        "enable_market_fallback": True, # switch to market order if unfilled
        # [2026-03-19] LongHold stop-loss threshold (absolute vs entry price, 0=disabled)
        "stop_loss_pct": -30.0,
    },
    "markets": {},   # market -> overrides
    "history": [],   # append-only (capped)
    "tracking": {},  # market -> {peak_price, trailing_active, limit_order_uuid, limit_order_ts}
}


DEFAULT_RISK = {
    "fee_bps_roundtrip": 10.0,
    "slippage_bps_est": 10.0,
    "min_spacing_bps_warn": 25.0,
    "min_spacing_bps_block": 15.0,  # MVP: warning only
}

DEFAULT_LIMITS = {
    "max_open_orders_per_market": 40,
}

DEFAULTS = {
    "version": 1,
    "enabled": False,
    "lower_bound": 0.0,
    "upper_bound": 0.0,
    "spacing_mode": "PERCENT",   # PERCENT | FIXED
    "spacing_value": 0.5,
    "order_usdt": 10,
    "max_levels": 30,
    "ladder_fixed_order_usdt": 10,  # [LADDER] fixed buy amount
    "ladder_max_buy_steps": 5,        # [LADDER] max buy steps (N)
    "ladder_pending_steps": 0,        # [LADDER] pending steps awaiting upward reversal (F)
    "ladder_last_buy_ts": 0.0,        # [LADDER] last buy timestamp
    "ladder_last_buy_price": 0.0,     # [LADDER] last buy price
    "reseed_mode": "LOCAL_ONLY",  # LOCAL_ONLY | NONE
    "risk": DEFAULT_RISK,
    "limits": DEFAULT_LIMITS,
    "ids": {"rid": "", "created_ts": 0.0, "updated_ts": 0.0},
    # [LADDER] borrowed buys on upward reversal
    "ladder_max_borrow_steps": 2,   # max borrow count allowed on upward reversal
    "ladder_borrowed_steps": 0,     # borrow count used so far
    # [LADDER] Sell lock (pairing) — never move sell lines down
    "sell_lock_mode": "TRAIL_UP",        # OFF | LOCK | TRAIL_UP
    "sell_lock_activate_pct": 0.4,       # activate ratchet when price > lock by this %
    "sell_lock_trail_pct": 0.3,          # ratchet distance from current price (%)
    "sell_lock_min_profit_pct": 0.0,     # min profit guard (0 = risk-based)
    "sell_lock_reprice_min_pct": 0.05,   # skip tiny reprices (%)
    "sell_lock_reprice_cooldown_sec": 15,  # throttle reprices
    # [LADDER] Phase-based lot tracking (v2 redesign)
    "max_down_buys": 3,
    "reversal_pct": 1.5,
    "profit_borrow_enabled": False,
    "profit_borrow_max": 3,
    "phase": "DOWN",
    "consecutive_down_buys": 0,
    "lowest_price_since_stop": 0.0,
    "reversal_entry_price": 0.0,
    "profit_count": 0,
    "lots": [],
}


class LadderManager:
        def get_global_ladder_exposure(self) -> dict:
            """Aggregate unrealized loss rate, total buy amount, and active market count across all LADDER markets."""
            all_cfg = self._read_json(CONFIG_PATH)
            total_unrealized_loss = 0.0
            total_exposure_usdt = 0.0
            total_alloc_usdt = 0.0
            active_markets = 0
            for m, cfg in all_cfg.items():
                if not isinstance(cfg, dict) or not cfg.get("enabled"):
                    continue
                alloc = float(cfg.get("allocated_capital", 0.0) or 0.0)
                avg_price = float(cfg.get("avg_buy_price", 0.0) or 0.0)
                holding = float(cfg.get("holding_qty", 0.0) or 0.0)
                cur_price = float(cfg.get("last_price", 0.0) or 0.0)
                if holding > 0 and avg_price > 0:
                    unrealized = (cur_price - avg_price) / avg_price * 100.0
                    exposure_usdt_market = holding * avg_price
                    total_unrealized_loss += unrealized * exposure_usdt_market  # [FIX C3] USDT-weighted average (was: qty-weighted -> dimension error shrank it 100x)
                    total_exposure_usdt += exposure_usdt_market
                    total_alloc_usdt += alloc
                    active_markets += 1
            avg_unrealized_loss_pct = (total_unrealized_loss / total_exposure_usdt) if total_exposure_usdt > 0 else 0.0  # [FIX C3] removed x100 (unrealized is already %)
            return {
                "avg_unrealized_loss_pct": avg_unrealized_loss_pct,
                "total_exposure_usdt": total_exposure_usdt,
                "total_alloc_usdt": total_alloc_usdt,
                "active_markets": active_markets,
            }

        def preview_cancel_reservations(self, market: str) -> dict:
            """
            Preview how much budget (USDT and qty) will be released if all open (active) buy/sell orders are canceled.
            Does not execute any trades or cancellations.
            """
            reg = self._read_order_registry()
            m = reg.get(market, {})
            total_reserved_usdt = 0.0
            total_reserved_qty = 0.0
            for uuid_, meta in m.items():
                if not (isinstance(meta, dict) and meta.get("status") == "active"):
                    continue
                side = meta.get("side")
                qty = float(meta.get("qty") or 0)
                order_price = float(meta.get("price") or 0)
                if side == "buy":
                    total_reserved_usdt += qty * order_price
                elif side == "sell":
                    total_reserved_qty += qty
            return {
                "reserved_usdt": total_reserved_usdt,
                "reserved_qty": total_reserved_qty,
                "reserved_order_count": sum(1 for meta in m.values() if isinstance(meta, dict) and meta.get("status") == "active"),
            }

        def sweep_dust_budget(self, market: str, min_dust_usdt: int = 10) -> dict:
            """
            Enhanced dust sweep:
            - Cancel all open (active) buy/sell orders if remaining budget or holding is below threshold.
            - Sum up all canceled order amounts and config budget.
            - Market buy/sell all at once if below threshold.
            """
            cfg = self.get_config(market)
            reg = self._read_order_registry()
            m = reg.get(market, {})
            price = self.get_current_price(market)
            total_reserved_usdt = 0.0
            total_reserved_qty = 0.0
            canceled_orders = []
            for uuid_, meta in list(m.items()):
                if not (isinstance(meta, dict) and meta.get("status") == "active"):
                    continue
                side = meta.get("side")
                qty = float(meta.get("qty") or 0)
                order_price = float(meta.get("price") or 0)
                try:
                    self._cancel_order(uuid_=uuid_)
                    canceled_orders.append(uuid_)
                    if side == "buy":
                        total_reserved_usdt += qty * order_price
                    elif side == "sell":
                        total_reserved_qty += qty
                except (AttributeError, TypeError) as exc:
                    logger.warning("[LADDER] sweep_dust_budget fallback: %s", exc, exc_info=True)
            budget = float(cfg.get("order_usdt") or 0)
            realized = float(cfg.get("realized_profit_usdt") or 0)
            total_usdt = budget + realized + total_reserved_usdt
            ctx = None
            try:
                ctx = self.system.coordinator.contexts.get(market)
            except (KeyError, AttributeError, TypeError):
                log.warning("LadderManager.sweep_dust_budget suppressed exception", exc_info=True)
                ctx = None
            qty = 0.0
            if ctx:
                pos = self._extract_position(ctx)
                if pos:
                    qty = float(pos.get("qty") or 0)
            total_qty = qty + total_reserved_qty
            trade_client = getattr(self.system, "trade_client", None)
            if total_usdt < min_dust_usdt and total_usdt > 0:
                try:
                    if trade_client and price > 0:
                        buy_qty = total_usdt / price
                        result = trade_client.market_buy(market, buy_qty)
                        cfg["order_usdt"] = 0
                        self.save_config(cfg)
                        return {"ok": True, "action": "market_buy", "amount": total_usdt, "qty": buy_qty, "canceled_orders": canceled_orders, "result": result}
                except (KeyError, AttributeError, TypeError) as e:
                    log.warning("LadderManager.sweep_dust_budget except: %s", e, exc_info=True)
                    return {"ok": False, "error": str(e), "canceled_orders": canceled_orders}
            if total_qty * price < min_dust_usdt and total_qty > 0:
                try:
                    if trade_client:
                        result = trade_client.market_sell(market, total_qty)
                        return {"ok": True, "action": "market_sell", "qty": total_qty, "canceled_orders": canceled_orders, "result": result}
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                    log.warning("LadderManager.sweep_dust_budget except: %s", e, exc_info=True)
                    return {"ok": False, "error": str(e), "canceled_orders": canceled_orders}
            return {"ok": True, "action": "none", "budget": total_usdt, "qty": total_qty, "canceled_orders": canceled_orders}

        def signal_based_liquidation(self, market: str, bullish_rsi: float = 60.0, bullish_macd: bool = True, golden_cross: bool = True) -> dict:
            """
            If a bullish signal (RSI, MACD, golden cross) is detected, cancel all active sell ladder orders
            and liquidate all accumulated sell volume at market price.
            """
            try:
                from app.strategy import indicators
            except ImportError:
                log.warning("LadderManager.signal_based_liquidation suppressed exception", exc_info=True)
                return {"error": "indicators import failed"}
            history = []
            try:
                u = self.get_trade_client()
                if hasattr(u, "fetch_candles"):
                    candles = u.fetch_candles(market=market, interval="minute5", count=30)
                    history = [float(c.get("trade_price", 0)) for c in candles if c.get("trade_price")]
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[LADDER] signal_based_liquidation fallback: %s", exc, exc_info=True)
            if not history or len(history) < 20:
                return {"error": "insufficient price history"}
            rsi = indicators.rsi(history, 14)
            macd_line, macd_signal, _ = indicators.macd(history, 12, 26, 9)
            ema5 = indicators.ema(history, 5)
            ema12 = indicators.ema(history, 12)
            ema20 = indicators.ema(history, 20)
            is_golden = ema5 > ema12 and ema12 > ema20 if (ema5 and ema12 and ema20) else False
            is_bullish_macd = (macd_line is not None and macd_signal is not None and macd_line > macd_signal)
            bullish = False
            if rsi is not None and rsi >= bullish_rsi:
                bullish = True
            if bullish_macd and is_bullish_macd:
                bullish = True
            if golden_cross and is_golden:
                bullish = True
            if not bullish:
                return {"ok": True, "signal": False, "reason": {"rsi": rsi, "macd": is_bullish_macd, "golden": is_golden}}
            reg = self._read_order_registry()
            m = reg.get(market, {})
            uuids_to_cancel = []
            total_qty = 0.0
            for uuid_, meta in m.items():
                if isinstance(meta, dict) and meta.get("side") == "sell" and meta.get("status") == "active":
                    uuids_to_cancel.append(uuid_)
                    total_qty += float(meta.get("qty", 0))
            canceled = []
            for uuid_ in uuids_to_cancel:
                try:
                    self._cancel_order(uuid_=uuid_)
                    canceled.append(uuid_)
                except (AttributeError, TypeError) as exc:
                    logger.error("[LADDER] cancel_order failed: %s", exc, exc_info=True)
            sell_result = None
            if total_qty > 0:
                try:
                    trade_client = getattr(self.system, "trade_client", None)
                    if trade_client:
                        sell_result = trade_client.market_sell(market, total_qty)
                except (KeyError, AttributeError, TypeError) as e:
                    log.warning("LadderManager.signal_based_liquidation except: %s", e, exc_info=True)
                    return {"ok": False, "signal": True, "error": str(e)}
            return {"ok": True, "signal": True, "canceled": canceled, "total_qty": total_qty, "sell_result": sell_result, "rsi": rsi, "macd": is_bullish_macd, "golden": is_golden}

        def reset_stats_for_market(self, market: str) -> bool:
            """Reset realized PnL/fee/buy/sell counts for a specific market."""
            reg = self._read_order_registry()
            if market in reg:
                filled_uuids = [
                    u for u, meta in reg[market].items()
                    if isinstance(meta, dict) and meta.get("status") == "filled"
                ]
                for u in filled_uuids:
                    del reg[market][u]
                self._write_order_registry(reg)
            cfg = self.get_config(market)
            for k in ("realized_profit_usdt", "total_fee_usdt", "buy_count", "sell_count"):
                cfg[k] = 0
            self.save_config(cfg)
            return True

        def reset_stats_for_all(self) -> int:
            """Reset realized PnL/fee/buy/sell counts across all markets."""
            all_cfg = self._read_json(CONFIG_PATH)
            reg = self._read_order_registry()
            count = 0
            for market in all_cfg.keys():
                self.reset_stats_for_market(market)
                count += 1
            return count

        def auto_tune_budget_and_levels(self, market: str, min_order_usdt: int = 5, max_order_usdt: int = 500, min_levels: int = 5, max_levels: int = 40) -> Dict[str, any]:
            """
            Based on the last 24h fill frequency, volatility, and volume:
            - Quiet market: budget down, step count up
            - Active market: budget up, step count down
            Auto-adjusts and writes back to config.
            """
            try:
                from app.backtest.candle_loader import CandleLoader
            except ImportError:
                log.warning("LadderManager.auto_tune_budget_and_levels suppressed exception", exc_info=True)
                return {"error": "CandleLoader import failed"}
            loader = CandleLoader()
            candles = loader.load_candles(market, days=1, interval_minutes=60, max_count=24)
            if not candles:
                return {"error": "No candle data"}
            prices = [float(c.get("trade_price", 0)) for c in candles if c.get("trade_price")]
            if not prices:
                return {"error": "No price data"}
            high = max([float(c.get("high_price", 0)) for c in candles])
            low = min([float(c.get("low_price", 0)) for c in candles])
            amplitude = high - low
            current_price = prices[-1]
            amplitude_pct = (amplitude / current_price) * 100.0 if current_price > 0 else 0.0
            volumes = [float(c.get("candle_acc_trade_volume", 0)) for c in candles]
            vol_change = max(volumes) - min(volumes) if volumes else 0.0
            if amplitude_pct < 2.0 or vol_change < 2.0:
                order_usdt = min_order_usdt
                levels = max_levels
            elif amplitude_pct > 5.0 or vol_change > 10.0:
                order_usdt = max_order_usdt
                levels = min_levels
            else:
                ratio = (amplitude_pct - 2.0) / (5.0 - 2.0)
                order_usdt = int(min_order_usdt + (max_order_usdt - min_order_usdt) * ratio)
                levels = int(max_levels - (max_levels - min_levels) * ratio)
            cfg = self.get_config(market)
            cfg["order_usdt"] = order_usdt
            cfg["limits"]["max_open_orders_per_market"] = levels
            self.save_config(cfg)
            return {
                "market": market,
                "order_usdt": order_usdt,
                "max_open_orders_per_market": levels,
                "amplitude_pct": amplitude_pct,
                "vol_change": vol_change,
            }

        def prune_deleted_orders(self, market: str = None) -> int:
            """Permanently remove orders with status 'deleted' from ladder_orders.json. If market is given, only that market; otherwise all markets."""
            reg = self._read_order_registry()
            count = 0
            markets = [market] if market else list(reg.keys())
            for m in markets:
                steps = reg.get(m, {})
                if not isinstance(steps, dict):
                    continue
                before = len(steps)
                steps = {u: meta for u, meta in steps.items() if meta.get("status") != "deleted"}
                after = len(steps)
                if after < before:
                    reg[m] = steps
                    count += before - after
            self._write_order_registry(reg)
            return count

        def auto_set_spacing_value(self, market: str, target_levels: int = 20, interval_minutes: int = 60, window: int = 24) -> float:
            """Auto-set spacing_value(%) based on amplitude: divide 24h amplitude into target_levels parts."""
            gap_info = self.get_dynamic_gap_info(market, interval_minutes, window)
            if gap_info.get("error"):
                return float(self.get_config(market).get("spacing_value") or 0.5)
            return float(gap_info.get("spacing_pct") or 0.5)

        def get_dynamic_gap_info(self, market: str, interval_minutes: int = 60, window: int = 24, target_levels: int = 20) -> Dict[str, Any]:
            """Compute amplitude from 24h candles -> derive spacing as amplitude/target_levels (min fee x3)."""
            try:
                from app.backtest.candle_loader import CandleLoader
            except ImportError:
                log.warning("LadderManager.get_dynamic_gap_info suppressed exception", exc_info=True)
                return {"error": "CandleLoader import failed"}
            loader = CandleLoader()
            candles = loader.load_candles(market, days=1, interval_minutes=interval_minutes, max_count=window)
            if not candles:
                return {"error": "No candle data"}
            highs = [float(c.get("high_price", 0)) for c in candles if c.get("high_price")]
            lows = [float(c.get("low_price", 0)) for c in candles if c.get("low_price")]
            prices = [float(c.get("trade_price", 0)) for c in candles if c.get("trade_price")]
            if not prices or not highs or not lows:
                return {"error": "No price data"}
            high = max(highs)
            low = min(lows)
            amplitude = high - low
            current_price = prices[-1]
            if current_price > 0 and amplitude > 0:
                amplitude_pct = (amplitude / current_price) * 100.0
                recommended_pct = amplitude_pct / max(target_levels, 1)
                recommended_pct = max(0.3, min(recommended_pct, 1.0))
            else:
                recommended_pct = 0.5
            gap_won = current_price * recommended_pct / 100.0
            actual_levels = int(amplitude / gap_won) if gap_won > 0 else 0
            return {
                "market": market,
                "interval_minutes": interval_minutes,
                "window": window,
                "current_price": current_price,
                "high": high,
                "low": low,
                "amplitude": amplitude,
                "spacing_pct": recommended_pct,
                "gap_won": gap_won,
                "levels_in_range": actual_levels,
                "gap_vs_amplitude_pct": (gap_won / amplitude * 100.0) if amplitude > 0 else 0.0
            }

            # --------------------------------------------------------
            # Per-step status update, edit, delete (API support)
            # --------------------------------------------------------
        def update_step_status(self, market: str, uuid_: str, status: str) -> bool:
            """Update the status of a ladder step (active/paused/deleted)."""
            if status not in ("active", "paused", "deleted"):
                return False
            return self.update_order_status(market, uuid_, status)

        def edit_step(self, market: str, uuid_: str, price: float = None, amount: float = None) -> bool:
            """Edit price and/or amount of a ladder step (if not filled or deleted)."""
            reg = self._read_order_registry()
            m = reg.get(market)
            if not isinstance(m, dict) or uuid_ not in m:
                return False
            step = m[uuid_]
            if step.get("status") == "deleted":
                return False
            if price is not None:
                step["price"] = float(price)
            if amount is not None:
                step["amount"] = float(amount)
            m[uuid_] = step
            reg[market] = m
            self._write_order_registry(reg)
            return True

        def __init__(self, system: Any):
            self.system = system
            self._cache_status: Dict[str, Dict[str, Any]] = {}
            self._poll_fail_counts: Dict[str, Dict[str, int]] = {}  # [FIX C2] per-market fill-detection failure counter (persists across calls)
            self._json_locks: Dict[str, threading.RLock] = {}  # [FIX H2] per-file RLock
            self._json_locks_lock = threading.Lock()           # [FIX H2] guards access to _json_locks
            self._longhold_candidates_cache: Dict[str, Any] = {}
            self._longhold_candidates_cache_ts: float = 0.0
            self._longhold_candidates_cache_ttl: float = 300.0
        def delete_step(self, market: str, uuid_: str) -> bool:
            """Mark a ladder step as deleted and cancel order if open."""
            ok = self.update_order_status(market, uuid_, "deleted")
            return ok

        def get_config(self, market: str) -> Dict[str, Any]:
            all_cfg = self._read_json(CONFIG_PATH)
            cfg = all_cfg.get(market)
            if not isinstance(cfg, dict):
                out = dict(DEFAULTS)
                out["market"] = market
                # Phase persist defaults
                out["ladder_phase"] = "DOWN"
                out["ladder_consecutive_down_buys"] = 0
                out["ladder_base_price"] = 0.0
                out["ladder_lowest_since_stop"] = 0.0
                out["ladder_active"] = False
                return out

            out = dict(DEFAULTS)
            out.update(cfg)
            out["market"] = market
            out.setdefault("risk", dict(DEFAULT_RISK))
            out.setdefault("limits", dict(DEFAULT_LIMITS))
            out.setdefault("ids", {"rid": "", "created_ts": 0.0, "updated_ts": 0.0})
            # Ensure Phase persist
            out.setdefault("ladder_phase", "DOWN")
            out.setdefault("ladder_consecutive_down_buys", 0)
            out.setdefault("ladder_base_price", 0.0)
            out.setdefault("ladder_lowest_since_stop", 0.0)
            out.setdefault("ladder_active", False)
            return out

        def save_config(self, cfg_dict: Dict[str, Any]) -> Dict[str, Any]:
            market = str(cfg_dict.get("market") or "").strip()
            if not market:
                raise ValueError("market is required")

            cfg = self.get_config(market)
            cfg.update(cfg_dict)

            # Save grid_auto_sync option if present (value from controls takes priority)
            if "grid_auto_sync" in cfg_dict:
                cfg["grid_auto_sync"] = bool(cfg_dict["grid_auto_sync"])

            now = time.time()
            ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
            rid = str(ids.get("rid") or "").strip()
            if not rid:
                rid = str(uuid.uuid4())
                ids["rid"] = rid
                ids["created_ts"] = now
            ids["updated_ts"] = now
            cfg["ids"] = ids

            cfg["enabled"] = bool(cfg.get("enabled"))
            cfg["lower_bound"] = float(cfg.get("lower_bound") or 0.0)
            cfg["upper_bound"] = float(cfg.get("upper_bound") or 0.0)
            cfg["spacing_mode"] = str(cfg.get("spacing_mode") or "PERCENT").upper()
            cfg["spacing_value"] = float(cfg.get("spacing_value") or 0.0)
            cfg["order_usdt"] = int(float(cfg.get("order_usdt") or 0))
            cfg["max_levels"] = int(float(cfg.get("max_levels") or 0))
            cfg["reseed_mode"] = str(cfg.get("reseed_mode") or "LOCAL_ONLY").upper()
            cfg["sell_lock_mode"] = str(cfg.get("sell_lock_mode") or "TRAIL_UP").upper()
            cfg["sell_lock_activate_pct"] = float(cfg.get("sell_lock_activate_pct") or 0.4)
            cfg["sell_lock_trail_pct"] = float(cfg.get("sell_lock_trail_pct") or 0.3)
            cfg["sell_lock_min_profit_pct"] = float(cfg.get("sell_lock_min_profit_pct") or 0.0)
            cfg["sell_lock_reprice_min_pct"] = float(cfg.get("sell_lock_reprice_min_pct") or 0.05)
            cfg["sell_lock_reprice_cooldown_sec"] = float(cfg.get("sell_lock_reprice_cooldown_sec") or 15)

            if not isinstance(cfg.get("risk"), dict):
                cfg["risk"] = dict(DEFAULT_RISK)
            else:
                r = dict(DEFAULT_RISK); r.update(cfg["risk"]); cfg["risk"] = r

            if not isinstance(cfg.get("limits"), dict):
                cfg["limits"] = dict(DEFAULT_LIMITS)
            else:
                lim = dict(DEFAULT_LIMITS); lim.update(cfg["limits"]); cfg["limits"] = lim

            # Ensure Phase persist
            cfg.setdefault("ladder_phase", "DOWN")
            cfg.setdefault("ladder_consecutive_down_buys", 0)
            cfg.setdefault("ladder_base_price", 0.0)
            cfg.setdefault("ladder_lowest_since_stop", 0.0)
            cfg.setdefault("ladder_active", False)

            # 2026-03-10: prevent saving a completely empty grid (max_levels=0, enabled=False)
            # bounds may be filled later by grid_auto_sync, so only check max_levels
            ml = int(float(cfg.get("max_levels") or 0))
            en = bool(cfg.get("enabled"))
            if ml <= 0 and not en:
                # max_levels=0 and disabled -> ghost entry, do not save
                return cfg

            all_cfg = self._read_json(CONFIG_PATH)
            all_cfg[market] = cfg
            self._write_json(CONFIG_PATH, all_cfg)
            return cfg

        def list_configs(self) -> List[Dict[str, Any]]:
            all_cfg = self._read_json(CONFIG_PATH)
            order_reg = self._read_order_registry()
            markets = sorted(all_cfg.keys())
            out = []
            for m in markets:
                cfg = self.get_config(m)
                # Aggregate realized PnL/fee/buy-sell counts
                reg = order_reg.get(m, {})
                realized_profit = 0.0
                total_fee = 0.0
                buy_count = 0
                sell_count = 0
                for uuid, meta in reg.items():
                    if not isinstance(meta, dict):
                        continue
                    if meta.get("status") != "filled":
                        continue
                    side = meta.get("side")
                    qty = float(meta.get("qty") or meta.get("volume") or meta.get("executed_volume") or 0)
                    fee = float(meta.get("fee") or meta.get("paid_fee") or 0)
                    if side == "buy":
                        buy_count += 1
                        total_fee += fee
                    elif side == "sell":
                        sell_count += 1
                        if "realized_profit" in meta:
                            realized_profit += float(meta["realized_profit"])
                        else:
                            entry_price = float(meta.get("entry_price") or meta.get("price") or 0)
                            sell_price = float(meta.get("avg_price") or meta.get("price") or 0)
                            if qty > 0 and sell_price > 0 and entry_price > 0:
                                realized_profit += (sell_price - entry_price) * qty
                        total_fee += fee
                # Deduct fees
                realized_profit -= total_fee
                cfg["realized_profit_usdt"] = round(realized_profit, 2)
                cfg["total_fee_usdt"] = round(total_fee, 2)
                cfg["buy_count"] = buy_count
                cfg["sell_count"] = sell_count
                out.append(cfg)
            return out

        def _get_json_lock(self, path: str) -> "threading.RLock":
            """[FIX H2] Return a per-file RLock — serialize concurrent read/write on the same file.
            [2026-03-15] longhold_config.json uses a shared lock with strategy_plugins.
            """
            # longhold_config.json -> global shared lock (same as strategy_plugins)
            if os.path.basename(path) == "longhold_config.json":
                from app.core.longhold_file_lock import longhold_file_lock
                return longhold_file_lock
            with self._json_locks_lock:
                if path not in self._json_locks:
                    self._json_locks[path] = threading.RLock()
                return self._json_locks[path]

        def _read_json(self, path: str) -> Dict[str, Any]:
            with self._get_json_lock(path):  # [FIX H2] file lock makes read-modify-write atomic
                try:
                    if not os.path.exists(path):
                        return {}
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        return data if isinstance(data, dict) else {}
                except (json.JSONDecodeError, OSError, ValueError):
                    log.warning("LadderManager._read_json suppressed exception", exc_info=True)
                    return {}

        def _write_json(self, path: str, data: Dict[str, Any]) -> None:
            from app.core.io_utils import safe_write_json
            with self._get_json_lock(path):  # [FIX H2] file lock guarantees atomic write
                safe_write_json(path, data)

            # --------------------------------------------------------
            # UUID registry for ladder orders
            # --------------------------------------------------------
        def _read_order_registry(self) -> Dict[str, Any]:
            return self._read_json(ORDERS_PATH)

        def _write_order_registry(self, reg: Dict[str, Any]) -> None:
            self._write_json(ORDERS_PATH, reg)

        def _register_order_uuid(self, *, market: str, rid: str, uuid_: str, side: str, price: float, seq: int, status: str = "active", qty: float = None, extra: Optional[Dict[str, Any]] = None) -> None:
            reg = self._read_order_registry()
            m = reg.get(market)
            if not isinstance(m, dict):
                m = {}
            m[uuid_] = {
                "rid": rid,
                "side": side,
                "price": float(price),
                "seq": int(seq),
                "created_ts": time.time(),
                "status": status,  # "active"|"paused"|"deleted"
            }
            if qty is not None:
                m[uuid_]["qty"] = qty
            if isinstance(extra, dict) and extra:
                try:
                    m[uuid_].update(extra)
                except (TypeError, AttributeError) as exc:
                    logger.warning("[LADDER] _register_order_uuid fallback: %s", exc, exc_info=True)
            reg[market] = m
            self._write_order_registry(reg)

        def update_order_status(self, market: str, uuid_: str, status: str) -> bool:
            """Change step (order) status: active/paused/deleted"""
            reg = self._read_order_registry()
            m = reg.get(market)
            if not isinstance(m, dict) or uuid_ not in m:
                return False
            m[uuid_]["status"] = status
            reg[market] = m
            self._write_order_registry(reg)
            return True

        def delete_order(self, market: str, uuid_: str) -> bool:
            """Permanently delete a step (order)"""
            reg = self._read_order_registry()
            m = reg.get(market)
            if not isinstance(m, dict) or uuid_ not in m:
                return False
            del m[uuid_]
            reg[market] = m
            self._write_order_registry(reg)
            return True

        def get_order_status(self, market: str, uuid_: str) -> str:
            reg = self._read_order_registry()
            m = reg.get(market)
            if not isinstance(m, dict) or uuid_ not in m:
                return "unknown"
            return m[uuid_].get("status", "active")

        def _ladder_uuids_for_market(self, *, market: str, rid: str) -> List[str]:
            reg = self._read_order_registry()
            m = reg.get(market)
            if not isinstance(m, dict):
                return []
            if not rid:
                return []
            return [u for u, meta in m.items() if isinstance(meta, dict) and str(meta.get("rid") or "") == rid]

        def _prune_registry(self, *, market: str, alive_uuids: List[str]) -> None:
            reg = self._read_order_registry()
            m = reg.get(market)
            if not isinstance(m, dict):
                return
            alive = set(alive_uuids)
            reg[market] = {u: meta for u, meta in m.items() if u in alive}
            self._write_order_registry(reg)

            # --------------------------------------------------------
            # Exclusivity
            # --------------------------------------------------------
        def validate_exclusive_mode(self, market: str) -> None:
            ctx = None
            try:
                ctx = self.system.coordinator.contexts.get(market)
            except (KeyError, AttributeError, TypeError):
                log.warning("LadderManager.validate_exclusive_mode suppressed exception", exc_info=True)
                ctx = None
            if ctx is None:
                return

            controls = getattr(ctx, "controls", {}) or {}
            if not isinstance(controls, dict):
                return
            st = controls.get("strategy", {}) or {}
            if not isinstance(st, dict):
                return

            enabled = bool(st.get("enabled"))
            mode = str(st.get("mode") or st.get("name") or "").upper()
            if enabled and mode in ("AUTOLOOP", "PINGPONG"):
                raise Exception(f"MODE_CONFLICT: {market} is running {mode}. Disable strategy before using LADDER.")

            # --------------------------------------------------------
            # Price
            # --------------------------------------------------------
        def get_current_price(self, market: str) -> Optional[float]:
            # 1) Prefer: look up from coordinator context (cheapest)
            ctx = None
            try:
                ctx = self.system.coordinator.contexts.get(market)
            except (KeyError, AttributeError, TypeError):
                log.warning("LadderManager.get_current_price suppressed exception", exc_info=True)
                ctx = None

            if ctx is not None:
                for k in ("last_price", "price", "last", "current_price"):
                    v = getattr(ctx, k, None)
                    if v is None:
                        continue
                    try:
                        fv = float(v)
                        if fv > 0:
                            return fv
                    except (TypeError, ValueError) as exc:
                        logger.warning("[LADDER] coordinator context price fetch: %s", exc, exc_info=True)

            # 2) fallback: price_store
            try:
                from app.core.hyper_price_store import price_store
                p = price_store.get_price(market)
                if p and float(p) > 0:
                    return float(p)
            except Exception as exc:
                log.debug("[LADDER] price_store fallback failed: %s", exc)

            # 3) fallback: Bybit ticker API with retry
            try:
                import requests as _req
                from app.core.constants import bybit_v5_rest_category, parse_bybit_list, normalize_bybit_ticker
                normalized_market = self._normalize_market(market)
                for _attempt in range(2):
                    try:
                        resp = _req.get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5)
                        resp.raise_for_status()
                        for t in parse_bybit_list(resp.json()):
                            if isinstance(t, dict):
                                tc = normalize_bybit_ticker(t)
                                if tc.get("market", "").upper() == normalized_market.upper():
                                    p = tc.get("trade_price")
                                    return float(p) if p else None
                        break
                    except (_req.exceptions.ConnectionError, _req.exceptions.Timeout) as _e:
                        if _attempt == 0:
                            import time as _t; _t.sleep(1.0)
                        else:
                            log.warning("[LADDER] ticker API failed after 2 attempts: %s", _e)
            except Exception as exc:
                log.warning("[LADDER] get_current_price ticker fallback: %s", exc)
            return None


            # --------------------------------------------------------
            # Levels + tick rounding
            # --------------------------------------------------------
        def calc_levels(self, cfg: Dict[str, Any]) -> List[float]:
            lower = float(cfg.get("lower_bound") or 0.0)
            upper = float(cfg.get("upper_bound") or 0.0)
            mode = str(cfg.get("spacing_mode") or "PERCENT").upper()
            value = float(cfg.get("spacing_value") or 0.0)
            max_levels = int(cfg.get("max_levels") or 0)

            if lower <= 0 or upper <= 0 or upper <= lower or value <= 0 or max_levels <= 0:
                return []

            levels: List[float] = []
            p = upper
            i = 0
            while p >= lower and i < max_levels:
                levels.append(p)
                if mode == "PERCENT":
                    p = p * (1.0 - value / 100.0)
                else:
                    p = p - value
                i += 1
                if not (p > 0) or not (p == p):
                    break
            return levels

        def _normalize_market(self, market: str) -> str:
            """Convert market format to current quote currency format.
        
            Accepts various formats: 'BTC', 'BTCUSDT', 'BTCUSDT', 'BTC/USDT', etc.
            Returns format based on QUOTE_CURRENCY setting (e.g., 'BTCUSDT' or 'BTCUSDT').
            """
            return Q.normalize(market)

        def round_to_tick(self, price: float, side: str) -> float:
            """Round a price to appropriate tick size (Decimal-safe).

            - BUY(bid): round down (floor) to avoid exceeding intended price.
            - SELL(ask): round up (ceil) to avoid undercutting intended price.

            Notes:
            - Using float+floor can mis-round due to binary floating point (e.g., 0.29 -> 0.28).
            - Bybit tick size varies by price range. This follows Bybit's official tick rules.
            """
            try:
                p = Decimal(str(price))
                if p <= 0:
                    return 0.0

                # Bybit tick size rules
                if p >= Decimal("2000000"):
                    tick = Decimal("1000")
                elif p >= Decimal("1000000"):
                    tick = Decimal("500")
                elif p >= Decimal("500000"):
                    tick = Decimal("100")
                elif p >= Decimal("100000"):
                    tick = Decimal("50")
                elif p >= Decimal("10000"):
                    tick = Decimal("10")
                elif p >= Decimal("1000"):
                    tick = Decimal("5")
                elif p >= Decimal("100"):
                    tick = Decimal("1")
                elif p >= Decimal("10"):
                    tick = Decimal("0.1")
                elif p >= Decimal("1"):
                    tick = Decimal("0.01")
                else:
                    tick = Decimal("0.001")

                s = str(side or "").strip().lower()
                rounding = ROUND_FLOOR if s in ("buy", "bid") else ROUND_CEILING if s in ("sell", "ask") else ROUND_FLOOR

                q = (p / tick).to_integral_value(rounding=rounding) * tick
                if q <= 0:
                    return 0.0

                # float() keeps downstream interfaces unchanged; str(float(q)) is typically safe (no long tail digits).
                return float(q)
            except (TypeError, ValueError, ArithmeticError):
                log.warning("LadderManager.round_to_tick suppressed exception", exc_info=True)
                return 0.0

            # --------------------------------------------------------
            # Warnings
            # --------------------------------------------------------
        def compute_warnings(self, cfg: Dict[str, Any], ref_price: float) -> List[Dict[str, Any]]:
            warnings: List[Dict[str, Any]] = []
            risk = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
            fee_bps = float(risk.get("fee_bps_roundtrip", DEFAULT_RISK["fee_bps_roundtrip"]))
            slip_bps = float(risk.get("slippage_bps_est", DEFAULT_RISK["slippage_bps_est"]))
            warn_bps = float(risk.get("min_spacing_bps_warn", DEFAULT_RISK["min_spacing_bps_warn"]))

            spacing_bps = self._spacing_bps(cfg, ref_price)
            cost_bps = fee_bps + slip_bps
            if spacing_bps <= warn_bps:
                warnings.append({
                    "type": "SPACING_TOO_TIGHT",
                    "spacing_bps": spacing_bps,
                    "cost_bps_est": cost_bps,
                    "message": "Spacing may be too tight vs fee+slippage; expected value can be negative.",
                })
            return warnings

        def _spacing_bps(self, cfg: Dict[str, Any], ref_price: float) -> float:
            mode = str(cfg.get("spacing_mode") or "PERCENT").upper()
            val = float(cfg.get("spacing_value") or 0.0)
            if mode == "PERCENT":
                return val * 100.0
            if ref_price <= 0:
                return 0.0
            return (val / ref_price) * 10000.0

            # --------------------------------------------------------
            # Reconcile: Bybit wait orders filtered by UUID registry
            # --------------------------------------------------------
        def reconcile(self, market: str) -> Dict[str, Any]:
            cfg = self.get_config(market)
            ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
            rid = str(ids.get("rid") or "")

            open_orders = self._list_wait_orders(market)

            ladder_uuids = set(self._ladder_uuids_for_market(market=market, rid=rid))
            ladder_open: List[Dict[str, Any]] = []
            alive: List[str] = []

            for o in open_orders:
                ou = str(o.get("uuid") or "")
                if not ou:
                    continue
                if ou in ladder_uuids:
                    ladder_open.append(o)
                    alive.append(ou)

            if rid:
                self._prune_registry(market=market, alive_uuids=alive)

            # Fill-check loop: update filled steps
            self.poll_filled_steps(market)

            buy_n = 0
            sell_n = 0
            for o in ladder_open:
                s = self._side(o)
                if s == "buy":
                    buy_n += 1
                elif s == "sell":
                    sell_n += 1

            st = {
                "market": market,
                "rid": rid,
                "last_sync_ts": time.time(),
                "open_orders": {"buy": buy_n, "sell": sell_n, "total": buy_n + sell_n},
                "latest": {"last_price": self.get_current_price(market), "last_sync_ts": time.time()},
                "blocked": {"high_risk": False, "high_risk_until_ts": None, "reason": ""},  # MVP
            }
            self._cache_status[market] = st
            return st

        def _side(self, o: Dict[str, Any]) -> str:
            v = o.get("side")
            if isinstance(v, str):
                v = v.lower()
                if v == "bid":
                    return "buy"
                if v == "ask":
                    return "sell"
            return "unknown"

            # --------------------------------------------------------
            # Trend detection helpers
            # --------------------------------------------------------
        def is_rebound(self, market: str, lookback: int = 5, threshold_pct: float = 2.0) -> bool:
            try:
                from app.backtest.candle_loader import CandleLoader
                loader = CandleLoader()
                candles = loader.load_candles(market, days=1, interval_minutes=60, max_count=lookback)
                if not candles or len(candles) < lookback:
                    return False
                prices = [float(c.get("trade_price", 0)) for c in candles if c.get("trade_price")]
                if len(prices) < lookback:
                    return False
                start = prices[0]
                end = prices[-1]
                if start <= 0 or end <= 0:
                    return False
                return ((end / start) - 1.0) * 100.0 >= threshold_pct
            except (KeyError, IndexError, AttributeError, TypeError, ValueError):
                log.warning("LadderManager.is_rebound suppressed exception", exc_info=True)
                return False

        def is_downtrend(self, market: str, lookback: int = 5, threshold_pct: float = -2.0) -> bool:
            try:
                from app.backtest.candle_loader import CandleLoader
                loader = CandleLoader()
                candles = loader.load_candles(market, days=1, interval_minutes=60, max_count=lookback)
                if not candles or len(candles) < lookback:
                    return False
                prices = [float(c.get("trade_price", 0)) for c in candles if c.get("trade_price")]
                if len(prices) < lookback:
                    return False
                start = prices[0]
                end = prices[-1]
                if start <= 0 or end <= 0:
                    return False
                return ((end / start) - 1.0) * 100.0 <= threshold_pct
            except (KeyError, IndexError, AttributeError, TypeError, ValueError):
                log.warning("LadderManager.is_downtrend suppressed exception", exc_info=True)
                return False

            # --------------------------------------------------------
            # Seed / Cancel
            # --------------------------------------------------------
        def seed_buy_orders(self, cfg: Dict[str, Any], current_price: float, is_rebound_trigger: bool = False) -> Dict[str, Any]:
            market = str(cfg.get("market") or "")
            if not market:
                raise ValueError("market is required")
            if not bool(cfg.get("enabled")):
                raise Exception("LADDER_DISABLED")
            lower = float(cfg.get("lower_bound") or 0)
            upper = float(cfg.get("upper_bound") or 0)
            if lower > 0 and upper > 0 and (current_price < lower * 0.5 or current_price > upper * 2.0):
                log.warning(  # [FIX L1] use module-level log
                    "seed_buy_orders BLOCKED: %s price=%.2f outside bounds [%.2f ~ %.2f]",
                    market, current_price, lower, upper,
                )
                return {"market": market, "skipped": True, "reason": "price_out_of_range"}

            ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
            rid = str(ids.get("rid") or "")
            if not rid:
                cfg = self.save_config(cfg)
                ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
                rid = str(ids.get("rid") or "")

            # Apply N-times fixed-buy structure

            max_buy_steps = int(cfg.get("ladder_max_buy_steps") or 5)
            fixed_order_usdt = int(cfg.get("ladder_fixed_order_usdt") or cfg.get("order_usdt") or 10)
            pending_steps = int(cfg.get("ladder_pending_steps") or 0)
            max_borrow_steps = int(cfg.get("ladder_max_borrow_steps") or 0)
            borrowed_steps = int(cfg.get("ladder_borrowed_steps") or 0)
            # Check the number of filled buy steps
            reg = self._read_order_registry()
            m = reg.get(market, {})
            filled_buy = [u for u, meta in m.items() if isinstance(meta, dict) and meta.get("side") == "buy" and meta.get("status") == "filled"]
            open_buy = [u for u, meta in m.items() if isinstance(meta, dict) and meta.get("side") == "buy" and meta.get("status") == "active"]
            total_buys = len(filled_buy) + len(open_buy)
            realized_buy_count = len(filled_buy)

            # When N is reached, stop additional buys; queue remaining steps as pending
            if total_buys >= max_buy_steps:
                # If not an upward-reversal trigger, queue as pending then stop
                if not is_rebound_trigger:
                    cfg["ladder_pending_steps"] = pending_steps + max(0, len(open_buy) - max_buy_steps)
                    self.save_config(cfg)
                    return {"market": market, "skipped": True, "reason": f"max_buy_steps({max_buy_steps}) reached", "pending_steps": cfg["ladder_pending_steps"]}
                # On upward-reversal trigger: if pending_steps > 0 && within allowed borrow count, do borrowed buys
                if pending_steps > 0 and borrowed_steps < min(max_borrow_steps, realized_buy_count):
                    # Do borrowed buys (as many as pending_steps)
                    allow_borrow = min(pending_steps, min(max_borrow_steps, realized_buy_count) - borrowed_steps)
                    if allow_borrow <= 0:
                        return {"market": market, "skipped": True, "reason": "borrow limit reached", "pending_steps": pending_steps, "borrowed_steps": borrowed_steps}
                    # Create steps: allow_borrow levels below the current price
                    levels = self.calc_levels(cfg)
                    lower = float(cfg.get("lower_bound") or 0.0)
                    upper = float(cfg.get("upper_bound") or 0.0)
                    levels = [p for p in levels if p < float(current_price) and lower <= p <= upper]
                    levels = [self.round_to_tick(p, side="buy") for p in levels]
                    levels = [p for p in levels if p > 0 and p < float(current_price) and lower <= p <= upper]
                    levels = sorted(set(levels), reverse=True)
                    levels = levels[:allow_borrow]
                    created = 0
                    failed: List[Dict[str, Any]] = []
                    for idx, price in enumerate(levels, start=1):
                        qty = float(fixed_order_usdt) / float(price) if price > 0 else 0.0
                        if qty <= 0:
                            continue
                        try:
                            resp = self._place_limit_buy_qty(market=market, price=price, qty=qty)
                            ou = str(resp.get("uuid") or "")
                            if ou:
                                self._register_order_uuid(market=market, rid=rid, uuid_=ou, side="buy", price=price, seq=idx, qty=qty)
                                resp["ladder_meta"] = {"type": "buy", "order_price": price, "current_price": current_price, "ts": time.time()}
                            created += 1
                        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
                            log.warning("LadderManager.seed_buy_orders except: %s", e, exc_info=True)
                            failed.append({"price": price, "error": str(e), "current_price": current_price})
                    # Increment borrow count, decrement pending_steps
                    cfg["ladder_borrowed_steps"] = borrowed_steps + allow_borrow
                    cfg["ladder_pending_steps"] = max(0, pending_steps - allow_borrow)
                    self.save_config(cfg)
                    return {
                        "market": market,
                        "created_borrowed": created,
                        "failed": failed,
                        "levels_used": levels,
                        "borrowed_steps": cfg["ladder_borrowed_steps"],
                        "pending_steps": cfg["ladder_pending_steps"],
                    }
                else:
                    return {"market": market, "skipped": True, "reason": "no pending or borrow not allowed", "pending_steps": pending_steps, "borrowed_steps": borrowed_steps}

            # Create steps: only below the current price, up to N times
            levels = self.calc_levels(cfg)
            lower = float(cfg.get("lower_bound") or 0.0)
            upper = float(cfg.get("upper_bound") or 0.0)
            levels = [p for p in levels if p < float(current_price) and lower <= p <= upper]
            levels = [self.round_to_tick(p, side="buy") for p in levels]
            levels = [p for p in levels if p > 0 and p < float(current_price) and lower <= p <= upper]
            levels = sorted(set(levels), reverse=True)
            # Create only as many as the remaining step count
            allow_new = max(0, max_buy_steps - total_buys)
            levels = levels[:allow_new]

            created = 0
            failed: List[Dict[str, Any]] = []
            # Prevent duplicate buy orders at nearly the same price (float-safe, min_profit_gap)
            getcontext().prec = 12  # [FIX L3] imported at module top, removed duplicate import inside function
            min_profit_gap = Decimal(str(cfg.get("min_profit_gap", 0.001)))  # 0.1% default
            existing_buy_prices = set()
            for u, meta in m.items():
                if isinstance(meta, dict) and meta.get("side") == "buy" and meta.get("status") == "active":
                    existing_buy_prices.add(Decimal(str(meta.get("price", 0))))

            # Apply cumulative buy amount cap
            max_total_usdt = int(cfg.get("max_total_buy_usdt", fixed_order_usdt * max_buy_steps * 3))
            total_usdt_used = 0.0

            for idx, price in enumerate(levels, start=1):
                price_dec = Decimal(str(price))
                # Skip if an active buy order already exists at nearly the same price (within min_profit_gap)
                duplicate = False
                for exist_price in existing_buy_prices:
                    if exist_price == 0:
                        continue
                    diff = abs(price_dec - exist_price) / abs(exist_price)
                    if diff < min_profit_gap:
                        duplicate = True
                        break
                if duplicate:
                    continue
                # move_count: if an existing order shares the same price band, use its move_count+1, else 0
                move_count = 0
                for u, meta in m.items():
                    if (
                        isinstance(meta, dict)
                        and meta.get("side") == "buy"
                        and meta.get("status") == "active"
                        and abs(float(meta.get("price", 0)) - price) < 1e-6
                    ):
                        move_count = int(meta.get("move_count", 0)) + 1

                # --- Improvement: prefer order_usdt, fall back otherwise ---
                # order_usdt first, else fixed_order_usdt, else allocated_capital/max_steps
                order_usdt = cfg.get("order_usdt")
                if order_usdt is not None and order_usdt > 0:
                    base_usdt = float(order_usdt)
                elif fixed_order_usdt is not None and fixed_order_usdt > 0:
                    base_usdt = float(fixed_order_usdt)
                else:
                    alloc = float(cfg.get("allocated_capital", 0.0) or 0.0)
                    max_steps = int(cfg.get("max_levels") or 1)
                    base_usdt = alloc / max_steps if max_steps > 0 else 10.0

                if move_count == 0:
                    qty = base_usdt / float(price) if price > 0 else 0.0
                    usdt = base_usdt
                elif move_count == 1:
                    qty = base_usdt * 2 / float(price) if price > 0 else 0.0
                    usdt = base_usdt * 2
                elif move_count >= 2:
                    qty = base_usdt * 3 / float(price) if price > 0 else 0.0
                    usdt = base_usdt * 3
                else:
                    qty = base_usdt / float(price) if price > 0 else 0.0
                    usdt = base_usdt
                if qty <= 0:
                    continue
                if total_usdt_used + usdt > max_total_usdt:
                    break
                try:
                    resp = self._place_limit_buy_qty(market=market, price=price, qty=qty)
                    ou = str(resp.get("uuid") or "")
                    if ou:
                        self._register_order_uuid(
                            market=market,
                            rid=rid,
                            uuid_=ou,
                            side="buy",
                            price=price,
                            seq=idx,
                            status="active",
                            qty=qty
                        )
                        resp["ladder_meta"] = {
                            "type": "buy",
                            "order_price": price,
                            "current_price": current_price,
                            "ts": time.time(),
                            "move_count": move_count
                        }
                    created += 1
                    total_usdt_used += usdt
                except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
                    log.warning("LadderManager.seed_buy_orders except: %s", e, exc_info=True)
                    failed.append({"price": price, "error": str(e), "current_price": current_price})

            return {
                "existing_open": total_buys,
                "created_buy": created,
                "failed": failed,
                "levels_used": levels,
                "max_buy_steps": max_buy_steps,
                "pending_steps": cfg.get("ladder_pending_steps", 0),
                "borrowed_steps": cfg.get("ladder_borrowed_steps", 0),
            }


        def cancel_ladder_orders(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
            market = str(cfg.get("market") or "")
            ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
            rid = str(ids.get("rid") or "")

            ladder_uuids = set(self._ladder_uuids_for_market(market=market, rid=rid))
            open_orders = self._list_wait_orders(market)

            canceled = 0
            failed: List[Dict[str, Any]] = []
            alive: List[str] = []

            for o in open_orders:
                ou = str(o.get("uuid") or "")
                if not ou:
                    continue
                if ou not in ladder_uuids:
                    continue
                try:
                    self._cancel_order(uuid_=ou)
                    canceled += 1
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                    log.warning("LadderManager.cancel_ladder_orders except: %s", e, exc_info=True)
                    failed.append({"uuid": ou, "error": str(e)})
                    alive.append(ou)

            if rid:
                self._prune_registry(market=market, alive_uuids=alive)

            return {"canceled": canceled, "failed": failed, "open_before": len(ladder_uuids)}

            # --------------------------------------------------------
            # TradeClient adapter (exchange-agnostic)
            # --------------------------------------------------------
        def _get_trade_client(self) -> Any:
            """Get the trade client from system (supports multiple attribute names)."""
            for attr in ("trade_client", "exchange", "bybit_trade", "bybit"):
                if hasattr(self.system, attr):
                    client = getattr(self.system, attr)
                    if client is not None:
                        return client
            raise RuntimeError("No TradeClient found on system (expected system.trade_client or system.exchange).")

        _get_trade_client_alias = _get_trade_client

        def _list_wait_orders(self, market: str) -> List[Dict[str, Any]]:
            u = self.get_trade_client()
            if hasattr(u, "list_wait_orders"):
                return u.list_wait_orders(market=market, max_pages=3, per_page=100, order_by="desc")
            if hasattr(u, "list_orders"):
                return u.list_orders(state="wait", market=market, limit=100, page=1, order_by="desc")
            raise Exception("BybitTradeClient missing list_wait_orders/list_orders.")

        def _place_limit_buy_qty(self, *, market: str, price: float, qty: float) -> Dict[str, Any]:
            from app.integrations.bybit_trade import adjust_price_to_tick
            price = adjust_price_to_tick(float(price), side="bid")
            if price <= 0:
                raise ValueError("invalid_price<=0")

            price_dec = Decimal(str(price))
            qty_dec = Decimal(str(qty)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if qty_dec <= 0:
                raise ValueError("invalid_qty<=0")

            # Prevent the case where float error falsely fails Bybit's minimum order-value boundary.
            min_total = Decimal(str(Q.min_order))
            try:
                min_buffer = Decimal(str(max(0.0, float(os.getenv("OMA_LADDER_MIN_ORDER_BUFFER_USDT", "10") or 10.0))))
            except (TypeError, ValueError):
                log.warning("LadderManager._place_limit_buy_qty suppressed exception", exc_info=True)
                min_buffer = Decimal("10")
            min_total = min_total + min_buffer

            total_dec = price_dec * qty_dec
            if total_dec < min_total:
                needed_qty = (min_total / price_dec).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
                if needed_qty > qty_dec:
                    qty_dec = needed_qty
                    total_dec = price_dec * qty_dec

            if total_dec < min_total:
                raise ValueError(f"min_order_blocked: {float(total_dec):.6f} < {float(min_total):.6f}")
            u = self.get_trade_client()
            if hasattr(u, "place_order"):
                return u.place_order(
                    market=market,
                    side="bid",
                    ord_type="limit",
                    price=str(price),
                    volume=str(qty_dec),
                )
            raise Exception("BybitTradeClient missing place_order().")

        def _place_limit_sell_qty(self, *, market: str, price: float, qty: float) -> Dict[str, Any]:
            from app.integrations.bybit_trade import adjust_price_to_tick
            price = adjust_price_to_tick(float(price), side="ask")
            u = self.get_trade_client()
            if hasattr(u, "place_order"):
                return u.place_order(
                    market=market,
                    side="ask",
                    ord_type="limit",
                    price=str(price),
                    volume=str(float(qty)),
                )
            raise Exception("BybitTradeClient missing place_order().")

        def _cancel_order(self, *, uuid_: str) -> Dict[str, Any]:
            u = self.get_trade_client()
            if hasattr(u, "cancel_order"):
                return u.cancel_order(uuid=uuid_)
            raise Exception("BybitTradeClient missing cancel_order(uuid=...).")

            # --------------------------------------------------------
            # Market stats proxy (server-side; avoids CORS)
            # - returns last_price, hi_24h, lo_24h and suggested_max_levels
            # --------------------------------------------------------
        def get_market_stats(self, market: str, spacing_mode: Optional[str] = None, spacing_value: Optional[float] = None) -> Dict[str, Any]:
            last_price, hi_24h, lo_24h = self._fetch_ticker(market)
            cfg = self.get_config(market)
            sm = (spacing_mode or cfg.get("spacing_mode") or "PERCENT").upper()
            sv = spacing_value if spacing_value is not None else float(cfg.get("spacing_value") or 0.5)
            suggested = self._suggest_max_levels(hi_24h, lo_24h, sm, sv, cap=40)
            # Add dynamic gap info
            gap_info = self.get_dynamic_gap_info(market)

            # --- Aggregate realized PnL, fees, buy/sell counts ---
            realized_pnl = 0.0
            total_fee = 0.0
            buy_count = 0
            sell_count = 0
            filled_count = 0
            try:
                reg = self._read_order_registry()
                steps = reg.get(market, {})
                for step in steps.values():
                    status = step.get("status")
                    side = step.get("side")
                    if status == "filled":
                        filled_count += 1
                        fee = float(step.get("fee") or 0)
                        total_fee += fee
                        if side == "buy":
                            buy_count += 1
                        elif side == "sell":
                            sell_count += 1
                            # Realized PnL: computed only on sell fills
                            realized_pnl += float(step.get("realized_profit") or 0)
            except (TypeError, ValueError, KeyError) as exc:
                logger.warning("[LADDER] realized PnL calc: %s", exc, exc_info=True)

            return {
                "market": market,
                "last_price": last_price,
                "hi_24h": hi_24h,
                "lo_24h": lo_24h,
                "suggested_max_levels": suggested,
                "ts": time.time(),
                "dynamic_gap_info": gap_info,
                # Added aggregate info
                "realized_pnl": realized_pnl,
                "total_fee": total_fee,
                "buy_count": buy_count,
                "sell_count": sell_count,
                "filled_count": filled_count,
            }

        def _fetch_ticker(self, market: str) -> tuple[float | None, float | None, float | None]:
            """Fetch ticker info from Bybit. Returns (trade_price, high_price, low_price)."""
            normalized_market = self._normalize_market(market)
            url = f"{BYBIT_MARKET_TICKERS}?markets={normalized_market}"
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if isinstance(data, list) and len(data) > 0:
                    item = data[0]
                    trade_price = item.get("trade_price")
                    hi = item.get("high_price")
                    lo = item.get("low_price")
                    try:
                        trade_f = float(trade_price) if trade_price is not None else None
                        hi_f = float(hi) if hi is not None else None
                        lo_f = float(lo) if lo is not None else None
                        return trade_f, hi_f, lo_f
                    except (TypeError, ValueError):
                        log.warning("LadderManager._fetch_ticker suppressed exception", exc_info=True)
                        return None, None, None
            except (OSError, json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError, ValueError):
                log.warning("LadderManager._fetch_ticker suppressed exception", exc_info=True)
                return None, None, None
            return None, None, None

        def _suggest_max_levels(self, hi: Optional[float], lo: Optional[float], spacing_mode: str, spacing_value: float, cap: int = 40) -> int:
            # fallback
            base = int((self.get_config("DUMMY").get("max_levels") or 30))
            if not (hi and lo and hi > 0 and lo > 0 and hi > lo and spacing_value > 0):
                return max(1, min(cap, base))

            if spacing_mode == "FIXED":
                n = int((hi - lo) / spacing_value) + 1
                return max(1, min(cap, n))

            # PERCENT
            step = spacing_value / 100.0
            if not (0 < step < 1):
                return max(1, min(cap, base))

            ratio = lo / hi
            # number of steps to reach lo from hi with geometric decay
            n = int(math.ceil(math.log(ratio) / math.log(1.0 - step))) + 1
            return max(1, min(cap, n))

        def purge_market(self, market: str):
            # ladder_config.json
            try:
                cfg = self._read_json(CONFIG_PATH)
                cfg.pop(market, None)
                self._write_json(CONFIG_PATH, cfg)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[LADDER] ladder_config.json cleanup: %s", exc, exc_info=True)

            # ladder_orders.json
            try:
                reg = self._read_json(ORDERS_PATH)
                reg.pop(market, None)
                self._write_json(ORDERS_PATH, reg)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[LADDER] ladder_orders.json cleanup: %s", exc, exc_info=True)

        def seed_ladder_orders(self, cfg: Dict[str, Any], current_price: float) -> Dict[str, Any]:
            """Create N/2 steps below (buy) and above (sell) the current price (Bybit reservation orders)."""
            market = str(cfg.get("market") or "")
            if not market:
                raise ValueError("market is required")
            if not bool(cfg.get("enabled")):
                raise Exception("LADDER_DISABLED")
            lower = float(cfg.get("lower_bound") or 0)
            upper = float(cfg.get("upper_bound") or 0)
            if lower > 0 and upper > 0 and (current_price < lower * 0.5 or current_price > upper * 2.0):
                log.warning(  # [FIX L1] use module-level log
                    "seed_ladder_orders BLOCKED: %s price=%.2f outside bounds [%.2f ~ %.2f]",
                    market, current_price, lower, upper,
                )
                return {"market": market, "skipped": True, "reason": "price_out_of_range"}
            cfg = self.get_config(market)
            ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
            rid = str(ids.get("rid") or "")
            if not rid:
                cfg = self.save_config(cfg)
                ids = cfg.get("ids") if isinstance(cfg.get("ids"), dict) else {}
                rid = str(ids.get("rid") or "")
            st = self.reconcile(market)
            existing_total = int(st.get("open_orders", {}).get("total", 0))
            limits = cfg.get("limits") if isinstance(cfg.get("limits"), dict) else {}
            max_open = int(limits.get("max_open_orders_per_market", DEFAULT_LIMITS["max_open_orders_per_market"]))
            allow_new = max(0, max_open - existing_total)
            levels = self.calc_levels(cfg)
            lower = float(cfg.get("lower_bound") or 0.0)
            upper = float(cfg.get("upper_bound") or 0.0)
            max_steps = int(cfg.get("max_levels") or 0)
            buy_levels = [p for p in levels if p < float(current_price) and lower <= p <= upper]
            sell_levels = [p for p in levels if p > float(current_price) and lower <= p <= upper]
            buy_levels = [self.round_to_tick(p, side="buy") for p in buy_levels]
            sell_levels = [self.round_to_tick(p, side="sell") for p in sell_levels]
            buy_levels = sorted(set(buy_levels), reverse=True)
            sell_levels = sorted(set(sell_levels))
            n_buy = max_steps // 2
            n_sell = max_steps - n_buy
            buy_levels = buy_levels[:n_buy]
            sell_levels = sell_levels[:n_sell]
            order_usdt = int(cfg.get("order_usdt") or 0)
            if order_usdt <= 0:
                raise ValueError("order_usdt must be > 0")
            created_buy = 0
            created_sell = 0
            failed: List[Dict[str, Any]] = []
            for idx, price in enumerate(buy_levels, start=1):
                qty = float(order_usdt) / float(price) if price > 0 else 0.0
                if qty <= 0:
                    continue
                try:
                    resp = self._place_limit_buy_qty(market=market, price=price, qty=qty)
                    ou = str(resp.get("uuid") or "")
                    if ou:
                        self._register_order_uuid(market=market, rid=rid, uuid_=ou, side="buy", price=price, seq=idx, qty=qty)
                        resp["ladder_meta"] = {"type": "buy", "order_price": price, "current_price": current_price, "ts": time.time()}
                    created_buy += 1
                except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
                    log.warning("LadderManager.seed_ladder_orders except: %s", e, exc_info=True)
                    failed.append({"side": "buy", "price": price, "error": str(e), "current_price": current_price})
            for idx, price in enumerate(sell_levels, start=1):
                qty = float(order_usdt) / float(price) if price > 0 else 0.0
                if qty <= 0:
                    continue
                try:
                    resp = self._place_limit_sell_qty(market=market, price=price, qty=qty)
                    ou = str(resp.get("uuid") or "")
                    if ou:
                        self._register_order_uuid(market=market, rid=rid, uuid_=ou, side="sell", price=price, seq=idx, qty=qty)
                        resp["ladder_meta"] = {"type": "sell", "order_price": price, "current_price": current_price, "ts": time.time()}
                    created_sell += 1
                except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
                    log.warning("LadderManager.seed_ladder_orders except: %s", e, exc_info=True)
                    failed.append({"side": "sell", "price": price, "error": str(e), "current_price": current_price})
            return {
                "existing_open": existing_total,
                "created_buy": created_buy,
                "created_sell": created_sell,
                "failed": failed,
                "buy_levels_used": buy_levels,
                "sell_levels_used": sell_levels,
                "max_open_orders_per_market": max_open,
            }

        def auto_expand_ladder_on_fill(self, market: str, uuid_: str, expand_n: int = 2) -> None:
            """On step fill, auto-add expand_n steps below/above."""
            # [FIX L1] use module-level log, removed in-function import logging

            reg = self._read_order_registry()
            m = reg.get(market)
            if not isinstance(m, dict) or uuid_ not in m:
                return
            step = m[uuid_]
            side = step.get("side")
            price = float(step.get("price") or 0.0)
            filled_qty = float(step.get("qty") or 0.0)

            cfg = self.get_config(market)
            current_price = self.get_current_price(market)
            order_usdt = int(cfg.get("order_usdt") or 0)

            # [NEW] Pairing Sell: on buy fill, immediately place a sell order (TP) to create a profit opportunity
            # Includes deduplication: skip if a sell order already exists at that price band
            if side == "buy" and filled_qty > 0:
                spacing_val = float(cfg.get("spacing_value") or 0.5)
                spacing_mode = str(cfg.get("spacing_mode") or "PERCENT").upper()
                
                if spacing_mode == "PERCENT":
                    sell_price = price * (1.0 + spacing_val / 100.0)
                else:
                    sell_price = price + spacing_val
                
                sell_price = self.round_to_tick(sell_price, side="sell")
                
                # Prevent duplicate orders: skip if an active sell order already exists at a similar price (within 0.05%)
                is_duplicate = False
                for u, meta in m.items():
                    if meta.get("status") == "active" and meta.get("side") == "sell":
                        existing_p = float(meta.get("price") or 0)
                        if existing_p > 0 and abs(existing_p - sell_price) / existing_p < 0.0005:
                            is_duplicate = True
                            break
                
                if not is_duplicate:
                    try:
                        # Create sell order (for the bought quantity)
                        resp = self._place_limit_sell_qty(market=market, price=sell_price, qty=filled_qty)
                        ou = str(resp.get("uuid") or "")
                        if ou:
                            self._register_order_uuid(
                                market=market, 
                                rid=step.get("rid"), 
                                uuid_=ou, 
                                side="sell", 
                                price=sell_price, 
                                seq=0, 
                                qty=filled_qty,
                                extra={"parent_buy_uuid": uuid_, "type": "pairing_sell"}
                            )
                            log.info(f"[LADDER] Pairing Sell Placed: {market} {sell_price} (qty={filled_qty})")
                    except (KeyError, AttributeError, TypeError, ValueError) as e:
                        log.error(f"[LADDER] Pairing Sell Failed: {market} {e}")
                else:
                    log.info(f"[LADDER] Pairing Sell Skipped (Duplicate): {market} {sell_price}")

            # [FIX] Ladder Expansion: on fill, expand steps up/down (with dedup applied)
            spacing_val = float(cfg.get("spacing_value") or 0.5)
            spacing_mode = str(cfg.get("spacing_mode") or "PERCENT").upper()

            for i in range(1, expand_n + 1):
                if side == "buy":
                    # [FIX M2] removed dead code: the first computation was immediately overwritten and meaningless
                    if spacing_mode == "PERCENT":
                        new_price = price * (1.0 - spacing_val / 100.0 * i)
                    else:
                        new_price = price - (spacing_val * i)
                    new_price = self.round_to_tick(new_price, side="buy")
                    # [FIX C1] check duplicates first, place order only when not duplicate (was: order->dedup check->reorder = double order)
                    is_duplicate = False
                    for u, meta in m.items():
                        if meta.get("status") == "active" and meta.get("side") == "buy":
                            existing_p = float(meta.get("price") or 0)
                            if existing_p > 0 and abs(existing_p - new_price) / existing_p < 0.0005:
                                is_duplicate = True
                                break
                    if not is_duplicate:
                        qty = float(order_usdt) / float(new_price) if new_price > 0 else 0.0
                        if qty > 0:
                            try:
                                resp = self._place_limit_buy_qty(market=market, price=new_price, qty=qty)
                                ou = str(resp.get("uuid") or "")
                                if ou:
                                    self._register_order_uuid(market=market, rid=step.get("rid"), uuid_=ou, side="buy", price=new_price, seq=0, qty=qty)
                            except (KeyError, AttributeError, TypeError) as _e:  # [FIX M1] added logging
                                log.warning(f"[LADDER] buy expand failed: {market} {new_price} {_e}")
                elif side == "sell":
                    # [FIX M2] removed dead code: the first computation was immediately overwritten and meaningless
                    if spacing_mode == "PERCENT":
                        new_price = price * (1.0 + spacing_val / 100.0 * i)
                    else:
                        new_price = price + (spacing_val * i)
                    new_price = self.round_to_tick(new_price, side="sell")
                    # [FIX C1] check duplicates first, place order only when not duplicate
                    is_duplicate = False
                    for u, meta in m.items():
                        if meta.get("status") == "active" and meta.get("side") == "sell":
                            existing_p = float(meta.get("price") or 0)
                            if existing_p > 0 and abs(existing_p - new_price) / existing_p < 0.0005:
                                is_duplicate = True
                                break
                    if not is_duplicate:
                        qty = float(order_usdt) / float(new_price) if new_price > 0 else 0.0
                        if qty > 0:
                            try:
                                resp = self._place_limit_sell_qty(market=market, price=new_price, qty=qty)
                                ou = str(resp.get("uuid") or "")
                                if ou:
                                    self._register_order_uuid(market=market, rid=step.get("rid"), uuid_=ou, side="sell", price=new_price, seq=0, qty=qty)
                            except (KeyError, AttributeError, TypeError) as _e:  # [FIX M1] added logging
                                log.warning(f"[LADDER] sell expand failed: {market} {new_price} {_e}")


        def poll_filled_steps(self, market: str) -> int:
            """Poll exchange for filled ladder orders, update registry, and on fill auto-add 2 opposite-side steps.
            On fill-detection failure: warn log, and after a threshold of failures, fall back to auto-cancel.
            """
            # [FIX L1] use module-level log, removed in-function import logging
            reg = self._read_order_registry()
            m = reg.get(market)
            if not isinstance(m, dict):
                return 0
            uuids = list(m.keys())
            if not uuids:
                return 0
            u = self.get_trade_client()
            updated = 0
            fail_counts = self._poll_fail_counts.setdefault(market, {})  # [FIX C2] persist at instance level -> the 5-failure logic actually works
            for uuid_ in uuids:
                step = m[uuid_]
                if step.get("status") in ("filled", "deleted"):
                    continue
                try:
                    order_info = u.get_order(uuid=uuid_)
                    state = order_info.get("state") if order_info else None
                    if state == "done":
                        m[uuid_]["status"] = "filled"
                        m[uuid_]["filled_ts"] = time.time()
                        try:
                            m[uuid_]["qty"] = float(order_info.get("executed_volume") or 0)
                            m[uuid_]["volume"] = m[uuid_]["qty"]
                            avg_p = float(order_info.get("avg_price") or order_info.get("price") or 0)
                            m[uuid_]["avg_price"] = avg_p
                            m[uuid_]["fee"] = float(order_info.get("paid_fee") or 0)
                            side = m[uuid_].get("side", "")
                            qty = m[uuid_]["qty"]
                            if side == "sell" and avg_p > 0 and qty > 0:
                                buy_orders = [meta for meta in m.values() if meta.get("side") == "buy" and meta.get("status") == "filled" and float(meta.get("qty", 0)) > 0]
                                buy_orders = sorted(buy_orders, key=lambda x: x.get("filled_ts", 0))
                                remain = qty
                                entry_sum = 0.0
                                entry_qty = 0.0
                                for bo in buy_orders:
                                    bqty = float(bo.get("qty", 0))
                                    bused = float(bo.get("used_for_sell", 0))
                                    bfree = max(0.0, bqty - bused)
                                    if bfree <= 0:
                                        continue
                                    take = min(remain, bfree)
                                    entry_sum += take * float(bo.get("avg_price", bo.get("price", 0)))
                                    entry_qty += take
                                    bo["used_for_sell"] = bused + take
                                    remain -= take
                                    if remain <= 1e-8:
                                        break
                                entry_price = entry_sum / entry_qty if entry_qty > 0 else 0.0
                                m[uuid_]["entry_price"] = entry_price
                                m[uuid_]["realized_profit"] = (avg_p - entry_price) * qty
                        except (TypeError, ValueError, ZeroDivisionError) as exc:
                            logger.error("[LADDER] entry_price/realized_profit calc: %s", exc, exc_info=True)
                        updated += 1
                        self.auto_expand_ladder_on_fill(market, uuid_, expand_n=2)
                        # On successful fill detection, clear the failure count
                        fail_counts.pop(uuid_, None)  # [FIX C2] clear so the next cycle starts clean
                    else:
                        # On fill-detection failure, increment count and warn
                        fail_counts[uuid_] = fail_counts.get(uuid_, 0) + 1
                        if fail_counts[uuid_] >= 5:
                            log.warning(f"[LADDER] fill detection failed 5+ times: {market} {uuid_} -> attempting auto-cancel")
                            try:
                                self._cancel_order(uuid_=uuid_)
                                # [FIX] on lookup failure, do not delete; keep as unknown for re-check next loop
                                # m[uuid_]["status"] = "deleted"
                            except (KeyError, AttributeError, TypeError) as e:
                                log.error(f"[LADDER] auto-cancel failed: {market} {uuid_} {e}")
                        else:
                            log.debug(f"[LADDER] awaiting fill: {market} {uuid_[:12]} (count={fail_counts[uuid_]})")
                except (OSError, KeyError, IndexError, AttributeError, TypeError, ValueError, OverflowError) as ex:
                    log.warning(f"[LADDER] fill status lookup exception: {market} {uuid_} {ex}")
                    fail_counts[uuid_] = fail_counts.get(uuid_, 0) + 1
                    if fail_counts[uuid_] >= 5:
                        try:
                            self._cancel_order(uuid_=uuid_)
                            # [FIX] do not delete even on exception
                            # m[uuid_]["status"] = "deleted"
                        except (KeyError, AttributeError, TypeError) as e:
                            log.error(f"[LADDER] auto-cancel failed (exception): {market} {uuid_} {e}")
            reg[market] = m
            self._write_order_registry(reg)
            return updated

            # --------------------------------------------------------
            # Backfill: retroactively record past filled orders
            # --------------------------------------------------------
        def backfill_filled_orders(self, since_ts: float = 0.0) -> Dict[str, Any]:
            """Query Bybit completed-fill history and retroactively record it into the registry.

            Args:
                since_ts: only process fills after this time (epoch). 0 means all.

            Returns:
                {"updated": int, "markets": {market: count}}
            """
            # [FIX L1] use module-level log/datetime, removed in-function imports
            u = self.get_trade_client()
            reg = self._read_order_registry()
            all_registry_uuids: Dict[str, str] = {}
            for mk, steps in reg.items():
                if not isinstance(steps, dict):
                    continue
                for uid in steps:
                    all_registry_uuids[uid] = mk

            markets = sorted(reg.keys())
            total_updated = 0
            per_market: Dict[str, int] = {}

            for market in markets:
                try:
                    done_orders = u.list_done_orders(market=market, per_page=100)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                    logger.warning("[LADDER] backfill: %s list_done_orders failed: %s", market, e, exc_info=True)
                    log.warning("backfill: %s list_done_orders failed: %s", market, e)
                    continue

                count = 0
                for order in done_orders:
                    uid = str(order.get("uuid") or "")
                    if not uid or uid not in all_registry_uuids:
                        continue
                    mk = all_registry_uuids[uid]
                    step = reg.get(mk, {}).get(uid)
                    if not isinstance(step, dict):
                        continue

                    created_at = str(order.get("created_at") or "")
                    if since_ts > 0 and created_at:
                        try:
                            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                            if dt.timestamp() < since_ts:
                                continue
                        except (ValueError, TypeError) as exc:
                            logger.warning("[LADDER] backfill timestamp parse: %s", exc, exc_info=True)

                    if step.get("status") == "filled" and "qty" in step:
                        continue

                    try:
                        qty = float(order.get("executed_volume") or 0)
                        avg_p = float(order.get("avg_price") or order.get("price") or 0)
                        fee = float(order.get("paid_fee") or 0)
                    except (TypeError, ValueError):
                        log.warning("LadderManager.backfill_filled_orders suppressed exception", exc_info=True)
                        continue

                    step["status"] = "filled"
                    step["filled_ts"] = time.time()
                    step["qty"] = qty
                    step["volume"] = qty
                    step["avg_price"] = avg_p
                    step["fee"] = fee

                    side = step.get("side", "")
                    if side == "sell" and avg_p > 0:
                        entry_price = float(step.get("price") or 0)
                        if entry_price > 0 and qty > 0:
                            step["entry_price"] = entry_price
                            step["realized_profit"] = (avg_p - entry_price) * qty

                    reg[mk][uid] = step
                    count += 1
                    total_updated += 1
                    log.info("backfill: %s %s %s qty=%.8f avg=%.2f fee=%.4f",
                             mk, uid[:12], side, qty, avg_p, fee)

                if count > 0:
                    per_market[market] = count

            self._write_order_registry(reg)
            log.info("backfill complete: %d orders updated across %s", total_updated, list(per_market.keys()))
            return {"updated": total_updated, "markets": per_market}

            # --------------------------------------------------------
            # LongHold (LADDER/GAZUA advisory)
            # --------------------------------------------------------

        def _load_longhold_store(self) -> Dict[str, Any]:
            raw = self._read_json(LONGHOLD_PATH)

            store = dict(LONGHOLD_STORE_DEFAULTS)
            if isinstance(raw, dict):
                # shallow merge known keys
                for k in ("version", "defaults", "markets", "history"):
                    if k in raw:
                        store[k] = raw.get(k)

            # normalize
            if not isinstance(store.get("defaults"), dict):
                store["defaults"] = dict(LONGHOLD_STORE_DEFAULTS["defaults"])
            else:
                d = dict(LONGHOLD_STORE_DEFAULTS["defaults"])
                d.update(store["defaults"])
                store["defaults"] = d

            if not isinstance(store.get("markets"), dict):
                store["markets"] = {}
            if not isinstance(store.get("history"), list):
                store["history"] = []

            # cap history to keep runtime file bounded
            try:
                if len(store["history"]) > 400:
                    store["history"] = store["history"][-400:]
            except (TypeError, AttributeError) as exc:
                logger.warning("[LADDER] cap history: %s", exc, exc_info=True)

            return store

        def _save_longhold_store(self, store: Dict[str, Any]) -> None:
            # keep file bounded
            try:
                if isinstance(store.get("history"), list) and len(store["history"]) > 400:
                    store["history"] = store["history"][-400:]
            except (TypeError, AttributeError) as exc:
                logger.warning("[LADDER] keep file bounded: %s", exc, exc_info=True)
            self._write_json(LONGHOLD_PATH, store)

        def _normalize_longhold_cfg(self, market: str, cfg: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
            out = dict(defaults)
            if isinstance(cfg, dict):
                out.update(cfg)
            out["market"] = market

            out["enabled"] = bool(out.get("enabled", True))
            out["strategy"] = str(out.get("strategy") or "LADDER").upper()
            if out["strategy"] not in ("LADDER", "GAZUA"):
                out["strategy"] = "LADDER"

            try:
                out["target_profit_pct"] = float(out.get("target_profit_pct") or defaults.get("target_profit_pct") or 0.0)
            except (TypeError, ValueError):
                log.warning("LadderManager._normalize_longhold_cfg suppressed exception", exc_info=True)
                out["target_profit_pct"] = float(defaults.get("target_profit_pct") or 0.0)
            out["target_profit_pct"] = max(0.0, min(10000.0, float(out["target_profit_pct"])))

            try:
                out["notify_cooldown_sec"] = int(float(out.get("notify_cooldown_sec") or defaults.get("notify_cooldown_sec") or 0))
            except (TypeError, ValueError):
                log.warning("LadderManager._normalize_longhold_cfg suppressed exception", exc_info=True)
                out["notify_cooldown_sec"] = int(float(defaults.get("notify_cooldown_sec") or 0))
            out["notify_cooldown_sec"] = max(0, int(out["notify_cooldown_sec"]))

            try:
                out["min_position_usdt"] = int(float(out.get("min_position_usdt") or defaults.get("min_position_usdt") or 0))
            except (TypeError, ValueError):
                log.warning("LadderManager._normalize_longhold_cfg suppressed exception", exc_info=True)
                out["min_position_usdt"] = int(float(defaults.get("min_position_usdt") or 0))


            try:
                out["budget_usdt"] = int(float(out.get("budget_usdt") or defaults.get("budget_usdt") or 0))
            except (TypeError, ValueError):
                log.warning("LadderManager._normalize_longhold_cfg suppressed exception", exc_info=True)
                out["budget_usdt"] = int(float(defaults.get("budget_usdt") or 0))
            out["budget_usdt"] = max(0, int(out["budget_usdt"]))

            out["min_position_usdt"] = max(0, int(out["min_position_usdt"]))

            out["repeat"] = bool(out.get("repeat", defaults.get("repeat", True)))
            out["auto_sell_on_target"] = bool(out.get("auto_sell_on_target", defaults.get("auto_sell_on_target", False)))
            out["note"] = str(out.get("note") or "")

            # state
            try:
                out["created_ts"] = float(out.get("created_ts") or 0.0)
            except (TypeError, ValueError):
                log.warning("LadderManager._normalize_longhold_cfg suppressed exception", exc_info=True)
                out["created_ts"] = 0.0
            try:
                out["updated_ts"] = float(out.get("updated_ts") or 0.0)
            except (TypeError, ValueError):
                log.warning("LadderManager._normalize_longhold_cfg suppressed exception", exc_info=True)
                out["updated_ts"] = 0.0
            try:
                out["last_notified_ts"] = float(out.get("last_notified_ts") or 0.0)
            except (TypeError, ValueError):
                log.warning("LadderManager._normalize_longhold_cfg suppressed exception", exc_info=True)
                out["last_notified_ts"] = 0.0
            try:
                out["last_notified_profit_pct"] = float(out.get("last_notified_profit_pct") or 0.0)
            except (TypeError, ValueError):
                log.warning("LadderManager._normalize_longhold_cfg suppressed exception", exc_info=True)
                out["last_notified_profit_pct"] = 0.0

            return out

        def get_longhold_config(self, market: str) -> Dict[str, Any]:
            store = self._load_longhold_store()
            defaults = store.get("defaults") if isinstance(store.get("defaults"), dict) else dict(LONGHOLD_STORE_DEFAULTS["defaults"])
            per = store.get("markets", {}).get(market, {}) if isinstance(store.get("markets"), dict) else {}
            return self._normalize_longhold_cfg(market, per if isinstance(per, dict) else {}, defaults)

        def list_longhold_configs(self) -> List[Dict[str, Any]]:
            store = self._load_longhold_store()
            defaults = store.get("defaults") if isinstance(store.get("defaults"), dict) else dict(LONGHOLD_STORE_DEFAULTS["defaults"])
            markets = store.get("markets") if isinstance(store.get("markets"), dict) else {}
            out: List[Dict[str, Any]] = []
            for mkt in sorted(markets.keys()):
                try:
                    out.append(self._normalize_longhold_cfg(mkt, markets.get(mkt, {}), defaults))
                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("[LADDER] list_longhold_configs: %s", exc)
                    continue
            return out

        def save_longhold_config(self, cfg_dict: Dict[str, Any]) -> Dict[str, Any]:
            market = str(cfg_dict.get("market") or "").strip()
            if not market:
                raise ValueError("market is required")

            store = self._load_longhold_store()
            defaults = store.get("defaults") if isinstance(store.get("defaults"), dict) else dict(LONGHOLD_STORE_DEFAULTS["defaults"])
            markets = store.get("markets") if isinstance(store.get("markets"), dict) else {}

            now = time.time()
            cur = markets.get(market, {})
            if not isinstance(cur, dict):
                cur = {}

            # preserve created_ts
            created_ts = cur.get("created_ts")
            if not created_ts:
                created_ts = now

            cur.update(cfg_dict)
            cur["created_ts"] = float(created_ts)
            cur["updated_ts"] = float(now)

            norm = self._normalize_longhold_cfg(market, cur, defaults)

            # store without the derived "market" field
            store["markets"][market] = {k: v for k, v in norm.items() if k != "market"}

            # history (config write)
            try:
                store["history"].append({
                    "ts": now,
                    "event": "LONGHOLD_CONFIG_SET",
                    "market": market,
                    "strategy": norm.get("strategy"),
                    "enabled": bool(norm.get("enabled")),
                    "target_profit_pct": float(norm.get("target_profit_pct") or 0.0),
                })
            except (TypeError, ValueError, AttributeError) as exc:
                logger.warning("[LADDER] history config write: %s", exc)

            self._save_longhold_store(store)
            return norm

            # --------------------------------------------------------
            # LongHold candidate scan (public Bybit data; no keys)
            # --------------------------------------------------------


        def remove_longhold_config(self, market: str) -> Dict[str, Any]:
            market = str(market or "").strip().upper()
            if not market:
                raise ValueError("market is required")

            store = self._load_longhold_store()
            markets = store.get("markets") if isinstance(store.get("markets"), dict) else {}
            existed = market in markets
            removed = markets.pop(market, None)
            store["markets"] = markets

            # history
            hist = store.get("history") if isinstance(store.get("history"), list) else []
            hist.append({
                "ts": time.time(),
                "market": market,
                "event": "LONGHOLD_REMOVE",
                "data": {"removed": bool(existed)},
            })
            # cap history size
            try:
                maxh = int(store.get("history_max") or LONGHOLD_STORE_DEFAULTS.get("history_max") or 200)
                maxh = max(0, min(5000, maxh))
                if maxh and len(hist) > maxh:
                    hist = hist[-maxh:]
            except (TypeError, ValueError, AttributeError) as exc:
                logger.warning("[LADDER] cap history size: %s", exc, exc_info=True)
            store["history"] = hist

            self._save_longhold_store(store)
            return {"ok": True, "market": market, "removed": bool(existed)}

        def _fetch_ticker_bulk(self, markets: List[str]) -> Dict[str, float]:
            """Fetch last price for multiple markets via Bybit API (best-effort)."""
            out: Dict[str, float] = {}
            if not markets:
                return out
            # Bybit ticker supports fetching multiple markets at once
            try:
                normalized_markets = [self._normalize_market(m) for m in markets]
                url = f"{BYBIT_MARKET_TICKERS}?markets={','.join(normalized_markets)}"
                with urllib.request.urlopen(url, timeout=10) as resp:
                    raw = resp.read().decode("utf-8", errors="ignore")
                arr = json.loads(raw) if raw else []
                if isinstance(arr, list):
                    for row in arr:
                        try:
                            mkt = str(row.get("market") or "").upper()
                            px = float(row.get("trade_price") or 0.0)
                            if mkt and px > 0:
                                out[mkt] = px
                        except (TypeError, ValueError) as exc:
                            logger.warning("[LADDER] Bybit ticker parse: %s", exc)
                            continue
            except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[LADDER] Bybit ticker fetch: %s", exc)
            return out

        def longhold_snapshot(self, market: Optional[str] = None, include_disabled: bool = True) -> Dict[str, Any]:
            """Return LongHold configs with computed position/price/PnL snapshot."""
            cfgs = self.list_longhold_configs()
            if market:
                m = str(market).strip().upper()
                cfgs = [c for c in cfgs if str(c.get("market") or "").upper() == m]

            if not include_disabled:
                cfgs = [c for c in cfgs if c.get("enabled", True)]

            # [PATCH] Mutual Exclusion: Exclude markets managed by OMA (Active/Watch/Recovery)
            # LongHold list should only show coins NOT in the main OMA list.
            try:
                oma = self.system.oma
                exclude = set(oma.list_active()) | set(oma.list_watch()) | set(oma.list_recovery())
                cfgs = [c for c in cfgs if str(c.get("market") or "").upper() not in exclude]
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[LADDER] LongHold list OMA exclusion: %s", exc, exc_info=True)

            # gather markets for bulk price
            markets = [str(c.get("market") or "").upper() for c in cfgs if c.get("market")]
            prices = self._fetch_ticker_bulk(markets)

            items: List[Dict[str, Any]] = []
            tot_budget = 0
            tot_pos = 0.0
            tot_pnl = 0.0

            for cfg in cfgs:
                mkt = str(cfg.get("market") or "").upper()
                enabled = bool(cfg.get("enabled", True))
                budget = int(cfg.get("budget_usdt") or 0)
                tot_budget += budget

                ctx = None
                try:
                    ctx = self.system.coordinator.contexts.get(mkt)
                except (KeyError, AttributeError, TypeError):
                    log.warning("LadderManager.longhold_snapshot suppressed exception", exc_info=True)
                    ctx = None

                pos = None
                try:
                    pos = self._extract_position(ctx)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                    log.warning("LadderManager.longhold_snapshot suppressed exception", exc_info=True)
                    pos = None

                qty = float(pos.get("qty") or 0.0) if isinstance(pos, dict) else 0.0
                entry = float(pos.get("entry") or 0.0) if isinstance(pos, dict) else 0.0

                price = float(prices.get(mkt) or 0.0)
                if price <= 0:
                    try:
                        price = float(self.get_current_price(mkt) or 0.0)
                    except (TypeError, ValueError):
                        log.warning("LadderManager.longhold_snapshot suppressed exception", exc_info=True)
                        price = 0.0

                position_usdt = (qty * price) if (qty > 0 and price > 0) else 0.0
                profit_usdt = (qty * (price - entry)) if (qty > 0 and price > 0 and entry > 0) else 0.0
                profit_pct = ((price / entry - 1.0) * 100.0) if (qty > 0 and price > 0 and entry > 0) else None

                status = "OK"
                if not enabled:
                    status = "DISABLED"
                elif qty <= 0 or entry <= 0:
                    status = "NO_POSITION"
                elif price <= 0:
                    status = "NO_PRICE"
                elif position_usdt < float(cfg.get("min_position_usdt") or 0):
                    status = "DUST"

                if position_usdt:
                    tot_pos += position_usdt
                if profit_usdt:
                    tot_pnl += profit_usdt

                items.append({
                    "market": mkt,
                    "enabled": enabled,
                    "strategy": str(cfg.get("strategy") or "").upper(),
                    "target_profit_pct": cfg.get("target_profit_pct"),
                    "notify_cooldown_sec": cfg.get("notify_cooldown_sec"),
                    "min_position_usdt": cfg.get("min_position_usdt"),
                    "repeat": cfg.get("repeat"),
                    "note": cfg.get("note"),
                    "budget_usdt": budget,

                    "qty": qty if qty > 0 else None,
                    "entry": entry if entry > 0 else None,
                    "price": price if price > 0 else None,
                    "position_usdt": position_usdt if position_usdt > 0 else None,
                    "profit_usdt": profit_usdt if (qty > 0 and price > 0 and entry > 0) else None,
                    "profit_pct": profit_pct,
                    "status": status,
                })

            # stable sort: enabled first, then position desc, then market
            items.sort(key=lambda x: (not bool(x.get("enabled", True)), -(x.get("position_usdt") or 0), x.get("market") or ""))

            return {
                "ok": True,
                "ts": time.time(),
                "items": items,
                "totals": {
                    "budget_usdt": tot_budget,
                    "position_usdt": tot_pos,
                    "profit_usdt": tot_pnl,
                },
            }

        def scan_longhold_candidates(
            self,
            *,
            strategy: str,
            n: int = 3,
            method: str = "candles",  # candles | buffer
            candle_unit_minutes: int = 5,
            candle_count: int = 200,
            request_sleep: float = 0.12,
            seconds: int = 180,
            interval_sec: float = 1.0,
            chunk_size: int = 100,
            max_markets: Optional[int] = None,
            force_refresh: bool = False,
            ) -> Dict[str, Any]:
            strat = str(strategy or "").upper()
            profile = "ladder" if strat == "LADDER" else "gazua"
            mth = str(method or "candles").lower()
        
            # [2026-01-30] Check cache (5-minute TTL)
            cache_key = f"{strat}_{profile}_{mth}_{n}"
            now = time.time()
            if not force_refresh and cache_key in self._longhold_candidates_cache:
                cached = self._longhold_candidates_cache[cache_key]
                if now - cached.get("ts", 0) < self._longhold_candidates_cache_ttl:
                    return {**cached, "cached": True}

            try:
                from app.manager import topn_selector
            except (ImportError, AttributeError, TypeError):
                log.warning("LadderManager.scan_longhold_candidates suppressed exception", exc_info=True)
                # fallback for monolithic deployments
                import topn_selector  # type: ignore

            if mth == "buffer":
                ranked = topn_selector.rank_topn_by_live_buffer(
                    n=int(n),
                    profile=profile,
                    seconds=int(seconds),
                    interval_sec=float(interval_sec),
                    chunk_size=int(chunk_size),
                    max_markets=max_markets,
                )
            else:
                # [2026-01-30] Limit markets for speed (top 100 only)
                effective_max = max_markets if max_markets else 100
                ranked = topn_selector.rank_topn_by_public_candles(
                    n=int(n),
                    profile=profile,
                    candle_unit_minutes=int(candle_unit_minutes),
                    candle_count=int(candle_count),
                    max_markets=effective_max,
                    request_sleep=float(request_sleep),
                )

            items: List[Dict[str, Any]] = []
            for score, f in ranked:
                try:
                    items.append({
                        "market": f.market,
                        "score": float(score),
                        "last_price": float(f.last_price),
                        "momentum": float(f.momentum),
                        "volatility": float(f.volatility),
                        "trend_slope": float(f.trend_slope),
                        "range_ratio": float(f.range_ratio),
                        "liquidity": float(f.liquidity),
                        "samples": int(f.samples),
                    })
                except (TypeError, ValueError) as exc:
                    logger.warning("[LADDER] market scan metadata: %s", exc)
                    continue

            result = {
                "ok": True,
                "strategy": strat,
                "profile": profile,
                "method": mth,
                "n": int(n),
                "items": items,
                "ts": time.time(),
                "cached": False,
            }
        
            # [2026-01-30] Store result in cache
            self._longhold_candidates_cache[cache_key] = result
        
            return result

            # --------------------------------------------------------
            # LongHold profit target alerts (Telegram)
            # --------------------------------------------------------
        def _send_telegram(self, msg: str) -> None:
            try:
                from app.notify.telegram import send_telegram
                send_telegram(str(msg))
                return
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[LADDER] _send_telegram: %s", exc)

            # fallback (when module is placed at root)
            try:
                from telegram import send_telegram  # type: ignore
                send_telegram(str(msg))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[LADDER] _send_telegram root fallback: %s", exc)
                return

        def _extract_position(self, ctx: Any) -> Optional[Dict[str, float]]:
            if ctx is None:
                return None

            pos = None
            try:
                if isinstance(ctx, dict):
                    pos = ctx.get("position")
                else:
                    pos = getattr(ctx, "position", None)
            except (KeyError, AttributeError, TypeError):
                log.warning("LadderManager._extract_position suppressed exception", exc_info=True)
                pos = None

            if pos is None:
                return None

            def _get(p: Any, *keys: str) -> Optional[float]:
                for k in keys:
                    try:
                        if isinstance(p, dict):
                            v = p.get(k)
                        else:
                            v = getattr(p, k, None)
                        if v is None:
                            continue
                        fv = float(v)
                        if fv == fv:
                            return fv
                    except (TypeError, ValueError, AttributeError) as exc:
                        logger.warning("[LADDER] _get helper: %s", exc)
                        continue
                return None

            qty = _get(pos, "qty", "volume", "amount", "balance") or 0.0
            entry = _get(pos, "entry", "avg_price", "avg", "price") or 0.0

            if qty <= 0 or entry <= 0:
                return None
            return {"qty": float(qty), "entry": float(entry)}

        def poll_longhold_alerts(self, market: Optional[str] = None) -> Dict[str, Any]:
            store = self._load_longhold_store()
            defaults = store.get("defaults") if isinstance(store.get("defaults"), dict) else dict(LONGHOLD_STORE_DEFAULTS["defaults"])
            markets = store.get("markets") if isinstance(store.get("markets"), dict) else {}

            # [2026-02-04] Check Auto Sell interval
            check_interval_min = float(defaults.get("auto_sell_check_interval_min", 10))
            last_check_ts = float(defaults.get("last_auto_sell_check_ts", 0.0))
        
            now = time.time()
        
            # Interval check - whether N minutes have passed since the last check
            if (now - last_check_ts) < (check_interval_min * 60):
                return {
                    "ok": True,
                    "checked": 0,
                    "triggered": [],
                    "ts": now,
                    "next_check_sec": int((check_interval_min * 60) - (now - last_check_ts)),
                }
        
            # Update check time
            defaults["last_auto_sell_check_ts"] = now
            store["defaults"] = defaults
            self._save_longhold_store(store)
        
            target_markets: List[str] = []
            if market:
                target_markets = [str(market)]
            else:
                target_markets = list(markets.keys())

            triggered: List[Dict[str, Any]] = []
            checked = 0
            updated = False

            for mkt in target_markets:
                if not mkt:
                    continue
                per = markets.get(mkt, {})
                if not isinstance(per, dict):
                    per = {}

                cfg = self._normalize_longhold_cfg(mkt, per, defaults)
                if not bool(cfg.get("enabled")):
                    continue

                checked += 1

                # position info (must exist; operator manual buy)
                ctx = None
                try:
                    ctx = self.system.coordinator.contexts.get(mkt)
                except (KeyError, AttributeError, TypeError):
                    log.warning("LadderManager.poll_longhold_alerts suppressed exception", exc_info=True)
                    ctx = None

                pos = self._extract_position(ctx)
                if not pos:
                    continue

                # current price
                price = self.get_current_price(mkt)
                if not price or float(price) <= 0:
                    continue

                qty = float(pos["qty"])
                entry = float(pos["entry"])
                pos_usdt = qty * float(price)

                if pos_usdt < float(cfg.get("min_position_usdt") or 0):
                    # ignore dust by default
                    continue

                profit_pct = (float(price) / entry - 1.0) * 100.0

                # [2026-03-19] SL check: absolute stop-loss vs entry price (0 = disabled)
                sl_pct = float(cfg.get("stop_loss_pct", defaults.get("stop_loss_pct", -30.0)) or 0.0)
                if sl_pct < 0 and profit_pct <= sl_pct:
                    logger.warning("[LongHold] SL reached %s: profit=%.1f%% <= sl=%.1f%%", mkt, profit_pct, sl_pct)
                    sl_msg = (
                        f"🔴 [LongHold SL] Stop-loss reached\n"
                        f"- Market: {mkt}\n"
                        f"- PnL: {profit_pct:.2f}% (SL {sl_pct:.1f}%)\n"
                        f"- Entry: {entry:,.0f}  Now: {float(price):,.0f}\n"
                        f"- Position: {pos_usdt:,.0f} USDT"
                    )
                    self._send_telegram(sl_msg)
                    try:
                        trade_client = getattr(self.system, "trade_client", None)
                        if trade_client and qty > 0:
                            sl_sell = trade_client.market_sell(mkt, qty)
                            if sl_sell and sl_sell.get("uuid"):
                                self._send_telegram(f"✅ [LongHold SL] {mkt} market sell completed ({sl_sell.get('uuid', '')})")
                                triggered.append({"market": mkt, "reason": "stop_loss", "profit_pct": profit_pct, "sl_pct": sl_pct})
                                updated = True
                    except (KeyError, AttributeError, TypeError, ValueError) as _e:
                        log.warning("LadderManager.poll_longhold_alerts except: %s", _e, exc_info=True)
                        logger.error("[LongHold] SL sell failed %s: %s", mkt, _e)
                    continue

                target_pct = float(cfg.get("target_profit_pct") or 0.0)

                if profit_pct < target_pct:
                    continue

                cooldown = int(cfg.get("notify_cooldown_sec") or 0)
                last_ts = float(cfg.get("last_notified_ts") or 0.0)
                allow_repeat = bool(cfg.get("repeat", True))

                if (now - last_ts) < max(0, cooldown):
                    continue

                # If repeat=False: only notify once per market until config changes
                if not allow_repeat and last_ts > 0:
                    continue

                msg = (
                    f"[{cfg.get('strategy')}] PROFIT TARGET HIT\n"
                    f"- Market: {mkt}\n"
                    f"- PnL: +{profit_pct:.2f}% (target {target_pct:.1f}%)\n"
                    f"- Entry: {entry:,.0f}  Now: {float(price):,.0f}\n"
                    f"- Position: {pos_usdt:,.0f} USDT"
                )
                self._send_telegram(msg)

                # ============================================================
                # HYBRID AUTO SELL (2026-02-04)
                # - Trailing Stop: track the peak after target is reached
                # - Limit -> Market: switch to market order if limit stays unfilled
                # ============================================================
                auto_sell = bool(cfg.get("auto_sell_on_target", False))
                sold_ok = False
                sell_result = None
                sell_method = "none"
            
                if auto_sell and qty > 0:
                    base_trailing_pct = float(cfg.get("trailing_stop_pct", 2.0))
                    # [2026-02-09] Reflect ATR-based volatility + time-of-day adjustment
                    trailing_pct = base_trailing_pct
                    try:
                        # Derive trailing range from ATR(14) (2~5% range)
                        from app.strategy import indicators
                        from app.core.hyper_price_store import price_store
                        candles = price_store.get_candles(mkt, count=20)
                        closes = [float(c["trade_price"]) for c in candles if c.get("trade_price")] if candles else []
                        if closes and len(closes) >= 15:
                            atr = indicators.atr(closes, 14)
                            if closes[-1] > 0:
                                atr_pct = (atr / closes[-1]) * 100.0
                                # Clip to 2~5% range
                                trailing_pct = min(5.0, max(2.0, atr_pct * 2.0))
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[LADDER] ATR trailing clip: %s", exc, exc_info=True)
                    # Additional time-of-day volatility adjustment
                    try:
                        from app.monitor.time_volatility_adjuster import get_time_volatility_adjuster
                        time_adjuster = get_time_volatility_adjuster()
                        if time_adjuster:
                            trailing_pct = time_adjuster.adjust_trailing_stop(trailing_pct)
                    except (ImportError, AttributeError, TypeError) as exc:
                        logger.warning("[LADDER] time adjuster: %s", exc, exc_info=True)
                
                    limit_timeout = int(cfg.get("limit_order_timeout_sec", 30))
                    market_fallback = bool(cfg.get("enable_market_fallback", True))
                
                    # Get tracking state
                    tracking = store.get("tracking", {})
                    if not isinstance(tracking, dict):
                        tracking = {}
                    track = tracking.get(mkt, {})
                    if not isinstance(track, dict):
                        track = {}
                
                    peak_price = float(track.get("peak_price", 0))
                    trailing_active = bool(track.get("trailing_active", False))
                    limit_order_uuid = track.get("limit_order_uuid")
                    limit_order_ts = float(track.get("limit_order_ts", 0))
                
                    # Step 1: Activate Trailing Stop (when target is reached)
                    if not trailing_active:
                        # Target achieved -> start Trailing
                        peak_price = float(price)
                        trailing_active = True
                        track["peak_price"] = peak_price
                        track["trailing_active"] = True
                        tracking[mkt] = track
                        store["tracking"] = tracking
                        updated = True
                    
                        trail_msg = (
                            f"[TRAILING START] {mkt}\n"
                            f"- Target: {target_pct:.1f}% (HIT)\n"
                            f"- Current: +{profit_pct:.2f}%\n"
                            f"- Peak: {peak_price:,.0f}\n"
                            f"- Trail: {trailing_pct}%"
                        )
                        self._send_telegram(trail_msg)
                
                    # Step 2: Track Trailing Stop
                    if trailing_active:
                        # Update peak
                        if float(price) > peak_price:
                            peak_price = float(price)
                            track["peak_price"] = peak_price
                            tracking[mkt] = track
                            store["tracking"] = tracking
                            updated = True
                    
                        # Check Trailing Stop trigger
                        trailing_trigger_price = peak_price * (1.0 - trailing_pct / 100.0)

                        if float(price) <= trailing_trigger_price and not limit_order_uuid:
                            # Step 3: Place limit order
                            try:
                                trade_client = getattr(self.system, "trade_client", None)
                                if trade_client:
                                    # Limit order at the current price
                                    sell_result = trade_client.limit_sell(mkt, qty, float(price))
                                    if sell_result and sell_result.get("uuid"):
                                        limit_order_uuid = sell_result.get("uuid")
                                        limit_order_ts = now
                                        track["limit_order_uuid"] = limit_order_uuid
                                        track["limit_order_ts"] = limit_order_ts
                                        tracking[mkt] = track
                                        store["tracking"] = tracking
                                        updated = True
                                        sell_method = "limit"
                                    
                                        limit_msg = (
                                            f"[LIMIT ORDER] {mkt}\n"
                                            f"- Peak: {peak_price:,.0f}\n"
                                            f"- Trail Trigger: {trailing_trigger_price:,.0f}\n"
                                            f"- Limit Price: {float(price):,.0f}\n"
                                            f"- Qty: {qty:.8f}\n"
                                            f"- Order: {limit_order_uuid}"
                                        )
                                        self._send_telegram(limit_msg)
                            except (KeyError, AttributeError, TypeError, ValueError) as e:
                                log.warning("[LIMIT ORDER FAILED] %s", mkt, exc_info=True)
                                err_msg = f"[LIMIT ORDER FAILED] {mkt}\n- Error: {str(e)}"
                                self._send_telegram(err_msg)
                    
                        # Step 4: Limit timeout -> market fallback
                        if limit_order_uuid and market_fallback:
                            elapsed = now - limit_order_ts
                            if elapsed >= limit_timeout:
                                # Check order status
                                try:
                                    trade_client = getattr(self.system, "trade_client", None)
                                    if trade_client:
                                        order_info = trade_client.get_order(uuid=limit_order_uuid)
                                        order_state = order_info.get("state") if order_info else None
                                    
                                        # If unfilled, cancel then market sell (accounting for partial fills)
                                        if order_state in ["wait", "watch"]:
                                            # Cancel order
                                            cancel_result = trade_client.cancel_order(limit_order_uuid)
                                            # On partial fill, market-sell only the remaining quantity
                                            remaining_qty = qty
                                            try:
                                                if order_info and "remaining_volume" in order_info:
                                                    remaining_qty = float(order_info["remaining_volume"])
                                                elif order_info and "remaining_qty" in order_info:
                                                    remaining_qty = float(order_info["remaining_qty"])
                                            except (TypeError, ValueError, KeyError) as exc:
                                                logger.error("[LADDER] partial fill remaining qty: %s", exc, exc_info=True)
                                            if remaining_qty > 0:
                                                sell_result = trade_client.market_sell(mkt, remaining_qty)
                                                sold_ok = bool(sell_result and sell_result.get("uuid"))
                                                sell_method = "market_fallback"
                                                if sold_ok:
                                                    market_msg = (
                                                        f"[MARKET FALLBACK] {mkt}\n"
                                                        f"- Reason: Limit timeout ({limit_timeout}s)\n"
                                                        f"- PnL: +{profit_pct:.2f}%\n"
                                                        f"- Qty: {remaining_qty:.8f}\n"
                                                        f"- Order: {sell_result.get('uuid', 'N/A')}"
                                                    )
                                                    self._send_telegram(market_msg)
                                                    # Reset tracking
                                                    tracking.pop(mkt, None)
                                                    store["tracking"] = tracking
                                                    updated = True
                                                    # Record to ledger
                                                    try:
                                                        ledger = getattr(self.system, "ledger", None)
                                                        if ledger:
                                                            ledger.append(
                                                                "LONGHOLD_AUTO_SELL",
                                                                market=mkt,
                                                                strategy=cfg.get("strategy"),
                                                                profit_pct=float(profit_pct),
                                                                qty=float(remaining_qty),
                                                                order_uuid=sell_result.get("uuid"),
                                                                sell_method=sell_method,
                                                            )
                                                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                                                        logger.error("[LADDER] ledger record (limit sell): %s", exc, exc_info=True)
                                        elif order_state == "done":
                                            # Fill completed
                                            sold_ok = True
                                            sell_method = "limit"
                                            done_msg = f"[LIMIT FILLED] {mkt}\n- Order: {limit_order_uuid}\n- PnL: +{profit_pct:.2f}%"
                                            self._send_telegram(done_msg)

                                            # Reset tracking
                                            tracking.pop(mkt, None)
                                            store["tracking"] = tracking
                                            updated = True

                                            # Record to ledger
                                            try:
                                                ledger = getattr(self.system, "ledger", None)
                                                if ledger:
                                                    ledger.append(
                                                        "LONGHOLD_AUTO_SELL",
                                                        market=mkt,
                                                        strategy=cfg.get("strategy"),
                                                        profit_pct=float(profit_pct),
                                                        qty=float(qty),
                                                        order_uuid=limit_order_uuid,
                                                        sell_method=sell_method,
                                                    )
                                            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                                                log.warning("[LADDER] ledger write: %s", exc, exc_info=True)
                                except Exception as e:
                                    log.warning("[FALLBACK FAILED] %s", mkt, exc_info=True)
                                    err_msg = f"[FALLBACK FAILED] {mkt}\n- Error: {str(e)}"
                                    self._send_telegram(err_msg)

                # persist state
                cfg["last_notified_ts"] = float(now)
                cfg["last_notified_profit_pct"] = float(profit_pct)
                cfg["updated_ts"] = float(now)

                store["markets"][mkt] = {k: v for k, v in cfg.items() if k != "market"}
                updated = True

                try:
                    store["history"].append({
                        "ts": now,
                        "event": "LONGHOLD_TARGET_HIT",
                        "market": mkt,
                        "strategy": cfg.get("strategy"),
                        "profit_pct": float(profit_pct),
                        "target_profit_pct": float(target_pct),
                        "entry": float(entry),
                        "price": float(price),
                        "position_usdt": float(pos_usdt),
                        "auto_sell": auto_sell,
                        "sold_ok": sold_ok,
                        "sell_method": sell_method,  # "none", "limit", "market_fallback"
                    })
                except (TypeError, AttributeError, KeyError) as exc:
                    log.warning("[LADDER] persist state: %s", exc, exc_info=True)

                triggered.append({
                    "market": mkt,
                    "strategy": cfg.get("strategy"),
                    "profit_pct": float(profit_pct),
                    "target_profit_pct": float(target_pct),
                    "entry": float(entry),
                    "price": float(price),
                    "position_usdt": float(pos_usdt),
                    "auto_sell": auto_sell,
                    "sold_ok": sold_ok,
                    "sell_method": sell_method,
                })

            if updated:
                self._save_longhold_store(store)

            return {
                "ok": True,
                "checked": checked,
                "triggered": triggered,
                "ts": now,
            }

            # ============================================================
            # [2026-02-04] Global Profit Take: force-sell all ACTIVE coins
            # - Ignore strategy TP; market-sell immediately at N% profit
            # ============================================================
        def poll_global_profit_take(self) -> Dict[str, Any]:
            """Poll all ACTIVE markets and force sell if profit >= global_profit_pct."""
            system = self.system
        
            # Check settings
            enabled = bool(getattr(system, "global_profit_take", False))
            if not enabled:
                return {"ok": True, "enabled": False, "checked": 0, "triggered": []}
        
            base_target_pct = float(getattr(system, "global_profit_pct", 5.0) or 5.0)
            interval_min = float(getattr(system, "global_profit_interval_min", 10.0) or 10.0)
            target_regime = "STATIC"
            target_pct = base_target_pct
            btc_guard_regime = "STATIC"
            btc_guard_adj_pct = 0.0
            detector = None
            MarketRegime = None

            # [2026-02-12] Regime-based dynamic profit target
            # - Apply a per-regime multiplier on top of base_target_pct.
            # - Defaults: BEAR(1.0x), SIDEWAYS(1.8x), BULL(4.0x), VOLATILE(2.6x)
            # - Clamp the result to the min/max range.
            def _fenv(name: str, default: float) -> float:
                try:
                    return float(os.getenv(name, str(default)) or default)
                except (TypeError, ValueError):
                    log.warning("LadderManager._fenv suppressed exception", exc_info=True)
                    return float(default)

            def _clamp(x: float, lo: float, hi: float) -> float:
                try:
                    return max(float(lo), min(float(hi), float(x)))
                except (TypeError, ValueError):
                    log.warning("LadderManager._clamp suppressed exception", exc_info=True)
                    return float(x)

            dynamic_enabled = str(os.getenv("OMA_GPT_DYNAMIC_ENABLED", "true")).strip().lower() in ("1", "true", "yes", "on")
            gpt_min = _fenv("OMA_GPT_DYNAMIC_MIN_PCT", 1.2)
            gpt_max = _fenv("OMA_GPT_DYNAMIC_MAX_PCT", 5.0)
            bear_x = _fenv("OMA_GPT_DYNAMIC_BEAR_X", 1.0)
            sideways_x = _fenv("OMA_GPT_DYNAMIC_SIDEWAYS_X", 1.8)
            bull_x = _fenv("OMA_GPT_DYNAMIC_BULL_X", 4.0)
            volatile_x = _fenv("OMA_GPT_DYNAMIC_VOLATILE_X", 2.6)
            btc_guard_enabled = str(os.getenv("OMA_GPT_DYNAMIC_BTC_GUARD_ENABLED", "true")).strip().lower() in ("1", "true", "yes", "on")
            btc_bear_adj = _fenv("OMA_GPT_DYNAMIC_BTC_BEAR_ADJ_PCT", -0.2)
            btc_sideways_adj = _fenv("OMA_GPT_DYNAMIC_BTC_SIDEWAYS_ADJ_PCT", 0.0)
            btc_bull_adj = _fenv("OMA_GPT_DYNAMIC_BTC_BULL_ADJ_PCT", 0.0)
            btc_volatile_adj = _fenv("OMA_GPT_DYNAMIC_BTC_VOLATILE_ADJ_PCT", -0.4)

            def _regime_mult(regime_val: Any) -> float:
                if MarketRegime is not None:
                    try:
                        if regime_val == MarketRegime.BULL:
                            return bull_x
                        if regime_val == MarketRegime.BEAR:
                            return bear_x
                        if regime_val == MarketRegime.VOLATILE:
                            return volatile_x
                        return sideways_x
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        log.warning("[LADDER] _regime_mult fallback: %s", exc, exc_info=True)
                regime_name = str(getattr(regime_val, "value", regime_val)).upper()
                if "BULL" in regime_name:
                    return bull_x
                if "BEAR" in regime_name:
                    return bear_x
                if "VOLATILE" in regime_name:
                    return volatile_x
                return sideways_x

            def _btc_guard_adj(regime_val: Any) -> float:
                if not btc_guard_enabled:
                    return 0.0
                if MarketRegime is not None:
                    try:
                        if regime_val == MarketRegime.BULL:
                            return btc_bull_adj
                        if regime_val == MarketRegime.BEAR:
                            return btc_bear_adj
                        if regime_val == MarketRegime.VOLATILE:
                            return btc_volatile_adj
                        return btc_sideways_adj
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        log.warning("[LADDER] _btc_guard_adj fallback: %s", exc, exc_info=True)
                regime_name = str(getattr(regime_val, "value", regime_val)).upper()
                if "BULL" in regime_name:
                    return btc_bull_adj
                if "BEAR" in regime_name:
                    return btc_bear_adj
                if "VOLATILE" in regime_name:
                    return btc_volatile_adj
                return btc_sideways_adj

            if dynamic_enabled:
                try:
                    from app.core.market_regime import get_regime_detector, MarketRegime as _MarketRegime
                    MarketRegime = _MarketRegime
                    detector = get_regime_detector()
                    btc_regime_result = detector.detect("BTCUSDT")
                    btc_regime = btc_regime_result.regime
                    btc_guard_regime = str(getattr(btc_regime, "value", btc_regime))
                    target_regime = f"BTC:{btc_guard_regime}"
                    target_pct = _clamp(base_target_pct * float(_regime_mult(btc_regime)), gpt_min, gpt_max)
                    btc_guard_adj_pct = float(_btc_guard_adj(btc_regime))
                except (KeyError, AttributeError, TypeError, ValueError):
                    log.warning("LadderManager._btc_guard_adj suppressed exception", exc_info=True)
                    target_regime = "STATIC_FALLBACK"
                    btc_guard_regime = "STATIC_FALLBACK"
                    target_pct = _clamp(base_target_pct, gpt_min, gpt_max)
                    btc_guard_adj_pct = 0.0
            else:
                target_pct = _clamp(base_target_pct, gpt_min, gpt_max)
        
            # Interval check
            now = time.time()
            last_check_ts = float(getattr(system, "_global_profit_last_check_ts", 0.0))
        
            if (now - last_check_ts) < (interval_min * 60):
                return {
                    "ok": True,
                    "enabled": True,
                    "checked": 0,
                    "triggered": [],
                    "next_check_sec": int((interval_min * 60) - (now - last_check_ts)),
                }
        
            setattr(system, "_global_profit_last_check_ts", now)
        
            triggered: List[Dict[str, Any]] = []
            checked = 0
        
            # Query all ACTIVE markets
            try:
                coordinator = getattr(system, "coordinator", None)
                if not coordinator:
                    return {"ok": False, "error": "no coordinator", "checked": 0, "triggered": []}
            
                contexts = getattr(coordinator, "contexts", {}) or {}
                lh_store = self._load_longhold_store()
                lh_markets = lh_store.get("markets") if isinstance(lh_store, dict) else {}
                if not isinstance(lh_markets, dict):
                    lh_markets = {}
            
                for market, ctx in contexts.items():
                    if not market or not ctx:
                        continue
                
                    # Target ACTIVE or RECOVERY state
                    state = str(getattr(ctx, "state", "") or getattr(ctx, "market_state", "") or "").upper()
                    if state not in ("ACTIVE", "RECOVERY"):
                        continue
                
                    # Exclude LongHold markets (handled by separate logic)
                    # NOTE:
                    # get_longhold_config() merges defaults.enabled, so when the
                    # default is True every market could be treated as LongHold.
                    # In Global Profit Take, exclude only "markets explicitly registered in LongHold".
                    try:
                        if market in lh_markets:
                            lh_cfg = self.get_longhold_config(market)
                            if lh_cfg and lh_cfg.get("enabled"):
                                continue
                    except (KeyError, AttributeError, TypeError) as exc:
                        log.warning("[LADDER] global profit take LH check: %s", exc, exc_info=True)
                
                    checked += 1
                
                    # Check position
                    pos = self._extract_position(ctx)
                    if not pos:
                        continue
                
                    qty = float(pos.get("qty") or 0)
                    entry = float(pos.get("entry") or 0)
                    if qty <= 0 or entry <= 0:
                        continue
                
                    # Get current price
                    price = self.get_current_price(market)
                    if not price or float(price) <= 0:
                        continue

                    # Compute profit rate
                    profit_pct = (float(price) / entry - 1.0) * 100.0

                    # Exclude losses (negative)
                    if profit_pct < 0:
                        continue

                    market_target_pct = target_pct
                    market_target_regime = target_regime
                    if dynamic_enabled and detector is not None:
                        try:
                            regime_result = detector.detect(market)
                            regime = regime_result.regime
                            market_regime = str(getattr(regime, "value", regime))
                            market_target_regime = f"COIN:{market_regime}|BTC:{btc_guard_regime}"
                            market_target_pct = _clamp(base_target_pct * float(_regime_mult(regime)), gpt_min, gpt_max)
                        except (KeyError, AttributeError, TypeError, ValueError):
                            log.warning("LadderManager._btc_guard_adj suppressed exception", exc_info=True)
                            market_target_regime = f"BTC_FALLBACK:{btc_guard_regime}"
                            market_target_pct = target_pct

                    if dynamic_enabled and btc_guard_enabled:
                        market_target_pct = _clamp(float(market_target_pct) + float(btc_guard_adj_pct), gpt_min, gpt_max)
                        if abs(float(btc_guard_adj_pct)) > 1e-9:
                            market_target_regime = f"{market_target_regime}|BTC_GUARD:{btc_guard_adj_pct:+.2f}"
                
                    # Force-sell if at or above target profit rate
                    if profit_pct >= market_target_pct:
                        sell_result = None
                        sell_submitted = False
                        sell_reason = ""
                        try:
                            order_fsm = getattr(system, "order_fsm", None)
                            if order_fsm is not None:
                                ok, msg = order_fsm.submit_market_sell(
                                    ctx=ctx,
                                    market=market,
                                    qty=qty,
                                    expected_price=float(price),
                                    reason="global_profit_take",
                                )
                                sell_result = msg
                                if ok and not str(msg).startswith("cleared("):
                                    sell_submitted = True
                                else:
                                    sell_reason = str(msg or "")
                            else:
                                trade_client = getattr(system, "trade_client", None)
                                if trade_client:
                                    sell_result = trade_client.market_sell(market, qty)
                                    sell_submitted = bool(sell_result)
                                    if not sell_submitted:
                                        sell_reason = "empty_sell_result"
                                else:
                                    sell_reason = "no_trade_client"
                        except (KeyError, AttributeError, TypeError, ValueError) as e:
                            log.warning("[LADDER] global profit take failed %s: %s", market, e, exc_info=True)
                            self._send_telegram(f"[GLOBAL PROFIT TAKE FAILED] {market}\n- Error: {str(e)}")
                            continue

                        if (not sell_submitted) and sell_reason:
                            lower_reason = sell_reason.lower()
                            soft_skip = (
                                lower_reason == "order_pending"
                                or lower_reason == "qty<=0"
                                or lower_reason == "no_position"
                                or lower_reason.startswith("min_value_blocked")
                                or lower_reason.startswith("soft:insufficient_qty")
                                or lower_reason.startswith("cleared(")
                            )
                            if soft_skip:
                                continue
                            self._send_telegram(
                                f"[GLOBAL PROFIT TAKE SKIP] {market}\n- Reason: {sell_reason}"
                            )
                            continue

                        if sell_submitted:
                            pos_usdt = qty * float(price)
                            msg = (
                                f"[GLOBAL PROFIT TAKE] {market}\n"
                                f"- PnL: +{profit_pct:.2f}% (target {market_target_pct:.2f}%)\n"
                                f"- Regime: {market_target_regime}\n"
                                f"- Entry: {entry:,.0f}  Now: {float(price):,.0f}\n"
                                f"- Position: {pos_usdt:,.0f} USDT\n"
                                f"- Qty: {qty:.8f}"
                            )
                            self._send_telegram(msg)
                        
                            # Record to ledger
                            try:
                                system.ledger.append(
                                    "GLOBAL_PROFIT_TAKE_SELL",
                                    market=market,
                                    qty=qty,
                                    price=float(price),
                                    entry=entry,
                                    profit_pct=profit_pct,
                                    target_pct=market_target_pct,
                                    target_regime=market_target_regime,
                                )
                            except (TypeError, ValueError) as exc:
                                log.warning("[LADDER] ledger write: %s", exc, exc_info=True)

                            triggered.append({
                                "market": market,
                                "profit_pct": profit_pct,
                                "target_pct": market_target_pct,
                                "target_regime": market_target_regime,
                                "qty": qty,
                                "price": float(price),
                            })
            except Exception as e:
                log.warning("LadderManager._btc_guard_adj except: %s", e, exc_info=True)
                return {"ok": False, "error": str(e), "checked": checked, "triggered": triggered}
        
            return {
                "ok": True,
                "enabled": True,
                "checked": checked,
                "triggered": triggered,
                "target_pct": target_pct,
                "target_regime": target_regime,
                "btc_guard_regime": btc_guard_regime,
                "btc_guard_adj_pct": btc_guard_adj_pct,
                "ts": now,
            }

            # ---- Bind LongHold functions as LadderManager methods (compat) ----
            # NOTE: The LongHold helpers below were appended at module scope for patchability.
