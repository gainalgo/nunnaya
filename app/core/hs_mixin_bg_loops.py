"""Phase 5F — Background loop methods extracted from HyperSystem.

── ASYNC SAFETY RULES ──
모든 루프는 async. 동기 블로킹 작업은 반드시 asyncio.to_thread() 사용.
- requests.get/post → asyncio.to_thread() 필수
- File I/O (ledger.tail 등) → asyncio.to_thread() 필수
- _scan_gate 점유 시 timeout 설정 필수 (교착 방지)
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
                # Check every N minutes (기본 6시간, OMA_AI_RETRAIN_INTERVAL_SEC으로 조정)
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
        """매일 자정(00:05)에 일일 리포트를 텔레그램으로 발송."""
        from app.core.constants import env_bool
        from datetime import datetime, timedelta

        enabled = env_bool("DAILY_REPORT_TELEGRAM_ENABLED", default=True)

        if not enabled:
            return

        while True:
            try:
                # 다음 00:05까지 대기 시간 계산
                now = datetime.now()
                tomorrow = now.replace(hour=0, minute=5, second=0, microsecond=0)
                if now >= tomorrow:
                    tomorrow += timedelta(days=1)

                wait_sec = (tomorrow - now).total_seconds()
                await asyncio.sleep(wait_sec)

                # 어제 날짜 리포트 발송 (자정 넘겼으니 어제 = 전일)
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

                try:
                    from app.manager.daily_pnl import get_daily_pnl_manager
                    mgr = get_daily_pnl_manager()

                    # 어제 리포트 로드 및 발송
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
                await asyncio.sleep(3600)  # 에러 시 1시간 대기

    async def _focus_loop(self):
        """FOCUS strategy background loop — 5-second tick."""
        await asyncio.sleep(30)  # initial warmup

        while True:
            try:
                # ★ A5 FIX: Emergency Stop 전파 — FOCUS/Harpoon 모두 정지
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

                    # ★ FOCUS 보유 코인을 price_feed에 등록 (실시간 가격 수신)
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

                    # ★ Harpoon (작살) tick — FOCUS 루프에 편승
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
                        # ★ A1 FIX: Harpoon crash 시 안전 상태로 복귀 (FOCUS에 영향 없음)
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
        """Upbit 현물 FOCUS background loop — 5초 tick. Bybit FOCUS 와 완전 격리.

        에러는 try/except 로 가둬 시스템/Bybit FOCUS 에 전파되지 않음.
        E-STOP 시 Bybit FOCUS 와 동일하게 자동 비활성화.
        """
        await asyncio.sleep(35)  # warmup (Bybit 30 과 어긋나게 — 부하 분산)

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
                    # btc_price 미사용(현물 자체판단) → 0.0 전달
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
        """Bithumb 현물 FOCUS background loop — Upbit loop 미러, 완전 격리.
        warmup 40s (Upbit 35·Bybit 30 과 어긋나게 — 부하 분산). E-STOP 시 자동 비활성화."""
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
        """Bybit 현물 FOCUS background loop — Upbit loop 미러, 완전 격리.
        warmup 45s (Upbit 35·Bybit선물 30·Bithumb 40 과 어긋나게 — 부하 분산). E-STOP 시 자동 비활성화."""
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

    async def _contrarian_loop(self):
        """Periodic Contrarian auto-scan (역행 코인 자동 감지 및 알림)."""
        from app.core.contrarian_scanner import get_contrarian_scanner
        from app.core.constants import env_bool, env_int

        enabled = env_bool("CONTRARIAN_AUTO_SCAN_ENABLED", default=True)
        interval_sec = env_int("CONTRARIAN_AUTO_SCAN_SEC", default=300)  # 5분마다

        if not enabled:
            return

        # 초기 대기 (가격 데이터 축적)
        await asyncio.sleep(120)

        while True:
            try:
                await asyncio.sleep(interval_sec)

                scanner = get_contrarian_scanner()
                if not scanner.enabled:
                    continue

                # 마켓 리스트 가져오기
                markets = list(self.coordinator._contexts.keys()) if hasattr(self.coordinator, '_contexts') else []
                if not markets:
                    continue

                # scan_gate 보유 중이면 이번 사이클 스킵 (5분 주기라 다음 사이클에 실행)
                if self._scan_gate.locked():
                    logger.debug("[ContrarianLoop] scan_gate locked — skipping cycle")
                    continue

                # 스캔 실행 (알림은 scanner 내부에서 처리)
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
        """Volume Spike Detector 데이터를 주기적으로 업데이트."""
        _vs_interval = _env_int("OMA_VOLUME_SPIKE_UPDATE_MIN", 10) * 60
        await asyncio.sleep(120)
        while True:
            try:
                from app.monitor.volume_spike_detector import get_volume_spike_detector
                from app.integrations.bybit_markets import fetch_bybit_markets, filter_quote_markets
                detector = get_volume_spike_detector()
                if detector:
                    # scan_gate 보유 중이면 스킵 (10분 주기라 한 사이클 늦어도 무방)
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
        """전략별 추천코인을 백그라운드에서 직렬로 pre-warm (SLOW_TICK 방지).

        대시보드가 전략화면 로드 시 동시 다발적으로 /recommendations를 호출하면
        스레드풀이 포화되어 tick loop가 지연된다. 이 루프가 각 전략을 미리,
        순서대로 계산·캐싱해두면 대시보드 요청은 캐시 히트로 즉시 반환된다.

        주기: 부팅 30초 후 시작 → 전략마다 45초 간격 → 한 사이클 완료 후 120초 휴식
        총 사이클: 7전략 × 45초 + 120초 = ~435초 ≈ 7.25분 (recommend read-TTL 600초가 이를 덮음)
        ★ LIGHTNING 을 첫 순서로 — v4 에서 가장 먼저 붙이는 활성 plugin → 부팅 후 가장 빨리 데움
        """
        _STRATEGIES = ["LIGHTNING", "PINGPONG", "AUTOLOOP", "GAZUA", "CONTRARIAN", "SNIPER", "LADDER"]
        _INTER_SEC = 45.0    # 전략 간 대기 — 15→45 (tick GIL 경합 감소)
        _CYCLE_SEC = 120.0   # 한 사이클 완료 후 휴식 — 60→120

        # OMA_PREWARM_ENABLED=0 → 백그라운드 prewarm 비활성화 (대시보드 요청 시만 계산)
        if os.getenv("OMA_PREWARM_ENABLED", "1").strip() == "0":
            logger.info("[Prewarm] disabled (OMA_PREWARM_ENABLED=0) — on-demand mode")
            return

        await asyncio.sleep(30.0)  # 부팅 안정화 대기

        while True:
            for strategy in _STRATEGIES:
                try:
                    from app.api.strategy_router import prewarm_recommendation
                    # scan_gate 대기: autopilot scan과 동시 실행 방지 (rate limiter / GIL 경합)
                    # 전용 _scan_executor 사용 → tick loop 스레드풀 경합 방지
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

            # 컨텍스트는 price 유무와 무관하게 항상 세팅
            ctx = self.coordinator.ensure_market(market)
            ctx.market_state = self.oma_registry.get_state(market).value
            ctx.trading_mode = self.trading_mode
            ctx.recovery = (ctx.market_state == "RECOVERY")

            # ------------------------------------------
            # price 없는 경우: REST 폴백 시도 후 warmup tick
            # ------------------------------------------
            if price is None:
                # ★ WebSocket 끊김 대비: REST API로 가격 조회 → price_store에 저장
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
                    # warmup/ticks 진행을 위해 dummy price 사용
                    await asyncio.to_thread(self.coordinator.tick, market, 0.0)
                    return

            # ------------------------------------------
            # price 있는 경우: 정상 tick 경로
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
            # 가격이 바뀌지 않았으면 새 정보 없음 → 인디케이터·AI·전략 결과 동일
            # → coordinator.tick CPU 비용(~25ms/마켓) 절감, GIL 직렬화 병목 해소
            #
            # 보장: 최소 3초마다 1회 full tick (시간 기반 로직 보호: time-stop 등)
            # 영향: order_fsm은 위에서 항상 실행됨 (주문 상태 추적 무관)
            # ──────────────────────────────────────────────────────
            _pcg_prev = getattr(ctx, '_pcg_last_price', None)
            _pcg_now = time.time()
            _pcg_changed = (_pcg_prev is None or float(price) != _pcg_prev)

            # 마켓별 분산 초기화: 첫 tick에서 0~3초 랜덤 오프셋
            # → 3초 타임아웃이 동시 만료되는 thundering herd 방지
            if not hasattr(ctx, '_pcg_last_full_ts'):
                import random
                ctx._pcg_last_full_ts = _pcg_now - random.uniform(0.0, 3.0)

            _pcg_elapsed = _pcg_now - ctx._pcg_last_full_ts

            # SLOW_MARKET 자중: 200ms 초과가 반복되면 full tick 간격을 넓힘
            _slow_cnt = int(getattr(ctx, '_slow_market_cnt', 0))
            _tick_interval = 3.0 if _slow_cnt < 10 else min(3.0 + _slow_cnt * 0.1, 6.0)

            if not _pcg_changed and _pcg_elapsed < _tick_interval:
                # [DIAG] 스킵 카운트
                self._diag_skip_ticks = getattr(self, '_diag_skip_ticks', 0) + 1
                return  # 가격 미변경 + tick 간격 미경과 → full tick 스킵

            ctx._pcg_last_price = float(price)
            ctx._pcg_last_full_ts = _pcg_now

            # SLOW_MARKET 카운터 감쇠 (빠른 tick이면 복구)
            if int(getattr(ctx, '_slow_market_cnt', 0)) > 0:
                ctx._slow_market_cnt = max(0, int(ctx._slow_market_cnt) - 1)

            # [DIAG] full tick 카운트 + 타이밍
            self._diag_full_ticks = getattr(self, '_diag_full_ticks', 0) + 1
            _t_coord_start = time.perf_counter()

            # 2) coordinator tick (정상 price)
            # -----------------------------------------------------------------
            # 🚨 TICK SAFETY (DO NOT REMOVE)
            # tick은 1회만: 중복 tick은 warmup/전략 판정을 2배로 밀어버립니다.
            # -----------------------------------------------------------------
            # [PERF] 동기 실행 — asyncio.to_thread 제거 (2026-03-18)
            # 이유: CPU-bound 작업을 N개 스레드에 넣으면 GIL 경합으로 오히려 느려짐
            # 동기 실행 시 GIL 스위칭 오버헤드 0, 일정한 성능
            out = self.coordinator.tick(market, float(price), float(volume or 0.0))

            # [DIAG] 마켓별 coordinator.tick 시간 — 200ms 초과 시 기록
            _t_coord_ms = (time.perf_counter() - _t_coord_start) * 1000
            if _t_coord_ms > 200:
                # [2026-03-24] TICK_DIAG_SLOW — 원장 기록 제거 (tick_perf.jsonl 전용 로그로 이관)
                # self.ledger.append("TICK_DIAG_SLOW", market=market, coord_ms=round(_t_coord_ms))
                pass
                _slow_cnt = int(getattr(ctx, '_slow_market_cnt', 0)) + 1
                ctx._slow_market_cnt = _slow_cnt
                # [PERF-LOG] coordinator 내부 분해 포함 (CPU time + thread count for GIL diagnosis)
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
            # [2026-03-14] 모든 전략에서 AI 학습용 SNAPSHOT 자동 수집
            try:
                if isinstance(engine_out, dict):
                    strategy_out = engine_out.get("strategy_out")
                    if isinstance(strategy_out, dict):
                        meta = strategy_out.get("meta")
                        # 기존 telemetry_emit 방식 (AUTOLOOP 등)
                        if isinstance(meta, dict) and meta.get("telemetry_emit") and isinstance(meta.get("telemetry"), dict):
                            snap = dict(meta.get("telemetry") or {})
                            snap.pop("market", None)
                            mode = str(strategy_out.get("mode") or "UNKNOWN").upper()
                            event_name = f"{mode}_SNAPSHOT"
                            self.ledger.append(event_name, market=market, **snap)
                        # [NEW] telemetry 없는 전략도 brain + meta에서 기본 SNAPSHOT 생성
                        elif isinstance(meta, dict) and not meta.get("telemetry_emit"):
                            _snap_key = f"_last_snapshot_ts_{market}"
                            _snap_now = time.time()
                            _snap_last = getattr(self, _snap_key, 0.0)
                            if _snap_now - _snap_last >= 60.0:  # 60초 throttle
                                setattr(self, _snap_key, _snap_now)
                                mode = str(strategy_out.get("mode") or "UNKNOWN").upper()
                                snap = {"price": float(price)}
                                # brain에서 AI 피처 추출
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
                                # meta에서 유용한 지표 추출
                                for _mk in ("profit_pct", "regime", "tp_pct", "sl_pct",
                                             "dynamic_tp", "dynamic_sl", "atr_pct"):
                                    if _mk in meta:
                                        snap[_mk] = meta[_mk]
                                self.ledger.append(f"{mode}_SNAPSHOT", market=market, **snap)
            except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                self.ledger.append("STRATEGY_SNAPSHOT_ERROR", market=market, error=str(exc))

            # [④] 수익 자동 락인 체크 (intent 처리 전, 포지션 보유 시)
            if self.profit_lock_enabled:
                await self._check_profit_lock_tick(market, float(price), ctx)

            # [2026-03-24] Peak Drawdown Guard (TP 근접 후 반전 방어)
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

            # 4) reserved WATCH 후보 → 실포지션 생기면 ACTIVE 승격
            # 부모님 모델: WATCH=대상 후보 / 실제 진입(position 보유)하면 ACTIVE 코인.
            # 8553bef(승인=WATCH always) 이후 일반 plugin 진입의 ACTIVE 승격 경로가 없어
            # 진입해도 WATCH 잔류 → reserved_watch tick 유지되나 상태 표시/관리가 어긋나던 문제 fix.
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
