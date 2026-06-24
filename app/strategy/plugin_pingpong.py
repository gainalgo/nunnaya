# Extracted from strategy_plugins.py — Phase 2 (file diet)
from __future__ import annotations
import logging
from typing import Any, Dict

from app.strategy import indicators
from app.core.currency import Q
from app.strategy.strategy_base import Decision, Signal, StrategyPlugin
from app.strategy.strategy_helpers import (
    adjust_ai_score_for_strategy,
    adjust_order_amount_and_price,
    _apply_atr_dynamic_limits,
    _common_dca_check,
    _check_longhold_recovery,
    _detect_regime,
    _get_calibrator,
    _is_breakout,
    _reset_dca_state,
    _restore_longhold_flag_from_config,
    _try_convert_to_longhold,
    _unregister_longhold,
    send_signal_telegram,
)

logger = logging.getLogger(__name__)


class PingPongPlugin(StrategyPlugin):
    """PingPong strategy plugin.

    - The internal implementation calls app.engine.pingpong_strategy.
    - The reverse-margin prevention logic lives in pingpong_strategy.
    """

    name: str = "pingpong"

    def decide(self, ctx: Any, price: float) -> Decision:
        params: Dict[str, Any] = {}
        try:
            ctrls = getattr(ctx, "controls", None) or {}
            if isinstance(ctrls, dict):
                params = dict((ctrls.get("strategy") or {}).get("params") or {})
        except (KeyError, AttributeError, TypeError):
            logger.warning("[%s] params extraction failed -> using defaults: %s", self.name if hasattr(self, 'name') else '?', getattr(ctx, 'market', '?'), exc_info=True)
            params = {}

        # ── Key Bridge: unify external keys -> pp_ internal keys ──
        # tp/tp_pct → pp_tp_pct, sl/sl_pct → pp_sl_pct
        # When pp_exit_gap_pct is unset, use pp_tp_pct as the exit gap (band take-profit = TP linked)
        if "pp_tp_pct" not in params:
            for k in ("tp_pct", "tp"):
                if k in params:
                    try:
                        params["pp_tp_pct"] = float(params[k])
                    except (TypeError, ValueError) as exc:
                        logger.warning("[PINGPONG_PLUGIN] failed to parse pp_tp_pct from %s: %s", k, exc, exc_info=True)
                    break
        if "pp_sl_pct" not in params:
            for k in ("sl_pct", "sl"):
                if k in params:
                    try:
                        params["pp_sl_pct"] = float(params[k])
                    except (TypeError, ValueError) as exc:
                        logger.warning("[PINGPONG_PLUGIN] failed to parse pp_sl_pct from %s: %s", k, exc, exc_info=True)
                    break
        if "pp_exit_gap_pct" not in params and "pp_tp_pct" in params:
            try:
                params["pp_exit_gap_pct"] = float(params["pp_tp_pct"])
            except (TypeError, ValueError) as exc:
                logger.warning("[PINGPONG_PLUGIN] failed to derive pp_exit_gap_pct from pp_tp_pct: %s", exc, exc_info=True)

        # tp_sl_mode: "auto" (allow AI/ATR dynamic adjustment) | "manual" (lock user fixed values)
        tp_sl_mode = str(params.get("tp_sl_mode", "auto")).strip().lower()
        is_manual = tp_sl_mode == "manual"

        # PingPong ExitPolicy v1 defaults (engine-side; UI does not tune)
        _pp_defaults = {
            "pp_exit_enabled": True,
            "pp_exit_lookback": 60,
            "pp_exit_dampen_need": 2,
            "pp_exit_trail_min_pct": 0.4,
            "pp_exit_trail_max_pct": 1.0,
            "pp_exit_trail_vol_mult": 0.6,
            "pp_exit_trail_vol_window": 30,
            "pp_exit_rsi_len": 14,
            "pp_exit_rsi_drop_ratio": 0.08,
            "pp_exit_macd_fast": 12,
            "pp_exit_macd_slow": 26,
            "pp_exit_macd_signal": 9,
            "pp_exit_macd_down_streak": 2,
            "pp_exit_band_len": 20,
            "pp_exit_band_k": 2.0,
            "pp_exit_min_profit_pct": 0.1
        }
        for k, v in _pp_defaults.items():
            if k not in params:
                params[k] = v

        # --------------------------------------------------------
        # AI-Driven Dynamic Tuning (PingPong) - 2026-01-30 v2
        # Dynamic adjustment based on per-strategy AI threshold + regime fitness
        # --------------------------------------------------------
        ai_score = 0.5
        regime = "UNKNOWN"
        if hasattr(ctx, "current_ai") and isinstance(ctx.current_ai, dict):
            brain = ctx.current_ai.get("brain", {})
            ai_score = float(brain.get("ai_prediction", 0.5))
            # Extract regime info
            regime = str(brain.get("regime", "UNKNOWN")).upper()

        # Per-strategy AI adjustment (coin_tiers.adjust_ai_score_for_strategy)
        ai_adjustment = adjust_ai_score_for_strategy(ai_score, strategy="pingpong", regime=regime)
        tp_scale = ai_adjustment["tp_scale"]
        sl_scale = ai_adjustment["sl_scale"]

        ai_influence = max(0.0, min(1.0, float(params.get("ai_influence", 0.15))))

        if ai_influence > 0:
            # factor: -0.5 (Bear) ~ +0.5 (Bull) scaled by influence
            factor = (ai_score - 0.5) * ai_influence

            # Entry Gap: Bullish -> decrease gap (buy closer/easier)
            gap = float(params.get("pp_entry_gap_pct", params.get("gap_pct", 0.35)))
            params["pp_entry_gap_pct"] = max(0.05, gap * (1.0 - factor))

            # Exit Gap: Bullish -> increase gap (aim higher)
            egap = float(params.get("pp_exit_gap_pct", params.get("pp_tp_pct", gap)))
            params["pp_exit_gap_pct"] = egap * (1.0 + factor)

            # Apply TP/SL scale (skipped in manual mode)
            if not is_manual:
                base_tp = float(params.get("pp_tp_pct", 2.5))
                base_sl = float(params.get("pp_sl_pct", -2.5))
                params["pp_tp_pct"] = max(1.2, base_tp * tp_scale)
                params["pp_sl_pct"] = min(-2.5, base_sl * sl_scale)

        # ATR Dynamic TP/SL
        history = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
        meta_atr: Dict[str, Any] = {}
        _apply_atr_dynamic_limits(ctx, params, float(price), history, meta_atr, "pingpong")

        # Reflect ATR dynamic values into strategy params (skipped in manual mode)
        if not is_manual:
            if meta_atr.get("dynamic_tp"):
                params["pp_tp_pct"] = float(meta_atr["dynamic_tp"])
                params["pp_exit_gap_pct"] = float(meta_atr["dynamic_tp"])
            if meta_atr.get("dynamic_sl"):
                params["pp_sl_pct"] = float(meta_atr["dynamic_sl"])

        # ── Online Calibration Overlay (Phase 3-A) ──
        if _get_calibrator and not is_manual:
            try:
                _cal = _get_calibrator()
                _cal_regime = _detect_regime(history)
                _cal_atr = indicators.atr_simplified(history)
                _cal_atr_pct = (_cal_atr / price * 100.0) if _cal_atr and price > 0 else 2.0
                _cal_adj = _cal.get_adjustments(_cal.classify_bucket(_cal_atr_pct, _cal_regime), "PINGPONG")
                if _cal_adj:
                    params["pp_tp_pct"] = float(params.get("pp_tp_pct", 2.5)) * _cal_adj.get("pp_tp_mult", 1.0)
                    params["pp_sl_pct"] = float(params.get("pp_sl_pct", -2.5)) * _cal_adj.get("pp_sl_mult", 1.0)
                    params["pp_exit_gap_pct"] = float(params.get("pp_exit_gap_pct", 0.35)) * _cal_adj.get("pp_gap_mult", 1.0)
            except (TypeError, ValueError, KeyError) as _cal_err:
                logger.warning("[PINGPONG] calendar adjust failed: %s", _cal_err, exc_info=True)

        # Local import to avoid early import/circular risks
        from app.engine.pingpong_strategy import decide as pp_decide, compute_levels

        # ── 2-A: position check ──
        pos = getattr(ctx, "position", None)
        has_pos = bool(pos and float((pos.get("qty") if isinstance(pos, dict) else getattr(pos, "qty", 0)) or 0) > 0)

        # ── LongHold: restore flag from config on server restart ──
        if has_pos:
            _restore_longhold_flag_from_config(ctx)
        # ── LongHold conversion done -> check recovery then keep hold ──
        if has_pos and ctx.get_var("longhold_converted", False):
            if not _check_longhold_recovery(ctx, pos, price, "PINGPONG"):
                return Decision(signal="hold", reason="pingpong:longhold_active",
                                meta={"longhold": True, "longhold_ts": ctx.get_var("longhold_convert_ts", 0)})
            # recovered -> proceed to normal strategy logic below
        if not has_pos and ctx.get_var("longhold_converted", False):
            ctx.set_var("longhold_converted", False)
            _unregister_longhold(getattr(ctx, "market", ""))

        # ── [2026-03-09] selector-trusted instant buy: if no position, selector already validated → buy ──
        # [2026-03-30] crash defense: minimal safeguard before selector instant buy
        if not has_pos:
            _safe_to_enter = True
            _block_reason = ""
            # Falling Knife: drop of -2% or more over the last 6 ticks → hold
            _hist = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
            if len(_hist) >= 6:
                try:
                    _recent = [float(x) for x in _hist[-6:] if float(x) > 0]
                    if len(_recent) >= 6 and _recent[0] > 0:
                        _drop_pct = (_recent[-1] / _recent[0] - 1.0) * 100.0
                        if _drop_pct < -2.0:
                            _safe_to_enter = False
                            _block_reason = f"knife:{_drop_pct:.1f}%"
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    logger.warning("[PINGPONG_PLUGIN] Falling Knife check failed: %s", exc, exc_info=True)
            # RSI Extreme: RSI(14) < 15 → extreme oversold = bottom not yet caught
            if _safe_to_enter:
                try:
                    _rsi = getattr(ctx, "rsi", None) or ctx.get_var("rsi_14", None)
                    if _rsi is not None and float(_rsi) < 15.0:
                        _safe_to_enter = False
                        _block_reason = f"rsi_extreme:{float(_rsi):.0f}"
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[PINGPONG_PLUGIN] RSI Extreme check failed: %s", exc, exc_info=True)
            if not _safe_to_enter:
                return Decision(signal="hold", reason=f"pingpong:selector_entry_blocked:{_block_reason}",
                                meta={"selector_blocked": True, "block_reason": _block_reason})
            # GreenPen PA check (only when greenpen_enabled=True)
            if bool(params.get("greenpen_enabled", False)):
                from app.strategy.greenpen import check_entry_guard
                _gp = check_entry_guard("PINGPONG", getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or []), price)
                if not _gp["allow"]:
                    return Decision(signal="hold", reason=f"pingpong:gp_{_gp['reason']}", meta={"gp": _gp})
            full_meta = {"selector_fast_entry": True}
            full_meta.update(meta_atr)
            try:
                ctx.set_var("pp_breakout_high", float(price))
                ctx.set_var("pp_breakout_active", False)
            except (TypeError, ValueError):
                logger.warning("[PINGPONG] breakout_high init failed: %s — risk of premature trailing stop", getattr(ctx, "market", "?"))
            return Decision(signal="buy", reason="pingpong:selector_entry", meta=full_meta)

        sig: Signal = pp_decide(ctx, float(price), params)
        levels = compute_levels(ctx, float(price), params)

        reason = "pingpong"
        if isinstance(levels, dict) and levels.get("valid"):
            if sig == "buy":
                reason = "pingpong:buy_at_band"
                # On entry, init pp_breakout_high to current price (0.0 -> prevent immediate trailing)
                try:
                    ctx.set_var("pp_breakout_high", float(price))
                    ctx.set_var("pp_breakout_active", False)
                except (TypeError, ValueError):
                    logger.warning("[PINGPONG] buy_at_band breakout_high init failed: %s", getattr(ctx, "market", "?"))
            elif sig == "sell":
                # Determine whether it's a stoploss from the levels
                sp = levels.get("stop_price")
                try:
                    if sp is not None and float(levels.get("price") or 0.0) <= float(sp):
                        reason = "pingpong:stoploss"
                    else:
                        # PingPong ExitPolicy v1: annotate when exit triggered
                        try:
                            ex0 = (levels.get("exit") if isinstance(levels, dict) else None)
                            if isinstance(ex0, dict) and bool(ex0.get("triggered")):
                                m0 = str(ex0.get("mode") or "").lower() or "exit"
                                reason = f"pingpong:exit:{m0}"
                            else:
                                reason = "pingpong:sell_at_band"
                        except (KeyError, AttributeError, TypeError, ValueError):
                            logger.warning("[PINGPONG] exit policy decision failed: %s", getattr(ctx, "market", "?"), exc_info=True)
                            reason = "pingpong:sell_at_band"
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[PINGPONG] stoploss decision failed: %s", getattr(ctx, "market", "?"), exc_info=True)
                    reason = "pingpong:sell"
            else:
                reason = "pingpong:hold"

        # ── 2-B: Breakout Guard — on breakout, suppress band take-profit -> switch to trailing exit ──
        if has_pos and not is_manual and sig == "sell" and reason == "pingpong:sell_at_band":
            if _is_breakout(history, price):
                # While breaking out, hold the band take-profit to maximize profit
                entry_price = 0.0
                try:
                    entry_price = float((pos.get("entry") if isinstance(pos, dict) else getattr(pos, "entry", 0)) or
                                        (pos.get("avg_price") if isinstance(pos, dict) else getattr(pos, "avg_price", 0)) or 0)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[PINGPONG_PLUGIN] failed to read entry price during breakout guard: %s", exc, exc_info=True)

                # trailing stop config: sell on a 2.0% drop from the high (previously 1.2%)
                trail_pct = float(params.get("breakout_trail_pct", 2.0))
                try:
                    prev_high = float(ctx.get_var("pp_breakout_high") or 0.0)
                except (TypeError, ValueError):
                    logger.warning("[PINGPONG] breakout prev_high parse failed: %s", getattr(ctx, "market", "?"), exc_info=True)
                    prev_high = 0.0
                if price > prev_high:
                    prev_high = price
                try:
                    ctx.set_var("pp_breakout_high", prev_high)
                    ctx.set_var("pp_breakout_active", True)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                    logger.warning("[PINGPONG] trailing high update failed: %s — risk of abnormal trailing stop behavior", getattr(ctx, "market", "?"))

                trail_price = prev_high * (1.0 - trail_pct / 100.0)
                if price <= trail_price:
                    # trailing triggered -> sell
                    reason = "pingpong:breakout_trail_sell"
                else:
                    sig = "hold"
                    reason = "pingpong:breakout_guard_hold"
        elif has_pos and not is_manual:
            # Not a breakout but breakout_active was set -> reset
            try:
                if ctx.get_var("pp_breakout_active"):
                    if not _is_breakout(history, price):
                        ctx.set_var("pp_breakout_active", False)
                        ctx.set_var("pp_breakout_high", 0.0)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[PINGPONG_PLUGIN] failed to reset stale breakout_active state: %s", exc, exc_info=True)
        elif not has_pos:
            # No position -> reset breakout state
            try:
                ctx.set_var("pp_breakout_active", False)
                ctx.set_var("pp_breakout_high", 0.0)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[PINGPONG_PLUGIN] failed to reset breakout state with no position: %s", exc, exc_info=True)

        # Merge ATR meta
        full_meta = {"levels": levels, "exit": (levels.get("exit") if isinstance(levels, dict) else None)}
        full_meta.update(meta_atr)
        full_meta["regime"] = regime

        # ── PINGPONG SL confirmation (2 consecutive ticks — noise defense) ──
        if has_pos and sig == "sell" and "stoploss" in reason:
            _pp_sl_confirm_need = int(params.get("sl_confirm_ticks", 2))
            _pp_sl_streak = int(ctx.get_var("pp_sl_streak", 0)) + 1
            ctx.set_var("pp_sl_streak", _pp_sl_streak)
            full_meta["sl_streak"] = _pp_sl_streak
            full_meta["sl_confirm_need"] = _pp_sl_confirm_need
            if _pp_sl_streak < _pp_sl_confirm_need:
                return Decision(signal="hold", reason="pingpong:sl_confirming", meta=full_meta)
            ctx.set_var("pp_sl_streak", 0)

            # Try DCA averaging-down first
            entry_price = 0.0
            try:
                entry_price = float((pos.get("entry") if isinstance(pos, dict) else getattr(pos, "entry", 0)) or
                                    (pos.get("avg_price") if isinstance(pos, dict) else getattr(pos, "avg_price", 0)) or 0)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[PINGPONG_PLUGIN] failed to read entry price before DCA: %s", exc, exc_info=True)
            dca_result = _common_dca_check(ctx, price, entry_price, params, "pp", full_meta)
            if dca_result is not None:
                return dca_result

            # ── DCA not possible → SL → try LongHold conversion ──
            _lh_market = getattr(ctx, "market", "")
            _lh_result = _try_convert_to_longhold(ctx, _lh_market, "PINGPONG", entry_price, price, full_meta)
            if _lh_result is not None:
                return _lh_result
        else:
            # Not an SL -> reset streak
            if has_pos:
                ctx.set_var("pp_sl_streak", 0)
            if has_pos and sig == "buy":
                # Reset DCA state on new entry
                _reset_dca_state(ctx, "pp")

        # Order adjustment (only on buy/sell)
        if sig in ("buy", "sell"):
            market = getattr(ctx, "market", "BTCUSDT")
            amount = full_meta.get("amount", Q.min_order)
            order_price = price
            amount, order_price = adjust_order_amount_and_price(amount, order_price, market)
            full_meta["amount"] = amount
            full_meta["price"] = order_price
        return Decision(signal=sig, reason=reason, meta=full_meta)
