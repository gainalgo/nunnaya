"""Phase 5D mixin -- guard / safety / throttle methods.

── ASYNC SAFETY RULES ──
Functions in this file are called from an async context (event loop).
- Do not call requests.get/post directly → asyncio.to_thread() is required
- High/candle lookups must come only from candle_cache or price_store (no HTTP)
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Any, Optional, List, Tuple

from app.manager.oma_market_registry import MarketState
from app.core.constants import BYBIT_MARKET_KLINE, DEFAULT_REQUEST_TIMEOUT_SEC

logger = logging.getLogger(__name__)

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.engine.hyper_engine_context import HyperEngineContext


class GuardsMixin:
    """Guard / safety / throttle helpers extracted from HyperSystem (Phase 5D)."""

    # ------------------------------------------------------------------
    # TP helper
    # ------------------------------------------------------------------
    def _get_effective_tp_for_market(self, ctx) -> float:
        """Get the current strategy's effective TP% from the context."""
        try:
            engine = getattr(self, "engine", None)
            if engine is None:
                return 0.0
            strategy_mode = engine._strategy_mode_from_context(ctx)
            if not strategy_mode:
                return 0.0
            policy = engine._normalize_tp_sl_policy(getattr(engine, "tp_sl_policy", {}))
            per = policy.get("per_strategy") if isinstance(policy.get("per_strategy"), dict) else {}
            p = per.get(strategy_mode, {}) if isinstance(per, dict) else {}
            tp_floor = float(policy.get("tp_floor_pct", 1.2) or 1.2)
            return float(p.get("tp_pct", tp_floor) or tp_floor)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Guards] _get_effective_tp_for_market error", exc_info=True)
            return 0.0

    # ------------------------------------------------------------------
    # Market regime inference
    # ------------------------------------------------------------------
    def _infer_market_regime(self, *, ctx: HyperEngineContext, price: float) -> Tuple[str, Optional[float]]:
        """Classify a simple up/down regime from the recent price change rate.

        Returns:
            (regime, change_pct)

        - regime: "BULL" | "BEAR" | "NEUTRAL" | "UNKNOWN"
        - change_pct: change rate (%) over the lookback window. None if not computable.

        NOTE:
        - This value is auxiliary info for the 'entry/re-entry guard'.
        - It is decoupled from signal generation (strategy), so the same safety
          layer applies regardless of which strategy is active.
        """
        try:
            last = float(price)
        except (TypeError, ValueError) as exc:
            try:
                self.ledger.append("SYSTEM_HELPER_ERROR", where="infer_market_regime:price", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[GUARD] _infer_market_regime fallback: %s", exc, exc_info=True)
            return "UNKNOWN", None

        if last <= 0.0:
            return "UNKNOWN", None

        lookback = int(getattr(self, "regime_lookback_ticks", 300) or 300)
        bull_thr = float(getattr(self, "regime_bull_pct", 0.0) or 0.0)
        bear_thr = float(getattr(self, "regime_bear_pct", 0.0) or 0.0)
        require_mom = bool(getattr(self, "regime_require_momentum", True))

        try:
            prices = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
        except (AttributeError, TypeError):
            logger.warning("[Guards] _infer_market_regime: failed to get prices", exc_info=True)
            prices = []

        n = len(prices)
        if n < 5:
            return "UNKNOWN", None

        # normalize lookback
        if lookback <= 0:
            lookback = min(300, n - 1)
        lookback = min(int(lookback), n - 1)
        if lookback < 3:
            return "UNKNOWN", None

        try:
            base = float(prices[-lookback])
        except (IndexError, TypeError, ValueError) as exc:
            try:
                self.ledger.append("SYSTEM_HELPER_ERROR", where="infer_market_regime:base", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[GUARD] normalize lookback: %s", exc, exc_info=True)
            return "UNKNOWN", None

        if base <= 0.0:
            return "UNKNOWN", None

        change_pct = (last - base) / base * 100.0

        # momentum (vs. previous tick)
        mom = None
        try:
            if n >= 2:
                mom = last - float(prices[-2])
        except (IndexError, TypeError, ValueError):
            logger.warning("[Guards] momentum calculation failed", exc_info=True)
            mom = None

        bull = bool(change_pct >= bull_thr) if bull_thr > 0 else bool(change_pct > 0.0)
        bear = bool(change_pct <= -bear_thr) if bear_thr > 0 else bool(change_pct < 0.0)

        if require_mom and mom is not None:
            if bull and mom < 0:
                bull = False
            if bear and mom > 0:
                bear = False

        if bull:
            return "BULL", float(change_pct)
        if bear:
            return "BEAR", float(change_pct)
        return "NEUTRAL", float(change_pct)

    # ------------------------------------------------------------------
    # Recent high price (API + local fallback)
    # ------------------------------------------------------------------
    def _get_recent_high_price(
        self,
        *,
        market: str,
        ctx: HyperEngineContext,
        lookback_hours: float,
        candle_unit_min: int,
        cache_sec: float,
    ) -> Tuple[Optional[float], str]:
        """Compute the recent N-hour high (api + local fallback).

        Returns:
            (recent_high, source)
            source: "api", "local", "api+local", "none"

        Design:
        - In Bybit mode, the high is computed primarily from public candles (minute bars).
        - On API failure/limit, fail-open to the local high from ctx.price_history.
        - A short TTL cache is used to avoid excessive API calls.
        """
        local_high: Optional[float] = None
        try:
            prices = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
            if prices:
                vals: List[float] = []
                for x in prices:
                    try:
                        v = float(x)
                    except (TypeError, ValueError) as exc:
                        logger.warning("[GUARD] _get_recent_high_price price parse: %s", exc, exc_info=True)
                        continue
                    if v > 0.0:
                        vals.append(v)
                if vals:
                    local_high = float(max(vals))
        except (AttributeError, TypeError, ValueError):
            logger.warning("[Guards] _get_recent_high_price local_high error for %s", market, exc_info=True)
            local_high = None

        api_high: Optional[float] = None
        if str(getattr(self, "exchange_type", "bybit") or "bybit").lower() == "bybit":
            unit = max(1, int(candle_unit_min or 1))
            mins = max(1, int(round(max(0.01, float(lookback_hours)) * 60.0)))
            count = max(1, min(200, int((mins + unit - 1) // unit)))
            ttl = max(1.0, float(cache_sec or 0.0))
            key = f"{market}|u{unit}|c{count}"
            now = time.time()

            cached: Optional[Dict[str, Any]] = None
            try:
                cached = (getattr(self, "_entry_recent_high_cache", {}) or {}).get(key)
            except AttributeError:
                logger.warning("_entry_recent_high_cache access failed for key=%s", key, exc_info=True)
                cached = None

            if isinstance(cached, dict):
                try:
                    ts = float(cached.get("ts") or 0.0)
                    if (now - ts) <= ttl:
                        h = float(cached.get("high") or 0.0)
                        if h > 0.0:
                            api_high = h
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[Guards] cached high parse error for %s", market, exc_info=True)
                    api_high = None

            # [2026-03-30] removed requests.get — it was blocking the event loop.
            # Look up the high from the candle_loader cache (no HTTP call, memory only)
            if api_high is None:
                try:
                    from app.backtest.candle_loader import CandleLoader
                    _cl = CandleLoader()
                    _cache_key = f"{market}_{unit}_{count}"
                    _cached_candles = _cl._cache.get(_cache_key)
                    if _cached_candles and isinstance(_cached_candles, list):
                        highs: List[float] = []
                        for c in _cached_candles:
                            try:
                                h = float(c.get("high_price") or c.get("trade_price") or 0.0)
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[GUARD] candle_loader cache high parse: %s", exc, exc_info=True)
                                continue
                            if h > 0.0:
                                highs.append(h)
                        if highs:
                            api_high = float(max(highs))
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[Guards] candle_loader cache high lookup error for %s", market, exc_info=True)
                    api_high = None

        if api_high is not None and local_high is not None:
            return max(float(api_high), float(local_high)), "api+local"
        if api_high is not None:
            return float(api_high), "api"
        if local_high is not None:
            return float(local_high), "local"
        return None, "none"


    # ------------------------------------------------------------------
    # Entry ceiling price
    # ------------------------------------------------------------------
    def _calc_entry_ceiling_price(
        self,
        *,
        last_exit_price: float,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        """Compute the re-entry ceiling price based on the last EXIT price.

        - fee_rate conservatively reflects a round-trip (sell + buy), i.e. 2x.
        - slippage/spread/extra bps are used as an additional price-based buffer.
        """
        try:
            lep = float(last_exit_price)
        except (TypeError, ValueError) as exc:
            try:
                self.ledger.append("ENTRY_CEILING_ERROR", where="parse_last_exit_price", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[GUARD] _calc_entry_ceiling_price fallback: %s", exc, exc_info=True)
            return None

        if lep <= 0.0:
            return None

        o = overrides if isinstance(overrides, dict) else {}

        # Per-market overrides (ctx.controls.guards) can provide:
        # - entry_ceiling_fee_rate
        # - entry_ceiling_slippage_guard_bps
        # - entry_ceiling_spread_guard_bps
        # - entry_ceiling_extra_bps
        def _pick_float(key: str, fallback: float) -> float:
            try:
                if key in o and o.get(key) is not None:
                    return float(o.get(key))
            except (TypeError, ValueError) as exc:
                logger.warning("[GUARD] _pick_float fallback: %s", exc, exc_info=True)
            return float(fallback)

        fee_rate = _pick_float("entry_ceiling_fee_rate", float(getattr(self, "entry_ceiling_fee_rate", 0.0) or 0.0))
        slip_bps = _pick_float("entry_ceiling_slippage_guard_bps", float(getattr(self, "entry_ceiling_slippage_guard_bps", 0.0) or 0.0))
        sprd_bps = _pick_float("entry_ceiling_spread_guard_bps", float(getattr(self, "entry_ceiling_spread_guard_bps", 0.0) or 0.0))
        extra_bps = _pick_float("entry_ceiling_extra_bps", float(getattr(self, "entry_ceiling_extra_bps", 0.0) or 0.0))

        bps_total = max(0.0, slip_bps + sprd_bps + extra_bps)
        cost_pct = max(0.0, float(fee_rate) * 2.0 + (bps_total / 10000.0))

        # guard against misconfig: treat >= 20% as abnormal and cap it
        cost_pct = min(cost_pct, 0.20)

        ceiling = lep * (1.0 - cost_pct)
        return float(ceiling) if ceiling > 0.0 else None


    # --------------------------------------------------------
    # Safety: Global entry cooldown (BUY only)
    # --------------------------------------------------------
    def _set_global_entry_cooldown(
        self,
        *,
        until_ts: float,
        reason: str,
        action: str = "cooldown",
    ) -> None:
        """Set a global BUY cooldown. (SELL remains allowed)

        - _handle_intent(BUY) references this value to block new entries.
        - It is also propagated per-market (entry_block_until_ts) for UI/observability.
        """
        try:
            until = float(until_ts)
        except (TypeError, ValueError) as exc:
            try:
                self.ledger.append("SYSTEM_HELPER_ERROR", where="set_global_entry_cooldown", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[GUARD] _set_global_entry_cooldown fallback: %s", exc, exc_info=True)
            return

        now = time.time()
        if until <= now:
            return

        cur = float(getattr(self, "_global_entry_block_until_ts", 0.0) or 0.0)
        if until > cur:
            self._global_entry_block_until_ts = until

        if reason:
            self._global_entry_block_reason = str(reason)

        # propagate to ACTIVE contexts (for observability/consistency)
        try:
            ctx_map = self.coordinator.get_contexts()
            for mk, ctx in ctx_map.items():
                try:
                    if self.oma_registry.get_state(mk) != MarketState.ACTIVE:
                        continue
                    cur2 = float(getattr(ctx, "entry_block_until_ts", 0.0) or 0.0)
                    if self._global_entry_block_until_ts > cur2:
                        ctx.entry_block_until_ts = float(self._global_entry_block_until_ts)
                        ctx.entry_block_reason = str(action)
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[GUARD] ACTIVE context propagation per-market: %s", exc, exc_info=True)
                    continue
        except (AttributeError, TypeError) as exc:
            logger.warning("[GUARD] ACTIVE context propagation: %s", exc, exc_info=True)

        # log (throttle to avoid short-interval spam)
        try:
            last = float(getattr(self, "_global_entry_block_last_log_ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            logger.warning("[Guards] _global_entry_block_last_log_ts parse error", exc_info=True)
            last = 0.0

        if (now - last) >= 5.0:
            try:
                self._global_entry_block_last_log_ts = now
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[GUARD] log spam throttle: %s", exc, exc_info=True)
            self.ledger.append(
                "GLOBAL_ENTRY_COOLDOWN_SET",
                until_ts=float(self._global_entry_block_until_ts),
                remaining_sec=float(self._cooldown_remaining(self._global_entry_block_until_ts)),
                action=str(action),
                reason=str(self._global_entry_block_reason or ""),
            )

    # --------------------------------------------------------
    # Safety: Global drawdown guard
    # --------------------------------------------------------
    def _check_drawdown_guard(self, *, equity_usdt: float, reason: str = "") -> None:
        """Max-loss (drawdown) guard based on account equity.

        - Active when OMA_DRAWDOWN_GUARD=1.
        - When (base - equity)/base relative to base_equity exceeds the threshold:
            * COOLDOWN     : block BUY for a period (auto-released)
            * RECOVERY     : block BUY + promote ACTIVE → RECOVERY (operator confirmation needed)
            * EMERGENCY_STOP : block BUY + promote ACTIVE → RECOVERY (same behavior; explicit name)
        """
        if not bool(getattr(self, "drawdown_guard", False)):
            return

        try:
            eq = float(equity_usdt)
        except (TypeError, ValueError) as exc:
            try:
                self.ledger.append("DRAWDOWN_GUARD_ERROR", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[GUARD] _check_drawdown_guard fallback: %s", exc, exc_info=True)
            return

        if eq <= 0.0:
            return

        # set base: in fixed_principal mode, prefer principal_base_equity
        base: Optional[float] = None
        try:
            b = getattr(self, "_principal_base_equity_usdt", None)
            if b is not None and float(b) > 0:
                base = float(b)
        except (TypeError, ValueError):
            logger.warning("[Guards] _principal_base_equity_usdt parse error", exc_info=True)
            base = None

        if base is None:
            try:
                b = getattr(self, "_drawdown_base_equity_usdt", None)
                if b is not None and float(b) > 0:
                    base = float(b)
            except (TypeError, ValueError):
                logger.warning("[Guards] _drawdown_base_equity_usdt parse error", exc_info=True)
                base = None

        if base is None:
            # set base once on first run, then return
            try:
                self._drawdown_base_equity_usdt = float(eq)
            except (TypeError, ValueError) as exc:
                logger.warning("[GUARD] drawdown base init: %s", exc, exc_info=True)
            self.ledger.append("DRAWDOWN_BASE_SET", base_equity_usdt=float(eq), reason=str(reason or "init"))
            return

        thr = float(getattr(self, "max_drawdown_pct", 0.0) or 0.0)
        if thr <= 0.0:
            return

        if float(base) <= 0.0:
            logger.debug("[GUARD] drawdown skip: base_equity=%.2f (zero or negative)", float(base))
            return

        drawdown_pct = max(0.0, (float(base) - float(eq)) / float(base) * 100.0)

        if drawdown_pct < thr:
            # Reset latch if equity recovers, so it can trigger again in the future
            if getattr(self, "_drawdown_latched", False):
                self._drawdown_latched = False
                self.ledger.append("DRAWDOWN_GUARD_RESET", reason=f"recovered: {drawdown_pct:.2f}% < {thr:.2f}%")
            return

        now = time.time()
        min_interval = float(getattr(self, "drawdown_trigger_min_interval_sec", 0.0) or 0.0)
        last = float(getattr(self, "_drawdown_last_trigger_ts", 0.0) or 0.0)

        action = str(getattr(self, "drawdown_action", "RECOVERY") or "RECOVERY").upper()
        if action not in ("COOLDOWN", "RECOVERY", "EMERGENCY_STOP"):
            action = "RECOVERY"

        msg = (
            f"drawdown {drawdown_pct:.2f}% >= {thr:.2f}% "
            f"(base={float(base):.0f} equity={float(eq):.0f})"
        )

        # avoid triggering too frequently (though COOLDOWN can be extended)
        if min_interval > 0 and (now - last) < min_interval:
            if action == "COOLDOWN":
                cd = float(getattr(self, "drawdown_cooldown_sec", 0.0) or 0.0)
                if cd > 0.0:
                    self._set_global_entry_cooldown(
                        until_ts=now + cd,
                        reason=msg,
                        action="drawdown",
                    )
            return

        try:
            self._drawdown_last_trigger_ts = now
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[GUARD] drawdown trigger timestamp: %s", exc, exc_info=True)

        self.ledger.append(
            "DRAWDOWN_BREACH",
            base_equity_usdt=float(base),
            equity_usdt=float(eq),
            drawdown_pct=float(drawdown_pct),
            threshold_pct=float(thr),
            action=str(action),
            reason=str(reason or ""),
            message=msg,
        )

        # 1) COOLDOWN: auto-released (temporary pause)
        if action == "COOLDOWN":
            cd = float(getattr(self, "drawdown_cooldown_sec", 0.0) or 0.0)
            if cd > 0.0:
                self._set_global_entry_cooldown(
                    until_ts=now + cd,
                    reason=msg,
                    action="drawdown",
                )
            if bool(getattr(self, "drawdown_notify", False)):
                self._send_telegram_safe("[AUTOCOIN] DRAWDOWN COOLDOWN\n" + msg)
            return

        # 2) RECOVERY / EMERGENCY_STOP: operator-intervention type (latches once tripped)
        if bool(getattr(self, "_drawdown_latched", False)):
            return
        try:
            self._drawdown_latched = True
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[GUARD] drawdown latch set: %s", exc, exc_info=True)

        # block BUY (SELL remains allowed)
        if not bool(getattr(self, "emergency_stop", False)):
            try:
                self.set_emergency_stop(True, reason=msg)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.error("[Guards] set_emergency_stop() failed, forcing directly", exc_info=True)
                self.emergency_stop = True
                self.ledger.append("EMERGENCY_STOP_SET", enabled=True, reason=msg)

        # promote ACTIVE → RECOVERY
        promoted: List[str] = []
        try:
            active = list(self.oma_registry.list_active())
        except (AttributeError, TypeError):
            logger.warning("[Guards] drawdown: list_active() failed", exc_info=True)
            active = []

        for mk in active:
            try:
                self.oma_registry.set_state(
                    market=mk,
                    state=MarketState.RECOVERY,
                    reason=["drawdown_breach"],
                    persist=True,
                )
                promoted.append(str(mk))
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[GUARD] ACTIVE -> RECOVERY promotion: %s", exc, exc_info=True)
                continue

        if promoted:
            self.ledger.append("DRAWDOWN_PROMOTE_RECOVERY", n=len(promoted), markets=promoted)
            try:
                self.price_feed.request_resubscribe()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[GUARD] resubscribe after RECOVERY promotion: %s", exc, exc_info=True)

        if bool(getattr(self, "drawdown_notify", False)):
            self._send_telegram_safe(
                "[AUTOCOIN] DRAWDOWN " + action + "\n" + msg + "\n(new entries blocked; exits allowed)"
            )

    # ------------------------------------------------------------------
    # Blocked-log throttle
    # ------------------------------------------------------------------
    def _log_blocked_throttled(
        self,
        event: str,
        *,
        market: str,
        cause: str,
        reason: str = "",
        min_interval_sec: Optional[float] = None,
        **extra: Any,
    ) -> None:
        """Record ENTRY_BLOCKED/EXIT_BLOCKED logs with spam prevention (rate limiting)."""
        interval = float(min_interval_sec) if min_interval_sec is not None else float(self._block_log_interval_sec or 0.0)
        if interval <= 0:
            # no throttle
            self.ledger.append(event, market=market, cause=cause, reason=reason, **extra)
            return

        now = time.time()
        key = (event, market, cause)
        last = float(self._block_log_last.get(key, 0.0) or 0.0)
        if (now - last) < interval:
            return
        self._block_log_last[key] = now
        self.ledger.append(event, market=market, cause=cause, reason=reason, **extra)

    # ------------------------------------------------------------------
    # Telegram helpers
    # ------------------------------------------------------------------
    def _send_telegram_safe(self, text: str) -> None:
        """Telegram notification (system keeps running even on failure). Fire-and-forget via a daemon thread."""
        import threading
        def _fire():
            try:
                from app.notify.telegram import send_telegram
                send_telegram(text)
            except Exception:
                pass
        try:
            threading.Thread(target=_fire, daemon=True).start()
        except (ImportError, AttributeError, TypeError) as exc:
            try:
                self.ledger.append("TELEGRAM_SEND_ERROR", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[GUARD] _send_telegram_safe fallback: %s", exc, exc_info=True)
            return

    def _notify_soft_once(self, *, market: str, intent: str, msg: str) -> None:
        """Notify a SOFT/WARN message via Telegram once (rate-limited)."""
        if not isinstance(msg, str) or not msg:
            return

        # tag for throttling
        tag = msg.split("|", 1)[0]
        if msg.startswith("WARN:"):
            parts = msg.split(":", 2)
            if len(parts) >= 2:
                tag = f"{parts[0]}:{parts[1]}"  # WARN:XYZ
            else:
                tag = "WARN"
        elif msg.startswith("SOFT:"):
            tag = msg.split("|", 1)[0]  # SOFT:xxx

        now = time.time()
        key = (market, tag)
        last = float(self._soft_notice_last.get(key, 0.0) or 0.0)
        interval = float(self._soft_notice_interval_sec or 0.0)
        if interval > 0 and (now - last) < interval:
            return

        self._soft_notice_last[key] = now

        head = f"[AUTOCOIN] {tag} {market} {intent}"
        body = msg
        self._send_telegram_safe(head + "\n" + body)

    # ------------------------------------------------------------------
    # Orderbook block cooldown
    # ------------------------------------------------------------------
    def _apply_ob_block_cooldown(self, ctx: Any, market: str, cause: str) -> None:
        """Apply a progressive cooldown when the orderbook guard (spread/stale) blocks.

        The more consecutive blocks accumulate, the longer the cooldown (30s → 60s → 120s → ... max 300s).
        The streak resets if there are no blocks for 10 minutes.
        """
        try:
            base_cd = float(getattr(self, "entry_ob_block_cooldown_sec", 30.0) or 30.0)
            max_cd = float(getattr(self, "entry_ob_block_max_cooldown_sec", 300.0) or 300.0)

            key = market
            streak = int(self._ob_block_streak.get(key, 0) or 0) + 1
            self._ob_block_streak[key] = streak

            cooldown = min(base_cd * (2 ** min(streak - 1, 5)), max_cd)

            now = time.time()
            cur = float(getattr(ctx, "entry_block_until_ts", 0.0) or 0.0)
            ctx.entry_block_until_ts = max(cur, now + cooldown)
            ctx.entry_block_reason = cause
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[GUARD] _apply_ob_block_cooldown: %s", exc, exc_info=True)

    def _reset_ob_block_streak(self, market: str) -> None:
        """Reset the OB block streak for the given market on a successful entry."""
        try:
            self._ob_block_streak.pop(market, None)
        except (AttributeError, TypeError) as exc:
            logger.warning("[GUARD] _reset_ob_block_streak: %s", exc, exc_info=True)
