"""Phase 5H – Intent handling mixin extracted from hyper_system.py.

── ASYNC SAFETY RULES ──
Functions in this file are called within an async context (event loop).
- Do NOT call requests.get/post directly → asyncio.to_thread() required
- File I/O / CPU-bound → run_in_executor() required
- No time.sleep() → use asyncio.sleep()
- Data lookups only from price_store/orderbook_store/cache (memory reads)
"""

from __future__ import annotations
import logging
import time
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.engine.hyper_engine_context import HyperEngineContext

from app.core.hyper_price_store import price_store, orderbook_store
from app.manager.oma_market_registry import MarketState

logger = logging.getLogger(__name__)


class IntentMixin:
    """Intent handling mixin (_handle_intent).

    Expects all self.* attributes from HyperSystem.__init__.
    """

    async def _handle_intent(self, market: str, price: float, intent: Dict[str, Any], ctx: HyperEngineContext):
        # ----------------------------------------------------
        # RESERVE (LADDER limit-order reservation)
        # ----------------------------------------------------
        if intent.get("type") == "reserve":
            side = str(intent.get("side", "buy")).lower()
            reserve_price = float(intent.get("price", 0))
            reserve_amount = float(intent.get("amount", 0))
            reserve_meta = intent.get("meta", {})
            reason = str(reserve_meta.get("reason", "ladder:reserve"))
            if side == "buy":
                ok, msg = self.order_fsm.submit_limit_buy(
                    ctx=ctx,
                    market=market,
                    usdt_amount=reserve_amount,
                    limit_price=reserve_price,
                    reason=reason,
                    attempts=1,
                    max_retries=0,
                    timeout_sec=10.0,
                )
            elif side == "sell":
                ok, msg = self.order_fsm.submit_limit_sell(
                    ctx=ctx,
                    market=market,
                    qty=reserve_amount,
                    limit_price=reserve_price,
                    expected_price=reserve_price,
                    reason=reason,
                    attempts=1,
                    max_retries=0,
                    timeout_sec=10.0,
                )
            else:
                ok, msg = False, "Invalid reserve side"
            if ok:
                ctx.entry_state = "ORDER_PLACED"
                self.ledger.append(
                    "ORDER_ACK",
                    market=market,
                    uuid=str(msg),
                    state="wait",
                    side=side,
                    meta=reserve_meta,
                )
            else:
                fallback = bool(intent.get("fallback_to_market", True))
                if fallback and side == "buy" and reserve_amount > 0:
                    self.ledger.append(
                        "RESERVE_FALLBACK_MARKET",
                        market=market,
                        reason=f"limit_failed:{msg}",
                        amount=reserve_amount,
                    )
                    intent = {"buy_usdt": reserve_amount, "reason": f"ladder:market_fallback({reason})"}
                else:
                    ctx.entry_state = "FAILED"
                    self._log_blocked_throttled(
                        "RESERVE_SUBMIT_FAILED",
                        market=market,
                        cause="reserve_submission_failed",
                        reason=reason,
                        error=str(msg),
                    )
                    return
        # intent (canonical):
        #   - BUY : {"buy_usdt": <float>, "reason": <str?>}
        #   - SELL: {"sell_qty": <float>, "reason": <str?>}
        # intent (compat):
        #   - {"action":"buy","usdt":...} / {"action":"sell","qty":...}
        if not intent:
            return

        # compat normalize (some engine/recovery implementations use the action/usdt/qty schema)
        if isinstance(intent, dict):
            act = str(intent.get("action") or "").lower().strip()
            if act == "buy" and "buy_usdt" not in intent and intent.get("usdt") is not None:
                intent = dict(intent)
                intent["buy_usdt"] = intent.get("usdt")
            elif act == "sell" and "sell_qty" not in intent and intent.get("qty") is not None:
                intent = dict(intent)
                intent["sell_qty"] = intent.get("qty")

        reason = str(intent.get("reason") or "")
        meta = intent.get("meta") or {}
        expected_price = float(price) if (price is not None and float(price) > 0) else None

        # SL → LongHold conversion: switch to GAZUA LongHold instead of selling
        # [FIX 2026-03-23] This was a side door that re-entered ACTIVE without a confidence gate
        # Any stopped-out coin was unconditionally converted to GAZUA LongHold → caused unbounded LongHold growth
        # Fix: disable SL→LongHold conversion, process normal stop-loss instead
        # If conversion is needed, use the LongHold Deploy API manually
        if act == "convert_to_longhold":
            # [2026-03-30] throttle: log at most once per 5 min per market (avoid per-tick repeats)
            import time as _time_mod
            _lh_blk_ts = getattr(self, "_sl_longhold_block_ts", {})
            _now_lh = _time_mod.time()
            if (_now_lh - _lh_blk_ts.get(market, 0.0)) > 300:
                try:
                    orig_strategy = str((meta or {}).get("original_strategy", "")).upper() or "UNKNOWN"
                    change_pct = (meta or {}).get("change_pct")
                    self.ledger.append(
                        "SL_TO_LONGHOLD_BLOCKED",
                        market=market,
                        from_strategy=orig_strategy,
                        change_pct=change_pct,
                        reason="confidence_gate_bypass_blocked",
                    )
                    logger.info("[SL→LongHold] BLOCKED %s (from %s, %.1f%%)",
                                market, orig_strategy, float(change_pct or 0))
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[INTENT] SL→LongHold throttle log failed: %s", exc)
                _lh_blk_ts[market] = _now_lh
                self._sl_longhold_block_ts = _lh_blk_ts
            return

        if False and act == "_convert_to_longhold_DISABLED":
            # legacy code preserved (for reference only, not executed)
            try:
                ladder_mgr = getattr(self, "ladder_manager", None)
                if ladder_mgr:
                    orig_strategy = str((meta or {}).get("original_strategy", "")).upper() or "UNKNOWN"
                    change_pct = (meta or {}).get("change_pct")

                    ladder_mgr.save_longhold_config({
                        "market": market,
                        "enabled": True,
                        "strategy": "GAZUA",
                        "target_profit_pct": 50.0,
                        "budget_usdt": 0,
                        "repeat": True,
                    })

                    from app.manager.market_controls import apply_engine_controls
                    apply_engine_controls(
                        self,
                        market,
                        "GAZUA",
                        recommended_params={
                            "sl_pct": -50.0,
                        },
                    )
                    try:
                        _ctx = self.coordinator.contexts.get(market)
                        if _ctx and hasattr(_ctx, "controls") and isinstance(_ctx.controls, dict):
                            _sp = _ctx.controls.get("strategy", {}).get("params", {})
                            _sp["user_sell_only"] = True
                            _sp["sl_to_longhold"] = False
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.warning("[INTENT] legacy sell_only flag update failed: %s", exc)

                    self.oma_set_market(
                        market=market,
                        state=MarketState.ACTIVE,
                        reason=[
                            "strategy:GAZUA",
                            "sl_to_longhold",
                            f"from:{orig_strategy}",
                        ],
                    )

                    self.ledger.append(
                        "SL_TO_LONGHOLD",
                        market=market,
                        from_strategy=orig_strategy,
                        change_pct=float(change_pct) if change_pct is not None else None,
                        sl=float((meta or {}).get("sl", 0)),
                    )
                    logger.info(
                        "[SL→LongHold] %s converted (was %s, pnl=%.2f%%)",
                        market, orig_strategy,
                        float(change_pct) if change_pct is not None else 0.0,
                    )
                else:
                    logger.warning("[SL→LongHold] ladder_manager not available for %s", market)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[SL→LongHold] failed for %s", market, exc_info=True)
            return

        # [FIX 2026-02-19] pre-define force_exit/exit_kind0 (used in the force-exit block below)
        force_exit = bool(intent.get("force_exit") or intent.get("force") or intent.get("force_sell"))
        exit_kind0 = str((intent.get("meta") or {}).get("exit_kind") or "").lower()
        # ----------------------------------------------------
        # Force-exit: cancel any existing pending order for this market
        # - purpose: ensure a forced exit (stop-loss/pp_exit) runs immediately even when pending (e.g. TP limit-exit).
        # ----------------------------------------------------
        try:
            if act == "sell" and force_exit and self.order_fsm:
                d0 = getattr(ctx, "order_state", None)
                if isinstance(d0, dict) and d0.get("uuid"):
                    self.order_fsm.force_cancel_pending(ctx=ctx, market=market, reason=f"force_exit:{exit_kind0 or 'sell'}")
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.error("[INTENT] force_cancel_pending: %s", exc, exc_info=True)

        now_ts = time.time()

        # ----------------------------------------------------
        # Dashboard per-market guard overrides
        # - Stored in ctx.controls.guards (persisted via runtime/context_state.json)
        # - Any value set here must override ENV/global defaults for that market
        # ----------------------------------------------------
        guards_ctl: Dict[str, Any] = {}
        try:
            ctrls = getattr(ctx, "controls", {}) or {}
            if isinstance(ctrls, dict):
                gc = ctrls.get("guards") or {}
                if isinstance(gc, dict):
                    guards_ctl = gc
        except (KeyError, AttributeError, TypeError):
            logger.warning("[%s] guards_ctl extraction failed", market, exc_info=True)
            guards_ctl = {}

        def _g_bool(key: str, default: bool) -> bool:
            if not isinstance(guards_ctl, dict) or key not in guards_ctl:
                return bool(default)
            b = self._ui_as_bool(guards_ctl.get(key))
            return bool(default) if b is None else bool(b)

        def _g_float(key: str, default: float) -> float:
            if not isinstance(guards_ctl, dict) or key not in guards_ctl:
                return float(default)
            x = self._ui_as_float(guards_ctl.get(key))
            return float(default) if x is None else float(x)

        def _g_int(key: str, default: int) -> int:
            if not isinstance(guards_ctl, dict) or key not in guards_ctl:
                return int(default)
            x = self._ui_as_int(guards_ctl.get(key))
            return int(default) if x is None else int(x)

        def _g_str(key: str, default: str) -> str:
            """Read per-market string guard override with sane fallback.

            IMPORTANT:
            - Dashboard/UI may store "inherit" as null *or* an empty string.
            - Treat both None and "" as "unset" so we correctly fall back to the
              provided default (typically the global/ENV value).
            """

            if not isinstance(guards_ctl, dict) or key not in guards_ctl:
                return str(default)

            s = self._ui_as_str(guards_ctl.get(key))
            if s is None:
                return str(default)

            try:
                if isinstance(s, str) and s.strip() == "":
                    return str(default)
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[INTENT] _g_str fallback: %s", exc)

            return str(s)

        # ----------------------------------------------------
        # BUY
        # ----------------------------------------------------
        if "buy_usdt" in intent and intent["buy_usdt"] is not None:
            # [FIX 2026-03-23] minimum price guard: a coin that passed at recommendation time but
            # dropped below candidate_price_min by buy time is blocked from buying + evicted from its slot
            _price_min_guard = float(getattr(self, "reserved_candidate_price_min_usdt", 0.0) or 0.0)
            if _price_min_guard > 0 and price > 0 and price < _price_min_guard:
                ctx.entry_state = "BLOCKED"
                self._log_blocked_throttled(
                    "ENTRY_BLOCKED", market=market, cause="price_below_min",
                    reason=f"price={price:.1f} < min={_price_min_guard:.0f} → slot evict",
                    cooldown_sec=60.0,
                )
                # slot eviction: DISABLED + context cleanup
                try:
                    self.oma_set_market(market, MarketState.DISABLED, reason=["price_below_min_evict"])
                    self.coordinator.remove_market(market)
                    self.ledger.append("PRICE_MIN_EVICT", market=market, price=price, min_price=_price_min_guard)
                    logger.info("[PriceMinGuard] %s evicted: price=%.1f < min=%.0f", market, price, _price_min_guard)
                except (KeyError, AttributeError, TypeError):
                    logger.debug("[PriceMinGuard] evict cleanup error: %s", market, exc_info=True)
                return

            # [FIX 2026-03-24] block buying low-liquidity coins: one-tick quote slippage >= 1%
            if price > 0:
                try:
                    from app.integrations.bybit_trade import get_tick_size
                    _tick = get_tick_size(market)
                    _tick_pct = (_tick / price) * 100.0
                    if _tick_pct >= 1.0:
                        ctx.entry_state = "BLOCKED"
                        self._log_blocked_throttled(
                            "ENTRY_BLOCKED", market=market, cause="tick_slippage_too_high",
                            reason=f"price={price:.1f} tick={_tick} tick_pct={_tick_pct:.1f}%",
                            cooldown_sec=300.0,
                        )
                        return
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[INTENT] tick_slippage_guard: %s", exc, exc_info=True)

            # [FIX 2026-03-24] block buying when indicator data is insufficient
            # Right after restart, if price_history is empty all strategy indicators are 0/default
            # force_ready() makes ready=True, but we must not buy without real data
            _min_data_ticks = int(getattr(self.coordinator, "min_ticks", 200) or 200)
            _cur_ticks = len(getattr(ctx, "price_history", []))
            if _cur_ticks < _min_data_ticks:
                ctx.entry_state = "BLOCKED"
                self._log_blocked_throttled(
                    "ENTRY_BLOCKED", market=market, cause="insufficient_indicator_data",
                    reason=f"ticks={_cur_ticks} < min={_min_data_ticks}",
                    cooldown_sec=30.0,
                )
                return

            usdt_amount = float(intent["buy_usdt"] or 0.0)

            # [2026-03-30] BTC Leading Signal: gate entries using Binance leading data
            # memory cache reads only — no HTTP calls
            try:
                from app.monitor.btc_leading_signal import get_btc_leading_detector
                _btc_det = get_btc_leading_detector()
                if _btc_det:
                    _strat_name_b = str(intent.get("reason", "")).split(":")[0].upper()
                    _action = _btc_det.get_strategy_action(_strat_name_b)
                    if isinstance(_action, dict):
                        if _action.get("action") == "halt":
                            ctx.entry_state = "BLOCKED"
                            self._log_blocked_throttled(
                                "ENTRY_BLOCKED", market=market, cause="btc_leading_halt",
                                reason=f"BTC leading signal halt for {_strat_name_b}",
                                cooldown_sec=30.0)
                            return
                        _size_mult = float(_action.get("size_mult", 1.0))
                        if _size_mult != 1.0:
                            usdt_amount *= _size_mult
            except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[INTENT] btc_leading_signal: %s", exc, exc_info=True)

            # [2026-03-30] time-of-day size adjustment: shrink during low-liquidity early-morning hours
            # pure computation based on time.localtime() — no HTTP
            try:
                from app.monitor.time_volatility_adjuster import get_time_volatility_multiplier
                _time_vol = get_time_volatility_multiplier()
                if _time_vol and _time_vol > 1.2:
                    _time_scale = max(0.5, 1.0 / _time_vol)
                    usdt_amount *= _time_scale
            except (ImportError, AttributeError, TypeError) as exc:
                logger.warning("[INTENT] time_volatility_adjuster: %s", exc)

            # PATCH 2025-12-26: wallet-mode => cap buy_usdt to usable_capital (no cross-subsidize)
            if self.wallet_mode:
                try:
                    avail = float(getattr(ctx, "usable_capital", 0.0) or 0.0)
                    if avail > 0.0 and float(usdt_amount) > avail:
                        usdt_amount = float(avail)
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[INTENT] wallet_mode_ledger: %s", exc)

            # [②] dynamic buy size: linear reduction based on portfolio PnL (in the -2% to -5% band)
            # ① and ② are mutually exclusive — both being ON is blocked in __init__ and _ui_apply_guard_settings
            # (if both ON, reduction stacks to 0.4 × 0.8 = 0.32x, risking unintended order shrinkage)
            if self.dynamic_size_mult_enabled and usdt_amount > 0:
                try:
                    _size_mult = self.portfolio_risk_manager.get_size_multiplier()
                    if _size_mult < 1.0:
                        usdt_amount *= _size_mult
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[INTENT] dynamic_size_mult: %s", exc, exc_info=True)

            # [①] regime-based strategy budget switching: apply budget_multiplier per BULL/BEAR/SIDEWAYS/VOLATILE
            if self.regime_per_strategy_enabled and getattr(self, "regime_enabled", False) and usdt_amount > 0:
                try:
                    if self._regime_strategy_manager is None:
                        from app.manager.regime_strategy import RegimeStrategyManager
                        self._regime_strategy_manager = RegimeStrategyManager()
                    _rm = self._regime_strategy_manager.get_strategy_mapping()  # 30s cache
                    _ctrls_r = getattr(ctx, "controls", {}) or {}
                    _strat_r = (_ctrls_r.get("strategy", {}) or {}) if isinstance(_ctrls_r, dict) else {}
                    _strat_name = str(_strat_r.get("mode") or _strat_r.get("name") or getattr(ctx, "strategy", "") or "").upper()
                    if _strat_name:
                        _sw = _rm.strategies.get(_strat_name)
                        if _sw and abs(_sw.budget_multiplier - 1.0) > 0.01:
                            usdt_amount *= _sw.budget_multiplier
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[INTENT] regime_strategy_budget: %s", exc, exc_info=True)

            # [NEWS] News Sentiment budget multiplier
            try:
                from app.core.news_sentiment import get_news_sentiment
                _ns = get_news_sentiment()
                if _ns.config.get("nunnaya_enabled"):
                    _ns_r = _ns.get_sentiment(coin=market.replace("USDT", "").upper())
                    if _ns_r and _ns_r.source != "fallback":
                        usdt_amount *= _ns_r.budget_multiplier
                        logger.debug("[INTENT] news budget mult: %.3f for %s",
                                     _ns_r.budget_multiplier, market)
            except Exception:
                pass

            # [2026-02-06] BTC Guard Mode - block buying (except CONTRARIAN)
            if self.btc_guard_mode:
                # [FIX 2026-03-05] ctx.strategy attribute is unreliable → read from the controls dict
                _ctrls = getattr(ctx, "controls", {}) or {}
                _strat_ctrl = _ctrls.get("strategy", {}) if isinstance(_ctrls, dict) else {}
                strategy = str(
                    _strat_ctrl.get("mode")
                    or _strat_ctrl.get("name")
                    or getattr(ctx, "strategy", "")
                ).upper()
                if strategy != "CONTRARIAN":
                    self.ledger.append(
                        "BUY_BLOCKED_BTC_GUARD",
                        market=market,
                        strategy=strategy,
                        reason=reason,
                        btc_guard_mode=True,
                    )
                    return  # block buying (except CONTRARIAN)

            if self.emergency_stop:
                try:
                    ctx.entry_block_reason = "emergency_stop"
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] emergency_stop_ledger: %s", exc2)
                ctx.entry_state = "BLOCKED"
                self._log_blocked_throttled(
                    "ENTRY_BLOCKED",
                    market=market,
                    cause="emergency_stop",
                    reason=reason,
                )
                return

            # manual mode (market isolation)
            if bool(((ctx.controls or {}).get("manual") or {}).get("enabled")):
                try:
                    ctx.entry_block_reason = "manual_mode"
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] manual_mode_buy_ledger: %s", exc2)
                ctx.entry_state = "BLOCKED"
                self._log_blocked_throttled(
                    "ENTRY_BLOCKED",
                    market=market,
                    cause="manual_mode",
                    reason=reason,
                )
                return

            # ----------------------------------------------------
            # RECOVERY / Dashboard per-market entry kill-switch / Global cooldown
            # ----------------------------------------------------
            # RECOVERY is the "no entry + allow withdrawal (SELL)" state.
            # - even if engine/strategy mistakenly emits a BUY intent, the System blocks it as a final gate.
            market_state = str(getattr(ctx, "market_state", "") or "").upper()
            
            if market_state == "RECOVERY" or bool(getattr(ctx, "recovery", False)):
                try:
                    ctx.entry_block_reason = "recovery_mode"
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] recovery_mode_ledger: %s", exc2)
                ctx.entry_state = "BLOCKED"
                self._log_blocked_throttled(
                    "ENTRY_BLOCKED",
                    market=market,
                    cause="recovery_mode",
                    reason=reason,
                )
                return

            # Dashboard per-market entry switch (ENTRY only, EXIT always allowed)
            # - This is a deliberate operator action: it should surface clearly.
            if not _g_bool("entry_enabled", True):
                try:
                    ctx.entry_block_reason = "entry_disabled_by_ui"
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] entry_disabled_ui_ledger: %s", exc2)
                ctx.entry_state = "BLOCKED"
                self._log_blocked_throttled(
                    "ENTRY_BLOCKED",
                    market=market,
                    cause="entry_disabled_by_ui",
                    reason=reason,
                )
                return

            # Pending order gate (prevents overlapping orders)
            if getattr(ctx, "order_state", None) is not None:
                try:
                    ctx.entry_block_reason = "order_pending"
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] pending_order_gate_ledger: %s", exc2)
                ctx.entry_state = "BLOCKED"
                self._log_blocked_throttled(
                    "ENTRY_BLOCKED",
                    market=market,
                    cause="order_pending",
                    reason=reason,
                )
                return

            # block BUY during a global cooldown (e.g. drawdown guard)
            g_until = float(getattr(self, "_global_entry_block_until_ts", 0.0) or 0.0)
            if g_until and time.time() < g_until:
                try:
                    cur = float(getattr(ctx, "entry_block_until_ts", 0.0) or 0.0)
                    ctx.entry_block_until_ts = max(cur, g_until)
                    ctx.entry_block_reason = "global_cooldown"
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] global_cooldown_ledger: %s", exc2)

                ctx.entry_state = "BLOCKED"
                global_reason = str(getattr(self, "_global_entry_block_reason", "") or "")
                self._log_blocked_throttled(
                    "ENTRY_BLOCKED",
                    market=market,
                    cause="global_cooldown",
                    reason=global_reason or reason,
                    cooldown_sec=self._cooldown_remaining(g_until),
                )
                return
            
            # ----------------------------------------------------
            # [TRIAGE MODE] block BUY — placed ahead of the PRM check
            # Even if PRM is_paused(-5%) fires at the same time, focus-market DCA must pass.
            # Strategy exemptions: CONTRARIAN (contrarian — a down market is the entry timing),
            #            SNIPER/WHALE (time-sensitive external signals — independent opportunities unrelated to the portfolio)
            # ----------------------------------------------------
            _triage_focus_bypass = False
            _tm_ref = getattr(self, "triage_manager", None)
            if getattr(self, "_triage_entry_blocked", False) and _tm_ref is not None:
                # strategy-exemption check
                # [FIX 2026-03-23] ctx.strategy is unreliable → prefer the controls dict, same as BTC Guard
                _ctrls_t = getattr(ctx, "controls", {}) or {}
                _strat_t = _ctrls_t.get("strategy", {}) if isinstance(_ctrls_t, dict) else {}
                _ctx_strategy = str(
                    _strat_t.get("mode")
                    or _strat_t.get("name")
                    or getattr(ctx, "strategy", "")
                    or intent.get("strategy", "")
                    or ""
                ).upper()
                _triage_exempt_strategies = set(
                    _tm_ref.settings.get("exempt_strategies", ["CONTRARIAN", "SNIPER", "WHALE"])
                )
                _is_strategy_exempt = _ctx_strategy in _triage_exempt_strategies

                # focus-market check (all active_targets — supports parallel recovery)
                _active_markets = set()
                for _at in getattr(_tm_ref, "active_targets", []):
                    if isinstance(_at, dict) and _at.get("market"):
                        _active_markets.add(_at["market"])
                _is_focus = (market in _active_markets)
                _focus_market = next(iter(_active_markets), None)  # for log/error messages
                _focus_dca_allow = _tm_ref.settings.get("focus_dca_allow", True)

                if _is_strategy_exempt:
                    # exempt strategy: pass without triage block (PRM check proceeds normally)
                    # but if DCA-reserved capital exists, block when available cash is insufficient
                    _reserved = float(getattr(self, "_triage_reserved_usdt", 0.0) or 0.0)
                    if _reserved > 0:
                        _cash = float(getattr(self, "_last_cash_usdt", 0.0) or 0.0)
                        # [FIX 2026-03-23] intent["buy_usdt"] was already extracted into usdt_amount
                        # the intent.get("usdt_amount") key is not a standard BUY key, so it was always 0
                        _buy_usdt = usdt_amount
                        if _buy_usdt > 0 and (_cash - _buy_usdt) < _reserved:
                            ctx.entry_state = "BLOCKED"
                            self._log_blocked_throttled(
                                "ENTRY_BLOCKED", market=market, cause="triage_capital_reserve",
                                reason=f"cash={_cash:.0f} - buy={_buy_usdt:.0f} < reserved={_reserved:.0f}",
                            )
                            return
                elif _is_focus and _focus_dca_allow:
                    # focus-market DCA: also skip the PRM block
                    _triage_focus_bypass = True
                else:
                    # non-focus market: handle according to buy_mode
                    _buy_mode = _tm_ref.settings.get("buy_mode", "block_all")
                    # [FIX 2026-03-23] based on initial_snapshot → updated to current loss coins
                    # a coin that newly turned to a loss during triage was absent from the snapshot, allowing allow_non_loss to be bypassed
                    try:
                        _cur_loss_list = _tm_ref._gather_loss_coins(self)
                        _loss_coins = {c["market"] for c in _cur_loss_list} if _cur_loss_list else set(_tm_ref.initial_snapshot.get("loss_coins", []))
                    except (AttributeError, TypeError, ValueError):
                        logger.warning("[%s] _gather_loss_coins failed, using snapshot fallback", market, exc_info=True)
                        _loss_coins = set(_tm_ref.initial_snapshot.get("loss_coins", []))
                    _is_loss_coin = market in _loss_coins
                    _reserved = float(getattr(self, "_triage_reserved_usdt", 0.0) or 0.0)
                    _cash = float(getattr(self, "_last_cash_usdt", 0.0) or 0.0)
                    # [FIX 2026-03-23] intent["buy_usdt"] was already extracted into usdt_amount
                    _buy_usdt = usdt_amount

                    if _buy_mode == "allow_non_loss" and not _is_loss_coin:
                        # Mode 2: non-loss coin — allowed after securing a cash buffer (PRM check proceeds normally)
                        if _buy_usdt > 0 and (_cash - _buy_usdt) < _reserved:
                            try:
                                ctx.entry_block_reason = "triage_capital_reserve"
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[INTENT] triage_mode2_non_loss: %s", exc, exc_info=True)
                            ctx.entry_state = "BLOCKED"
                            self._log_blocked_throttled(
                                "ENTRY_BLOCKED", market=market, cause="triage_capital_reserve",
                                reason=f"non_loss_buy: cash={_cash:.0f}-buy={_buy_usdt:.0f} < reserved={_reserved:.0f}",
                            )
                            return
                        # pass — continue PRM check
                    elif _is_loss_coin and _tm_ref.settings.get("opportunistic_dca", False):
                        # Mode 3: conditional immediate DCA on a loss coin — strategy already judged conditions favorable
                        if _buy_usdt > 0 and (_cash - _buy_usdt) < _reserved:
                            try:
                                ctx.entry_block_reason = "triage_capital_reserve"
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[INTENT] triage_mode3_dca: %s", exc, exc_info=True)
                            ctx.entry_state = "BLOCKED"
                            self._log_blocked_throttled(
                                "ENTRY_BLOCKED", market=market, cause="triage_capital_reserve",
                                reason=f"opportunistic_dca: cash={_cash:.0f}-buy={_buy_usdt:.0f} < reserved={_reserved:.0f}",
                            )
                            return
                        # allow PRM bypass (same as focus DCA)
                        _triage_focus_bypass = True
                    else:
                        # Mode 1 (block_all), or a loss coin under allow_non_loss: block BUY
                        try:
                            ctx.entry_block_reason = "triage_mode"
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[INTENT] triage_mode1_block: %s", exc, exc_info=True)
                        ctx.entry_state = "BLOCKED"
                        self._log_blocked_throttled(
                            "ENTRY_BLOCKED",
                            market=market,
                            cause="triage_mode",
                            reason=f"triage active (focus={_focus_market}, strategy={_ctx_strategy}, buy_mode={_buy_mode})",
                        )
                        return

            # ----------------------------------------------------
            # Portfolio Risk Manager Check (daily loss limit / Circuit Breaker)
            # triage focus markets skip the PRM block (allow recovery DCA)
            # ----------------------------------------------------
            if not _triage_focus_bypass:
                try:
                    can_enter, risk_reason = self.portfolio_risk_manager.can_enter_new_position()
                    if not can_enter:
                        try:
                            ctx.entry_block_reason = "portfolio_risk_guard"
                            ctx.entry_block_until_ts = time.time() + 60.0  # 1-min cooldown
                        except (AttributeError, TypeError, ValueError) as exc:
                            try:
                                self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                            except (AttributeError, TypeError, ValueError) as exc2:
                                logger.warning("[INTENT] portfolio_risk_guard_ledger: %s", exc2)

                        ctx.entry_state = "BLOCKED"
                        self._log_blocked_throttled(
                            "ENTRY_BLOCKED",
                            market=market,
                            cause="portfolio_risk_guard",
                            reason=risk_reason,
                            cooldown_sec=60.0,
                        )
                        return
                except (OSError, TypeError, ValueError, OverflowError) as exc:
                    logger.error(
                        "[RISK_GUARD] PRM check failed, BLOCKING entry for safety: %s",
                        exc, exc_info=True,
                    )
                    ctx.entry_state = "BLOCKED"
                    self._log_blocked_throttled(
                        "ENTRY_BLOCKED", market=market,
                        cause="portfolio_risk_guard_error",
                        reason=f"PRM check failed: {exc}",
                        cooldown_sec=60.0,
                    )
                    return

            # [③] single-coin concentration limit
            if self.concentration_limit_enabled and not _triage_focus_bypass:
                try:
                    equity = float(self._last_equity_usdt or 0.0)
                    _conc_price = float(price or 0.0)
                    # [FIX 2026-03-23] removed entry fallback when price=None
                    # with an entry(buy-price) fallback, a falling coin is overvalued → concentration limit blocks early
                    # a rising coin is undervalued → concentration limit can be bypassed. Directions oppose, so skip if no price.
                    if equity > 0 and _conc_price > 0:
                        pos = getattr(ctx, "position", None) or {}
                        pos_val = float(pos.get("qty", 0.0) or 0.0) * _conc_price
                        projected_pct = (pos_val + usdt_amount) / equity * 100.0
                        if projected_pct > self.concentration_limit_pct:
                            ctx.entry_state = "BLOCKED"
                            self._log_blocked_throttled(
                                "ENTRY_BLOCKED", market=market, cause="concentration_limit",
                                reason=f"projected={projected_pct:.1f}% > limit={self.concentration_limit_pct:.1f}%",
                                cooldown_sec=30.0,
                            )
                            return
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[INTENT] concentration_limit: %s", exc, exc_info=True)

            if ctx.entry_block_until_ts and time.time() < float(ctx.entry_block_until_ts):
                ctx.entry_state = "BLOCKED"
                cause = getattr(ctx, "entry_block_reason", None) or "entry_cooldown"
                try:
                    ctx.entry_block_reason = str(cause)
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] entry_cooldown_ledger: %s", exc2)
                self._log_blocked_throttled(
                    "ENTRY_BLOCKED",
                    market=market,
                    cause=str(cause),
                    reason=reason,
                    cooldown_sec=self._cooldown_remaining(ctx.entry_block_until_ts),
                )
                return

            # ---- GLOBAL ORDER PRESSURE GUARDS (BUY) ----
            # 1) pending-order cap across all markets (prevents API bursts)
            max_pending_total = int(getattr(self, "max_pending_orders_total", 0) or 0)
            if max_pending_total > 0:
                pending_total = 0
                for _m, _c in self.coordinator.contexts.items():
                    if getattr(_c, "order_state", None) is not None:
                        pending_total += 1
                if pending_total >= max_pending_total:
                    now_ts = time.time()
                    cd = 0.8
                    ctx.entry_state = "BLOCKED"
                    ctx.entry_block_until_ts = now_ts + cd
                    ctx.entry_block_reason = "max_pending_orders_total"
                    self._log_blocked_throttled(
                        "ENTRY_BLOCKED",
                        market=market,
                        cause="max_pending_orders_total",
                        reason=reason,
                        pending_total=pending_total,
                        max_pending_total=max_pending_total,
                        cooldown_sec=cd,
                    )
                    return

            # 2) global gap between BUY submissions (reduces simultaneous order delay)
            gap = float(getattr(self, "entry_global_gap_sec", 0.0) or 0.0)
            if gap > 0.0:
                last_ts = float(getattr(self, "_last_entry_submit_ts", 0.0) or 0.0)
                now_ts = time.time()
                dt = now_ts - last_ts
                if dt < gap:
                    cd = gap - dt
                    ctx.entry_state = "BLOCKED"
                    ctx.entry_block_until_ts = now_ts + cd
                    ctx.entry_block_reason = "entry_global_gap"
                    self._log_blocked_throttled(
                        "ENTRY_BLOCKED",
                        market=market,
                        cause="entry_global_gap",
                        reason=reason,
                        cooldown_sec=cd,
                    )
                    return

            if usdt_amount < float(self.min_order_usdt):
                try:
                    ctx.entry_block_reason = "min_order"
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] min_order_ledger: %s", exc2)
                ctx.entry_state = "BLOCKED"
                self._log_blocked_throttled(
                    "ENTRY_BLOCKED",
                    market=market,
                    cause="min_order",
                    reason=reason,
                    min_order_usdt=float(self.min_order_usdt),
                    requested_usdt=float(usdt_amount),
                )
                return

            intent_meta = intent.get("meta") or {}
            allow_add_buy_intent = bool(intent_meta.get("allow_add_buy", False))
            add_buy_reason_allowed = (
                reason.startswith("gazua:")
                or reason.startswith("sniper:confirm")
                or reason.startswith("sniper:dca")
                or reason.startswith("lightning:confirm")
                or reason.startswith("autoloop:")  # [FIX 2026-03-05] allow AUTOLOOP add-buy
            )

            # if already in a position, forbid re-entry (default policy)
            if ctx.position and float(ctx.position.get("qty", 0.0) or 0.0) > 0.0:
                # exception: only an engine-specified add-buy is allowed, with restrictions
                if allow_add_buy_intent and add_buy_reason_allowed:
                    pass
                else:
                    try:
                        ctx.entry_block_reason = "already_in_position"
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        try:
                            self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                        except (AttributeError, TypeError, ValueError) as exc2:
                            logger.warning("[INTENT] already_in_position_ledger: %s", exc2)
                    ctx.entry_state = "BLOCKED"
                    self._log_blocked_throttled(
                        "ENTRY_BLOCKED",
                        market=market,
                        cause="already_in_position",
                        reason=reason,
                        qty=float(ctx.position.get("qty", 0.0) or 0.0),
                    )
                    return

            # ★ Cross-strategy guard: block entry if FOCUS already holds this market
            try:
                from app.core.cross_strategy_guard import is_market_owned_by_other
                _cross_owner = is_market_owned_by_other(self, market, "NUNNAYA")
                if _cross_owner:
                    ctx.entry_state = "BLOCKED"
                    try:
                        ctx.entry_block_reason = "cross_strategy_focus"
                    except (AttributeError, TypeError, ValueError):
                        pass
                    self._log_blocked_throttled(
                        "ENTRY_BLOCKED",
                        market=market,
                        cause="cross_strategy_focus",
                        reason=f"FOCUS holds {market} (qty={_cross_owner.qty:.4f}, dir={_cross_owner.direction})",
                    )
                    return
            except Exception as exc:
                logger.debug("[INTENT] cross_strategy_guard: %s", exc)

            # ----------------------------------------------------
            # ENTRY CEILING GUARD: re-entry price ceiling in down/non-bull markets
            # ----------------------------------------------------
            # At any price not sufficiently cheaper than the previous FULL EXIT average (last_exit_price),
            # block re-entry (BUY) to prevent the "buy back at a higher price" negative-margin loop.
            #
            # - if the market regime is BULL, relax (default: do not block)
            # - if the regime is BEAR or NEUTRAL, block BUYs above ceiling_price (default: NON_BULL)
            ce_on = _g_bool("entry_ceiling_guard", bool(getattr(self, "entry_ceiling_guard", False)))
            if ce_on and expected_price is not None:
                try:
                    lep = getattr(ctx, "last_exit_price", None)
                    lep_f = float(lep) if lep is not None else 0.0
                except (TypeError, ValueError):
                    logger.warning("[%s] last_exit_price conversion failed", market, exc_info=True)
                    lep_f = 0.0

                if lep_f > 0.0:
                    regime, change_pct = self._infer_market_regime(ctx=ctx, price=float(expected_price))

                    mode = str(_g_str("entry_ceiling_apply", str(getattr(self, "entry_ceiling_apply", "NON_BULL") or "NON_BULL"))).strip().upper() or "NON_BULL"
                    if mode not in ("BEAR", "NON_BULL", "ALWAYS"):
                        mode = str(getattr(self, "entry_ceiling_apply", "NON_BULL") or "NON_BULL").strip().upper() or "NON_BULL"
                    apply_guard = (
                        (mode == "ALWAYS")
                        or (mode == "NON_BULL" and str(regime) != "BULL")
                        or (mode == "BEAR" and str(regime) == "BEAR")
                    )

                    # last_exit age (for max_age / decay)
                    last_exit_ts = 0.0
                    age_sec: Optional[float] = None
                    try:
                        last_exit_ts = float(getattr(ctx, "last_exit_ts", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        logger.warning("[%s] last_exit_ts conversion failed", market, exc_info=True)
                        last_exit_ts = 0.0
                    if last_exit_ts > 0.0:
                        try:
                            age_sec = float(max(0.0, time.time() - float(last_exit_ts)))
                        except (TypeError, ValueError):
                            logger.warning("[%s] age_sec calculation failed", market, exc_info=True)
                            age_sec = None

                    # Optional max-age: expire last_exit reference after N seconds (prevents stale ceiling blocks)
                    max_age_sec = float(_g_float("entry_ceiling_max_age_sec", float(getattr(self, "entry_ceiling_max_age_sec", 0.0) or 0.0)) or 0.0)
                    if apply_guard and max_age_sec > 0.0 and age_sec is not None and age_sec > max_age_sec:
                        apply_guard = False

                    # Force guard if exit was very recent (even if BULL)
                    force_bull_sec = float(_g_float("entry_ceiling_force_on_bull_sec", float(getattr(self, "entry_ceiling_force_on_bull_sec", 0.0) or 0.0)))
                    if (not apply_guard) and force_bull_sec > 0.0 and age_sec is not None and age_sec < force_bull_sec:
                        # If mode was NON_BULL/BEAR and we skipped because of BULL, but it's too recent -> Force ON
                        if mode != "ALWAYS":
                            apply_guard = True

                    if apply_guard:
                        ceiling_base = self._calc_entry_ceiling_price(last_exit_price=float(lep_f), overrides=guards_ctl)
                        ceiling_price = ceiling_base

                        # Optional: decay (relax) ceiling within the max-age window
                        # - LINEAR: interpolate strict ceiling -> last_exit_price over max_age
                        # - EXP: exponential-like curve (normalized to reach 100% at max_age)
                        decay_mode = str(_g_str("entry_ceiling_decay_mode", str(getattr(self, "entry_ceiling_decay_mode", "LINEAR") or "LINEAR")) or "LINEAR").strip().upper()
                        if decay_mode in ("OFF", "FALSE", "0"):
                            decay_mode = "NONE"
                        if decay_mode not in ("NONE", "LINEAR", "EXP"):
                            decay_mode = str(getattr(self, "entry_ceiling_decay_mode", "LINEAR") or "LINEAR").strip().upper() or "LINEAR"
                            if decay_mode not in ("NONE", "LINEAR", "EXP"):
                                decay_mode = "LINEAR"

                        decay_progress = 0.0
                        decay_half_life_used = None
                        if (
                            ceiling_base is not None
                            and max_age_sec > 0.0
                            and age_sec is not None
                            and 0.0 <= age_sec <= max_age_sec
                            and decay_mode in ("LINEAR", "EXP")
                        ):
                            try:
                                if decay_mode == "LINEAR":
                                    decay_progress = float(age_sec) / float(max_age_sec) if max_age_sec > 0.0 else 0.0
                                else:
                                    hl = float(_g_float(
                                        "entry_ceiling_decay_half_life_sec",
                                        float(getattr(self, "entry_ceiling_decay_half_life_sec", 0.0) or 0.0),
                                    ) or 0.0)
                                    if hl <= 0.0:
                                        hl = float(max_age_sec) / 2.0
                                    hl = max(1e-9, float(hl))
                                    decay_half_life_used = float(hl)

                                    # raw(t) = 1 - 0.5^(t/hl)
                                    raw = 1.0 - pow(0.5, float(age_sec) / float(hl))
                                    rawT = 1.0 - pow(0.5, float(max_age_sec) / float(hl))
                                    if rawT > 0.0:
                                        decay_progress = float(raw) / float(rawT)
                                    else:
                                        decay_progress = 0.0
                            except (OverflowError, TypeError, ValueError):
                                logger.warning("[%s] decay_progress calculation failed", market, exc_info=True)
                                decay_progress = 0.0

                            # clamp 0..1
                            if decay_progress < 0.0:
                                decay_progress = 0.0
                            if decay_progress > 1.0:
                                decay_progress = 1.0

                            # interpolate ceiling_base -> last_exit_price
                            try:
                                ceiling_price = float(ceiling_base) + (float(lep_f) - float(ceiling_base)) * float(decay_progress)
                            except (TypeError, ValueError):
                                logger.warning("[%s] ceiling_price interpolation failed", market, exc_info=True)
                                ceiling_price = ceiling_base

                        if ceiling_price is not None and float(expected_price) > float(ceiling_price):
                            # small cooldown (mitigate spam / infinite loop)
                            try:
                                cd = float(_g_float("entry_ceiling_cooldown_sec", float(getattr(self, "entry_ceiling_cooldown_sec", 2.0) or 2.0)))
                            except (TypeError, ValueError):
                                logger.warning("[%s] entry_ceiling_cooldown_sec conversion failed", market, exc_info=True)
                                cd = 2.0

                            if cd and cd > 0.0:
                                now = time.time()
                                try:
                                    cur = float(getattr(ctx, "entry_block_until_ts", 0.0) or 0.0)
                                except (TypeError, ValueError):
                                    logger.warning("[%s] entry_block_until_ts read failed (ceiling)", market, exc_info=True)
                                    cur = 0.0
                                try:
                                    ctx.entry_block_until_ts = max(cur, now + float(cd))
                                    ctx.entry_block_reason = "entry_ceiling_guard"
                                except (TypeError, ValueError) as exc:
                                    logger.warning("[INTENT] entry_ceiling_cooldown: %s", exc)

                            ctx.entry_state = "BLOCKED"
                            self._log_blocked_throttled(
                                "ENTRY_BLOCKED",
                                market=market,
                                cause="entry_ceiling_guard",
                                reason=reason,
                                expected_price=float(expected_price),
                                last_exit_price=float(lep_f),
                                last_exit_ts=float(last_exit_ts or 0.0),
                                last_exit_age_sec=float(age_sec or 0.0),
                                max_age_sec=float(_g_float("entry_ceiling_max_age_sec", float(getattr(self, "entry_ceiling_max_age_sec", 0.0) or 0.0)) or 0.0),
                                ceiling_base=(float(ceiling_base) if ceiling_base is not None else None),
                                ceiling_price=float(ceiling_price),
                                ceiling_decay_mode=str(decay_mode),
                                ceiling_decay_progress=float(decay_progress) if decay_progress is not None else 0.0,
                                ceiling_decay_half_life_sec=(float(decay_half_life_used) if decay_half_life_used is not None else None),
                                regime=str(regime),
                                regime_change_pct=float(change_pct) if change_pct is not None else None,
                                fee_rate=float(_g_float("entry_ceiling_fee_rate", float(getattr(self, "entry_ceiling_fee_rate", 0.0) or 0.0))),
                                slippage_guard_bps=float(_g_float("entry_ceiling_slippage_guard_bps", float(getattr(self, "entry_ceiling_slippage_guard_bps", 0.0) or 0.0))),
                                spread_guard_bps=float(_g_float("entry_ceiling_spread_guard_bps", float(getattr(self, "entry_ceiling_spread_guard_bps", 0.0) or 0.0))),
                                extra_bps=float(_g_float("entry_ceiling_extra_bps", float(getattr(self, "entry_ceiling_extra_bps", 0.0) or 0.0))),
                                cooldown_sec=float(cd) if cd else 0.0,
                            )
                            return
                    
                    # Logging for visibility: If we skipped the guard due to regime, but price was high
                    elif (not apply_guard) and lep_f > 0.0:
                        # Calculate what ceiling WOULD have been
                        c_test = self._calc_entry_ceiling_price(last_exit_price=float(lep_f), overrides=guards_ctl)
                        if c_test is not None and float(expected_price) > float(c_test):
                            self._log_blocked_throttled(
                                "ENTRY_CEILING_SKIPPED",
                                market=market,
                                cause=f"regime_{str(regime).lower()}",
                                reason=reason,
                                expected_price=float(expected_price),
                                last_exit_price=float(lep_f),
                                ceiling_would_be=float(c_test),
                                age_sec=float(age_sec) if age_sec is not None else None
                            )
                # lep_f <= 0 → first entry / unknown → skip guard
            # ----------------------------------------------------


            # ----------------------------------------------------
            # ENTRY RECENT-HIGH GUARD: block chase-buying near the last N-hour high
            # ----------------------------------------------------
            # - apply mode:
            #   ALWAYS / NON_BULL / BEAR
            # - near threshold:
            #   expected_price >= recent_high * (1 - near_pct/100)
            # - breakout escape:
            #   if it's a genuine breakout (margin/regime/spread conditions), lift the block
            rh_on = _g_bool("entry_recent_high_guard", bool(getattr(self, "entry_recent_high_guard", False)))
            if rh_on and expected_price is not None and float(expected_price) > 0.0:
                regime, change_pct = self._infer_market_regime(ctx=ctx, price=float(expected_price))

                rh_mode = str(
                    _g_str(
                        "entry_recent_high_apply",
                        str(getattr(self, "entry_recent_high_apply", "NON_BULL") or "NON_BULL"),
                    )
                ).strip().upper() or "NON_BULL"
                if rh_mode not in ("BEAR", "NON_BULL", "ALWAYS"):
                    rh_mode = str(getattr(self, "entry_recent_high_apply", "NON_BULL") or "NON_BULL").strip().upper() or "NON_BULL"

                apply_rh_guard = (
                    (rh_mode == "ALWAYS")
                    or (rh_mode == "NON_BULL" and str(regime) != "BULL")
                    or (rh_mode == "BEAR" and str(regime) == "BEAR")
                )

                if apply_rh_guard:
                    lookback_hours = float(
                        _g_float(
                            "entry_recent_high_lookback_hours",
                            float(getattr(self, "entry_recent_high_lookback_hours", 24.0) or 24.0),
                        ) or 0.0
                    )
                    near_pct = float(
                        _g_float(
                            "entry_recent_high_near_pct",
                            float(getattr(self, "entry_recent_high_near_pct", 0.8) or 0.8),
                        ) or 0.0
                    )
                    candle_unit_min = int(
                        _g_int(
                            "entry_recent_high_candle_unit_min",
                            int(getattr(self, "entry_recent_high_candle_unit_min", 15) or 15),
                        ) or 15
                    )
                    cache_sec = float(
                        _g_float(
                            "entry_recent_high_cache_sec",
                            float(getattr(self, "entry_recent_high_cache_sec", 30.0) or 30.0),
                        ) or 0.0
                    )

                    lookback_hours = max(0.01, float(lookback_hours))
                    near_pct = max(0.0, float(near_pct))
                    candle_unit_min = max(1, int(candle_unit_min))
                    cache_sec = max(1.0, float(cache_sec))

                    recent_high, recent_high_source = self._get_recent_high_price(
                        market=market,
                        ctx=ctx,
                        lookback_hours=lookback_hours,
                        candle_unit_min=candle_unit_min,
                        cache_sec=cache_sec,
                    )

                    if recent_high is not None and float(recent_high) > 0.0:
                        near_floor = float(recent_high) * (1.0 - (float(near_pct) / 100.0))
                        is_near_high = float(expected_price) >= float(near_floor)

                        if is_near_high:
                            breakout_allowed = False
                            breakout_spread_bps: Optional[float] = None

                            if _g_bool(
                                "entry_recent_high_breakout_enabled",
                                bool(getattr(self, "entry_recent_high_breakout_enabled", True)),
                            ):
                                bo_margin_pct = max(
                                    0.0,
                                    float(
                                        _g_float(
                                            "entry_recent_high_breakout_margin_pct",
                                            float(getattr(self, "entry_recent_high_breakout_margin_pct", 0.25) or 0.25),
                                        ) or 0.0
                                    ),
                                )
                                bo_require_bull = _g_bool(
                                    "entry_recent_high_breakout_require_bull",
                                    bool(getattr(self, "entry_recent_high_breakout_require_bull", True)),
                                )
                                bo_min_regime_change_pct = max(
                                    0.0,
                                    float(
                                        _g_float(
                                            "entry_recent_high_breakout_min_regime_change_pct",
                                            float(
                                                getattr(self, "entry_recent_high_breakout_min_regime_change_pct", 0.35)
                                                or 0.35
                                            ),
                                        ) or 0.0
                                    ),
                                )
                                bo_max_spread_bps = max(
                                    0.0,
                                    float(
                                        _g_float(
                                            "entry_recent_high_breakout_max_spread_bps",
                                            float(getattr(self, "entry_recent_high_breakout_max_spread_bps", 18.0) or 18.0),
                                        ) or 0.0
                                    ),
                                )

                                cond_price = float(expected_price) >= (float(recent_high) * (1.0 + bo_margin_pct / 100.0))
                                cond_regime = (not bo_require_bull) or (str(regime) == "BULL")
                                cond_change = True if bo_min_regime_change_pct <= 0.0 else (
                                    (change_pct is not None) and (float(change_pct) >= bo_min_regime_change_pct)
                                )

                                cond_spread = True
                                if bo_max_spread_bps > 0.0:
                                    cond_spread = False
                                    try:
                                        ob = orderbook_store.get(market)
                                        if isinstance(ob, dict):
                                            bid = float(ob.get("best_bid") or 0.0)
                                            ask = float(ob.get("best_ask") or 0.0)
                                            if bid > 0.0 and ask > 0.0 and ask >= bid:
                                                mid = (bid + ask) / 2.0
                                                if mid > 0.0:
                                                    breakout_spread_bps = ((ask - bid) / mid) * 10000.0
                                                    cond_spread = float(breakout_spread_bps) <= bo_max_spread_bps
                                    except (AttributeError, TypeError, ValueError):
                                        logger.warning("[%s] breakout spread calculation failed", market, exc_info=True)
                                        cond_spread = False

                                breakout_allowed = bool(cond_price and cond_regime and cond_change and cond_spread)

                            if breakout_allowed:
                                self._log_blocked_throttled(
                                    "ENTRY_RECENT_HIGH_SKIPPED",
                                    market=market,
                                    cause="entry_recent_high_breakout",
                                    reason=reason,
                                    expected_price=float(expected_price),
                                    recent_high=float(recent_high),
                                    near_pct=float(near_pct),
                                    regime=str(regime),
                                    regime_change_pct=float(change_pct) if change_pct is not None else None,
                                    spread_bps=float(breakout_spread_bps) if breakout_spread_bps is not None else None,
                                )
                            else:
                                try:
                                    cd = float(
                                        _g_float(
                                            "entry_recent_high_cooldown_sec",
                                            float(getattr(self, "entry_recent_high_cooldown_sec", 10.0) or 10.0),
                                        ) or 0.0
                                    )
                                except (TypeError, ValueError):
                                    logger.warning("[%s] entry_recent_high_cooldown_sec conversion failed", market, exc_info=True)
                                    cd = 10.0

                                if cd > 0.0:
                                    now = time.time()
                                    try:
                                        cur = float(getattr(ctx, "entry_block_until_ts", 0.0) or 0.0)
                                    except (TypeError, ValueError):
                                        logger.warning("[%s] entry_block_until_ts read failed (recent_high)", market, exc_info=True)
                                        cur = 0.0
                                    try:
                                        ctx.entry_block_until_ts = max(cur, now + float(cd))
                                        ctx.entry_block_reason = "entry_recent_high_guard"
                                    except (TypeError, ValueError) as exc:
                                        logger.warning("[INTENT] recent_high_cooldown: %s", exc)

                                ctx.entry_state = "BLOCKED"
                                self._log_blocked_throttled(
                                    "ENTRY_BLOCKED",
                                    market=market,
                                    cause="entry_recent_high_guard",
                                    reason=reason,
                                    expected_price=float(expected_price),
                                    recent_high=float(recent_high),
                                    recent_high_source=str(recent_high_source),
                                    near_pct=float(near_pct),
                                    near_floor=float(near_floor),
                                    lookback_hours=float(lookback_hours),
                                    candle_unit_min=int(candle_unit_min),
                                    regime=str(regime),
                                    regime_change_pct=float(change_pct) if change_pct is not None else None,
                                    cooldown_sec=float(cd) if cd else 0.0,
                                )
                                return
            # ----------------------------------------------------


            # ----------------------------------------------------
            # ENTRY QTY GUARD (prevent low-price × high-principal negative margin)
            # ----------------------------------------------------
            # - qty_est = buy_usdt / expected_price
            # - if qty_est is excessive, it scrapes deep into the orderbook, raising slippage/partial-fill risk.
            qty_on = _g_bool("entry_qty_guard", bool(getattr(self, "entry_qty_guard", False)))
            if qty_on and expected_price is not None and float(expected_price) > 0.0:
                try:
                    max_qty = float(_g_float("entry_max_qty", float(getattr(self, "entry_max_qty", 0.0) or 0.0)))
                except (TypeError, ValueError):
                    logger.warning("[%s] entry_max_qty conversion failed", market, exc_info=True)
                    max_qty = 0.0

                if max_qty > 0.0:
                    qty_est = float(usdt_amount) / float(expected_price)

                    if qty_est > max_qty:
                        # apply cooldown (prevent consecutive-signal spam)
                        try:
                            cd = float(_g_float("entry_qty_cooldown_sec", float(getattr(self, "entry_qty_cooldown_sec", 2.0) or 0.0)))
                        except (TypeError, ValueError):
                            logger.warning("[%s] entry_qty_cooldown_sec conversion failed", market, exc_info=True)
                            cd = 2.0

                        if cd and cd > 0.0:
                            now_ts = time.time()
                            try:
                                cur_until = float(getattr(ctx, "entry_block_until_ts", 0.0) or 0.0)
                            except (TypeError, ValueError):
                                logger.warning("[%s] entry_block_until_ts read failed (qty_guard)", market, exc_info=True)
                                cur_until = 0.0

                            try:
                                ctx.entry_block_until_ts = max(cur_until, now_ts + float(cd))
                                ctx.entry_block_reason = "entry_qty_guard"
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[INTENT] qty_guard_cooldown: %s", exc)

                        ctx.entry_state = "BLOCKED"
                        min_price_required = float(usdt_amount) / float(max_qty) if max_qty > 0.0 else None

                        self._log_blocked_throttled(
                            "ENTRY_BLOCKED",
                            market=market,
                            cause="entry_qty_guard",
                            reason=reason,
                            expected_price=float(expected_price),
                            usdt_amount=float(usdt_amount),
                            qty_est=float(qty_est),
                            max_qty=float(max_qty),
                            min_price_required=float(min_price_required) if min_price_required is not None else None,
                            cooldown_sec=float(cd) if cd else 0.0,
                        )
                        return


            # PATCH 2025-12-26: orderbook spread/depth guard (ENTRY)
            # - supports per-market overrides via ctx.controls.guards
            ob_on = _g_bool("entry_ob_guard_enabled", bool(getattr(self, "entry_ob_guard_enabled", False)))
            if ob_on and float(usdt_amount) > 0.0:
                ob = orderbook_store.get(market)
                if not ob:
                    try:
                        ctx.entry_block_reason = "orderbook_guard_missing"
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[INTENT] ob_guard_missing: %s", exc, exc_info=True)
                    ctx.entry_state = "BLOCKED"
                    self._log_blocked_throttled(
                        "ENTRY_BLOCKED",
                        market=market,
                        cause="orderbook_guard_missing",
                        buy_usdt=float(usdt_amount),
                        expected_price=float(expected_price),
                        reason=reason,
                    )
                    return

                try:
                    ob_ts = float(ob.get("ts") or 0.0)
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[%s] orderbook ts conversion failed", market, exc_info=True)
                    ob_ts = 0.0

                stale_sec = (float(now_ts) - ob_ts) if ob_ts > 0.0 else None
                stale_limit_sec = float(_g_float("entry_ob_stale_sec", float(getattr(self, "entry_ob_stale_sec", 0.0) or 0.0)))
                if stale_limit_sec > 0.0 and (stale_sec is None or float(stale_sec) > stale_limit_sec):
                    try:
                        ctx.entry_block_reason = "orderbook_guard_stale"
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[INTENT] ob_guard_stale: %s", exc)
                    ctx.entry_state = "BLOCKED"
                    self._apply_ob_block_cooldown(ctx, market, "orderbook_guard_stale")
                    self._log_blocked_throttled(
                        "ENTRY_BLOCKED",
                        market=market,
                        cause="orderbook_guard_stale",
                        buy_usdt=float(usdt_amount),
                        expected_price=float(expected_price),
                        reason=reason,
                        ob_stale_sec=float(stale_sec) if stale_sec is not None else None,
                        ob_stale_limit_sec=float(stale_limit_sec),
                    )
                    return

                try:
                    best_bid = float(ob.get("best_bid") or 0.0)
                    best_ask = float(ob.get("best_ask") or 0.0)
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[%s] best_bid/best_ask conversion failed", market, exc_info=True)
                    best_bid, best_ask = 0.0, 0.0

                if best_bid <= 0.0 or best_ask <= 0.0:
                    try:
                        ctx.entry_block_reason = "orderbook_guard_bad_top"
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[INTENT] ob_guard_bad_top: %s", exc)
                    ctx.entry_state = "BLOCKED"
                    self._log_blocked_throttled(
                        "ENTRY_BLOCKED",
                        market=market,
                        cause="orderbook_guard_bad_top",
                        buy_usdt=float(usdt_amount),
                        expected_price=float(expected_price),
                        reason=reason,
                        best_bid=float(best_bid),
                        best_ask=float(best_ask),
                    )
                    return

                mid = (best_bid + best_ask) / 2.0
                spread_bps = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 else 999999.0
                max_spread_bps = float(_g_float("entry_ob_max_spread_bps", float(getattr(self, "entry_ob_max_spread_bps", 0.0) or 0.0)))
                if max_spread_bps > 0.0 and float(spread_bps) > max_spread_bps:
                    try:
                        ctx.entry_block_reason = "orderbook_guard_spread"
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("[INTENT] ob_guard_spread: %s", exc)
                    ctx.entry_state = "BLOCKED"
                    self._apply_ob_block_cooldown(ctx, market, "orderbook_guard_spread")
                    self._log_blocked_throttled(
                        "ENTRY_BLOCKED",
                        market=market,
                        cause="orderbook_guard_spread",
                        buy_usdt=float(usdt_amount),
                        expected_price=float(expected_price),
                        reason=reason,
                        best_bid=float(best_bid),
                        best_ask=float(best_ask),
                        spread_bps=float(spread_bps),
                        max_spread_bps=float(max_spread_bps),
                    )
                    return

                units = ob.get("units") or []
                depth_bps = float(_g_float("entry_ob_depth_bps", float(getattr(self, "entry_ob_depth_bps", 0.0) or 0.0)))
                depth_factor = float(_g_float("entry_ob_depth_factor", float(getattr(self, "entry_ob_depth_factor", 0.0) or 0.0)))

                # If parameters are misconfigured, skip depth guard rather than blocking everything.
                if depth_bps <= 0.0 or depth_factor <= 0.0:
                    units = []

                ask_lim = best_ask * (1.0 + depth_bps / 10000.0) if depth_bps > 0.0 else best_ask
                bid_lim = best_bid * (1.0 - depth_bps / 10000.0) if depth_bps > 0.0 else best_bid

                ask_notional = 0.0
                bid_notional = 0.0
                for u in units:
                    try:
                        ap = float(u.get("ask_price") or 0.0)
                        asz = float(u.get("ask_size") or 0.0)
                        bp = float(u.get("bid_price") or 0.0)
                        bsz = float(u.get("bid_size") or 0.0)
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[INTENT] ob_depth_unit_parse: %s", exc)
                        continue

                    if ap > 0.0 and asz > 0.0 and ap <= ask_lim:
                        ask_notional += ap * asz
                    if bp > 0.0 and bsz > 0.0 and bp >= bid_lim:
                        bid_notional += bp * bsz

                required_notional = float(usdt_amount) * float(depth_factor) if depth_factor > 0.0 else 0.0
                if units and required_notional > 0.0 and (ask_notional < required_notional or bid_notional < required_notional):
                    try:
                        ctx.entry_block_reason = "orderbook_guard_depth"
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[INTENT] ob_guard_depth: %s", exc)
                    ctx.entry_state = "BLOCKED"
                    self._log_blocked_throttled(
                        "ENTRY_BLOCKED",
                        market=market,
                        cause="orderbook_guard_depth",
                        buy_usdt=float(usdt_amount),
                        expected_price=float(expected_price),
                        reason=reason,
                        best_bid=float(best_bid),
                        best_ask=float(best_ask),
                        spread_bps=float(spread_bps),
                        depth_bps=float(depth_bps),
                        ask_notional_usdt=float(ask_notional),
                        bid_notional_usdt=float(bid_notional),
                        required_notional_usdt=float(required_notional),
                        depth_factor=float(depth_factor),
                    )
                    return

            # record global BUY submission time (used by OMA_ENTRY_GLOBAL_GAP_SEC)
            self._last_entry_submit_ts = time.time()

            # PATCH 2026-01-31: support SNIPER use_limit entry
            sniper_use_limit_entry = bool((meta or {}).get("use_limit", False))
            sniper_fallback_entry = bool((meta or {}).get("fallback_to_market", True))
            is_sniper_buy = reason and "sniper:" in reason.lower()

            # PATCH 2026-01: limit-order entry option
            entry_limit_enabled = _g_bool("entry_limit_buy_enabled", bool(getattr(self, "entry_limit_buy_enabled", False)))
            
            # sniper:probe / sniper:dca — prefer limit (best_bid), fallback to market after timeout
            # fill rate 12.6% → goal is to reduce slippage; use best_bid to lower slippage,
            # but fall back to market if unfilled to avoid missed opportunity
            _is_probe_or_dca = is_sniper_buy and reason and (
                reason.startswith("sniper:probe")
                or reason.startswith("sniper:dca")
            )
            if _is_probe_or_dca and not sniper_use_limit_entry:
                ob = orderbook_store.get(market)
                _probe_limit_price = 0.0
                if ob and isinstance(ob, dict):
                    _probe_limit_price = float(ob.get("best_bid") or 0.0)
                if _probe_limit_price > 0.0:
                    _probe_timeout = float(getattr(self, "sniper_probe_limit_timeout_sec", 5.0) or 5.0)
                    ok, msg = self.order_fsm.submit_limit_buy(
                        ctx=ctx,
                        market=market,
                        usdt_amount=float(usdt_amount),
                        limit_price=_probe_limit_price,
                        reason=(reason or "sniper:probe") + ":limit_bid",
                        attempts=1,
                        max_retries=1,
                        timeout_sec=_probe_timeout,
                    )
                else:
                    ok, msg = self.order_fsm.submit_market_buy(
                        ctx=ctx,
                        market=market,
                        usdt_amount=float(usdt_amount),
                        expected_price=expected_price,
                        reason=(reason or "sniper:probe") + ":market_no_bid",
                    )
            elif is_sniper_buy and sniper_use_limit_entry:
                # SNIPER limit entry: based on best_ask
                ob = orderbook_store.get(market)
                if not ob or not isinstance(ob, dict):
                    ok, msg = self.order_fsm.submit_market_buy(
                        ctx=ctx,
                        market=market,
                        usdt_amount=float(usdt_amount),
                        expected_price=expected_price,
                        reason=reason or "sniper:market_buy:no_ob",
                    )
                else:
                    sniper_limit_price = float(ob.get("best_ask") or 0)
                    if sniper_limit_price <= 0:
                        ok, msg = self.order_fsm.submit_market_buy(
                            ctx=ctx,
                            market=market,
                            usdt_amount=float(usdt_amount),
                            expected_price=expected_price,
                            reason=reason or "sniper:market_buy:no_ask",
                        )
                    else:
                        # attempt limit entry (fallback after timeout)
                        timeout_sec = float(getattr(self, "sniper_limit_timeout_sec", 3.0) or 3.0)
                        ok, msg = self.order_fsm.submit_limit_buy(
                            ctx=ctx,
                            market=market,
                            usdt_amount=float(usdt_amount),
                            limit_price=float(sniper_limit_price),
                            reason=reason or "sniper:limit_buy",
                            attempts=1,
                            max_retries=1 if sniper_fallback_entry else 0,
                            timeout_sec=timeout_sec,
                        )
            elif entry_limit_enabled:
                # limit entry: determine limit price from best_bid/best_ask
                ob = orderbook_store.get(market)
                if not ob or not isinstance(ob, dict):
                    # no orderbook → fallback to market
                    ok, msg = self.order_fsm.submit_market_buy(
                        ctx=ctx,
                        market=market,
                        usdt_amount=float(usdt_amount),
                        expected_price=expected_price,
                        reason=reason or "engine_buy:limit_fallback_no_ob",
                    )
                else:
                    price_mode = str(getattr(self, "entry_limit_price_mode", "best_bid") or "best_bid").lower()
                    if price_mode == "best_ask":
                        limit_price = float(ob.get("best_ask") or 0)
                    else:
                        limit_price = float(ob.get("best_bid") or 0)
                    
                    if limit_price <= 0:
                        # no price → fallback to market
                        ok, msg = self.order_fsm.submit_market_buy(
                            ctx=ctx,
                            market=market,
                            usdt_amount=float(usdt_amount),
                            expected_price=expected_price,
                            reason=reason or "engine_buy:limit_fallback_no_price",
                        )
                    else:
                        # limit entry
                        timeout_sec = float(getattr(self, "entry_limit_timeout_sec", 5.0) or 5.0)
                        ok, msg = self.order_fsm.submit_limit_buy(
                            ctx=ctx,
                            market=market,
                            usdt_amount=float(usdt_amount),
                            limit_price=float(limit_price),
                            reason=reason or "engine_buy:limit",
                            attempts=1,
                            max_retries=0,  # no retry if unfilled
                            timeout_sec=timeout_sec,
                        )
            else:
                # existing market entry
                ok, msg = self.order_fsm.submit_market_buy(
                    ctx=ctx,
                    market=market,
                    usdt_amount=float(usdt_amount),
                    expected_price=expected_price,
                    reason=reason or "engine_buy",
                )

            # WARN payload (order succeeded but adjusted)
            warn: Optional[str] = None
            if ok and isinstance(msg, str) and "|WARN:" in msg:
                msg, warn_payload = msg.split("|WARN:", 1)
                warn = "WARN:" + warn_payload

            if not ok:
                if isinstance(msg, str) and msg.startswith("SOFT:"):
                    # expected/handled condition (insufficient funds, etc.)
                    ctx.entry_state = "SOFTFAIL"
                    self._notify_soft_once(market=market, intent="BUY", msg=msg)
                    return

                ctx.entry_state = "FAILED"
                self._log_blocked_throttled(
                    "ENTRY_SUBMIT_FAILED",
                    market=market,
                    cause="submission_failed",
                    reason=reason,
                    error=str(msg),
                )
                return

            # OK
            ctx.entry_state = "ORDER_PLACED"
            self._reset_ob_block_streak(market)
            try:
                ctx.entry_block_reason = None
            except (AttributeError, TypeError, ValueError) as exc:
                logger.error("[INTENT] buy_order_placed_clear: %s", exc, exc_info=True)
            self.ledger.append(
                "ORDER_ACK",
                market=market,
                uuid=str(msg),
                state="wait",
                side="bid",
            )
            if warn:
                self._notify_soft_once(market=market, intent="BUY", msg=warn)

            # --------------------------------------------------------
            # flush context_state.json immediately right after a successful DCA order
            # — prevents dca_count loss if a crash happens within the 10s periodic-save gap
            # --------------------------------------------------------
            _is_dca_buy = (
                reason.startswith("sniper:dca")
                or reason.startswith("gazua:dca")
            )
            if _is_dca_buy:
                try:
                    self._save_context_state()
                    self._last_context_save_ts = time.time()
                except (OSError, TypeError, ValueError, OverflowError) as exc:
                    logger.warning("[INTENT] dca_context_save: %s", exc)

            # --------------------------------------------------------
            # PATCH: Ladder Strategy Notification (BUY)
            # --------------------------------------------------------
            if reason.startswith("ladder:"):
                 self._send_telegram_safe(f"[LADDER] BUY {market}\nAmount: {float(usdt_amount):,.2f} USDT\nReason: {reason}")

            # --------------------------------------------------------
            # PATCH: Autoloop Add-Buy Notification
            # --------------------------------------------------------
            if reason.startswith("autoloop:add_buy"):
                 self._send_telegram_safe(f"[AUTOLOOP] ADD BUY {market}\nAmount: {float(usdt_amount):,.2f} USDT\nReason: {reason}")

        # ----------------------------------------------------
        # SELL
        # ----------------------------------------------------
        if "sell_qty" in intent and intent["sell_qty"] is not None:
            qty = float(intent["sell_qty"] or 0.0)
            if qty <= 0.0:
                return

            # manual mode (market isolation)
            if bool(((ctx.controls or {}).get("manual") or {}).get("enabled")):
                try:
                    ctx.exit_block_reason = "manual_mode"
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] manual_mode_sell_ledger: %s", exc2)
                ctx.exit_state = "BLOCKED"
                self._log_blocked_throttled(
                    "EXIT_BLOCKED",
                    market=market,
                    cause="manual_mode",
                    reason=reason,
                )
                return

            # ============================================================
            # [PROTECTED] LONGHOLD absolute protection (2026-02-01)
            # - LongHold markets block all automatic sells, including SL
            # - only manual user sells are allowed
            # DO NOT MODIFY: this logic is protected by owner instruction
            # ============================================================
            is_manual_exit = reason and ("manual" in str(reason).lower() or "user" in str(reason).lower() or "api_manual" in str(reason).lower())
            # [FIX 2026-03-23 P1] profit_lock system partial sells are exempt from LongHold protection
            # profit_lock only fires in profit territory — it doesn't conflict with the hold policy
            # but handle separately to avoid consuming the cooldown timestamp
            _pl_exit_kind = str((intent.get("meta") or {}).get("exit_kind", "") or "")
            is_manual_exit = is_manual_exit or _pl_exit_kind == "profit_lock"
            
            # check LongHold settings (longhold_config.json)
            is_longhold_market = False
            try:
                ladder_mgr = getattr(self, "ladder_manager", None)
                if ladder_mgr:
                    lh_cfg = ladder_mgr.get_longhold_config(market)
                    if lh_cfg and lh_cfg.get("enabled"):
                        is_longhold_market = True
            except (AttributeError, TypeError) as exc:
                logger.warning("[INTENT] longhold_config: %s", exc, exc_info=True)

            # check user_sell_only setting (context.controls.strategy.params)
            user_sell_only = False
            try:
                controls = ctx.controls or {}
                strat = controls.get("strategy", {}) or {}
                params = strat.get("params", {}) or {}
                user_sell_only = bool(params.get("user_sell_only", False))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[INTENT] user_sell_only_config: %s", exc, exc_info=True)

            if (is_longhold_market or user_sell_only) and not is_manual_exit:
                ctx.exit_state = "BLOCKED"
                ctx.exit_block_reason = "longhold_protected"
                self._log_blocked_throttled(
                    "EXIT_BLOCKED",
                    market=market,
                    cause="longhold_protected",
                    reason=reason,
                    is_longhold=is_longhold_market,
                    user_sell_only=user_sell_only,
                )
                self.ledger.append(
                    "LONGHOLD_SELL_BLOCKED",
                    market=market,
                    reason=reason,
                    is_longhold=is_longhold_market,
                    user_sell_only=user_sell_only,
                )
                return

            # ============================================================
            # WARMUP SELL PROTECTION (2026-01-30)
            # - block engine sell signals after server restart until warmup completes
            # - allow manual sells / API sells / forced exits / SL
            # ============================================================
            is_force_exit = bool(intent.get("force_exit") or intent.get("force") or intent.get("force_sell"))
            is_manual_sell = reason and ("manual" in reason.lower() or "api" in reason.lower() or "user" in reason.lower())
            is_sl_exit = reason and ("sl" in reason.lower() or "stoploss" in reason.lower() or "stop_loss" in reason.lower())
            
            if not ctx.is_ready() and not is_force_exit and not is_manual_sell and not is_sl_exit:
                try:
                    ctx.exit_block_reason = "warmup_protection"
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[INTENT] warmup_protection: %s", exc, exc_info=True)
                ctx.exit_state = "BLOCKED"
                self._log_blocked_throttled(
                    "EXIT_BLOCKED",
                    market=market,
                    cause="warmup_protection",
                    reason=reason,
                    warmup_ticks=getattr(ctx, "ticks", 0),
                    min_ticks=getattr(ctx, "min_ticks", 100),
                )
                return

            # [2026-03-10] Night Mode SL Guard: widen the night SL to avoid early stop-loss on transient dips
            # - it's an SL sell signal, but block it if within the widened SL range
            # - TP, signal, manual, and force sells are unaffected
            if is_sl_exit and not is_force_exit and not is_manual_sell and self.is_night_mode_active():
                try:
                    _nm_mult = float(getattr(self, 'night_mode_sl_multiplier', 1.5) or 1.5)
                    if _nm_mult > 1.0:
                        _entry = 0.0
                        _pos = getattr(ctx, "position", None)
                        if isinstance(_pos, dict):
                            _entry = float(_pos.get("entry") or _pos.get("avg_price") or _pos.get("avg_buy_price") or 0.0)
                        if _entry <= 0:
                            _entry = float(getattr(ctx, "avg_buy_price", 0.0) or 0.0)
                        if _entry > 0 and expected_price and expected_price > 0:
                            _profit_pct = (float(expected_price) - _entry) / _entry * 100.0
                            # read the strategy's original SL
                            _orig_sl = -2.5  # default
                            try:
                                _ss = getattr(ctx, "strategy_state", {}) or {}
                                _orig_sl = float(_ss.get("sl_pct") or _ss.get("sl") or -2.5)
                                if _orig_sl > 0:
                                    _orig_sl = -abs(_orig_sl)
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[INTENT] night_mode_sl_read: %s", exc, exc_info=True)
                            _night_sl = _orig_sl * _nm_mult  # e.g. -2.5 * 1.5 = -3.75
                            # hit the original SL but not the widened SL → block
                            if _profit_pct > _night_sl:
                                self._log_blocked_throttled(
                                    "EXIT_BLOCKED",
                                    market=market,
                                    cause="night_mode_sl_guard",
                                    reason=reason,
                                    profit_pct=round(_profit_pct, 2),
                                    orig_sl=round(_orig_sl, 2),
                                    night_sl=round(_night_sl, 2),
                                )
                                return
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[INTENT] night_mode_sl_guard: %s", exc, exc_info=True)

            if getattr(ctx, "order_state", None) is not None:
                try:
                    ctx.exit_block_reason = "order_pending"
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] order_pending_sell_ledger: %s", exc2)
                ctx.exit_state = "BLOCKED"
                self._log_blocked_throttled(
                    "EXIT_BLOCKED",
                    market=market,
                    cause="order_pending",
                    reason=reason,
                )
                return

            if ctx.exit_block_until_ts and time.time() < float(ctx.exit_block_until_ts):
                ctx.exit_state = "BLOCKED"
                cause = getattr(ctx, "exit_block_reason", None) or "exit_cooldown"
                try:
                    ctx.exit_block_reason = str(cause)
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] exit_cooldown_ledger: %s", exc2)
                self._log_blocked_throttled(
                    "EXIT_BLOCKED",
                    market=market,
                    cause=str(cause),
                    reason=reason,
                    cooldown_sec=self._cooldown_remaining(ctx.exit_block_until_ts),
                )
                return

            # [FIX] Dust block check: apply a cooldown when blocked for being below minimum order size
            dust_block_ts = getattr(ctx, "dust_block_until_ts", 0.0) or 0.0
            if dust_block_ts > 0.0 and time.time() < float(dust_block_ts):
                ctx.exit_state = "DUST_BLOCKED"
                self._log_blocked_throttled(
                    "EXIT_BLOCKED",
                    market=market,
                    cause="dust_cooldown",
                    reason=reason,
                    cooldown_sec=self._cooldown_remaining(dust_block_ts),
                )
                return

            # cannot sell without a position
            if not ctx.position or float(ctx.position.get("qty", 0.0) or 0.0) <= 0.0:
                try:
                    ctx.exit_block_reason = "no_position"
                except (AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[INTENT] no_position_sell_ledger: %s", exc2)
                ctx.exit_state = "BLOCKED"
                self._log_blocked_throttled(
                    "EXIT_BLOCKED",
                    market=market,
                    cause="no_position",
                    reason=reason,
                )
                return

            # [PATCH] Dust Prevention: If remaining balance is dust, sell all.
            # "If a tiny remainder would be left before handling it, just process that whole tiny amount. Everything."
            pos_qty = float(ctx.position.get("qty", 0.0) or 0.0)
            if pos_qty > qty:
                remain_qty = pos_qty - qty
                est_px = expected_price if (expected_price and expected_price > 0) else float(price_store.get_price(market) or 0.0)
                if est_px > 0:
                    remain_val = remain_qty * est_px
                    # if the remaining value is below minimum order size, switch to selling everything
                    if remain_val < self.min_order_usdt:
                        self.ledger.append("SELL_UPGRADED_TO_FULL", market=market, reason="dust_prevention", original_qty=qty, full_qty=pos_qty, remain_val=remain_val)
                        qty = pos_qty

            # ----------------------------------------------------
            # PROFIT GUARD (HARD FIX): block negative-margin / micro-scalping
            # ----------------------------------------------------
            # NOTE:
            # - even if the engine emits 'sell', if the expected NET profit is below the minimum threshold,
            #   the System blocks the order itself. (final safety net regardless of strategy)
            #
            # - forced EXITs like SL (stop-loss) must not be blocked, so intent.force_exit=True is exempted.
            # - the RECOVERY state also prioritizes withdrawal logic, so it is exempted by default.
            force_exit = bool(intent.get("force_exit") or intent.get("force") or intent.get("force_sell"))
            # PingPong peak-proximal exits should bypass profit-guard (engine already flags force_exit, but keep as safety)
            exit_kind0 = str((intent.get("meta") or {}).get("exit_kind") or "").lower()
            if exit_kind0 in ("pp_trail", "pp_dampen"):
                force_exit = True
            market_state = str(getattr(ctx, "market_state", "") or "").upper()
            
            # PATCH 2025-12-26: TP limit exit candidate (best_bid) — set expected_price for profit guard realism
            exit_kind = str((meta or {}).get("exit_kind") or "").lower()
            use_limit_exit = False
            limit_price = None

            # per-market override: tp_limit_exit_enabled
            tp_limit_on = _g_bool("tp_limit_exit_enabled", bool(getattr(self, "tp_limit_exit_enabled", False)))
            
            if (
                tp_limit_on
                and exit_kind == "tp"
                and (not force_exit)
                and market_state != "RECOVERY"
            ):
                ob = orderbook_store.get(market) or {}
                try:
                    ob_ts = float(ob.get("ts") or 0.0)
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[%s] exit orderbook ts conversion failed", market, exc_info=True)
                    ob_ts = 0.0
                stale_sec = (float(now_ts) - ob_ts) if ob_ts > 0.0 else None

                try:
                    bid = float(ob.get("best_bid")) if ob else 0.0
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[%s] exit best_bid conversion failed", market, exc_info=True)
                    bid = 0.0
            
                stale_limit_sec = float(_g_float("entry_ob_stale_sec", float(getattr(self, "entry_ob_stale_sec", 0.0) or 0.0)))
                if bid > 0.0 and (stale_sec is None or float(stale_sec) <= float(stale_limit_sec)):
                    use_limit_exit = True
                    limit_price = float(bid)
                    expected_price = float(bid)

            pg_on = _g_bool("exit_profit_guard", bool(getattr(self, "exit_profit_guard", False)))
            _is_manual_sell = reason and any(t in reason.lower() for t in ("manual", "user_sell", "ui_sell"))
            if (
                pg_on
                and not _is_manual_sell
                and market_state != "RECOVERY"
                and expected_price is not None
            ):
                try:
                    pos = ctx.position or {}
                    entry = float(pos.get("entry") or 0.0)
                    pos_qty = float(pos.get("qty") or 0.0)
                    if entry > 0.0 and pos_qty > 0.0:
                        sell_qty = min(float(qty), float(pos_qty))

                        principal_total = float(pos.get("usdt") or (entry * pos_qty))
                        principal_part = principal_total * (sell_qty / pos_qty) if pos_qty > 0 else (entry * sell_qty)

                        sell_value = float(expected_price) * sell_qty

                        fee_rate = float(_g_float("exit_fee_rate", float(getattr(self, "exit_fee_rate", 0.0) or 0.0)))
                        slip_bps = float(_g_float("exit_slippage_guard_bps", float(getattr(self, "exit_slippage_guard_bps", 0.0) or 0.0)))

                        est_fee = fee_rate * (principal_part + sell_value)
                        slip_buf = sell_value * (slip_bps / 10000.0)

                        gross_profit = (float(expected_price) - float(entry)) * float(sell_qty)
                        net_profit = gross_profit - est_fee - slip_buf

                        net_profit_pct = (net_profit / principal_part * 100.0) if principal_part > 0 else None

                        # ── HARD GUARD: absolutely block selling at or below buy-principal + fees ──
                        # even on force_exit (SL etc.), block if net_profit < 0
                        # (manual sells and RECOVERY were already excluded above)
                        if net_profit < 0 and force_exit:
                            now = time.time()
                            try:
                                cur = float(getattr(ctx, "exit_block_until_ts", 0.0) or 0.0)
                                ctx.exit_block_until_ts = max(cur, float(now) + 5.0)
                                ctx.exit_block_reason = "hard_profit_guard"
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.error("[INTENT] hard_profit_guard: %s", exc, exc_info=True)
                            ctx.exit_state = "BLOCKED"
                            self._log_blocked_throttled(
                                "EXIT_BLOCKED",
                                market=market,
                                cause="hard_profit_guard",
                                reason=reason,
                                expected_price=float(expected_price),
                                entry=float(entry),
                                sell_qty=float(sell_qty),
                                principal_usdt=float(principal_part),
                                net_profit_usdt=float(net_profit),
                                net_profit_pct=float(net_profit_pct) if net_profit_pct is not None else None,
                                force_exit=True,
                            )
                            return

                        min_pct = float(_g_float("exit_min_net_profit_pct", float(getattr(self, "exit_min_net_profit_pct", 0.0) or 0.0)))
                        min_usdt = float(_g_float("exit_min_net_profit_usdt", float(getattr(self, "exit_min_net_profit_usdt", 0.0) or 0.0)))

                        fail_pct = (not force_exit) and (net_profit_pct is not None) and (net_profit_pct < min_pct)
                        fail_usdt = (not force_exit) and (min_usdt > 0.0) and (net_profit < min_usdt)

                        if fail_pct or fail_usdt:
                            # ----------------------------------------------------
                            # profit_guard consecutive-block (streak) guard
                            # ----------------------------------------------------
                            # - default: 2s cooldown (eases log/order loop)
                            # - on N consecutive blocks: longer cooldown + (optional) RECOVERY promotion
                            now = time.time()
                            streak = 0
                            cooldown = 2.0

                            try:
                                win = float(getattr(self, "exit_profit_guard_streak_window_sec", 0.0) or 0.0)
                                last_ts = float(getattr(ctx, "profit_guard_block_last_ts", 0.0) or 0.0)
                                if win > 0.0 and last_ts > 0.0 and (now - last_ts) > win:
                                    setattr(ctx, "profit_guard_block_streak", 0)

                                streak = int(getattr(ctx, "profit_guard_block_streak", 0) or 0) + 1
                                setattr(ctx, "profit_guard_block_streak", int(streak))
                                setattr(ctx, "profit_guard_block_last_ts", float(now))
                            except (AttributeError, TypeError, ValueError):
                                logger.warning("[%s] profit_guard streak update failed", market, exc_info=True)
                                try:
                                    streak = int(getattr(ctx, "profit_guard_block_streak", 0) or 0)
                                except (TypeError, ValueError):
                                    logger.warning("[%s] profit_guard streak read failed", market, exc_info=True)
                                    streak = 0

                            # streak-trigger decision
                            try:
                                n = int(getattr(self, "exit_profit_guard_streak_n", 0) or 0)
                                cd = float(getattr(self, "exit_profit_guard_streak_cooldown_sec", 0.0) or 0.0)
                                if n > 0 and cd > 0.0 and streak >= n:
                                    cooldown = float(cd)

                                    # (optional) promote the market to RECOVERY
                                    if bool(getattr(self, "exit_profit_guard_streak_to_recovery", False)):
                                        try:
                                            self.oma_registry.set_state(
                                                market=market,
                                                state=MarketState.RECOVERY,
                                                reason=[f"profit_guard_streak:{streak}"],
                                                persist=True,
                                            )
                                            try:
                                                self.price_feed.request_resubscribe()
                                            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                                                logger.warning("[INTENT] recovery_resubscribe: %s", exc)
                                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                                            logger.warning("[INTENT] recovery_promotion: %s", exc, exc_info=True)

                                    # (optional) one-time notification
                                    if bool(getattr(self, "exit_profit_guard_streak_notify", False)):
                                        try:
                                            self._send_telegram_safe(
                                                f"[AUTOCOIN] PROFIT_GUARD STREAK\n{market} blocked {streak}x → cooldown {cooldown:.0f}s"
                                            )
                                        except (AttributeError, TypeError, ValueError) as exc:
                                            logger.warning("[INTENT] streak_notify_telegram: %s", exc)

                                    # streak reset (prevent repeated firing)
                                    try:
                                        setattr(ctx, "profit_guard_block_streak", 0)
                                    except (AttributeError, TypeError, ValueError) as exc:
                                        logger.warning("[INTENT] streak_reset: %s", exc)

                                    # log the streak event separately
                                    try:
                                        self.ledger.append(
                                            "EXIT_PROFIT_GUARD_STREAK",
                                            market=market,
                                            streak=int(streak),
                                            cooldown_sec=float(cooldown),
                                            entry=float(entry),
                                            expected_price=float(expected_price),
                                            net_profit_usdt=float(net_profit),
                                            net_profit_pct=float(net_profit_pct) if net_profit_pct is not None else None,
                                        )
                                    except (TypeError, ValueError) as exc:
                                        logger.warning("[INTENT] streak_event_ledger: %s", exc)
                            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[INTENT] streak_trigger: %s", exc)

                            # apply cooldown (default 2s, may be longer on streak-trigger)
                            try:
                                cur = float(getattr(ctx, "exit_block_until_ts", 0.0) or 0.0)
                                ctx.exit_block_until_ts = max(cur, float(now) + float(cooldown))
                                ctx.exit_block_reason = "profit_guard_streak" if float(cooldown) > 2.0 else "profit_guard"
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.error("[INTENT] profit_guard_cooldown: %s", exc, exc_info=True)

                            ctx.exit_state = "BLOCKED"
                            self._log_blocked_throttled(
                                "EXIT_BLOCKED",
                                market=market,
                                cause="profit_guard",
                                reason=reason,
                                expected_price=float(expected_price),
                                entry=float(entry),
                                sell_qty=float(sell_qty),
                                principal_usdt=float(principal_part),
                                gross_profit_usdt=float(gross_profit),
                                est_fee_usdt=float(est_fee),
                                slippage_buf_usdt=float(slip_buf),
                                net_profit_usdt=float(net_profit),
                                net_profit_pct=float(net_profit_pct) if net_profit_pct is not None else None,
                                min_net_profit_pct=float(min_pct),
                                min_net_profit_usdt=float(min_usdt),
                                fee_rate=float(fee_rate),
                                slippage_guard_bps=float(slip_bps),
                                profit_guard_streak=int(streak),
                                profit_guard_cooldown_sec=float(cooldown),
                            )
                            return
                except Exception as exc:
                    logger.error(
                        "[EXIT_PROFIT_GUARD] calculation failed for %s, BLOCKING sell for safety: %s",
                        market, exc, exc_info=True,
                    )
                    ctx.exit_state = "BLOCKED"
                    ctx.exit_block_reason = "profit_guard_calc_error"
                    return

            # PATCH 2025-12-26: TP => LIMIT EXIT, SL/force_exit => MARKET
            # PATCH 2026-01-31: support SNIPER use_limit + fallback_to_market
            sniper_use_limit = bool((meta or {}).get("use_limit", False))
            sniper_fallback = bool((meta or {}).get("fallback_to_market", True))
            is_sniper_sell = reason and "sniper:" in reason.lower()
            
            if is_sniper_sell and sniper_use_limit and not force_exit:
                # SNIPER limit sell: quick_sell (IOC) + fallback
                ob = orderbook_store.get(market) or {}
                try:
                    sniper_limit_price = float(ob.get("best_bid") or 0.0)
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[%s] sniper limit price conversion failed", market, exc_info=True)
                    sniper_limit_price = 0.0
                
                if sniper_limit_price > 0:
                    ok, msg = self.order_fsm.submit_quick_sell(
                        ctx=ctx,
                        market=market,
                        qty=float(qty),
                        price=float(sniper_limit_price),
                        reason=reason or "sniper:limit_sell",
                        fallback_to_market=sniper_fallback,
                    )
                else:
                    # no best_bid → fallback to market
                    ok, msg = self.order_fsm.submit_market_sell(
                        ctx=ctx,
                        market=market,
                        qty=float(qty),
                        expected_price=expected_price,
                        reason=reason or "sniper:market_sell:no_bid",
                    )
            elif use_limit_exit and limit_price is not None:
                ok, msg = self.order_fsm.submit_limit_sell(
                    ctx=ctx,
                    market=market,
                    qty=float(qty),
                    limit_price=float(limit_price),
                    expected_price=float(expected_price),
                    reason=reason or "engine_sell:tp",
                    attempts=1,
                    max_retries=int(_g_int("tp_limit_max_retries", int(getattr(self, "tp_limit_max_retries", 0) or 0))),
                    timeout_sec=float(_g_float("tp_limit_timeout_sec", float(getattr(self, "tp_limit_timeout_sec", 0.0) or 0.0))),
                )
            else:
                ok, msg = self.order_fsm.submit_market_sell(
                    ctx=ctx,
                    market=market,
                    qty=float(qty),
                    expected_price=expected_price,
                    reason=reason or "engine_sell",
                )

            # [FIX] Auto Dust Cleanup: If blocked due to small value, try to clear it
            # User requested to disable "Buy->Sell" cleanup logic. ("not a buy-then-sell")
            # if not ok and "min_value_blocked" in str(msg):
            #      await self._run_dust_cleanup(ctx, market, float(expected_price or 0))
            #      return

            warn: Optional[str] = None
            if ok and isinstance(msg, str) and "|WARN:" in msg:
                msg, warn_payload = msg.split("|WARN:", 1)
                warn = "WARN:" + warn_payload

            if not ok:
                if isinstance(msg, str) and msg.startswith("SOFT:"):
                    ctx.exit_state = "SOFTFAIL"
                    self._notify_soft_once(market=market, intent="SELL", msg=msg)
                    return

                # [FIX] Dust prevention: prevent retries for 5 minutes when below minimum order size
                if isinstance(msg, str) and "min_value_blocked" in msg:
                    ctx.exit_state = "DUST_BLOCKED"
                    ctx.dust_block_until_ts = time.time() + 300.0  # 5-min cooldown
                    self._log_blocked_throttled(
                        "EXIT_DUST_BLOCKED",
                        market=market,
                        cause="dust_below_min_order",
                        reason=reason,
                        error=str(msg),
                        cooldown_sec=300,
                    )
                    return

                ctx.exit_state = "FAILED"
                self._log_blocked_throttled(
                    "EXIT_SUBMIT_FAILED",
                    market=market,
                    cause="submission_failed",
                    reason=reason,
                    error=str(msg),
                )
                return

            ctx.exit_state = "ORDER_PLACED"
            try:
                ctx.exit_block_reason = None
            except (AttributeError, TypeError, ValueError) as exc:
                logger.error("[INTENT] sell_order_placed_clear: %s", exc, exc_info=True)
            # profit_guard streak reset: reset the consecutive-block counter once a real SELL order goes out
            try:
                setattr(ctx, "profit_guard_block_streak", 0)
                setattr(ctx, "profit_guard_block_last_ts", 0.0)
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[INTENT] profit_guard_streak_reset: %s", exc)
            # [FIX H7] save sniper_last_exit_ts immediately when a SNIPER sell order is submitted
            # (so _release_scope_sold_slots can detect ghost positions even after a reboot)
            if is_sniper_sell:
                try:
                    ctx.set_var("sniper_last_exit_ts", time.time())
                    self._save_context_state()
                except (OSError, TypeError, ValueError, OverflowError) as exc:
                    logger.warning("[INTENT] sniper_exit_save: %s", exc)

            self.ledger.append(
                "ORDER_ACK",
                market=market,
                uuid=str(msg),
                state="wait",
                side="ask",
            )
            if warn:
                self._notify_soft_once(market=market, intent="SELL", msg=warn)
