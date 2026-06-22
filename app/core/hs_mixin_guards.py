"""Phase 5D mixin -- guard / safety / throttle methods.

── ASYNC SAFETY RULES ──
이 파일의 함수는 async context(이벤트 루프)에서 호출됨.
- requests.get/post 직접 호출 금지 → asyncio.to_thread() 필수
- 고점/캔들 조회는 candle_cache 또는 price_store에서만 (HTTP 금지)
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
        """컨텍스트에서 현재 전략의 effective TP%를 가져온다."""
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
        """최근 가격 변화율로 단순 상승/하락 레짐을 분류한다.

        반환:
            (regime, change_pct)

        - regime: "BULL" | "BEAR" | "NEUTRAL" | "UNKNOWN"
        - change_pct: lookback 구간 기준 변화율(%). 계산 불가 시 None.

        NOTE:
        - 이 값은 '진입/재진입 가드'를 위한 보조 정보다.
        - 신호 생성(전략)과 분리되어 있어, 전략이 무엇이든 동일한 안전 레이어를 적용할 수 있다.
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

        # lookback 정규화
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
                logger.warning("[GUARD] lookback 정규화: %s", exc, exc_info=True)
            return "UNKNOWN", None

        if base <= 0.0:
            return "UNKNOWN", None

        change_pct = (last - base) / base * 100.0

        # 모멘텀(직전 tick 대비)
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
        """최근 N시간 고점을 계산한다 (api + local fallback).

        반환:
            (recent_high, source)
            source: "api", "local", "api+local", "none"

        설계:
        - Bybit 모드에서는 public candles(분봉)에서 고점을 우선 계산한다.
        - API 실패/제한 시 ctx.price_history의 local high로 fail-open한다.
        - 과도한 API 호출을 막기 위해 짧은 TTL 캐시를 사용한다.
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

            # [2026-03-30] requests.get 제거 — 이벤트 루프 블로킹 원인.
            # candle_loader 캐시에서 고점 조회 (HTTP 호출 없음, 메모리만)
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
        """직전 EXIT 가격을 기준으로 재진입 한계가격(ceiling)을 계산한다.

        - fee_rate는 round-trip(매도+매수) 2회분을 보수적으로 반영한다.
        - slippage/spread/extra bps는 price 기준 추가 버퍼로 사용한다.
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

        # misconfig 방지: 20% 이상은 비정상으로 보고 캡
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
        """전역 BUY 쿨다운을 설정한다. (SELL은 계속 허용)

        - _handle_intent(BUY)에서 이 값을 참조하여 신규 진입을 차단한다.
        - 시장별(entry_block_until_ts)에도 전파하여 UI/관측이 가능하도록 한다.
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

        # ACTIVE 컨텍스트에 전파(관측/일관성 목적)
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

        # 로그(짧은 주기 스팸 방지)
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
        """계정 equity 기반 최대 손실(Drawdown) 가드.

        - OMA_DRAWDOWN_GUARD=1일 때 동작.
        - base_equity 대비 (base - equity)/base 가 threshold를 넘으면:
            * COOLDOWN     : 일정 시간 BUY 차단(자동 해제)
            * RECOVERY     : BUY 차단 + ACTIVE → RECOVERY 승격(운영자 확인 필요)
            * EMERGENCY_STOP : BUY 차단 + ACTIVE → RECOVERY 승격(동일 동작; 명시적 이름)
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

        # base 설정: fixed_principal 모드면 principal_base_equity를 우선 사용
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
            # 최초 1회 base를 설정하고 종료
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

        # 너무 자주 반복 트리거 방지(단, COOLDOWN은 연장 가능)
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

        # 1) COOLDOWN: 자동 해제형(일시 정지)
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

        # 2) RECOVERY / EMERGENCY_STOP: 운영자 개입형(한 번 걸리면 latch)
        if bool(getattr(self, "_drawdown_latched", False)):
            return
        try:
            self._drawdown_latched = True
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[GUARD] drawdown latch set: %s", exc, exc_info=True)

        # BUY 차단(SELL은 계속 허용)
        if not bool(getattr(self, "emergency_stop", False)):
            try:
                self.set_emergency_stop(True, reason=msg)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.error("[Guards] set_emergency_stop() failed, forcing directly", exc_info=True)
                self.emergency_stop = True
                self.ledger.append("EMERGENCY_STOP_SET", enabled=True, reason=msg)

        # ACTIVE → RECOVERY 승격
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
        """ENTRY_BLOCKED/EXIT_BLOCKED 로그를 스팸 방지(주기 제한)로 기록."""
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
        """텔레그램 알림(실패해도 시스템 동작은 계속). daemon 스레드로 fire-and-forget."""
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
        """SOFT/WARN 메시지를 텔레그램으로 1회(주기 제한)만 알림."""
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
        """Orderbook guard(spread/stale) 차단 시 점진적 쿨다운 적용.

        연속 차단이 누적될수록 쿨다운이 길어짐 (30s → 60s → 120s → ... max 300s).
        10분간 차단이 없으면 streak이 리셋됨.
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
        """진입 성공 시 해당 마켓의 OB block streak 리셋."""
        try:
            self._ob_block_streak.pop(market, None)
        except (AttributeError, TypeError) as exc:
            logger.warning("[GUARD] _reset_ob_block_streak: %s", exc, exc_info=True)
