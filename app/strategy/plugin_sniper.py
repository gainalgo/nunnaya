# Extracted from strategy_plugins.py — Phase 2 (file diet)
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from app.strategy import indicators
from app.core.currency import Q
from app.strategy.strategy_base import Decision, StrategyPlugin
from app.strategy.strategy_helpers import (
    adjust_order_amount_and_price,
    _evaluate_reversal_buy_guard,
    reserved_queue,
    send_signal_telegram,
)

logger = logging.getLogger(__name__)


class SniperPlugin(StrategyPlugin):
    """SNIPER v2 — state-based precision sniping system.

    6-State Machine: IDLE → WATCH → PROBE → ACTIVE → ARM_TRAIL → EXIT
    - Phase 0 (WATCH): observe — enter after confirming the condition holds
    - Phase 1 (PROBE): scout entry — small 30% buy
    - Phase 2 (ACTIVE): confirm entry — remaining 70% once the bounce is confirmed
    - ARM_TRAIL: TP reached → start trailing
    - TIMEOUT/ABORT: exit on timeout or failure

    [2026-02-23] ATR/BB-based precision params, respects the reserved_selector contract.
    """

    name: str = "sniper"

    # ── state constants ──
    _ST_IDLE = "IDLE"
    _ST_WATCH = "WATCH"
    _ST_PROBE = "PROBE"
    _ST_ACTIVE = "ACTIVE"
    _ST_ARM_TRAIL = "ARM_TRAIL"

    # ── daily shot limit ──
    _MAX_DAILY_SHOTS = 7
    # Operational floor: only the minimum is set here; actual TP/SL is tuned in UI Guards
    # [2026-03-18] 0.8/2.5 — keep defaults low, run flexibly via Strategy TP/SL Guards + Global Profit Take
    _MIN_TP_PCT = 0.8
    _MIN_SL_PCT = 2.5

    # [FIX N2+N8] performance stats: instance-level isolation + thread lock
    # SNIPER / SNIPER(S) are two singletons of the same class, so class vars would be shared → isolate in __init__
    def __init__(self) -> None:
        self._stats: Dict[str, int] = {"probe": 0, "confirm": 0, "win": 0, "loss": 0, "abort": 0}
        self._stats_reset_day: str = ""
        self._stats_lock: threading.RLock = threading.RLock()

    def _ensure_daily_stats(self) -> None:
        today = time.strftime("%Y%m%d")
        if self._stats_reset_day != today:
            self._stats = {"probe": 0, "confirm": 0, "win": 0, "loss": 0, "abort": 0}
            self._stats_reset_day = today

    def _record_stat(self, event: str) -> None:
        with self._stats_lock:  # [FIX N2] thread-safe stat access
            self._ensure_daily_stats()
            self._stats[event] = self._stats.get(event, 0) + 1

    def get_stats(self) -> Dict[str, Any]:
        """Query stats from outside (API/Telegram, etc.)."""
        with self._stats_lock:
            self._ensure_daily_stats()
            s = dict(self._stats)
        probe_rate = s["confirm"] / s["probe"] * 100 if s["probe"] > 0 else 0.0
        total_exits = s["win"] + s["loss"]
        win_rate = s["win"] / total_exits * 100 if total_exits > 0 else 0.0
        return {
            **s,
            "probe_success_rate": round(probe_rate, 1),
            "win_rate": round(win_rate, 1),
            "dynamic_probe_ratio": round(self._calc_dynamic_probe_ratio() or 0.0, 2),
        }

    def _calc_dynamic_probe_ratio(self) -> Optional[float]:
        """Win-rate-based dynamic probe ratio (0.15~0.45). Returns None when data is insufficient."""
        self._ensure_daily_stats()
        s = self._stats
        total_exits = s["win"] + s["loss"]
        if total_exits < 3:
            return None  # insufficient data → None (use params fallback)

        win_rate = s["win"] / total_exits
        # Tied to win rate: conservative when low, aggressive when high
        # below 45% → 0.20 (conservative), 60%+ → 0.40 (aggressive)
        if win_rate < 0.45:
            return 0.20
        elif win_rate < 0.55:
            return 0.30
        elif win_rate < 0.65:
            return 0.35
        else:
            return 0.40

    def _get_state(self, ctx: Any) -> str:
        return str(ctx.get_var("sniper_state", self._ST_IDLE))

    def _set_state(self, ctx: Any, state: str) -> None:
        ctx.set_var("sniper_state", state)

    def _reset_state(self, ctx: Any) -> None:
        # [2026-03-07] Carry over SNIPER(S) scope_start_ts elapsed time
        # Preserve the prior relaxation-timer elapsed even if the coin is swapped out
        _scope_ts = float(ctx.get_var("snipers_scope_start_ts", 0.0) or 0.0)
        if _scope_ts > 0:
            import time as _t
            _elapsed = max(0.0, _t.time() - _scope_ts)
            ctx.set_var("snipers_scope_elapsed_carry", _elapsed)
        ctx.set_var("snipers_scope_start_ts", 0.0)
        ctx.set_var("sniper_state", self._ST_IDLE)
        ctx.set_var("sniper_watch_ts", 0.0)
        ctx.set_var("sniper_probe_ts", 0.0)
        ctx.set_var("sniper_probe_price", 0.0)
        ctx.set_var("sniper_probe_ratio", 0.0)
        ctx.set_var("sniper_peak_pct", 0.0)
        ctx.set_var("sniper_active_ts", 0.0)
        # DCA state reset
        ctx.set_var("sniper_dca_count", 0)
        ctx.set_var("sniper_dca_initial_entry", 0.0)

    def _mark_exit(self, ctx: Any, now: float, ai_score: float, profile: str = "") -> None:
        """Record variables used for re-entry decisions on sell."""
        ctx.set_var("sniper_last_exit_ts", now)
        ctx.set_var("sniper_exit_ai_score", ai_score)
        ctx.set_var("sniper_exit_count", int(ctx.get_var("sniper_exit_count", 0)) + 1)
        # [FIX #4] save profile on exit → distinguish variants (SNIPER↔SNIPER(S)) on re-entry
        ctx.set_var("sniper_exit_profile", profile)

    def _check_execution_quality(self, ctx: Any, history: list) -> Dict[str, Any]:
        """WATCH stage: check fill strength + depth imbalance."""
        result: Dict[str, Any] = {"vol_surge": False, "depth_bullish": False, "score": 0.0}
        try:
            # Fill strength: last 5 ticks volume vs prior 10-tick average
            vol_hist = list(getattr(ctx, "volume_history", []) or [])
            if len(vol_hist) >= 15:
                recent_vol = sum(vol_hist[-5:]) / 5
                baseline_vol = sum(vol_hist[-15:-5]) / 10
                if baseline_vol > 0 and recent_vol > baseline_vol * 1.5:
                    result["vol_surge"] = True
                    result["score"] += 2.0
                elif baseline_vol > 0 and recent_vol > baseline_vol * 1.2:
                    result["score"] += 1.0

            # Depth imbalance: bid > ask = buy-side dominance
            depth_bid = float(getattr(ctx, "depth_bid_usdt", 0) or 0)
            depth_ask = float(getattr(ctx, "depth_ask_usdt", 0) or 0)
            if depth_bid == 0 and depth_ask == 0:
                # fetch from controls
                ctrls = getattr(ctx, "controls", {}) or {}
                p = ((ctrls.get("strategy") or {}).get("params") or {})
                depth_bid = float(p.get("depth_bid_usdt", 0) or 0)
                depth_ask = float(p.get("depth_ask_usdt", 0) or 0)
            if depth_ask > 0:
                bid_ask_ratio = depth_bid / depth_ask
                result["bid_ask_ratio"] = round(bid_ask_ratio, 2)
                if bid_ask_ratio > 1.3:
                    result["depth_bullish"] = True
                    result["score"] += 2.0
                elif bid_ask_ratio > 1.1:
                    result["score"] += 1.0
                elif bid_ask_ratio < 0.7:
                    result["score"] -= 2.0  # sell-pressure dominance
        except (KeyError, AttributeError, TypeError, ValueError) as _e:
            # [FIX N12] return neutral score when fill-quality check fails (cannot detect sell pressure)
            logging.getLogger("sniper.exec_quality").warning("exec_quality check failed: %s", _e, exc_info=True)
        return result

    def _make_sell_meta(self, meta: Dict[str, Any], price: float, market: str) -> Dict[str, Any]:
        amount = meta.get("amount", Q.min_order)
        amount, order_price = adjust_order_amount_and_price(amount, price, market)
        meta["amount"] = amount
        meta["price"] = order_price
        meta["force_exit"] = True
        # [FIX N3] use_limit is already set in meta from params — do not overwrite
        # meta["use_limit"] is filled from params["use_limit"] at the start of decide()
        meta["fallback_to_market"] = True  # if use_limit=True and unfilled → fall back to market order
        return meta

    def decide(self, ctx: Any, price: float) -> Decision:
        ctrls = getattr(ctx, "controls", None) or {}
        if isinstance(ctrls, dict):
            params = dict((ctrls.get("strategy") or {}).get("params") or {})
        else:
            params = getattr(ctx, "strategy_params", None) or {}
        market = str(getattr(ctx, "market", "") or getattr(ctx, "code", ""))
        now = time.time()

        # ── load params (respect reserved_selector contract) ──
        schema_ver = int(params.get("sniper_schema_ver", 1))
        tp_pct = float(params.get("tp_pct", self._MIN_TP_PCT))
        sl_pct = abs(float(params.get("sl_pct", self._MIN_SL_PCT)))
        entry_enabled = bool(params.get("entry_enabled", True))
        entry_lookback = int(params.get("entry_lookback_min", params.get("lookback_min", 15)))
        entry_threshold = float(params.get("entry_threshold_pct", params.get("threshold_pct", 0.3)))
        exit_enabled = bool(params.get("exit_enabled", True))
        exit_lookback = int(params.get("exit_lookback_min", 15))
        exit_threshold = float(params.get("exit_threshold_pct", 0.3))
        trail_tp = bool(params.get("trail_tp", False))
        trail_dist_pct = float(params.get("trail_dist_pct", 1.5))
        hold_sell = bool(params.get("hold_sell", False))
        use_limit = bool(params.get("use_limit", False))
        fallback_to_market = bool(params.get("fallback_to_market", True))
        ai_gate_enabled = bool(params.get("ai_gate_enabled", True))
        ai_min_score = float(params.get("ai_min_score", 0.55))
        rsi_entry_enabled = bool(params.get("rsi_entry_enabled", True))
        rsi_exit_enabled = bool(params.get("rsi_exit_enabled", True))
        expiry_min = int(params.get("expiry_min", 30))
        # [PROTECTED] default True - core volatility-based dynamic adjustment feature
        atr_auto = bool(params.get("atr_auto", True))

        # v2 params (computed by selector, respected by the plugin)
        # Dynamic probe ratio: auto-adjusted by win rate (uses selector value when fewer than 3 data points)
        dynamic_ratio = self._calc_dynamic_probe_ratio()
        probe_ratio = dynamic_ratio if dynamic_ratio is not None else float(params.get("probe_ratio", 0.2))
        confirm_ratio = 1.0 - probe_ratio
        watch_sec = float(params.get("watch_sec", 180))
        confirm_window_sec = float(params.get("confirm_window_sec", 300))
        time_stop_min = float(params.get("time_stop_min", 60))
        param_atr_pct = float(params.get("atr_pct", 0.0))
        # market regime mode
        cycle_mode = str(params.get("cycle_mode", "AUTO") or "AUTO").upper()
        if cycle_mode not in ("AUTO", "UP", "DOWN"):
            cycle_mode = "AUTO"

        profile = str(params.get("profile", "SNIPER") or "SNIPER").upper()
        source_tag = str(params.get("source", "") or "").strip().lower()
        is_scope_snipers = (profile == "SNIPERS") or (source_tag == "precision_scope")

        meta: Dict[str, Any] = {
            "market": market, "price": price,
            "tp_pct": tp_pct, "sl_pct": sl_pct,
            "entry_threshold_pct": entry_threshold,
            "entry_lookback_min": entry_lookback,
            "trail_tp": trail_tp, "trail_dist_pct": trail_dist_pct,
            "schema_ver": schema_ver,
            "use_limit": use_limit, "fallback_to_market": fallback_to_market,
            "probe_ratio": probe_ratio, "confirm_ratio": confirm_ratio,
        }

        # SNIPER(s)-specific entry relaxation:
        # Leave the existing SNIPER untouched; only the precision_scope slot gradually relaxes the AI/RSI entry thresholds.
        effective_ai_min = ai_min_score
        # [2026-03-07] RSI gate: hardcoded 30 → injectable via params (default 38)
        # The selector allows RSI up to 55, but the plugin clipping at 30
        # caused most selected candidates to drop out at execution — this resolves that core bottleneck
        effective_rsi_entry_max = float(params.get("rsi_entry_max", 38.0))
        if is_scope_snipers:
            # Even when relaxed, the Scope slot never lowers the AI floor below 50%.
            scope_ai_floor = 0.50
            effective_ai_min = float(params.get("ai_min_score_scope", min(ai_min_score, 0.55)))
            effective_rsi_entry_max = float(params.get("rsi_entry_max_scope", 42.0))
            scope_start_ts = float(ctx.get_var("snipers_scope_start_ts", 0.0) or 0.0)
            if scope_start_ts <= 0:
                # [2026-03-07] scope_start_ts carry-over: even if the coin is swapped out,
                # inherit the prior slot's elapsed time so the 20-min relaxation timer does not reset
                _prev_elapsed = float(ctx.get_var("snipers_scope_elapsed_carry", 0.0) or 0.0)
                scope_start_ts = now - _prev_elapsed
                ctx.set_var("snipers_scope_start_ts", scope_start_ts)
            scope_wait_min = max(0.0, (now - scope_start_ts) / 60.0)
            # [FIX #9] update elapsed carry every tick → timer survives even on crash
            ctx.set_var("snipers_scope_elapsed_carry", max(0.0, now - scope_start_ts))
            relax_after_min = float(params.get("scope_relax_after_min", 20.0))
            if scope_wait_min >= relax_after_min:
                effective_ai_min = min(
                    effective_ai_min,
                    float(params.get("scope_relaxed_ai_min", scope_ai_floor)),
                )
                effective_rsi_entry_max = max(
                    effective_rsi_entry_max,
                    float(params.get("scope_relaxed_rsi_entry_max", 48.0)),
                )
            effective_ai_min = max(scope_ai_floor, effective_ai_min)
            meta["scope_wait_min"] = round(scope_wait_min, 1)
            meta["ai_min_effective"] = round(effective_ai_min, 4)
            meta["rsi_entry_max_effective"] = round(effective_rsi_entry_max, 2)

        # ── AI / RSI lookup ──
        ai_score = 0.5
        rsi = 50.0
        selected_tf = "1m"

        use_multi_tf = bool(params.get("use_multi_timeframe", True))
        if use_multi_tf and market:
            try:
                from app.core.multi_timeframe_ai import analyze_multi_timeframe
                mtf_result = analyze_multi_timeframe(market)
                if mtf_result and mtf_result.best_timeframe:
                    best = mtf_result.best_timeframe
                    ai_score = best.ai_score
                    rsi = best.rsi
                    selected_tf = best.label
                    meta["multi_tf"] = {
                        "selected": best.label,
                        "ai_score": best.ai_score,
                        "rsi": best.rsi,
                        "signal": best.signal,
                        "confidence": best.confidence,
                        "reason": mtf_result.selection_reason,
                        "all_scores": {
                            tf.label: {"ai": tf.ai_score, "rsi": tf.rsi, "signal": tf.signal}
                            for tf in mtf_result.all_timeframes
                        },
                    }
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[SNIPER] AI / RSI lookup (multi-timeframe): %s", exc, exc_info=True)

        if ai_score == 0.5 and rsi == 50.0:
            try:
                brain = getattr(ctx, "current_ai", {}).get("brain", {})
                ai_score = float(brain.get("ai_prediction", 0.5))
                rsi = float(brain.get("rsi", 50.0))
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[SNIPER] AI / RSI lookup (brain fallback): %s", exc, exc_info=True)

        meta["ai_score"] = ai_score
        meta["selected_timeframe"] = selected_tf

        # [2026-03-07] RSI 3-tick smoothing: cushion momentary RSI noise
        # RSI swings 3~10pt per tick, so a candidate at selection (RSI 28) can become
        # RSI 35 at execution and drop out of the hard gate — this resolves that
        rsi_raw = rsi
        _rsi_buf_key = "sniper_rsi_smooth_buf"
        _rsi_buf = list(ctx.get_var(_rsi_buf_key, []) or [])
        _rsi_buf.append(float(rsi_raw))
        if len(_rsi_buf) > 5:
            _rsi_buf = _rsi_buf[-5:]
        ctx.set_var(_rsi_buf_key, _rsi_buf)
        if len(_rsi_buf) >= 3:
            rsi = sum(_rsi_buf[-3:]) / 3.0
        meta["rsi_raw"] = round(rsi_raw, 2)
        meta["rsi"] = round(rsi, 2)

        # ── price history ──
        history = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
        if not history or len(history) < 3:
            return Decision(signal="hold", reason="sniper:insufficient_data", meta=meta)

        # ── determine market regime mode ──
        effective_cycle_mode = cycle_mode
        if cycle_mode == "AUTO":
            try:
                if len(history) >= 26:
                    ema_f = indicators.ema(history, 12)
                    ema_s = indicators.ema(history, 26)
                    if ema_f and ema_s:
                        effective_cycle_mode = "UP" if ema_f >= ema_s else "DOWN"
                    else:
                        effective_cycle_mode = "UP" if rsi >= 50 else "DOWN"
                else:
                    effective_cycle_mode = "UP" if rsi >= 50 else "DOWN"
            except (AttributeError, TypeError):
                logger.warning("[SNIPER] cycle_mode EMA determination failed: %s", getattr(ctx, "market", "?"), exc_info=True)
                effective_cycle_mode = "UP" if rsi >= 50 else "DOWN"
        meta["cycle_mode"] = cycle_mode
        meta["cycle_mode_effective"] = effective_cycle_mode

        # ── ATR-based threshold adjustment (only when no selector value) ──
        if atr_auto and schema_ver < 2 and len(history) >= 14:
            try:
                atr_val = indicators.atr_simplified(history, 14)
                if atr_val and price > 0:
                    atr_pct = (atr_val / price) * 100
                    if atr_pct >= 3.0:
                        auto_threshold = min(2.0, atr_pct * 0.4)
                    elif atr_pct >= 1.0:
                        auto_threshold = atr_pct * 0.35
                    else:
                        auto_threshold = max(0.1, atr_pct * 0.5)
                    entry_threshold = max(0.1, min(2.5, auto_threshold))
                    exit_threshold = max(0.1, min(2.5, auto_threshold * 0.8))
                    meta["atr_pct"] = round(atr_pct, 2)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[SNIPER] ATR-based threshold adjustment: %s", exc, exc_info=True)

        # ── ATR-based dynamic TP/SL (shared by SNIPER / SNIPER(s)) ──
        if len(history) >= 14:
            try:
                atr_val = indicators.atr_simplified(history, 14)
                if atr_val and price > 0:
                    atr_pct = (atr_val / price) * 100
                    param_atr_pct = atr_pct
                    atr_tp_mult = float(params.get("atr_tp_mult", 2.0))
                    atr_sl_mult = float(params.get("atr_sl_mult", 1.2))
                    atr_tp = atr_pct * atr_tp_mult
                    atr_sl = atr_pct * atr_sl_mult
                    tp_pct = max(self._MIN_TP_PCT, min(atr_tp, 10.0))
                    sl_pct = max(self._MIN_SL_PCT, min(atr_sl, 5.0))
                    meta["atr_dynamic_tp_sl"] = True
                    meta["atr_raw_pct"] = round(atr_pct, 3)
                    meta["atr_tp_mult"] = atr_tp_mult
                    meta["atr_sl_mult"] = atr_sl_mult
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[SNIPER] ATR-based dynamic TP/SL: %s", exc, exc_info=True)

        # market regime mode post-processing
        if effective_cycle_mode == "UP":
            tp_pct = max(self._MIN_TP_PCT, tp_pct)
            sl_pct = max(self._MIN_SL_PCT, sl_pct)
        elif effective_cycle_mode == "DOWN":
            tp_pct = max(self._MIN_TP_PCT, min(tp_pct, 1.8))
            sl_pct = max(self._MIN_SL_PCT, min(sl_pct, 5.0))
            trail_tp = True
            trail_dist_pct = max(0.3, min(trail_dist_pct, 1.0))

        # final safety floor/ceiling (guard against legacy/runtime values)
        tp_pct = max(self._MIN_TP_PCT, min(tp_pct, 30.0))  # [FIX N11] cap at 30% (prevent unbounded TP)
        sl_pct = max(self._MIN_SL_PCT, sl_pct)

        meta["tp_pct"] = round(tp_pct, 4)
        meta["sl_pct"] = round(sl_pct, 4)
        meta["trail_tp"] = trail_tp
        meta["trail_dist_pct"] = round(trail_dist_pct, 4)
        meta["entry_threshold_pct"] = round(entry_threshold, 4)

        # ── load current state ──
        state = self._get_state(ctx)
        pos = getattr(ctx, "position", None)
        has_pos = False
        try:
            has_pos = pos is not None and float(pos.get("qty", 0) or 0) > 0
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[SNIPER] position qty parse failed: %s", getattr(ctx, "market", "?"), exc_info=True)
            has_pos = False

        # Server-restart safeguard: position exists but state is IDLE → recover to ACTIVE
        if has_pos and state == self._ST_IDLE:
            state = self._ST_ACTIVE
            self._set_state(ctx, state)
            ctx.set_var("sniper_active_ts", now)
        holding = getattr(ctx, "holding_qty", 0.0)
        if not has_pos and holding and float(holding) > 0:
            has_pos = True
            if state == self._ST_IDLE:
                state = self._ST_ACTIVE
                self._set_state(ctx, state)
                ctx.set_var("sniper_active_ts", now)

        meta["sniper_state"] = state

        # =============================================
        # Holding: handle PROBE / ACTIVE / ARM_TRAIL states
        # =============================================
        if has_pos:
            entry_price = float(
                (pos or {}).get("avg_price", 0)
                or (pos or {}).get("entry_price", 0)
                or (pos or {}).get("entry", 0)
                or getattr(ctx, "avg_buy_price", 0)
                or 0
            )
            if entry_price <= 0:
                return Decision(signal="hold", reason="sniper:no_entry_price", meta=meta)

            profit_pct = (price - entry_price) / entry_price * 100
            meta["profit_pct"] = profit_pct
            meta["entry_price"] = entry_price

            if hold_sell:
                return Decision(signal="hold", reason="sniper:hold_mode", meta=meta)

            # ── PROBE state: awaiting confirmation ──
            if state == self._ST_PROBE:
                probe_ts = float(ctx.get_var("sniper_probe_ts", now))
                probe_price = float(ctx.get_var("sniper_probe_price", entry_price))
                elapsed = now - probe_ts
                meta["probe_elapsed_sec"] = round(elapsed)
                meta["sniper_phase"] = "PROBE"

                # ── CONFIRM strategy after 3-min-candle bottom confirmation ──
                # Condition: 180s (one 3-min candle) elapsed since probe + current price >= entry → bottom confirmed
                confirm_ok = False

                if elapsed >= 180.0 and price >= probe_price:
                    confirm_ok = True
                    meta["confirm_trigger"] = "3min_hold"

                # Secondary: EMA golden cross + 60s + +0.5% vs entry (strong trend)
                if not confirm_ok and elapsed >= 60.0 and len(history) >= 12:
                    try:
                        ema_f = indicators.ema(history, 5)
                        ema_s = indicators.ema(history, 12)
                        if ema_f and ema_s and ema_f > ema_s and price > probe_price * 1.005:
                            confirm_ok = True
                            meta["confirm_trigger"] = "ema_golden"
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.warning("[SNIPER] secondary EMA golden-cross confirm: %s", exc, exc_info=True)

                if confirm_ok and elapsed < confirm_window_sec:
                    # → ACTIVE: buy remaining budget
                    self._record_stat("confirm")
                    self._set_state(ctx, self._ST_ACTIVE)
                    ctx.set_var("sniper_active_ts", now)
                    meta["sniper_phase"] = "CONFIRM"
                    probe_ratio_eff = float(ctx.get_var("sniper_probe_ratio", probe_ratio) or probe_ratio)
                    # For SNIPER(s) DCA, some budget can be reserved at confirm.
                    default_reserve = 0.0
                    if str(params.get("source") or "").strip().lower() == "precision_scope":
                        default_reserve = 0.2
                    dca_reserve_ratio = max(0.0, min(0.6, float(params.get("dca_reserve_ratio", default_reserve) or default_reserve)))
                    if float(params.get("dca_max_depth_pct", 0.0) or 0.0) <= 0.0:
                        dca_reserve_ratio = 0.0
                    confirm_ratio_eff = max(0.0, min(1.0, 1.0 - probe_ratio_eff - dca_reserve_ratio))
                    if confirm_ratio_eff <= 0.0:
                        confirm_ratio_eff = max(0.0, min(1.0, 1.0 - probe_ratio_eff))
                    meta["confirm_buy_ratio"] = confirm_ratio_eff
                    meta["dca_reserve_ratio"] = round(dca_reserve_ratio, 4)
                    meta["allow_add_buy"] = True
                    meta["size_scale"] = confirm_ratio_eff
                    send_signal_telegram(
                        f"🎯🎯 [SNIPER v2] {market} confirm entry!\n"
                        f"• Probe +{(price - probe_price) / probe_price * 100:.2f}%\n"
                        f"• {meta.get('confirm_trigger', 'OK')}\n"
                        f"• add {confirm_ratio_eff:.0%} buy"
                    )
                    return Decision(signal="buy", reason="sniper:confirm", meta=meta)

                # timeout: confirm_window exceeded → abandon probe, abort sell
                if elapsed >= confirm_window_sec:
                    self._record_stat("abort")
                    self._reset_state(ctx)
                    self._mark_exit(ctx, now, ai_score, profile=profile)
                    meta = self._make_sell_meta(meta, price, market)
                    meta["abort_reason"] = "probe_timeout"
                    return Decision(signal="sell", reason="sniper:abort_timeout", meta=meta)

                if profit_pct <= -(sl_pct * 0.5):
                    self._record_stat("abort")
                    self._reset_state(ctx)
                    self._mark_exit(ctx, now, ai_score, profile=profile)
                    meta = self._make_sell_meta(meta, price, market)
                    meta["abort_reason"] = "probe_sl"
                    return Decision(signal="sell", reason="sniper:abort_sl", meta=meta)

                return Decision(signal="hold", reason="sniper:probe_waiting", meta=meta)

            # ── ACTIVE / ARM_TRAIL states ──
            meta["sniper_phase"] = state

            # 1) ARM_TRAIL: trailing mode
            if state == self._ST_ARM_TRAIL:
                peak = float(ctx.get_var("sniper_peak_pct", profit_pct))
                if profit_pct > peak:
                    ctx.set_var("sniper_peak_pct", profit_pct)
                    peak = profit_pct
                meta["trail_peak_pct"] = peak

                # ATR-based dynamic trail-distance adjustment
                effective_trail = trail_dist_pct
                if param_atr_pct > 4.0:
                    effective_trail = max(trail_dist_pct, param_atr_pct * 0.3)
                meta["effective_trail_dist"] = round(effective_trail, 3)

                if (peak - profit_pct) >= effective_trail:
                    self._record_stat("win")
                    self._reset_state(ctx)
                    self._mark_exit(ctx, now, ai_score, profile=profile)
                    self._check_contrarian_opportunity(market, price, rsi, ai_score, "trail_tp")
                    meta = self._make_sell_meta(meta, price, market)
                    return Decision(signal="sell", reason="sniper:trail_tp", meta=meta)

                return Decision(signal="hold", reason="sniper:trailing", meta=meta)

            # 2) ACTIVE state: TP/SL/Time-stop/RSI exit
            # TP reached → switch to ARM_TRAIL (do not exit immediately)
            if trail_tp and profit_pct >= tp_pct:
                self._set_state(ctx, self._ST_ARM_TRAIL)
                ctx.set_var("sniper_peak_pct", profit_pct)
                meta["arm_trail_at"] = round(profit_pct, 3)
                return Decision(signal="hold", reason="sniper:arm_trail", meta=meta)

            # TP reached (when trail is disabled)
            if not trail_tp and profit_pct >= tp_pct:
                self._record_stat("win")
                self._reset_state(ctx)
                self._mark_exit(ctx, now, ai_score, profile=profile)
                self._check_contrarian_opportunity(market, price, rsi, ai_score, "tp")
                meta = self._make_sell_meta(meta, price, market)
                return Decision(signal="sell", reason="sniper:tp", meta=meta)

            # ── DCA averaging-down (shared by SNIPER / SNIPER(s)) ──
            dca_initial_entry = float(ctx.get_var("sniper_dca_initial_entry", 0.0))
            if dca_initial_entry <= 0:
                dca_initial_entry = entry_price
                ctx.set_var("sniper_dca_initial_entry", dca_initial_entry)
            # [FIX #6] if entry_price is 0, DCA cannot be computed → skip
            if dca_initial_entry <= 0:
                dca_initial_entry = 0.0  # guard the drop_from_initial calc below

            dca_step_pct = float(params.get("dca_step_pct", 0.5))
            if dca_step_pct <= 0:
                dca_step_pct = 0.5  # [FIX #10] guard against negative/0 → restore default
            dca_add_ratio = float(params.get("dca_add_ratio", 0.5))
            _sl_for_depth = abs(float(params.get("sl_pct", sl_pct) or sl_pct))
            _default_depth = round(min(3.0, _sl_for_depth * 0.75), 1)
            dca_max_depth_pct = float(params.get("dca_max_depth_pct", _default_depth))
            dca_count = int(ctx.get_var("sniper_dca_count", 0))
            max_dca_steps = int(dca_max_depth_pct / dca_step_pct) if dca_step_pct > 0 else 0

            # Liquidity assessment: based on volume_history (recent volume average)
            dca_liq_label = "normal"
            vol_hist = list(getattr(ctx, "volume_history", []) or [])
            avg_vol = sum(vol_hist[-20:]) / max(len(vol_hist[-20:]), 1) if vol_hist else 0
            dca_low_vol_threshold = float(params.get("dca_low_vol_threshold", 0.5))
            dca_high_vol_threshold = float(params.get("dca_high_vol_threshold", 2.0))
            baseline_vol = sum(vol_hist[-40:]) / max(len(vol_hist[-40:]), 1) if len(vol_hist) >= 40 else 0
            if baseline_vol > 0 and avg_vol < baseline_vol * dca_low_vol_threshold:
                dca_liq_label = "low"
            elif baseline_vol > 0 and avg_vol > baseline_vol * dca_high_vol_threshold:
                dca_liq_label = "high"

            # DCA adjustment by liquidity
            if dca_liq_label == "low":
                # Low liquidity: max 2 times, widen step x2, shrink ratio x0.6
                max_dca_steps = min(max_dca_steps, 2)
                dca_step_pct = dca_step_pct * 2.0
                dca_add_ratio = dca_add_ratio * 0.6
            elif dca_liq_label == "high":
                # High liquidity: reverse-pyramiding multiplier may increase
                pass

            # Reverse pyramiding: ratio grows with depth (1x → 1.25x → ... → max 3x)
            pyramid_mult = min(1.0 + dca_count * 0.25, 3.0)  # [FIX N7] cap at 3x (prevent unbounded scaling)
            effective_ratio = round(dca_add_ratio * pyramid_mult, 4)

            drop_from_initial = ((dca_initial_entry - price) / dca_initial_entry * 100) if dca_initial_entry > 0 else 0.0  # [FIX #6] guard div/0
            next_dca_level = (dca_count + 1) * dca_step_pct

            meta["dca_count"] = dca_count
            meta["dca_max_steps"] = max_dca_steps
            meta["dca_initial_entry"] = dca_initial_entry
            meta["drop_from_initial_pct"] = round(drop_from_initial, 4)
            meta["dca_liquidity"] = dca_liq_label
            meta["dca_effective_ratio"] = effective_ratio

            if (dca_count < max_dca_steps
                    and drop_from_initial >= next_dca_level
                    and profit_pct < 0
                    and profit_pct > -sl_pct):
                ctx.set_var("sniper_dca_count", dca_count + 1)
                meta["allow_add_buy"] = True
                meta["size_scale"] = effective_ratio
                meta["buy_reason"] = "sniper:dca"
                meta["dca_level"] = dca_count + 1
                meta["dca_next_pct"] = round(next_dca_level, 2)
                liq_tag = " ⚠️low-liq" if dca_liq_label == "low" else ""
                send_signal_telegram(
                    f"📊 [SNIPER DCA] {market} average-down #{dca_count + 1}/{max_dca_steps}{liq_tag}\n"
                    f"• -{drop_from_initial:.2f}% vs initial price\n"
                    f"• add buy {effective_ratio:.0%} (reverse-pyramid x{pyramid_mult:.2f})\n"
                    f"• avg: {entry_price:,.0f} → now: {price:,.0f}"
                )
                return Decision(signal="buy", reason="sniper:dca", meta=meta)

            # [2026-03-08] instant-buy protection: block SL/timeout/RSI exit for 3 min after a buy (TP only)
            # Prevents instant stop-out from minor dips/noise right after a rapid buy
            _buy_grace_sec = float(params.get("instant_buy_grace_sec", 180.0))
            _active_ts_grace = float(ctx.get_var("sniper_active_ts", 0.0))
            _in_buy_grace = (_active_ts_grace > 0 and (now - _active_ts_grace) < _buy_grace_sec)
            if _in_buy_grace:
                meta["buy_grace_remaining_sec"] = round(_buy_grace_sec - (now - _active_ts_grace))

            # SL (3 consecutive ticks confirmation — noise guard)
            sl_confirm_need = int(params.get("sl_confirm_ticks", 3))
            if profit_pct <= -sl_pct and not _in_buy_grace:
                sl_streak = int(ctx.get_var("sniper_sl_streak", 0)) + 1
                ctx.set_var("sniper_sl_streak", sl_streak)
                meta["sl_streak"] = sl_streak
                meta["sl_confirm_need"] = sl_confirm_need
                if sl_streak >= sl_confirm_need:
                    self._record_stat("loss")
                    self._reset_state(ctx)
                    ctx.set_var("sniper_sl_streak", 0)
                    self._mark_exit(ctx, now, ai_score, profile=profile)
                    meta = self._make_sell_meta(meta, price, market)
                    return Decision(signal="sell", reason="sniper:sl", meta=meta)
                return Decision(signal="hold", reason="sniper:sl_confirming", meta=meta)
            else:
                ctx.set_var("sniper_sl_streak", 0)

            # RSI Exit (overbought) — blocked during buy grace
            if rsi_exit_enabled and rsi >= 70 and not _in_buy_grace:
                self._record_stat("win" if profit_pct > 0 else "loss")
                self._reset_state(ctx)
                self._mark_exit(ctx, now, ai_score, profile=profile)
                meta = self._make_sell_meta(meta, price, market)
                return Decision(signal="sell", reason="sniper:rsi_exit", meta=meta)

            # TIME-STOP: sideways timeout (prevents fee loops)
            active_ts = float(ctx.get_var("sniper_active_ts", 0.0))
            # [FIX #7] active_ts unset on manual placement/recovery → fall back to position entry_ts
            if active_ts <= 0:
                active_ts = float((pos or {}).get("entry_ts", 0) or (pos or {}).get("ts", 0) or 0)
                if active_ts > 0:
                    ctx.set_var("sniper_active_ts", active_ts)  # avoid recomputing on later ticks
            if active_ts > 0:
                hold_minutes = (now - active_ts) / 60.0
                meta["hold_minutes"] = round(hold_minutes, 1)
                if hold_minutes >= time_stop_min and abs(profit_pct) < 0.5 and profit_pct <= 0:
                    # [FIX] record as win if timing out in profit (previously always recorded loss)
                    self._record_stat("win" if profit_pct > 0 else "loss")
                    self._reset_state(ctx)
                    self._mark_exit(ctx, now, ai_score, profile=profile)
                    meta = self._make_sell_meta(meta, price, market)
                    meta["timeout_reason"] = f"{hold_minutes:.0f}min_flat"
                    return Decision(signal="sell", reason="sniper:timeout", meta=meta)

            # Trend protection: block early exit while trending up (but bypass once max protect time is exceeded)
            trend_protect = bool(params.get("trend_protect_enabled", True))
            max_protect_hours = float(params.get("max_trend_protect_hours", 48.0))  # [FIX M7] prevent indefinite protection
            # hold_minutes is only defined inside the active_ts block, so recompute safely
            _active_ts_for_protect = float(ctx.get_var("sniper_active_ts", 0.0))
            protect_elapsed_hours = ((now - _active_ts_for_protect) / 3600.0) if _active_ts_for_protect > 0 else 0.0
            if trend_protect and exit_enabled and profit_pct < tp_pct and protect_elapsed_hours < max_protect_hours:
                try:
                    if len(history) >= 50:
                        ema_fast = indicators.ema(history, 12)
                        ema_slow = indicators.ema(history, 26)
                        if ema_fast and ema_slow and ema_fast > ema_slow:
                            meta["trend_protected"] = True
                            meta["trend_protect_elapsed_h"] = round(protect_elapsed_hours, 1)
                            return Decision(signal="hold", reason="sniper:uptrend_protect", meta=meta)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[SNIPER] trend-protect EMA check: %s", exc, exc_info=True)

            # sniper sell (near_high exit)
            if exit_enabled:
                exit_high = 0.0
                try:
                    exit_high = float(ctx.get_rolling_high(float(exit_lookback)) or 0.0)
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[SNIPER] rolling_high lookup failed: %s", getattr(ctx, "market", "?"), exc_info=True)
                    exit_high = 0.0
                if exit_high <= 0.0:
                    exit_bars = min(exit_lookback, len(history))
                    exit_recent = history[-exit_bars:] if exit_bars > 0 else history
                    exit_high = max(exit_recent) if exit_recent else float(price)
                exit_target = exit_high * (1 - exit_threshold / 100)
                meta["exit_high_price"] = exit_high
                meta["exit_target_price"] = exit_target
                # near_high exit: only allowed at or above the TP Guards floor (1.2%)
                # Before: regardless of profit → sold even at 0.6% → bypassed TP Guards
                _near_high_min_pct = max(self._MIN_TP_PCT, float(params.get("near_high_min_profit_pct", self._MIN_TP_PCT)))
                if price >= exit_target and profit_pct >= _near_high_min_pct:
                    self._record_stat("win" if profit_pct > 0 else "loss")
                    self._reset_state(ctx)
                    self._mark_exit(ctx, now, ai_score, profile=profile)
                    meta = self._make_sell_meta(meta, price, market)
                    return Decision(signal="sell", reason="sniper:near_high", meta=meta)

            return Decision(signal="hold", reason="sniper:holding", meta=meta)

        # =============================================
        # No position: handle IDLE / WATCH states (Entry pipeline)
        # =============================================
        # State cleanup: if no position but state is PROBE/ACTIVE, return to IDLE
        if state not in (self._ST_IDLE, self._ST_WATCH):
            self._reset_state(ctx)
            state = self._ST_IDLE

        if not entry_enabled:
            return Decision(signal="hold", reason="sniper:entry_disabled", meta=meta)

        # [2026-03-07] manual placement (buy_now=True) → bypass all gates, buy immediately
        # Manual placement already attempts a market buy in strategy_router.py,
        # but SniperPlugin handles fallback paths such as FSM failure / paper mode.
        if bool(params.get("buy_now", False)):
            meta["buy_now"] = True
            return Decision(signal="buy", reason="sniper:buy_now_manual", meta=meta)

        # cooldown / re-entry control
        auto_reentry = bool(params.get("auto_reentry", False))
        last_exit_ts = float(ctx.get_var("sniper_last_exit_ts", 0.0))
        cooldown_sec = expiry_min * 60
        # [FIX #4] ignore cooldown left by another variant (SNIPER↔SNIPER(S))
        _exit_profile = str(ctx.get_var("sniper_exit_profile", "") or "").upper()
        if _exit_profile and _exit_profile != profile:
            last_exit_ts = 0.0  # exit from another variant → cooldown/re-entry limits not applied
        if last_exit_ts > 0:
            if not auto_reentry:
                # auto_reentry=False → max 2 re-entries, only when AI score improves by 10%p or more
                exit_count = int(ctx.get_var("sniper_exit_count", 0))
                max_reentry = int(params.get("max_reentry", 2))
                if exit_count > max_reentry:
                    meta["reentry_blocked"] = True
                    meta["exit_count"] = exit_count
                    return Decision(signal="hold", reason="sniper:reentry_maxed", meta=meta)
                last_exit_ai = float(ctx.get_var("sniper_exit_ai_score", 0.0))
                ai_improvement = ai_score - last_exit_ai
                meta["exit_count"] = exit_count
                meta["last_exit_ai"] = round(last_exit_ai, 4)
                meta["ai_improvement"] = round(ai_improvement, 4)
                if ai_improvement < 0.10:
                    meta["reentry_blocked"] = True
                    return Decision(signal="hold", reason="sniper:reentry_ai_low", meta=meta)
                # AI improved enough → allow re-entry after cooldown
            if (now - last_exit_ts) < cooldown_sec:
                meta["cooldown_remaining"] = cooldown_sec - (now - last_exit_ts)
                return Decision(signal="hold", reason="sniper:cooldown", meta=meta)

        # daily shot-count limit
        daily_key = f"sniper_shots_{time.strftime('%Y%m%d')}"
        daily_shots = int(ctx.get_var(daily_key, 0))
        meta["daily_shots"] = daily_shots
        if daily_shots >= self._MAX_DAILY_SHOTS:
            return Decision(signal="hold", reason="sniper:daily_limit", meta=meta)

        # AI Gate — [FIX] hard gate → soft grace zone
        # AI also fluctuates, so allow a slight miss when RSI is deeply oversold
        _ai_grace_pct = float(params.get("ai_grace_pct", 10.0))  # default 10% grace
        _ai_hard_floor = effective_ai_min * (1 - _ai_grace_pct / 100.0)
        if ai_gate_enabled and ai_score < effective_ai_min:
            meta["ai_required"] = round(effective_ai_min, 4)
            meta["ai_hard_floor"] = round(_ai_hard_floor, 4)
            # below hard floor → always reject (ex: 0.50 * 0.90 = 0.45)
            if ai_score < _ai_hard_floor:
                meta["ai_blocked"] = True
                return Decision(signal="hold", reason="sniper:ai_gate", meta=meta)
            # grace zone (hard_floor <= ai < ai_min): pass if RSI deeply oversold
            if rsi < 30:
                meta["ai_grace_pass"] = True
                meta["ai_grace_reason"] = "rsi_deeply_oversold"
            else:
                meta["ai_blocked"] = True
                meta["ai_grace_fail"] = True
                return Decision(signal="hold", reason="sniper:ai_gate_grace", meta=meta)

        # RSI Entry filter — [FIX] hard gate → soft grace zone
        # RSI fluctuates often, so allow other indicators to compensate on a slight overshoot (grace_zone)
        # Within the grace_zone, pass if AI is high enough or RSI is in a downtrend
        _rsi_grace_pct = float(params.get("rsi_grace_pct", 15.0))  # default 15% grace
        _rsi_hard_cap = effective_rsi_entry_max * (1 + _rsi_grace_pct / 100.0)
        if rsi_entry_enabled and rsi > effective_rsi_entry_max:
            meta["rsi_required_max"] = round(effective_rsi_entry_max, 2)
            meta["rsi_hard_cap"] = round(_rsi_hard_cap, 2)
            # above hard cap → always reject (ex: 38 * 1.15 ≈ 43.7)
            if rsi > _rsi_hard_cap:
                meta["rsi_blocked"] = True
                return Decision(signal="hold", reason="sniper:rsi_entry", meta=meta)
            # grace zone (entry_max < rsi <= hard_cap): check compensating conditions
            _rsi_falling = len(_rsi_buf) >= 3 and _rsi_buf[-1] < _rsi_buf[-2]  # RSI falling
            _ai_strong = ai_score >= (effective_ai_min + 0.10)  # AI is floor + 10%p or more
            if _rsi_falling or _ai_strong:
                meta["rsi_grace_pass"] = True
                meta["rsi_grace_reason"] = "rsi_falling" if _rsi_falling else "ai_strong"
            else:
                meta["rsi_blocked"] = True
                meta["rsi_grace_fail"] = True
                return Decision(signal="hold", reason="sniper:rsi_entry_grace", meta=meta)

        if bool(params.get("reversal_guard_enabled", True)):
            key = "reversal_guard_min_score_scope" if is_scope_snipers else "reversal_guard_min_score"
            default_min = 1.0 if is_scope_snipers else 1.5
            guard_min_score = float(params.get(key, default_min))
            guard_ok, guard_meta = _evaluate_reversal_buy_guard(
                history=history,
                price=float(price),
                strategy_tag="snipers" if is_scope_snipers else "sniper",
                rsi_value=float(rsi),
                rsi_low_static=float(effective_rsi_entry_max),
                min_score=guard_min_score,
                require_macd_turn=bool(params.get("reversal_guard_require_macd_turn", False)),
                require_extreme_rsi=False,
            )
            meta.update(guard_meta)
            if not guard_ok:
                return Decision(signal="hold", reason="sniper:reversal_guard", meta=meta)

        # capital check
        capital = 0.0
        try:
            c = getattr(ctx, "usable_capital", None)
            if c is None:
                c = getattr(ctx, "allocated_capital", None)
            capital = float(c or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[SNIPER] capital check failed: %s", getattr(ctx, "market", "?"), exc_info=True)
            capital = 0.0

        min_order = float(params.get("min_order_usdt", Q.min_order))
        if capital < min_order:
            return Decision(signal="hold", reason="sniper:insufficient_capital", meta=meta)

        # Check bottom-proximity condition — prefer rolling low on the 5-min candle
        # Interpret entry_lookback_min in minutes to compute a timestamp-based low
        # → aligned to the same time axis as the scanner (5-min candle)
        entry_low = 0.0
        try:
            entry_low = ctx.get_rolling_low(float(entry_lookback))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[SNIPER] rolling_low lookup failed: %s", exc, exc_info=True)
        if entry_low <= 0:
            # fallback: existing tick-based
            entry_bars = min(entry_lookback, len(history))
            entry_recent = history[-entry_bars:] if entry_bars > 0 else history
            entry_low = min(entry_recent) if entry_recent else float(price)
        entry_target = entry_low * (1 + entry_threshold / 100)
        meta["entry_low_price"] = entry_low
        meta["entry_target_price"] = entry_target
        meta["distance_pct"] = (price - entry_low) / entry_low * 100 if entry_low > 0 else 999

        near_low = price <= entry_target

        # EMA cross verification (optional)
        ema_cross_enabled = bool(params.get("ema_cross_enabled", False))
        if ema_cross_enabled and near_low and len(history) >= 50:
            try:
                ema_fast = indicators.ema(history, 12)
                ema_slow = indicators.ema(history, 26)
                if ema_fast and ema_slow and ema_fast <= ema_slow:
                    meta["ema_cross_blocked"] = True
                    return Decision(signal="hold", reason="sniper:no_golden_cross", meta=meta)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[SNIPER] EMA cross verification: %s", exc, exc_info=True)

        # ── Bottom Probability Score (BPS) ──
        # Sum 6 indicators to score 0~100 the probability that the current price is in a bottom zone
        bps = 0.0
        bps_detail: dict = {}
        try:
            # 1) RSI (0~30pts): oversold depth
            if rsi < 25:
                bps += 30; bps_detail["rsi"] = 30
            elif rsi < 30:
                bps += 22; bps_detail["rsi"] = 22
            elif rsi < 35:
                bps += 14; bps_detail["rsi"] = 14
            elif rsi < 42:
                bps += 6;  bps_detail["rsi"] = 6

            # 2) MACD histogram turn (0~20pts): bounce from negative
            if len(history) >= 36:
                _ml, _sl, _hist = indicators.macd(list(history))
                _ml2, _sl2, _hist2 = indicators.macd(list(history)[:-3])
                if _hist is not None and _hist2 is not None:
                    if _hist2 < 0 and _hist > _hist2:   # rising from negative
                        bps += 20; bps_detail["macd"] = 20
                    elif _hist > _hist2:                 # rising (incl. positive zone)
                        bps += 10; bps_detail["macd"] = 10

            # 3) BB z-score (0~20pts): depth below the lower band
            if len(history) >= 20:
                _bb = indicators.bollinger_bands(list(history))
                if _bb:
                    _bw = _bb["upper"] - _bb["lower"]
                    _bb_pos = (float(price) - _bb["lower"]) / _bw * 100 if _bw > 0 else 50
                    bps_detail["bb_pos"] = round(_bb_pos, 1)
                    if _bb_pos < 0:
                        bps += 20; bps_detail["bb"] = 20
                    elif _bb_pos < 10:
                        bps += 15; bps_detail["bb"] = 15
                    elif _bb_pos < 20:
                        bps += 10; bps_detail["bb"] = 10
                    elif _bb_pos < 30:
                        bps += 5;  bps_detail["bb"] = 5

            # 4) Wick-candle recovery (0~15pts): small bounce after hitting a low
            if len(history) >= 5:
                _recent_min = min(list(history)[-5:])
                if _recent_min > 0 and float(price) > _recent_min:
                    _bounce = (float(price) - _recent_min) / _recent_min * 100
                    if 0.05 <= _bounce <= 1.5:   # exclude moves that already ran too far up
                        bps += 15 if _bounce >= 0.3 else 8
                        bps_detail["tail"] = round(_bounce, 3)

            # 5) Volume surge (0~10pts): capitulation-sell signal
            _vols = list(getattr(ctx, "volume_history", []) or [])  # [FIX #2] guard AttributeError
            if len(_vols) >= 10:
                _recent_v = _vols[-1]
                _avg_v = sum(_vols[-20:-1]) / max(1, len(_vols[-20:-1]))
                if _avg_v > 0:
                    _vr = _recent_v / _avg_v
                    if _vr >= 2.0:
                        bps += 10; bps_detail["vol"] = round(_vr, 2)
                    elif _vr >= 1.5:
                        bps += 5;  bps_detail["vol"] = round(_vr, 2)

            # 6) AI confidence bonus (0~5pts)
            if ai_score >= 0.70:
                bps += 5; bps_detail["ai"] = 5
            elif ai_score >= 0.60:
                bps += 2; bps_detail["ai"] = 2

        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[SNIPER] BPS computation: %s", exc, exc_info=True)

        bps = min(100.0, bps)
        meta["bps"] = round(bps, 1)
        meta["bps_detail"] = bps_detail

        # ── IDLE → PROBE Fast Entry (BPS-based immediate entry, skips WATCH) ──
        fast_entry_enabled = bool(params.get("fast_entry_enabled", True))
        fast_entry_bps_min = float(params.get("fast_entry_bps_min", 55.0))

        # [2026-03-07] SNIPER(S) BPS Fire gradual relaxation:
        # starts at max 75 → decays over 30 min → floor fast_entry_bps_min(55)
        # Right after slot placement it demands a high bar (75),
        # then lowers the bar over time to widen entry opportunities.
        if is_scope_snipers:
            _bps_start = float(params.get("scope_bps_fire_start", 75.0))
            _bps_floor = float(params.get("scope_bps_fire_floor", fast_entry_bps_min))
            _bps_decay_min = float(params.get("scope_bps_fire_decay_min", 30.0))  # decay over 30 min
            _scope_elapsed = float(meta.get("scope_wait_min", 0.0))
            if _bps_decay_min > 0 and _scope_elapsed < _bps_decay_min:
                _ratio = min(1.0, _scope_elapsed / _bps_decay_min)
                fast_entry_bps_min = _bps_start - (_bps_start - _bps_floor) * _ratio
            else:
                fast_entry_bps_min = _bps_floor
            meta["bps_fire_threshold"] = round(fast_entry_bps_min, 1)

        # GreenPen PA check (only when greenpen_enabled=True)
        _gp_ok = True
        if bool(params.get("greenpen_enabled", False)) and state == self._ST_IDLE:
            from app.strategy.greenpen import check_entry_guard
            _gp = check_entry_guard("SNIPER", history, price)
            _gp_ok = _gp["allow"]
            if _gp_ok and _gp.get("pa_pattern"):
                meta["gp_pa"] = _gp["pa_pattern"]
                meta["gp_direction"] = _gp["pa_direction"]
            elif not _gp_ok:
                meta["gp"] = _gp

        if state == self._ST_IDLE and near_low and fast_entry_enabled and bps >= fast_entry_bps_min and _gp_ok:
            self._record_stat("probe")
            self._set_state(ctx, self._ST_PROBE)
            ctx.set_var("sniper_probe_ts", now)
            ctx.set_var("sniper_probe_price", price)
            ctx.set_var("sniper_probe_ratio", probe_ratio)
            ctx.set_var(daily_key, daily_shots + 1)
            meta["sniper_phase"] = "PROBE"
            meta["fast_entry"] = True
            meta["probe_ratio"] = probe_ratio
            meta["size_scale"] = probe_ratio
            send_signal_telegram(
                f"⚡ [SNIPER BPS Fast] {market} instant Probe ({probe_ratio:.0%}) | BPS {bps:.0f}pt\n"
                f"• now: {price:,.0f} | RSI: {rsi:.1f} | AI: {ai_score:.0%}\n"
                f"• {entry_lookback}min low: {entry_low:,.0f}\n"
                f"• TP: {tp_pct}% / SL: {sl_pct}%"
            )
            return Decision(signal="buy", reason="sniper:probe", meta=meta)

        # ── IDLE → WATCH transition (Phase 0: start observing) ──
        if state == self._ST_IDLE:
            if near_low:
                self._set_state(ctx, self._ST_WATCH)
                ctx.set_var("sniper_watch_ts", now)
                ctx.set_var("sniper_watch_low", price)
                meta["watch_started"] = True
                return Decision(signal="hold", reason="sniper:watch_start", meta=meta)
            return Decision(signal="hold", reason="sniper:wait", meta=meta)

        # ── WATCH state: observation window ──
        if state == self._ST_WATCH:
            watch_ts = float(ctx.get_var("sniper_watch_ts", now))
            watch_low = float(ctx.get_var("sniper_watch_low", price))
            elapsed = now - watch_ts
            meta["watch_elapsed_sec"] = round(elapsed)

            # Condition breach: moved away from the low
            # [2026-03-07] WATCH abort tolerance: allow margin above entry_target
            # On a fast bounce, slightly exceeding entry_target does not abort instantly
            # Higher BPS/confidence widens the margin (a strong signal's bounce may be genuine)
            _deploy_conf = float(params.get("deploy_confidence", 0) or 0)
            if _deploy_conf >= 60.0 or bps >= 65.0:
                _abort_margin = 1.008   # 0.8% — strong signal
            elif bps >= 50.0:
                _abort_margin = 1.005   # 0.5%
            else:
                _abort_margin = 1.003   # 0.3%
            _abort_price = entry_target * _abort_margin
            if price > _abort_price:
                self._reset_state(ctx)
                meta["abort_margin"] = round((_abort_margin - 1.0) * 100, 2)
                return Decision(signal="hold", reason="sniper:watch_abort", meta=meta)

            # Adaptive watch_sec: lower RSI / higher AI shortens observation time (min 30s)
            try:
                _rsi_factor = max(0.0, min(1.0, (rsi - 20.0) / 30.0))   # RSI 20→0, 50→1
                _ai_factor = max(0.0, min(1.0, (0.8 - ai_score) / 0.4))  # AI 0.8→0, 0.4→1
                _compress = 1.0 - 0.6 * (1.0 - (_rsi_factor + _ai_factor) / 2.0)
                effective_watch_sec = max(30.0, watch_sec * _compress)
            except (TypeError, ValueError):
                logger.warning("[SNIPER] WATCH compression calc failed: %s", getattr(ctx, "market", "?"), exc_info=True)
                effective_watch_sec = watch_sec
            meta["effective_watch_sec"] = round(effective_watch_sec)

            # observation time not yet met
            if elapsed < effective_watch_sec:
                # track a lower price during observation
                if price < watch_low:
                    ctx.set_var("sniper_watch_low", price)
                return Decision(signal="hold", reason="sniper:watching", meta=meta)

            # observation passed! → enter PROBE after fill-quality + momentum check
            exec_quality = self._check_execution_quality(ctx, history)
            meta["exec_quality"] = exec_quality

            momentum_ok = False
            if len(history) >= 5:
                recent_5 = history[-5:]
                if recent_5[-1] >= recent_5[0]:
                    momentum_ok = True
            if price >= watch_low:
                momentum_ok = True

            # block entry if sell pressure dominates
            if exec_quality["score"] < -1.0:
                self._reset_state(ctx)
                meta["watch_blocked"] = "sell_pressure"
                return Decision(signal="hold", reason="sniper:watch_sell_pressure", meta=meta)

            if momentum_ok:
                # → PROBE: small entry
                self._record_stat("probe")
                self._set_state(ctx, self._ST_PROBE)
                ctx.set_var("sniper_probe_ts", now)
                ctx.set_var("sniper_probe_price", price)
                ctx.set_var("sniper_probe_ratio", probe_ratio)
                ctx.set_var(daily_key, daily_shots + 1)
                meta["sniper_phase"] = "PROBE"
                meta["probe_ratio"] = probe_ratio
                meta["size_scale"] = probe_ratio
                filters = []
                if ai_gate_enabled:
                    filters.append(f"AI:{ai_score:.0%}")
                if rsi_entry_enabled:
                    filters.append(f"RSI:{rsi:.0f}")
                filter_str = " | ".join(filters) if filters else ""
                send_signal_telegram(
                    f"🔭 [SNIPER v2] {market} Probe entry ({probe_ratio:.0%})\n"
                    f"• now: {price:,.0f}\n"
                    f"• {entry_lookback}min low: {entry_low:,.0f}\n"
                    f"• observation {elapsed:.0f}s passed\n"
                    f"• TP: {tp_pct}% / SL: {sl_pct}%"
                    + (f"\n• {filter_str}" if filter_str else "")
                )
                return Decision(signal="buy", reason="sniper:probe", meta=meta)
            else:
                # no momentum: reset observation
                self._reset_state(ctx)
                return Decision(signal="hold", reason="sniper:watch_no_momentum", meta=meta)

        return Decision(signal="hold", reason="sniper:wait", meta=meta)
    
    def _check_contrarian_opportunity(
        self, 
        market: str, 
        price: float, 
        rsi: float, 
        ai_score: float, 
        exit_reason: str
    ) -> None:
        """After a SNIPER take-profit, check for a contrarian buy opportunity, register to the Reserved Queue, and send a Telegram alert.

        Condition: treated as a contrarian opportunity only when RSI <= 35 AND AI >= 0.5
        """
        try:
            # contrarian buy conditions (safeguards)
            if rsi > 35:
                return  # high RSI → no contrarian
            if ai_score < 0.5:
                return  # low AI → no contrarian

            # auto-register as a CONTRARIAN candidate in the Reserved Queue
            registered = False
            import uuid
            if reserved_queue is not None:
                try:
                    candidate = {
                        "rid": f"sniper_ct_{uuid.uuid4().hex[:8]}",
                        "market": market,
                        "strategy": "CONTRARIAN",
                        "source": "sniper_exit",
                        "exit_reason": exit_reason,
                        "price": price,
                        "rsi": rsi,
                        "ai_score": ai_score,
                        "suggested_budget_usdt": 50,  # default 50 USDT
                        "recommended_params": {
                            "tp_pct": 5.0,
                            "sl_pct": -3.0,
                            "trail_tp": True,
                            "trail_dist_pct": 2.0,
                            "min_score": 2,
                        },
                        "reason": f"SNIPER {exit_reason} take-profit → contrarian opportunity",
                    }
                    reserved_queue.push(candidate)
                    registered = True
                except (KeyError, IndexError, TypeError) as exc:
                    logger.warning("[SNIPER] Reserved Queue CONTRARIAN auto-register: %s", exc, exc_info=True)
            reg_msg = " ✅ Reserved registered" if registered else ""
            send_signal_telegram(
                f"🔄 [Contrarian buy opportunity] {market}\n"
                f"• SNIPER {exit_reason} take-profit → sell pressure emerged\n"
                f"• RSI: {rsi:.1f} (oversold)\n"
                f"• AI: {ai_score:.0%}\n"
                f"• now: {price:,.0f}\n"
                f"• 💡 Sell if holding, buy if not!{reg_msg}"
            )
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[SNIPER] Reserved Queue CONTRARIAN auto-register: %s", exc, exc_info=True)

