"""Phase 5F — Background loop methods extracted from HyperSystem.

── ASYNC SAFETY RULES ──
All loops are async. Synchronous blocking work must use asyncio.to_thread().
- requests.get/post → asyncio.to_thread() required
- File I/O (ledger.tail etc.) → asyncio.to_thread() required
- Must set a timeout when holding _scan_gate (deadlock prevention)
"""

from __future__ import annotations
import asyncio
import functools
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

from app.core.constants import env_bool as _env_bool, env_float as _env_float, env_int as _env_int
from app.core.hyper_price_store import price_store, orderbook_store


class BackgroundLoopsMixin:
    """Background async loops: AI retrain, daily report, contrarian, volume spike, etc."""

    async def _ai_loop(self):
        """Periodic AI maintenance (retrain if accuracy drops)."""
        from app.manager.ai_trainer import ai_trainer

        # Initial delay
        await asyncio.sleep(60)

        while True:
            try:
                # Check every N minutes (default 6 hours, tunable via OMA_AI_RETRAIN_INTERVAL_SEC)
                await asyncio.sleep(_env_float("OMA_AI_RETRAIN_INTERVAL_SEC", 21600))

                # Run in thread to avoid blocking event loop
                res = await asyncio.to_thread(ai_trainer.check_and_retrain, threshold=float(self.ai_retrain_threshold))

                if res.get("triggered"):
                    self.ledger.append("AI_AUTO_RETRAIN", result=res)
                    # Reload brain if successful
                    if res.get("train_result", {}).get("ok"):
                         if hasattr(self.engine, "pipeline") and hasattr(self.engine.pipeline, "brain"):
                            self.engine.pipeline.brain.reload_model()
                            self.ledger.append("AI_MODEL_RELOADED", source="auto_retrain")

            except asyncio.CancelledError:
                logger.info("[AI_LOOP] shutdown")
                return
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                try:
                    self.ledger.append("AI_LOOP_ERROR", error=str(exc))
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[BG_LOOPS] AI_LOOP ledger append failed: %s", exc, exc_info=True)

    async def _daily_report_loop(self):
        """Send the daily report via Telegram every midnight (00:05)."""
        from app.core.constants import env_bool
        from datetime import datetime, timedelta

        enabled = env_bool("DAILY_REPORT_TELEGRAM_ENABLED", default=True)

        if not enabled:
            return

        while True:
            try:
                # Compute the wait time until the next 00:05
                now = datetime.now()
                tomorrow = now.replace(hour=0, minute=5, second=0, microsecond=0)
                if now >= tomorrow:
                    tomorrow += timedelta(days=1)

                wait_sec = (tomorrow - now).total_seconds()
                await asyncio.sleep(wait_sec)

                # Send yesterday's report (past midnight, so "yesterday" = the previous day)
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

                try:
                    from app.manager.daily_pnl import get_daily_pnl_manager
                    mgr = get_daily_pnl_manager()

                    # Load and send yesterday's report
                    report = mgr.load_report(yesterday)
                    if report:
                        mgr.send_daily_report_telegram(report)
                        self.ledger.append("DAILY_REPORT_SENT", date=yesterday)
                    else:
                        self.ledger.append("DAILY_REPORT_SKIP", date=yesterday, reason="no_data")
                except (AttributeError, TypeError) as e:
                    self.ledger.append("DAILY_REPORT_ERROR", error=str(e))

            except asyncio.CancelledError:
                logger.info("[DAILY_REPORT_LOOP] shutdown")
                return
            except (OSError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                try:
                    self.ledger.append("DAILY_REPORT_LOOP_ERROR", error=str(exc))
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[BG_LOOPS] daily report ledger append failed: %s", exc, exc_info=True)
                await asyncio.sleep(3600)  # wait 1 hour on error

    async def _focus_loop(self):
        """FOCUS strategy background loop — 5-second tick."""
        await asyncio.sleep(30)  # initial warmup

        while True:
            try:
                # ★ A5 FIX: Emergency Stop propagation — halt both FOCUS and Harpoon
                if bool(getattr(self, 'emergency_stop', False)):
                    fm_check = getattr(self, "focus_manager", None)
                    if fm_check and fm_check.enabled:
                        fm_check.update_config({"enabled": False})
                        logger.warning("[FOCUS_LOOP] E-STOP → FOCUS disabled")
                    hm_check = getattr(self, "harpoon_manager", None)
                    if hm_check and hm_check.enabled:
                        hm_check.update_config({"enabled": False})
                        logger.warning("[FOCUS_LOOP] E-STOP → Harpoon disabled")
                    await asyncio.sleep(30.0)
                    continue

                fm = getattr(self, "focus_manager", None)
                if fm and fm.enabled:
                    from app.core.hyper_price_store import price_store
                    btc_price = price_store.get_price("BTCUSDT") or 0

                    # ★ Register FOCUS-held coins with price_feed (receive real-time prices)
                    try:
                        feed = getattr(self, "price_feed", None)
                        if feed and hasattr(feed, "add_symbol"):
                            for pos in (fm.positions or []):
                                if pos.market:
                                    feed.add_symbol(pos.market)
                            if fm.selected_market:
                                feed.add_symbol(fm.selected_market)
                    except Exception:
                        pass

                    await asyncio.get_running_loop().run_in_executor(
                        self._bg_executor, fm.tick, float(btc_price),
                    )

                    # ★ Harpoon tick — piggybacks on the FOCUS loop
                    try:
                        hm = getattr(self, "harpoon_manager", None)
                        if hm is None:
                            from app.manager.harpoon_manager import HarpoonManager
                            hm = HarpoonManager(focus_manager=fm, system=self)
                            self.harpoon_manager = hm
                        elif hm.focus is None and fm is not None:
                            hm.focus = fm
                            logger.info("[HARPOON] FocusManager re-linked to existing HarpoonManager")
                        if hm.enabled:
                            await asyncio.get_running_loop().run_in_executor(
                                self._bg_executor, hm.tick,
                            )
                    except Exception as h_exc:
                        # ★ A1 FIX: on Harpoon crash, revert to a safe state (no impact on FOCUS)
                        logger.warning("[HARPOON_LOOP] tick error (reset to STANDBY): %s", h_exc)
                        try:
                            if hm:
                                from app.manager.harpoon_manager import HarpoonState
                                hm.state = HarpoonState.STANDBY
                        except Exception:
                            pass

                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                logger.info("[FOCUS_LOOP] shutdown")
                return
            except Exception as exc:
                logger.warning("[FOCUS_LOOP] error: %s", exc)
                await asyncio.sleep(10.0)

    async def _upbit_gazua_loop(self):
        """Upbit spot FOCUS background loop — 5s tick. Fully isolated from Bybit FOCUS.

        Errors are confined by try/except and do not propagate to the system / Bybit FOCUS.
        On E-STOP, auto-disabled the same way as Bybit FOCUS.
        """
        await asyncio.sleep(35)  # warmup (offset from Bybit's 30 — load balancing)

        while True:
            try:
                if bool(getattr(self, 'emergency_stop', False)):
                    um_check = getattr(self, "upbit_gazua_manager", None)
                    if um_check and um_check.enabled:
                        um_check.update_config(enabled=False)
                        logger.warning("[UPBIT_FOCUS_LOOP] E-STOP → disabled")
                    await asyncio.sleep(30.0)
                    continue

                um = getattr(self, "upbit_gazua_manager", None)
                if um and um.enabled:
                    # btc_price unused (spot judges on its own) → pass 0.0
                    await asyncio.get_running_loop().run_in_executor(
                        self._bg_executor, um.tick, 0.0,
                    )

                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                logger.info("[UPBIT_FOCUS_LOOP] shutdown")
                return
            except Exception as exc:
                logger.warning("[UPBIT_FOCUS_LOOP] error: %s", exc)
                await asyncio.sleep(10.0)

    async def _bithumb_gazua_loop(self):
        """Bithumb spot FOCUS background loop — mirrors the Upbit loop, fully isolated.
        warmup 40s (offset from Upbit's 35 / Bybit's 30 — load balancing). Auto-disabled on E-STOP."""
        await asyncio.sleep(40)
        while True:
            try:
                if bool(getattr(self, 'emergency_stop', False)):
                    um_check = getattr(self, "bithumb_gazua_manager", None)
                    if um_check and um_check.enabled:
                        um_check.update_config(enabled=False)
                        logger.warning("[BITHUMB_FOCUS_LOOP] E-STOP → disabled")
                    await asyncio.sleep(30.0)
                    continue
                um = getattr(self, "bithumb_gazua_manager", None)
                if um and um.enabled:
                    await asyncio.get_running_loop().run_in_executor(
                        self._bg_executor, um.tick, 0.0,
                    )
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                logger.info("[BITHUMB_FOCUS_LOOP] shutdown")
                return
            except Exception as exc:
                logger.warning("[BITHUMB_FOCUS_LOOP] error: %s", exc)
                await asyncio.sleep(10.0)

    async def _bybit_spot_gazua_loop(self):
        """Bybit spot FOCUS background loop — mirrors the Upbit loop, fully isolated.
        warmup 45s (offset from Upbit's 35 / Bybit futures' 30 / Bithumb's 40 — load balancing). Auto-disabled on E-STOP."""
        await asyncio.sleep(45)
        while True:
            try:
                if bool(getattr(self, 'emergency_stop', False)):
                    um_check = getattr(self, "bybit_spot_gazua_manager", None)
                    if um_check and um_check.enabled:
                        um_check.update_config(enabled=False)
                        logger.warning("[BYBIT_SPOT_FOCUS_LOOP] E-STOP → disabled")
                    await asyncio.sleep(30.0)
                    continue
                um = getattr(self, "bybit_spot_gazua_manager", None)
                if um and um.enabled:
                    await asyncio.get_running_loop().run_in_executor(
                        self._bg_executor, um.tick, 0.0,
                    )
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                logger.info("[BYBIT_SPOT_FOCUS_LOOP] shutdown")
                return
            except Exception as exc:
                logger.warning("[BYBIT_SPOT_FOCUS_LOOP] error: %s", exc)
                await asyncio.sleep(10.0)

    async def _binance_spot_gazua_loop(self):
        """Binance spot FOCUS background loop — mirrors the Bybit spot loop, fully isolated.
        warmup 50s (offset from Upbit's 35 / Bybit futures' 30 / Bithumb's 40 / Bybit spot's 45 — load balancing). Auto-disabled on E-STOP."""
        await asyncio.sleep(50)
        while True:
            try:
                if bool(getattr(self, 'emergency_stop', False)):
                    um_check = getattr(self, "binance_spot_gazua_manager", None)
                    if um_check and um_check.enabled:
                        um_check.update_config(enabled=False)
                        logger.warning("[BINANCE_SPOT_FOCUS_LOOP] E-STOP → disabled")
                    await asyncio.sleep(30.0)
                    continue
                um = getattr(self, "binance_spot_gazua_manager", None)
                if um and um.enabled:
                    await asyncio.get_running_loop().run_in_executor(
                        self._bg_executor, um.tick, 0.0,
                    )
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                logger.info("[BINANCE_SPOT_FOCUS_LOOP] shutdown")
                return
            except Exception as exc:
                logger.warning("[BINANCE_SPOT_FOCUS_LOOP] error: %s", exc)
                await asyncio.sleep(10.0)

    async def _binance_futures_loop(self):
        """Binance USDT-M futures FOCUS background loop — mirrors Bybit FOCUS (_focus_loop), fully isolated.
        warmup 55s (load balancing). Auto-disabled on E-STOP."""
        await asyncio.sleep(55)
        while True:
            try:
                if bool(getattr(self, 'emergency_stop', False)):
                    fm_check = getattr(self, "binance_futures_manager", None)
                    if fm_check and fm_check.enabled:
                        fm_check.update_config({"enabled": False})
                        logger.warning("[BINANCE_FUTURES_LOOP] E-STOP → disabled")
                    await asyncio.sleep(30.0)
                    continue
                fm = getattr(self, "binance_futures_manager", None)
                if fm and fm.enabled:
                    from app.core.hyper_price_store import price_store
                    btc_price = price_store.get_price("BTCUSDT") or 0
                    try:
                        feed = getattr(self, "price_feed", None)
                        if feed and hasattr(feed, "add_symbol"):
                            for pos in (fm.positions or []):
                                if pos.market:
                                    feed.add_symbol(pos.market)
                            if fm.selected_market:
                                feed.add_symbol(fm.selected_market)
                    except Exception:
                        pass
                    await asyncio.get_running_loop().run_in_executor(
                        self._bg_executor, fm.tick, float(btc_price),
                    )
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                logger.info("[BINANCE_FUTURES_LOOP] shutdown")
                return
            except Exception as exc:
                logger.warning("[BINANCE_FUTURES_LOOP] error: %s", exc)
                await asyncio.sleep(10.0)

    async def _contrarian_loop(self):
        """Periodic Contrarian auto-scan (auto-detect contrarian coins and alert)."""
        from app.core.contrarian_scanner import get_contrarian_scanner
        from app.core.constants import env_bool, env_int

        enabled = env_bool("CONTRARIAN_AUTO_SCAN_ENABLED", default=True)
        interval_sec = env_int("CONTRARIAN_AUTO_SCAN_SEC", default=300)  # every 5 minutes

        if not enabled:
            return

        # Initial wait (accumulate price data)
        await asyncio.sleep(120)

        while True:
            try:
                await asyncio.sleep(interval_sec)

                scanner = get_contrarian_scanner()
                if not scanner.enabled:
                    continue

                # Fetch the market list
                markets = list(self.coordinator._contexts.keys()) if hasattr(self.coordinator, '_contexts') else []
                if not markets:
                    continue

                # If scan_gate is held, skip this cycle (5-min interval, so run next cycle)
                if self._scan_gate.locked():
                    logger.debug("[ContrarianLoop] scan_gate locked — skipping cycle")
                    continue

                # Run the scan (alerts are handled inside the scanner)
                await asyncio.to_thread(scanner.scan, markets, True, "BTC")

            except asyncio.CancelledError:
                logger.info("[CONTRARIAN_LOOP] shutdown")
                return
            except (KeyError, AttributeError, TypeError) as exc:
                try:
                    self.ledger.append("CONTRARIAN_LOOP_ERROR", error=str(exc))
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[BG_LOOPS] contrarian scan ledger append failed: %s", exc, exc_info=True)

    async def _volume_spike_update_loop(self) -> None:
        """Periodically update Volume Spike Detector data."""
        _vs_interval = _env_int("OMA_VOLUME_SPIKE_UPDATE_MIN", 10) * 60
        await asyncio.sleep(120)
        while True:
            try:
                from app.monitor.volume_spike_detector import get_volume_spike_detector
                from app.integrations.bybit_markets import fetch_bybit_markets, filter_quote_markets
                detector = get_volume_spike_detector()
                if detector:
                    # If scan_gate is held, skip (10-min interval, so one delayed cycle is fine)
                    if self._scan_gate.locked():
                        logger.debug("[VolumeSpikeLoop] scan_gate locked — skipping cycle")
                    else:
                        def _vs_sync_update():
                            raw = fetch_bybit_markets()
                            usdt = filter_quote_markets(raw)
                            markets = list(usdt)[:80]
                            detector.update_volume_data(markets)
                            detector.detect_spikes()
                            return len(markets), len(detector.recent_signals)
                        _n_mkts, _n_sigs = await asyncio.to_thread(_vs_sync_update)
                        logger.debug("[VolumeSpikeLoop] updated %s markets, signals=%s", _n_mkts, _n_sigs)
            except asyncio.CancelledError:
                logger.info("[VolumeSpikeLoop] shutdown")
                return
            except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[VolumeSpikeLoop] error: %s", exc)
            await asyncio.sleep(_vs_interval)

    async def _watchlist_subscribe_loop(self) -> None:
        """Sync _last_prefetch_markets → price_feed._manual_symbols.

        After the first autopilot scan populates _last_prefetch_markets,
        this loop registers the top-N markets with the WebSocket feed so
        price_store/orderbook_store receive real-time data for them.
        On subsequent runs, markets that fell off the list are unsubscribed.
        """
        _interval = max(60, _env_int("OMA_WATCHLIST_SUBSCRIBE_SEC", 300))
        _max_ws = max(10, _env_int("OMA_WATCHLIST_SUBSCRIBE_MAX", 80))
        await asyncio.sleep(150.0)  # stagger: wait for watchlist+candle to settle first

        _prev_set: set[str] = set()

        while True:
            try:
                from app.manager.reserved_selector import _last_prefetch_markets
                targets = [m.upper() for m in (_last_prefetch_markets or []) if m][:_max_ws]
                new_set = set(targets)

                added = new_set - _prev_set
                removed = _prev_set - new_set

                for sym in added:
                    self.price_feed.add_symbol(sym)
                for sym in removed:
                    self.price_feed.remove_symbol(sym)

                if added or removed:
                    logger.info("[WatchlistSubscribe] +%d/-%d symbols (total=%d)",
                                len(added), len(removed), len(new_set))
                else:
                    logger.debug("[WatchlistSubscribe] no changes (%d symbols)", len(new_set))

                _prev_set = new_set
            except asyncio.CancelledError:
                logger.info("[WatchlistSubscribe] shutdown")
                return
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[WatchlistSubscribe] error: %s", exc)
            await asyncio.sleep(_interval)

    async def _strategy_recommend_loop(self) -> None:
        """Pre-warm per-strategy recommended coins serially in the background (avoid SLOW_TICK).

        When the dashboard loads the strategy screen it calls /recommendations in bursts,
        saturating the thread pool and delaying the tick loop. By having this loop compute
        and cache each strategy in advance, in order, dashboard requests return immediately
        as cache hits.

        Cadence: start 30s after boot → 45s gap per strategy → 120s rest after a full cycle
        Full cycle: 7 strategies × 45s + 120s = ~435s ≈ 7.25min (recommend read-TTL of 600s covers this)
        ★ LIGHTNING first — the first active plugin attached in v4 → warms up fastest after boot
        """
        _STRATEGIES = ["LIGHTNING", "PINGPONG", "AUTOLOOP", "GAZUA", "CONTRARIAN", "SNIPER", "LADDER"]
        _INTER_SEC = 45.0    # gap between strategies — 15→45 (reduces tick GIL contention)
        _CYCLE_SEC = 120.0   # rest after a full cycle — 60→120

        # OMA_PREWARM_ENABLED=0 → disable background prewarm (compute only on dashboard request)
        if os.getenv("OMA_PREWARM_ENABLED", "1").strip() == "0":
            logger.info("[Prewarm] disabled (OMA_PREWARM_ENABLED=0) — on-demand mode")
            return

        await asyncio.sleep(30.0)  # wait for boot to stabilize

        while True:
            for strategy in _STRATEGIES:
                try:
                    from app.api.strategy_router import prewarm_recommendation
                    # Wait on scan_gate: prevent running concurrently with autopilot scan (rate limiter / GIL contention)
                    # Use the dedicated _scan_executor → avoid tick loop thread-pool contention
                    async with self._scan_gate:
                        _loop = asyncio.get_event_loop()
                        await _loop.run_in_executor(
                            self._scan_executor, prewarm_recommendation, self, strategy
                        )
                except asyncio.CancelledError:
                    logger.info("[StrategyRecommendLoop] shutdown")
                    return
                except (ImportError, AttributeError, TypeError) as exc:
                    logger.warning("strategy_recommend_prewarm %s: %s", strategy, exc)
                await asyncio.sleep(_INTER_SEC)
            await asyncio.sleep(_CYCLE_SEC)

    async def _process_market(self, market: str) -> None:
        """Process a single market tick (async/parallel friendly)."""
        try:
            price = price_store.get_price(market)
            volume = price_store.get_volume(market)

            # Always set the context regardless of whether price exists
            ctx = self.coordinator.ensure_market(market)
            ctx.market_state = self.oma_registry.get_state(market).value
            ctx.trading_mode = self.trading_mode
            ctx.recovery = (ctx.market_state == "RECOVERY")

            # ------------------------------------------
            # No price: try REST fallback, then warmup tick
            # ------------------------------------------
            if price is None:
                # ★ WebSocket-drop fallback: fetch price via REST API → store in price_store
                try:
                    from app.integrations.bybit_trade import BybitTradeClient
                    _fc = getattr(self, "_price_fallback_client", None)
                    if _fc is None:
                        _fc = BybitTradeClient(category="linear")
                        self._price_fallback_client = _fc
                    _p = _fc._linear_last_price(market)
                    if _p and _p > 0:
                        price_store.set_price(market, float(_p))
                        price = float(_p)
                except Exception:
                    pass

                if price is None:
                    # use a dummy price to keep warmup/ticks progressing
                    await asyncio.to_thread(self.coordinator.tick, market, 0.0)
                    return

            # ------------------------------------------
            # Price present: normal tick path
            # ------------------------------------------

            # 1) pending order progression
            if self.order_fsm and getattr(ctx, "order_state", None):
                try:
                    # PATCH 2025-12-26: pass best_bid for LIMIT-TP retry pricing
                    ob = orderbook_store.get(market) or {}
                    best_bid = None
                    try:
                        best_bid = float(ob.get("best_bid")) if ob else None
                    except (KeyError, AttributeError, TypeError, ValueError):
                        logger.warning("[_process_market] best_bid parse error for %s", market, exc_info=True)
                        best_bid = None

                    prog = await asyncio.get_running_loop().run_in_executor(
                        self._order_executor,
                        functools.partial(
                            self.order_fsm.process_pending,
                            ctx=ctx,
                            market=market,
                            current_price=float(price),
                            current_bid=best_bid,
                        )
                    )

                    if prog.get("needs_emergency_stop"):
                        if not getattr(self, "emergency_manual_override", False):
                            self.set_emergency_stop(
                                True,
                                reason=str(prog.get("reason") or "order_fsm")
                            )

                    if prog.get("needs_recovery"):
                        pass
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    self.ledger.append(
                        "ORDER_FSM_FATAL",
                        market=market,
                        error=str(exc)
                    )

            # ──────────────────────────────────────────────────────
            # [PERF] Price-Change Gate  (2026-03-18)
            # ──────────────────────────────────────────────────────
            # If price hasn't changed there's no new info → indicators / AI / strategy results are identical
            # → save coordinator.tick CPU cost (~25ms/market), relieve GIL serialization bottleneck
            #
            # Guarantee: at least one full tick every 3s (protects time-based logic: time-stop etc.)
            # Impact: order_fsm always runs above (independent of order-state tracking)
            # ──────────────────────────────────────────────────────
            _pcg_prev = getattr(ctx, '_pcg_last_price', None)
            _pcg_now = time.time()
            _pcg_changed = (_pcg_prev is None or float(price) != _pcg_prev)

            # Per-market staggered init: 0~3s random offset on the first tick
            # → prevent a thundering herd where all 3s timeouts expire at once
            if not hasattr(ctx, '_pcg_last_full_ts'):
                import random
                ctx._pcg_last_full_ts = _pcg_now - random.uniform(0.0, 3.0)

            _pcg_elapsed = _pcg_now - ctx._pcg_last_full_ts

            # SLOW_MARKET self-throttle: if 200ms is repeatedly exceeded, widen the full-tick interval
            _slow_cnt = int(getattr(ctx, '_slow_market_cnt', 0))
            _tick_interval = 3.0 if _slow_cnt < 10 else min(3.0 + _slow_cnt * 0.1, 6.0)

            if not _pcg_changed and _pcg_elapsed < _tick_interval:
                # [DIAG] skip count
                self._diag_skip_ticks = getattr(self, '_diag_skip_ticks', 0) + 1
                return  # price unchanged + tick interval not elapsed → skip full tick

            ctx._pcg_last_price = float(price)
            ctx._pcg_last_full_ts = _pcg_now

            # Decay the SLOW_MARKET counter (recovers on fast ticks)
            if int(getattr(ctx, '_slow_market_cnt', 0)) > 0:
                ctx._slow_market_cnt = max(0, int(ctx._slow_market_cnt) - 1)

            # [DIAG] full tick count + timing
            self._diag_full_ticks = getattr(self, '_diag_full_ticks', 0) + 1
            _t_coord_start = time.perf_counter()

            # 2) coordinator tick (normal price)
            # -----------------------------------------------------------------
            # 🚨 TICK SAFETY (DO NOT REMOVE)
            # tick exactly once: a duplicate tick double-advances warmup/strategy decisions.
            # -----------------------------------------------------------------
            # [PERF] synchronous execution — removed asyncio.to_thread (2026-03-18)
            # Reason: spreading CPU-bound work across N threads is actually slower due to GIL contention
            # Synchronous execution has zero GIL-switching overhead and consistent performance
            out = self.coordinator.tick(market, float(price), float(volume or 0.0))

            # [DIAG] per-market coordinator.tick time — record when it exceeds 200ms
            _t_coord_ms = (time.perf_counter() - _t_coord_start) * 1000
            if _t_coord_ms > 200:
                # [2026-03-24] TICK_DIAG_SLOW — removed ledger record (moved to dedicated tick_perf.jsonl log)
                # self.ledger.append("TICK_DIAG_SLOW", market=market, coord_ms=round(_t_coord_ms))
                pass
                _slow_cnt = int(getattr(ctx, '_slow_market_cnt', 0)) + 1
                ctx._slow_market_cnt = _slow_cnt
                # [PERF-LOG] includes coordinator internal breakdown (CPU time + thread count for GIL diagnosis)
                if self._perf_ledger is not None:
                    import threading as _thr
                    self._perf_ledger.append("SLOW_MARKET", market=market,
                        coord_ms=round(_t_coord_ms, 1),
                        selector_ms=round(getattr(ctx, '_perf_selector_ms', 0), 1),
                        risk_ms=round(getattr(ctx, '_perf_risk_ms', 0), 1),
                        engine_ms=round(getattr(ctx, '_perf_engine_ms', 0), 1),
                        engine_cpu_ms=round(getattr(ctx, '_perf_engine_cpu_ms', 0), 1),
                        threads=_thr.active_count(),
                    )

            engine_out = None
            if isinstance(out, dict):
                engine_out = out.get("engine_out")

            # 2.1) STRATEGY telemetry snapshot (generic)
            # [2026-03-14] Auto-collect AI-training SNAPSHOTs from every strategy
            try:
                if isinstance(engine_out, dict):
                    strategy_out = engine_out.get("strategy_out")
                    if isinstance(strategy_out, dict):
                        meta = strategy_out.get("meta")
                        # Existing telemetry_emit approach (AUTOLOOP etc.)
                        if isinstance(meta, dict) and meta.get("telemetry_emit") and isinstance(meta.get("telemetry"), dict):
                            snap = dict(meta.get("telemetry") or {})
                            snap.pop("market", None)
                            mode = str(strategy_out.get("mode") or "UNKNOWN").upper()
                            event_name = f"{mode}_SNAPSHOT"
                            self.ledger.append(event_name, market=market, **snap)
                        # [NEW] Strategies without telemetry also get a basic SNAPSHOT from brain + meta
                        elif isinstance(meta, dict) and not meta.get("telemetry_emit"):
                            _snap_key = f"_last_snapshot_ts_{market}"
                            _snap_now = time.time()
                            _snap_last = getattr(self, _snap_key, 0.0)
                            if _snap_now - _snap_last >= 60.0:  # 60s throttle
                                setattr(self, _snap_key, _snap_now)
                                mode = str(strategy_out.get("mode") or "UNKNOWN").upper()
                                snap = {"price": float(price)}
                                # Extract AI features from brain
                                try:
                                    _brain = getattr(ctx, "current_ai", {}) or {}
                                    if isinstance(_brain, dict):
                                        _bd = _brain.get("brain", {}) or {}
                                        for _bk in ("ai_prediction", "confidence", "regime",
                                                     "trend", "momentum", "volatility", "rsi",
                                                     "macd_histogram", "volume_change"):
                                            if _bk in _bd:
                                                snap[_bk] = _bd[_bk]
                                except (KeyError, AttributeError, TypeError) as exc:
                                    logger.warning("[BG_LOOPS] brain AI feature extraction: %s", exc, exc_info=True)
                                # Extract useful metrics from meta
                                for _mk in ("profit_pct", "regime", "tp_pct", "sl_pct",
                                             "dynamic_tp", "dynamic_sl", "atr_pct"):
                                    if _mk in meta:
                                        snap[_mk] = meta[_mk]
                                self.ledger.append(f"{mode}_SNAPSHOT", market=market, **snap)
            except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                self.ledger.append("STRATEGY_SNAPSHOT_ERROR", market=market, error=str(exc))

            # [④] Auto profit lock-in check (before intent handling, when a position is held)
            if self.profit_lock_enabled:
                await self._check_profit_lock_tick(market, float(price), ctx)

            # [2026-03-24] Peak Drawdown Guard (defend against reversal after approaching TP)
            if self.peak_drawdown_guard_enabled:
                await self._check_peak_drawdown_tick(market, float(price), ctx)

            # 3) apply intents (LIVE)
            if self.order_fsm and isinstance(engine_out, dict):
                intent = engine_out.get("intent")
                if isinstance(intent, dict):
                    await self._handle_intent(
                        market=market,
                        price=float(price),
                        ctx=ctx,
                        intent=intent,
                    )

            # 4) reserved WATCH candidate → promote to ACTIVE once a real position exists
            # Owner's model: WATCH = target candidate / actual entry (position held) = ACTIVE coin.
            # Since 8553bef (approval = WATCH always) there was no ACTIVE-promotion path for normal
            # plugin entries, so entries stayed in WATCH → reserved_watch tick kept running but the
            # state display/management was out of sync. This fixes that.
            try:
                if getattr(ctx, "position", None) is not None:
                    from app.manager.oma_market_registry import MarketState as _MS
                    if self.oma_registry.get_state(market) == _MS.WATCH:
                        self.oma_set_market(market, _MS.ACTIVE, reason=["entry_filled", "watch_to_active"])
                        self.ledger.append("WATCH_TO_ACTIVE", market=market, reason="position_detected")
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[_process_market] WATCH→ACTIVE promote failed %s: %s", market, exc)
        except Exception as e:
            self.ledger.append("MARKET_TICK_ERROR", market=market, error=str(e))
