# Extracted from strategy_plugins.py — Phase 2 (file diet)
from __future__ import annotations

import logging
import time
from typing import Any, Dict

from app.strategy import indicators
from app.core.currency import Q
from app.strategy.strategy_base import Decision, Signal, StrategyPlugin
from app.strategy.strategy_helpers import (
    adjust_order_amount_and_price,
    should_buy_global_default,
    _apply_atr_dynamic_limits,
    _check_longhold_recovery,
    _common_dca_check,
    _evaluate_reversal_buy_guard,
    _restore_longhold_flag_from_config,
    _try_convert_to_longhold,
    _unregister_longhold,
)

logger = logging.getLogger(__name__)


class GazuaPlugin(StrategyPlugin):
    """Gazua strategy plugin - AI-based swing trading.

    Core concept: "manual first, AI backup"
    - Buy: buy_now=False → AI decides the optimal timing then auto-buys
    - Sell: TP hit → Telegram alert → 5 min wait → auto-sell if no manual sell
    - SL: immediate auto-sell (no wait)

    tp 15% / sl -10% (for large swings)
    """
    name: str = "gazua"

    # AI buy condition threshold (restored to pre-2026-02-03 level)
    AI_BUY_THRESHOLD = 0.65  # AI score >= 0.65 → buy signal (0.75→0.65)
    GRACE_PERIOD_SEC = 21600   # 6h wait after TP alert (24h→6h)

    def decide(self, ctx: Any, price: float) -> Decision:
        # Entry Guards: Global Defaults
        entry_defaults = {
            "observe_candles": 3,
            "bounce_pct_min": 0.3,
            "ema_periods": [5, 12, 20],
            "ema_cross_required": True,
            "rsi_min": 30,
            "rsi_max": 40,
            "momentum_min": 0.3,
            "ai_score_min": 0.7,
            # Regime profile
            "profile_mode": "auto",  # auto | sideways | trend
            "sideways_ai_score_min": 0.58,
            "sideways_rsi_max": 55,
            "sideways_bounce_pct_min": 0.15,
            "sideways_momentum_min": 0.05,
            "sideways_ema_cross_required": False,
            "trend_ai_score_min": 0.68,
            "trend_rsi_max": 60,
            "trend_bounce_pct_min": 0.25,
            "trend_momentum_min": 0.15,
            "trend_ema_cross_required": True,
            # 2-stage entry (probe -> confirm)
            "scale_in_enabled": True,
            "entry_probe_frac": 0.35,
            "entry_confirm_frac": 0.65,
            "confirm_window_sec": 1200,
            "confirm_profit_pct": 0.35,
            "confirm_ai_threshold": 0.64,
            "confirm_momentum_min": 0.05,
            "add_buy_cooldown_sec": 180,
        }
        params: Dict[str, Any] = {}
        try:
            ctrls = getattr(ctx, "controls", None) or {}
            if isinstance(ctrls, dict):
                params = dict((ctrls.get("strategy") or {}).get("params") or {})
        except (KeyError, AttributeError, TypeError):
            logger.warning("[%s] params extraction failed → using defaults: %s", self.name if hasattr(self, 'name') else '?', getattr(ctx, 'market', '?'), exc_info=True)
            params = {}
        for k, v in entry_defaults.items():
            params.setdefault(k, v)

        # Keep confirm add-buy threshold in conservative range.
        # Existing contexts may carry legacy 0.25; raise to 0.35 and persist once.
        try:
            cur_confirm = float(params.get("confirm_profit_pct", 0.35) or 0.35)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[GAZUA] confirm_profit_pct parse failed: %s", getattr(ctx, "market", "?"), exc_info=True)
            cur_confirm = 0.35
        if cur_confirm < 0.35:
            params["confirm_profit_pct"] = 0.35
            try:
                if hasattr(ctx, "update_controls"):
                    ctx.update_controls({"strategy": {"params": {"confirm_profit_pct": 0.35}}})
            except (KeyError, AttributeError, TypeError):
                logger.warning("[GAZUA] confirm_profit_pct persist failed: %s — previous value restored on restart", getattr(ctx, "market", "?"))
        ai_score = 0.5
        regime = "UNKNOWN"
        if hasattr(ctx, "current_ai") and isinstance(ctx.current_ai, dict):
            brain = ctx.current_ai.get("brain", {})
            ai_score = float(brain.get("ai_prediction", 0.5))
            regime = str(brain.get("regime", "UNKNOWN")).upper()

        profile_mode = str(params.get("profile_mode", "auto") or "auto").strip().lower()
        if profile_mode in ("sideways", "range"):
            profile = "SIDEWAYS"
        elif profile_mode in ("trend", "bull", "volatile"):
            profile = "TREND"
        else:
            profile = "SIDEWAYS" if regime in ("SIDEWAYS", "BEAR", "UNKNOWN") else "TREND"

        entry_params = dict(params)
        if profile == "SIDEWAYS":
            entry_params["ai_score_min"] = min(float(entry_params.get("ai_score_min", 0.7)), float(params.get("sideways_ai_score_min", 0.58)))
            entry_params["rsi_max"] = max(int(entry_params.get("rsi_max", 40)), int(params.get("sideways_rsi_max", 55)))
            entry_params["bounce_pct_min"] = min(float(entry_params.get("bounce_pct_min", 0.3)), float(params.get("sideways_bounce_pct_min", 0.15)))
            entry_params["momentum_min"] = min(float(entry_params.get("momentum_min", 0.3)), float(params.get("sideways_momentum_min", 0.05)))
            entry_params["ema_cross_required"] = bool(params.get("sideways_ema_cross_required", False))
        else:
            entry_params["ai_score_min"] = max(float(entry_params.get("ai_score_min", 0.7)), float(params.get("trend_ai_score_min", 0.68)))
            entry_params["rsi_max"] = max(int(entry_params.get("rsi_max", 40)), int(params.get("trend_rsi_max", 60)))
            entry_params["bounce_pct_min"] = max(float(entry_params.get("bounce_pct_min", 0.3)), float(params.get("trend_bounce_pct_min", 0.25)))
            entry_params["momentum_min"] = max(float(entry_params.get("momentum_min", 0.3)), float(params.get("trend_momentum_min", 0.15)))
            entry_params["ema_cross_required"] = bool(params.get("trend_ema_cross_required", True))

        should_buy, entry_meta = should_buy_global_default(ctx, price, entry_params)

        # --- Exit/TP/SL logic ---
        pos = getattr(ctx, "position", None)
        has_pos = (pos is not None and float(pos.get("qty", 0.0) or 0.0) > 0)

        tp_pct = float(params.get("tp", 25.0))
        tp_price = float(params.get("tp_price", 0.0))
        sl_price = float(params.get("sl_price", 0.0))
        sl_pct = float(params.get("sl", params.get("sl_pct", -25.0)))
        if sl_pct > 0:
            sl_pct = -sl_pct
        buy_now = bool(params.get("buy_now", False))
        hold_sell = bool(params.get("hold_sell", False))
        user_sell_only = bool(params.get("user_sell_only", False))

        ai_threshold = float(params.get("ai_buy_threshold", entry_params.get("ai_score_min", self.AI_BUY_THRESHOLD)))
        grace_sec = float(params.get("grace_period_sec", self.GRACE_PERIOD_SEC))
        scale_in_enabled = bool(params.get("scale_in_enabled", True))

        history = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
        momentum_now = 0.0
        try:
            if len(history) >= 3 and float(history[-3] or 0.0) > 0:
                momentum_now = ((float(price) - float(history[-3])) / float(history[-3])) * 100.0
        except (KeyError, IndexError, TypeError, ValueError):
            logger.warning("[GAZUA] momentum calculation failed: %s", getattr(ctx, "market", "?"), exc_info=True)
            momentum_now = 0.0

        meta: Dict[str, Any] = dict(entry_meta or {})
        meta.update({
            "ai_score": ai_score,
            "regime": regime,
            "entry_profile": profile,
            "tp_pct": tp_pct,
            "tp_price": tp_price,
            "sl_pct": sl_pct,
            "sl_price": sl_price,
            "buy_now": buy_now,
            "hold_sell": hold_sell,
            "user_sell_only": user_sell_only,
            "ai_threshold": ai_threshold,
            "grace_sec": grace_sec,
            "scale_in_enabled": scale_in_enabled,
            "entry_rsi_min": int(entry_params.get("rsi_min", 30)),
            "entry_rsi_max": int(entry_params.get("rsi_max", 40)),
            "entry_bounce_min": float(entry_params.get("bounce_pct_min", 0.3)),
            "entry_momentum_min": float(entry_params.get("momentum_min", 0.3)),
            "entry_ai_min": float(entry_params.get("ai_score_min", 0.7)),
            "momentum_now": momentum_now,
        })

        try:
            if len(history) >= 20:
                _apply_atr_dynamic_limits(ctx, params, float(price), history, meta, "gazua")
                if "dynamic_tp" in meta and not any(k in params for k in ("tp", "tp_pct", "tp_price")):
                    tp_pct = float(meta["dynamic_tp"])
                    meta["tp_pct"] = tp_pct
                # GAZUA SL is not overwritten by ATR dynamic_sl — long-hold strategy keeps -25%
                # (if ATR computes -2~3%, GAZUA's intended SL -25% gets neutralized)
                # The engine also reads meta["dynamic_sl"] to overwrite the final SL, so remove the key itself
                meta.pop("dynamic_sl", None)
            # Apply SL even in trail TP mode
            meta["sl_pct"] = sl_pct
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[GAZUA] ATR dynamic TP/SL setup failed: %s — using default TP/SL", getattr(ctx, "market", "?"), exc_info=True)
            meta["sl_pct"] = sl_pct  # SL must always be set even on failure

        def _reset_trailing() -> None:
            ctx.set_var("gazua_trailing_active", False)
            ctx.set_var("gazua_trail_peak_price", 0.0)
            ctx.set_var("gazua_trail_stop_price", 0.0)
            ctx.set_var("gazua_tp_alert_ts", 0.0)

        def _reset_entry_stage() -> None:
            ctx.set_var("gazua_entry_stage", 0)
            ctx.set_var("gazua_entry_stage_ts", 0.0)
            ctx.set_var("gazua_last_add_ts", 0.0)

        now = time.time()

        # ── [2026-03-09] selector-trusted immediate buy ──
        if not has_pos and not buy_now:
            # GreenPen PA check
            if bool(params.get("greenpen_enabled", False)):
                from app.strategy.greenpen import check_entry_guard
                _gp = check_entry_guard("GAZUA", getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or []), price)
                if not _gp["allow"]:
                    return Decision(signal="hold", reason=f"gazua:gp_{_gp['reason']}", meta={"gp": _gp})
            meta["selector_fast_entry"] = True
            _reset_entry_stage = lambda: None  # suppress reset on first entry
            return Decision(signal="buy", reason="gazua:selector_entry", meta=meta)

        # ── LongHold: restore flag from config on server restart ──
        if has_pos:
            _restore_longhold_flag_from_config(ctx)
        # ── LongHold guard: a converted coin holds until recovery ──
        if has_pos and ctx.get_var("longhold_converted", False):
            if not _check_longhold_recovery(ctx, pos, price, "GAZUA"):
                return Decision(signal="hold", reason="gazua:longhold_active",
                                meta={**meta, "LOCK_PROTECTED": True})
        if not has_pos and ctx.get_var("longhold_converted", False):
            ctx.set_var("longhold_converted", False)
            _unregister_longhold(getattr(ctx, "market", ""))

        if has_pos:

            entry = float(
                pos.get("entry", 0.0)
                or pos.get("avg_price", 0.0)
                or pos.get("entry_price", 0.0)
                or getattr(ctx, "avg_buy_price", 0.0)
                or 0.0
            )
            if entry > 0:
                profit_pct = (price - entry) / entry * 100.0
                meta["profit_pct"] = profit_pct
                qty = float(pos.get("qty") or 0.0)
                profit_usdt = (price - entry) * qty
                meta["profit_usdt"] = profit_usdt

                if user_sell_only:
                    meta["user_sell_only_active"] = True
                    meta["LOCK_PROTECTED"] = True
                    return Decision(signal="hold", reason="gazua:user_sell_only", meta=meta)

                target_hit = False
                if tp_price > 0 and price >= tp_price:
                    target_hit = True
                elif profit_pct >= tp_pct:
                    target_hit = True

                sl_hit = False
                if sl_price > 0 and price <= sl_price:
                    sl_hit = True
                elif profit_pct <= sl_pct:
                    sl_hit = True

                # ① Absolute SL — hard floor on cumulative loss vs initial entry price (protects even when DCA lowers avg)
                _g_initial_entry = float(ctx.get_var("gazua_initial_entry_price", 0.0))
                if _g_initial_entry <= 0 and entry > 0:
                    _g_initial_entry = entry
                    ctx.set_var("gazua_initial_entry_price", entry)
                _g_abs_sl_pct = float(params.get("gazua_abs_sl_pct", -35.0))
                if _g_abs_sl_pct > 0:
                    _g_abs_sl_pct = -_g_abs_sl_pct
                if _g_initial_entry > 0 and price <= _g_initial_entry * (1.0 + _g_abs_sl_pct / 100.0):
                    sl_hit = True
                    meta["abs_sl_triggered"] = True
                    meta["gazua_initial_entry"] = _g_initial_entry
                    meta["drop_from_initial_pct"] = round((price - _g_initial_entry) / _g_initial_entry * 100.0, 2)

                # ② SL — sell only after LongHold conversion attempt fails
                if sl_hit:
                    _reset_trailing()
                    _reset_entry_stage()
                    # Try DCA first (when scale_in is enabled)
                    if scale_in_enabled:
                        dca_result = _common_dca_check(ctx, price, entry, params, "gazua", meta)
                        if dca_result is not None:
                            return dca_result
                    # DCA not possible → try LongHold conversion
                    _lh_market = getattr(ctx, "market", "")
                    _lh_result = _try_convert_to_longhold(ctx, _lh_market, "GAZUA", entry, price, meta)
                    if _lh_result is not None:
                        return _lh_result
                    # Sell when LongHold conversion fails
                    ctx.set_var("gazua_initial_entry_price", 0.0)
                    market = getattr(ctx, "market", "BTCUSDT")
                    amount = meta.get("amount", Q.min_order)
                    order_price = price
                    amount, order_price = adjust_order_amount_and_price(amount, order_price, market)
                    meta["amount"] = amount
                    meta["price"] = order_price
                    return Decision(signal="sell", reason="gazua:sl", meta=meta)

                if hold_sell:
                    _reset_trailing()
                    meta["hold_active"] = True
                    return Decision(signal="hold", reason="gazua:hold_active", meta=meta)

                # Compute holding time
                entry_ts = float(pos.get("entry_ts", 0)) or float(pos.get("ts", 0)) or 0
                elapsed_sec = (now - entry_ts) if entry_ts > 0 else float("inf")
                elapsed_h = elapsed_sec / 3600.0

                # V2 DCA: 2-stage averaging down (-5% → 40%, -10% → 20%)
                if scale_in_enabled:
                    stage = int(ctx.get_var("gazua_entry_stage", 0) or 0)
                    if stage <= 0:
                        ctx.set_var("gazua_entry_stage", 3)
                    elif stage == 1:
                        dca_trigger = float(params.get("gazua_dca_trigger_pct", -5.0))
                        if profit_pct <= dca_trigger:
                            dca_ratio = float(params.get("gazua_dca_ratio",
                                              params.get("entry_confirm_frac", 0.4)))
                            dca_ratio = max(0.05, min(1.0, dca_ratio))
                            ctx.set_var("gazua_entry_stage", 2)
                            ctx.set_var("gazua_entry_stage_ts", 0.0)
                            meta["scale_in_stage"] = "dca1"
                            meta["allow_add_buy"] = True
                            meta["size_scale"] = dca_ratio
                            meta["buy_reason"] = "gazua:dca_buy"
                            meta["dca_trigger_pct"] = dca_trigger
                            return Decision(signal="buy", reason="gazua:dca_buy", meta=meta)
                        # Existing confirm buy logic (fallback)
                        stage_ts = float(ctx.get_var("gazua_entry_stage_ts", 0.0) or 0.0)
                        confirm_window_sec = max(60.0, float(params.get("confirm_window_sec", 1200) or 1200))
                        if stage_ts > 0 and (now - stage_ts) > confirm_window_sec:
                            _reset_entry_stage()
                            meta["scale_in_expired"] = True
                        else:
                            confirm_profit_pct = max(0.0, float(params.get("confirm_profit_pct", 0.35) or 0.35))
                            confirm_ai_threshold = max(ai_threshold, float(params.get("confirm_ai_threshold", 0.64) or 0.64))
                            confirm_momentum_min = float(params.get("confirm_momentum_min", 0.05) or 0.05)
                            confirm_ok = (
                                profit_pct >= confirm_profit_pct
                                or (ai_score >= confirm_ai_threshold and momentum_now >= confirm_momentum_min)
                            )
                            if confirm_ok and (not target_hit):
                                second_frac = float(params.get("entry_confirm_frac", 0.40) or 0.40)
                                second_frac = max(0.05, min(1.0, second_frac))
                                ctx.set_var("gazua_entry_stage", 2)
                                ctx.set_var("gazua_entry_stage_ts", 0.0)
                                meta["scale_in_stage"] = "confirm"
                                meta["allow_add_buy"] = True
                                meta["size_scale"] = second_frac
                                meta["buy_reason"] = "gazua:add_buy_confirm"
                                return Decision(signal="buy", reason="gazua:scale_in_confirm", meta=meta)
                    elif stage == 2:
                        # DCA stage 2: buy an extra 20% on a -10% drop
                        dca2_trigger = float(params.get("gazua_dca2_trigger_pct", -10.0))
                        if profit_pct <= dca2_trigger:
                            dca2_ratio = float(params.get("gazua_dca2_ratio", 0.2))
                            dca2_ratio = max(0.05, min(0.5, dca2_ratio))
                            ctx.set_var("gazua_entry_stage", 3)
                            ctx.set_var("gazua_entry_stage_ts", 0.0)
                            meta["scale_in_stage"] = "dca2"
                            meta["allow_add_buy"] = True
                            meta["size_scale"] = dca2_ratio
                            meta["buy_reason"] = "gazua:dca2_buy"
                            meta["dca_trigger_pct"] = dca2_trigger
                            return Decision(signal="buy", reason="gazua:dca2_buy", meta=meta)

                # ② Grace Period — 24h protection (after SL, strategic sell delay)
                if elapsed_sec < grace_sec:
                    meta["grace_remaining_sec"] = grace_sec - elapsed_sec
                    return Decision(signal="hold", reason="gazua:grace_period", meta=meta)

                # ③ AI confidence drop (AI < 0.75 and profit ≥ 5% → exit while in profit)
                if ai_score < self.AI_BUY_THRESHOLD and profit_pct >= 5.0:
                    _reset_trailing()
                    _reset_entry_stage()
                    ctx.set_var("gazua_initial_entry_price", 0.0)
                    meta["ai_exit_score"] = ai_score
                    return Decision(signal="sell", reason="gazua:ai_exit", meta=meta)

                # ④⑤ Multi-stage partial sell (V2 core)
                partial_stage = int(ctx.get_var("gazua_partial_stage", 0))
                if partial_stage == 0 and bool(ctx.get_var("gazua_partial_sold", False)):
                    partial_stage = 1

                trigger1_pct = float(params.get("partial_sell_trigger_pct", 20.0))
                fraction1 = float(params.get("partial_sell_fraction", 0.3))
                trigger2_pct = float(params.get("partial_sell_trigger2_pct", 35.0))
                fraction2 = float(params.get("partial_sell_fraction2", 0.4))

                # ATR linkage: widen triggers for high-volatility coins
                try:
                    if len(history) >= 14:
                        _atr_val = indicators.atr_simplified(history, 14)
                        if _atr_val and price > 0:
                            _atr_pct = (_atr_val / price) * 100
                            meta["gazua_atr_pct"] = round(_atr_pct, 2)
                            if _atr_pct > 3.0:
                                _atr_mult = min(2.0, _atr_pct / 3.0)
                                trigger1_pct *= _atr_mult
                                trigger2_pct *= _atr_mult
                                meta["partial_trigger_atr_adjusted"] = True
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[plugin_gazua] %s: %s", 'ATR linkage: widen triggers for high-volatility coins', exc, exc_info=True)

                # Stage 1: +20% → 30% partial sell
                if partial_stage == 0 and profit_pct >= trigger1_pct:
                    meta["sell_fraction"] = fraction1
                    meta["stage"] = 1
                    meta["partial_trigger_pct"] = trigger1_pct
                    return Decision(signal="sell", reason="gazua_partial", meta=meta)

                # TP: sell the entire remainder (qty left after Stage 1)
                if target_hit:
                    _reset_trailing()
                    _reset_entry_stage()
                    ctx.set_var("gazua_initial_entry_price", 0.0)
                    meta["tp_after_partial"] = partial_stage
                    return Decision(signal="sell", reason="gazua:tp", meta=meta)

                # Stage 2: +35% → 40% partial sell (reached only when TP > 35% is set)
                if partial_stage == 1 and profit_pct >= trigger2_pct:
                    meta["sell_fraction"] = fraction2
                    meta["stage"] = 2
                    meta["partial_trigger_pct"] = trigger2_pct
                    return Decision(signal="sell", reason="gazua_partial", meta=meta)

                # ⑥ Momentum decay guard (auto-exit on prolonged stagnation)
                if elapsed_h > 168 and 0 <= profit_pct < 3.0:
                    _reset_trailing()
                    _reset_entry_stage()
                    ctx.set_var("gazua_initial_entry_price", 0.0)
                    meta["momentum_decay_hours"] = elapsed_h
                    return Decision(signal="sell", reason="gazua:momentum_decay", meta=meta)

                # ⑦ Trailing Stop (TimeVolatility linkage + staged partial sell)
                trail_activate_pct = float(params.get("gazua_trailing_activate_pct", 10.0))
                trail_callback_pct = float(params.get("gazua_trailing_callback_pct",
                                           params.get("trail_dist_pct", 3.0)))

                try:
                    from app.monitor.time_volatility_adjuster import get_time_volatility_adjuster
                    _tv = get_time_volatility_adjuster()
                    trail_callback_pct *= _tv.get_volatility_multiplier()
                except (ImportError, AttributeError, TypeError) as exc:
                    logger.warning("[plugin_gazua] %s: %s", '⑦ Trailing Stop (TimeVolatility linkage + staged partial sell)', exc, exc_info=True)

                # BTC regime linkage: SHOCK → tighten trailing by 50%
                try:
                    from app.monitor.btc_leading_signal import get_btc_leading_detector
                    _btc = get_btc_leading_detector()
                    if _btc:
                        gz_action = _btc.get_strategy_action("GAZUA")
                        btc_trail_mult = float(gz_action.get("trailing_mult", 1.0))
                        trail_callback_pct *= btc_trail_mult
                        meta["btc_regime"] = gz_action.get("regime", "TREND")
                        meta["btc_trailing_mult"] = btc_trail_mult
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[plugin_gazua] %s: %s", 'BTC regime linkage: SHOCK → tighten trailing by 50%', exc, exc_info=True)

                # 72h+ low profit → tighten trailing (callback reduced by 50%)
                if elapsed_h > 72 and profit_pct < 5.0:
                    trail_callback_pct *= 0.5

                trailing_active = bool(ctx.get_var("gazua_trailing_active", False))
                trail_peak = float(ctx.get_var("gazua_trail_peak_price", 0.0))
                trail_stop = float(ctx.get_var("gazua_trail_stop_price", 0.0))
                meta["trail_tp_enabled"] = True
                meta["trailing_active"] = trailing_active
                meta["trail_callback_pct"] = trail_callback_pct
                meta["trail_activate_pct"] = trail_activate_pct

                if trailing_active:
                    if price > trail_peak:
                        trail_peak = price
                        new_stop = trail_peak * (1.0 - trail_callback_pct / 100.0)
                        if new_stop > trail_stop:
                            trail_stop = new_stop
                        ctx.set_var("gazua_trail_peak_price", trail_peak)
                        ctx.set_var("gazua_trail_stop_price", trail_stop)
                    meta["trail_peak"] = trail_peak
                    meta["trail_stop"] = trail_stop

                    if price <= trail_stop:
                        if partial_stage == 0:
                            meta["sell_fraction"] = fraction1
                            meta["stage"] = 1
                            meta["trail_triggered"] = True
                            return Decision(signal="sell", reason="gazua_partial", meta=meta)
                        elif partial_stage == 1:
                            meta["sell_fraction"] = fraction2
                            meta["stage"] = 2
                            meta["trail_triggered"] = True
                            return Decision(signal="sell", reason="gazua_partial", meta=meta)
                        else:
                            _reset_trailing()
                            _reset_entry_stage()
                            ctx.set_var("gazua_initial_entry_price", 0.0)
                            return Decision(signal="sell", reason="gazua:trailing_stop", meta=meta)
                    return Decision(signal="hold", reason="gazua:trailing_active", meta=meta)

                elif profit_pct >= trail_activate_pct:
                    trail_peak = price
                    trail_stop = trail_peak * (1.0 - trail_callback_pct / 100.0)
                    ctx.set_var("gazua_trailing_active", True)
                    ctx.set_var("gazua_trail_peak_price", trail_peak)
                    ctx.set_var("gazua_trail_stop_price", trail_stop)
                    meta["trailing_active"] = True
                    meta["trail_peak"] = trail_peak
                    meta["trail_stop"] = trail_stop
                    return Decision(signal="hold", reason="gazua:trailing_armed", meta=meta)
            else:
                meta["entry_missing"] = True
                return Decision(signal="hold", reason="gazua:no_entry_price", meta=meta)

            return Decision(signal="hold", reason="gazua:monitoring", meta=meta)

        _reset_trailing()
        # In the no-position state, keep the state right after the probe order (stage=1)
        # so it naturally flows into the confirm stage once filled.
        try:
            st0 = int(ctx.get_var("gazua_entry_stage", 0) or 0)
        except (TypeError, ValueError):
            logger.warning("[GAZUA] entry_stage parse failed: %s", getattr(ctx, "market", "?"), exc_info=True)
            st0 = 0
        if st0 >= 3:
            _reset_entry_stage()

        capital = 0.0
        try:
            c = getattr(ctx, "usable_capital", None)
            if c is None:
                c = getattr(ctx, "allocated_capital", None)
            capital = float(c or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[GAZUA] capital check failed: %s", getattr(ctx, "market", "?"), exc_info=True)
            capital = 0.0

        min_order = float(params.get("min_order_usdt", Q.min_order))
        if capital < min_order:
            return Decision(signal="hold", reason="gazua:insufficient_capital", meta=meta)

        if buy_now:
            return Decision(signal="buy", reason="gazua:buy_now", meta=meta)

        if bool(params.get("reversal_guard_enabled", True)):
            entry_rsi_now = indicators.rsi(history, 14) if len(history) >= 15 else None
            if entry_rsi_now is not None:
                meta["entry_rsi"] = round(float(entry_rsi_now), 3)
            guard_min_score = float(params.get("reversal_guard_min_score", 2.5))
            if user_sell_only:
                guard_min_score = max(3.0, guard_min_score)
            guard_ok, guard_meta = _evaluate_reversal_buy_guard(
                history=history,
                price=float(price),
                strategy_tag="gazua_longhold" if user_sell_only else "gazua",
                rsi_value=float(entry_rsi_now) if entry_rsi_now is not None else None,
                rsi_low_static=float(entry_params.get("rsi_max", 40)),
                min_score=guard_min_score,
                require_macd_turn=bool(params.get("reversal_guard_require_macd_turn", True)),
                require_extreme_rsi=bool(params.get("reversal_guard_require_extreme_rsi", False)),
            )
            meta.update(guard_meta)
            if not guard_ok:
                return Decision(signal="hold", reason="gazua:reversal_guard", meta=meta)

        entry_trigger = should_buy or (ai_score >= ai_threshold)
        if entry_trigger:
            probe_frac = float(params.get("entry_probe_frac",
                               params.get("gazua_initial_ratio", 0.6)) or 0.6)
            probe_frac = max(0.05, min(1.0, probe_frac))
            if scale_in_enabled and probe_frac < 0.99:
                ctx.set_var("gazua_entry_stage", 1)
                ctx.set_var("gazua_entry_stage_ts", now)
                meta["scale_in_stage"] = "probe"
                meta["size_scale"] = probe_frac
                meta["entry_trigger"] = "global_default" if should_buy else "ai_buy"
                return Decision(
                    signal="buy",
                    reason="gazua:probe_entry" if should_buy else "gazua:probe_ai_buy",
                    meta=meta,
                )

            ctx.set_var("gazua_entry_stage", 2)
            ctx.set_var("gazua_entry_stage_ts", 0.0)
            if should_buy:
                return Decision(signal="buy", reason="gazua:global_default_entry", meta=meta)
            meta["ai_buy_triggered"] = True
            return Decision(signal="buy", reason="gazua:ai_buy", meta=meta)

        return Decision(signal="hold", reason="gazua:wait_ai", meta=meta)
