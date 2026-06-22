# Extracted from strategy_plugins.py — Phase 2 (file diet)
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict

from app.strategy import indicators
from app.core.currency import Q
from app.strategy.strategy_base import Decision, Signal, StrategyPlugin
from app.strategy.strategy_helpers import (
    adjust_order_amount_and_price,
    _apply_atr_dynamic_limits,
    _check_longhold_recovery,
    _common_dca_check,
    _reset_dca_state,
    _restore_longhold_flag_from_config,
    _try_convert_to_longhold,
    _unregister_longhold,
    send_signal_telegram,
)

logger = logging.getLogger(__name__)


class LightningPlugin(StrategyPlugin):
    """Lightning v2 — 국면 적응형 돌파 엔진.

    3-Regime × 상태 머신:
      TREND_BURST  : 평시 돌파 스윙 (WATCH → PROBE → ACTIVE → ARM_TRAIL)
      SHOCK_DIVERGE: BTC 급락 시 역행 생존 스윙 (contrarian 연동)
      RECOVERY     : 반등 초입 재탑승 (BTC Leading 확인)
      DRIFT        : BTC 서서히 하락 → 보수적 TREND (threshold ×1.5)

    [2026-02-23] v1 → v2 전면 개편.
    """

    name: str = "lightning"

    # ── 상태 상수 ──
    _ST_IDLE = "IDLE"
    _ST_WATCH = "WATCH"
    _ST_PROBE = "PROBE"
    _ST_ACTIVE = "ACTIVE"
    _ST_ARM_TRAIL = "ARM_TRAIL"

    _MAX_DAILY_SHOTS = 10

    # ── ATR% 버킷별 파라미터 ──
    _ATR_BUCKETS = [
        # (atr_pct_max, burst_window, atr_burst_mult, min_confidence, watch_timeout, confirm_wait)
        (1.5,  20, 5.0, 0.55, 90.0,  300.0),
        (4.0,  15, 4.0, 0.60, 60.0,  180.0),
        (999,  10, 3.5, 0.70, 45.0,  120.0),
    ]

    # ── 성과 통계 (모듈 레벨) ──
    _stats: Dict[str, int] = {
        "probe": 0, "confirm": 0, "win": 0, "loss": 0, "abort": 0,
    }
    _stats_reset_day: str = ""
    _stats_lock: threading.RLock = threading.RLock()  # [FIX L7] thread-safe stat access

    @classmethod
    def _ensure_daily_stats(cls) -> None:
        today = time.strftime("%Y%m%d")
        if cls._stats_reset_day != today:
            cls._stats = {"probe": 0, "confirm": 0, "win": 0, "loss": 0, "abort": 0}
            cls._stats_reset_day = today

    @classmethod
    def _record_stat(cls, event: str) -> None:
        with cls._stats_lock:  # [FIX L7] thread-safe
            cls._ensure_daily_stats()
            cls._stats[event] = cls._stats.get(event, 0) + 1

    @classmethod
    def get_stats(cls) -> Dict[str, Any]:
        cls._ensure_daily_stats()
        s = cls._stats
        total_exits = s["win"] + s["loss"]
        return {
            **s,
            "win_rate": round(s["win"] / total_exits * 100 if total_exits > 0 else 0.0, 1),
        }

    # ── 상태 헬퍼 ──
    def _get_state(self, ctx: Any) -> str:
        return str(ctx.get_var("lt_state", self._ST_IDLE))

    def _set_state(self, ctx: Any, state: str) -> None:
        ctx.set_var("lt_state", state)

    def _reset_state(self, ctx: Any, exit_price: float = 0.0) -> None:
        # 재진입 방어용 exit 기록
        if exit_price > 0:
            ctx.set_var("lt_last_exit_ts", time.time())
            ctx.set_var("lt_last_exit_price", float(exit_price))
        ctx.set_var("lt_state", self._ST_IDLE)
        ctx.set_var("lt_watch_ts", 0.0)
        ctx.set_var("lt_watch_peak_mom", 0.0)
        ctx.set_var("lt_probe_ts", 0.0)
        ctx.set_var("lt_probe_price", 0.0)
        ctx.set_var("lt_probe_ratio", 0.0)
        ctx.set_var("lt_active_ts", 0.0)
        ctx.set_var("lt_peak_price", 0.0)
        _reset_dca_state(ctx, "lt")

    def _get_atr_bucket(self, atr_pct: float) -> tuple:
        for max_atr, bw, abm, mc, wt, cw in self._ATR_BUCKETS:
            if atr_pct < max_atr:
                return bw, abm, mc, wt, cw
        return self._ATR_BUCKETS[-1][1:]

    def _detect_regime(self) -> str:
        """BTC Leading Signal 기반 국면 판정."""
        try:
            from app.monitor.btc_leading_signal import get_btc_leading_detector
            detector = get_btc_leading_detector()
            if detector:
                return detector.get_regime_for_lightning()
        except (ImportError, AttributeError, TypeError) as exc:
            logger.warning("[LIGHTNING] _detect_regime fallback: %s", exc, exc_info=True)
        return "TREND"

    def _check_btc_delay(self) -> tuple:
        """BTC 급변동 시 진입 지연 체크."""
        try:
            from app.monitor.btc_leading_signal import get_btc_leading_detector
            detector = get_btc_leading_detector()
            if detector:
                return detector.should_delay_entry()
        except (ImportError, AttributeError, TypeError) as exc:
            logger.warning("[LIGHTNING] _check_btc_delay fallback: %s", exc, exc_info=True)
        return False, 0.0

    def _check_execution_quality(self, ctx: Any) -> Dict[str, Any]:
        """WATCH 단계: 거래량 서지 + depth imbalance 체크."""
        result: Dict[str, Any] = {"vol_surge": False, "depth_bullish": False, "score": 0.0}
        try:
            vol_hist = list(getattr(ctx, "volume_history", []) or [])
            if len(vol_hist) >= 15:
                recent_vol = sum(vol_hist[-5:]) / 5
                baseline_vol = sum(vol_hist[-15:-5]) / 10
                if baseline_vol > 0 and recent_vol > baseline_vol * 1.5:
                    result["vol_surge"] = True
                    result["score"] += 2.0
                elif baseline_vol > 0 and recent_vol > baseline_vol * 1.2:
                    result["score"] += 1.0

            depth_bid = float(getattr(ctx, "depth_bid_usdt", 0) or 0)
            depth_ask = float(getattr(ctx, "depth_ask_usdt", 0) or 0)
            if depth_bid == 0 and depth_ask == 0:
                ctrls = getattr(ctx, "controls", {}) or {}
                p = ((ctrls.get("strategy") or {}).get("params") or {})
                depth_bid = float(p.get("depth_bid_usdt", 0) or 0)
                depth_ask = float(p.get("depth_ask_usdt", 0) or 0)
            if depth_ask > 0:
                ratio = depth_bid / depth_ask
                result["bid_ask_ratio"] = round(ratio, 2)
                if ratio > 1.3:
                    result["depth_bullish"] = True
                    result["score"] += 2.0
                elif ratio > 1.1:
                    result["score"] += 1.0
                elif ratio < 0.7:
                    result["score"] -= 2.0
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LIGHTNING] _check_execution_quality fallback: %s", exc, exc_info=True)
        return result

    def _make_sell_meta(self, meta: Dict[str, Any], price: float, market: str) -> Dict[str, Any]:
        amount = meta.get("amount", Q.min_order)
        amount, order_price = adjust_order_amount_and_price(amount, price, market)
        meta["amount"] = amount
        meta["price"] = order_price
        meta["force_exit"] = True
        meta["use_limit"] = False
        meta["fallback_to_market"] = True
        return meta

    def _try_shock_entry(self, ctx: Any, price: float, params: Dict[str, Any],
                         meta: Dict[str, Any]) -> Decision:
        """SHOCK 국면: contrarian early_signal 기반 역행 스윙 진입."""
        try:
            from app.core.contrarian_scanner import get_contrarian_scanner
            scanner = get_contrarian_scanner()
            if not scanner:
                return Decision(signal="hold", reason="lightning:shock_no_scanner", meta=meta)

            market = str(getattr(ctx, "market", ""))
            result = scanner.scan()
            if not result or not result.candidates:
                return Decision(signal="hold", reason="lightning:shock_no_candidates", meta=meta)

            for c in result.candidates:
                if c.market != market:
                    continue
                if not c.early_signal and c.score < 2:
                    continue
                if c.rs_momentum < 0.3:
                    continue
                # 역행 확인됨 → probe 진입 (타이트 TP/SL)
                meta["shock_mode"] = True
                meta["rs_momentum"] = c.rs_momentum
                meta["acceleration"] = c.acceleration
                meta["early_signal"] = c.early_signal
                meta["contrarian_score"] = c.score
                shock_tp = float(params.get("shock_tp_pct", 2.0))
                shock_sl = float(params.get("shock_sl_pct", 2.0))
                if shock_sl > 0:
                    shock_sl = -shock_sl
                probe_ratio = float(meta.get("probe_ratio", params.get("probe_ratio", 0.30)))
                probe_ratio = max(0.05, min(1.0, probe_ratio))
                meta["tp_pct"] = shock_tp
                meta["sl_pct"] = shock_sl
                self._record_stat("probe")
                self._set_state(ctx, self._ST_PROBE)
                now = time.time()
                ctx.set_var("lt_probe_ts", now)
                ctx.set_var("lt_probe_price", price)
                ctx.set_var("lt_probe_ratio", probe_ratio)
                ctx.set_var("lt_regime", "SHOCK")
                daily_key = f"lt_shots_{time.strftime('%Y%m%d')}"
                ctx.set_var(daily_key, int(ctx.get_var(daily_key, 0)) + 1)
                meta["probe_ratio"] = probe_ratio
                meta["size_scale"] = probe_ratio
                send_signal_telegram(
                    f"⚡🔮 [LIGHTNING v2] {market} SHOCK Probe\n"
                    f"• RS모멘텀: {c.rs_momentum:+.2f}\n"
                    f"• 가속도: {c.acceleration:+.2f}\n"
                    f"• TP: {meta['tp_pct']}% / SL: {meta['sl_pct']}%"
                )
                return Decision(signal="buy", reason="lightning:shock_probe", meta=meta)
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
            logger.warning("[LIGHTNING] SHOCK 진입 로직 전체 실패: %s — 급등 진입 기회 상실", getattr(ctx, "market", "?"), exc_info=True)
        return Decision(signal="hold", reason="lightning:shock_miss", meta=meta)

    def decide(self, ctx: Any, price: float) -> Decision:
        params: Dict[str, Any] = {}
        try:
            ctrls = getattr(ctx, "controls", None) or {}
            if isinstance(ctrls, dict):
                params = dict((ctrls.get("strategy") or {}).get("params") or {})
        except (KeyError, AttributeError, TypeError):
            logger.warning("[%s] params 추출 실패 → 기본값 사용: %s", self.name if hasattr(self, 'name') else '?', getattr(ctx, 'market', '?'), exc_info=True)
            params = {}

        if bool(params.get("buy_now", False)):
            return Decision(signal="buy", reason="lightning:buy_now", meta={})

        market = str(getattr(ctx, "market", "") or "")
        now = time.time()
        history = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])

        # ── ATR 계산 + 버킷 결정 ──
        atr_period = int(params.get("atr_period", 14))
        atr = indicators.atr_simplified(history, atr_period) if len(history) >= atr_period else None
        atr_pct = (atr / price * 100.0) if atr and price > 0 else 0.0
        bkt_window, bkt_burst_mult, bkt_min_conf, bkt_watch_timeout, bkt_confirm_wait = self._get_atr_bucket(atr_pct)

        burst_window = int(params.get("burst_window", bkt_window))
        burst_threshold = float(params.get("burst_threshold", 2.0))
        min_conf = float(params.get("min_ai_confidence", bkt_min_conf))
        watch_timeout = float(params.get("watch_timeout_sec", bkt_watch_timeout))
        confirm_wait = float(params.get("confirm_wait_sec", bkt_confirm_wait))

        if not history or len(history) < burst_window + 1:
            return Decision(signal="hold", reason="lightning:insufficient_data")

        momentum_pct = indicators.trend(history, burst_window + 1) or 0.0
        short_momentum = indicators.trend(history, 6) or 0.0 if len(history) >= 6 else 0.0

        if atr_pct > 0:
            atr_burst_mult = float(params.get("atr_burst_mult", bkt_burst_mult))
            if atr_burst_mult > 0:
                burst_threshold = max(burst_threshold, atr_pct * atr_burst_mult)

        # ── AI / Confidence ──
        ai_score = 0.5
        ai_confidence = 0.0
        try:
            if hasattr(ctx, "current_ai") and isinstance(ctx.current_ai, dict):
                brain = ctx.current_ai.get("brain", {})
                ai_score = float(brain.get("ai_prediction", 0.5))
                ai_confidence = float(brain.get("ai_confidence", 0.0))
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LIGHTNING] AI / Confidence: %s", exc, exc_info=True)

        ai_influence = float(params.get("ai_influence", 0.15))
        adj_factor = 1.0
        if ai_influence > 0:
            adj_factor = 1.0 + (0.5 - ai_score) * ai_influence
        effective_threshold = burst_threshold * adj_factor

        # ── 국면 판정 ──
        regime = self._detect_regime()
        if regime == "DRIFT":
            effective_threshold *= 1.5
            confirm_wait *= 1.5
        ctx.set_var("lt_regime", regime)

        meta: Dict[str, Any] = {
            "momentum_pct": momentum_pct,
            "short_momentum": short_momentum,
            "burst_threshold": burst_threshold,
            "effective_threshold": effective_threshold,
            "window": burst_window,
            "atr_pct": round(atr_pct, 2),
            "ai_score": ai_score,
            "ai_confidence": ai_confidence,
            "regime": regime,
        }

        _apply_atr_dynamic_limits(ctx, params, float(price), history, meta, "lightning")

        tp_pct = float(params.get("tp_pct", max(1.0, momentum_pct * 0.5) if momentum_pct > 0 else 1.5))
        sl_pct = float(params.get("sl_pct", max(-5.0, -atr_pct * 2.0) if atr_pct > 0 else -2.0))
        if sl_pct > 0:
            sl_pct = -abs(sl_pct)
        trailing_pct = float(params.get("trailing_pct", max(0.8, atr_pct * 0.5) if atr_pct > 0 else 1.5))
        probe_ratio = float(params.get("probe_ratio", 0.30))

        meta["tp_pct"] = tp_pct
        meta["sl_pct"] = sl_pct
        meta["trailing_pct"] = trailing_pct
        meta["probe_ratio"] = probe_ratio

        # ── 현재 상태 로드 ──
        state = self._get_state(ctx)
        pos = getattr(ctx, "position", None)
        has_pos = False
        try:
            has_pos = pos is not None and float(pos.get("qty", 0) or 0) > 0
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LIGHTNING] 현재 상태 로드: %s", exc, exc_info=True)

        # ── LongHold: 서버 재시작 시 config에서 플래그 복원 ──
        if has_pos:
            _restore_longhold_flag_from_config(ctx)
        # ── LongHold 전환 완료 → 회복 체크 후 hold 유지 ──
        if has_pos and ctx.get_var("longhold_converted", False):
            if not _check_longhold_recovery(ctx, pos, price, "LIGHTNING"):
                return Decision(signal="hold", reason="lightning:longhold_active",
                                meta={"longhold": True, "longhold_ts": ctx.get_var("longhold_convert_ts", 0)})
        if not has_pos and ctx.get_var("longhold_converted", False):
            ctx.set_var("longhold_converted", False)
            _unregister_longhold(getattr(ctx, "market", ""))

        # 서버 재시작 안전장치
        if has_pos and state == self._ST_IDLE:
            state = self._ST_ACTIVE
            self._set_state(ctx, state)
            ctx.set_var("lt_active_ts", now)

        holding = getattr(ctx, "holding_qty", 0.0)
        if not has_pos and holding and float(holding) > 0:
            has_pos = True
            if state == self._ST_IDLE:
                state = self._ST_ACTIVE
                self._set_state(ctx, state)
                ctx.set_var("lt_active_ts", now)

        meta["lt_state"] = state

        # =============================================
        # 보유 중: PROBE / ACTIVE / ARM_TRAIL 관리
        # =============================================
        if has_pos:
            avg_price = 0.0
            try:
                avg_price = float(
                    (pos or {}).get("avg_price", 0) or (pos or {}).get("entry_price", 0)
                    or getattr(ctx, "avg_buy_price", 0) or 0
                )
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[LIGHTNING] avg_price 추출: %s", exc, exc_info=True)
            if avg_price <= 0:
                return Decision(signal="hold", reason="lightning:no_entry_price", meta=meta)

            profit_pct = (price - avg_price) / avg_price * 100
            meta["profit_pct"] = profit_pct
            meta["entry_price"] = avg_price

            # ── PROBE 상태: 확인 대기 ──
            if state == self._ST_PROBE:
                probe_ts = float(ctx.get_var("lt_probe_ts", now))
                probe_price = float(ctx.get_var("lt_probe_price", avg_price))
                elapsed = now - probe_ts
                meta["probe_elapsed_sec"] = round(elapsed)
                meta["lt_phase"] = "PROBE"

                confirm_ok = False
                if price > probe_price * 1.003:
                    recent_3 = history[-3:] if len(history) >= 3 else history
                    if len(recent_3) >= 3 and recent_3[-1] > recent_3[0]:
                        confirm_ok = True
                        meta["confirm_trigger"] = "momentum+higher"
                if not confirm_ok and len(history) >= 12:
                    try:
                        ema_f = indicators.ema(history, 5)
                        ema_s = indicators.ema(history, 12)
                        if ema_f and ema_s and ema_f > ema_s and price > probe_price:
                            confirm_ok = True
                            meta["confirm_trigger"] = "ema_cross+higher"
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.warning("[LIGHTNING] PROBE 확인 대기: %s", exc, exc_info=True)

                if confirm_ok and elapsed < confirm_wait:
                    self._record_stat("confirm")
                    self._set_state(ctx, self._ST_ACTIVE)
                    ctx.set_var("lt_active_ts", now)
                    meta["lt_phase"] = "CONFIRM"
                    probe_ratio_eff = float(ctx.get_var("lt_probe_ratio", probe_ratio) or probe_ratio)
                    confirm_ratio = max(0.0, min(1.0, 1.0 - probe_ratio_eff))
                    meta["confirm_buy_ratio"] = confirm_ratio
                    meta["allow_add_buy"] = True
                    meta["size_scale"] = confirm_ratio
                    send_signal_telegram(
                        f"⚡⚡ [LIGHTNING v2] {market} 확인 진입!\n"
                        f"• Probe +{(price - probe_price) / probe_price * 100:.2f}%\n"
                        f"• {meta.get('confirm_trigger', 'OK')}\n"
                        f"• 추가 {confirm_ratio:.0%} 매수"
                    )
                    return Decision(signal="buy", reason="lightning:confirm", meta=meta)

                if elapsed >= confirm_wait:
                    self._record_stat("abort")
                    self._reset_state(ctx, exit_price=price)
                    meta = self._make_sell_meta(meta, price, market)
                    meta["abort_reason"] = "confirm_timeout"
                    return Decision(signal="sell", reason="lightning:abort_timeout", meta=meta)

                if profit_pct <= sl_pct * 0.5:
                    self._record_stat("abort")
                    self._reset_state(ctx, exit_price=price)
                    meta = self._make_sell_meta(meta, price, market)
                    return Decision(signal="sell", reason="lightning:abort_sl", meta=meta)

                return Decision(signal="hold", reason="lightning:probe_waiting", meta=meta)

            # ── ARM_TRAIL: 트레일링 ──
            if state == self._ST_ARM_TRAIL:
                peak = float(ctx.get_var("lt_peak_price", avg_price))
                if price > peak:
                    ctx.set_var("lt_peak_price", price)
                    peak = price
                peak_pct = (peak - avg_price) / avg_price * 100 if avg_price > 0 else 0
                drawdown = (peak - price) / peak * 100 if peak > 0 else 0
                meta["trail_peak_pct"] = round(peak_pct, 2)
                meta["trail_drawdown"] = round(drawdown, 2)

                if drawdown >= trailing_pct:
                    self._record_stat("win")
                    self._reset_state(ctx, exit_price=price)
                    meta = self._make_sell_meta(meta, price, market)
                    return Decision(signal="sell", reason="lightning:trail_tp", meta=meta)

                return Decision(signal="hold", reason="lightning:trailing", meta=meta)

            # ── ACTIVE 상태: TP/SL ──
            meta["lt_phase"] = "ACTIVE"

            # SL 아니면 streak 리셋
            ctx.set_var("lt_sl_streak", 0)

            if profit_pct >= tp_pct:
                self._set_state(ctx, self._ST_ARM_TRAIL)
                ctx.set_var("lt_peak_price", price)
                meta["arm_trail_at"] = round(profit_pct, 2)
                return Decision(signal="hold", reason="lightning:arm_trail", meta=meta)

            if profit_pct <= sl_pct:
                # ── LIGHTNING SL 확인 (2틱 연속 — 노이즈 방어) ──
                _lt_sl_confirm_need = int(params.get("sl_confirm_ticks", 2))
                _lt_sl_streak = int(ctx.get_var("lt_sl_streak", 0)) + 1
                ctx.set_var("lt_sl_streak", _lt_sl_streak)
                meta["sl_streak"] = _lt_sl_streak
                meta["sl_confirm_need"] = _lt_sl_confirm_need
                if _lt_sl_streak < _lt_sl_confirm_need:
                    return Decision(signal="hold", reason="lightning:sl_confirming", meta=meta)
                ctx.set_var("lt_sl_streak", 0)
                # DCA 물타기 먼저 시도
                dca_result = _common_dca_check(ctx, price, avg_price, params, "lt", meta)
                if dca_result is not None:
                    return dca_result
                # ── DCA 불가 → SL → LongHold 전환 시도 ──
                _lh_result = _try_convert_to_longhold(ctx, market, "LIGHTNING", avg_price, price, meta)
                if _lh_result is not None:
                    return _lh_result
                self._record_stat("loss")
                self._reset_state(ctx, exit_price=price)
                meta = self._make_sell_meta(meta, price, market)
                return Decision(signal="sell", reason="lightning:sl", meta=meta)

            # Time-stop: 60분 횡보
            active_ts = float(ctx.get_var("lt_active_ts", 0.0))
            time_stop_min = float(params.get("time_stop_min", 60))
            if active_ts > 0:
                hold_min = (now - active_ts) / 60.0
                meta["hold_minutes"] = round(hold_min, 1)
                if hold_min >= time_stop_min and abs(profit_pct) < 0.5:
                    self._record_stat("loss")
                    self._reset_state(ctx, exit_price=price)
                    meta = self._make_sell_meta(meta, price, market)
                    return Decision(signal="sell", reason="lightning:timeout", meta=meta)

            # Trailing (ACTIVE 중 고점 추적)
            peak_price = float(ctx.get_var("lt_peak_price", avg_price))
            if price > peak_price:
                ctx.set_var("lt_peak_price", price)
                peak_price = price
            if peak_price > avg_price:
                drawdown = (peak_price - price) / peak_price * 100
                if drawdown >= trailing_pct:
                    self._record_stat("win" if profit_pct > 0 else "loss")
                    self._reset_state(ctx, exit_price=price)
                    meta = self._make_sell_meta(meta, price, market)
                    return Decision(signal="sell", reason=f"lightning:trailing(-{drawdown:.1f}%)", meta=meta)

            return Decision(signal="hold", reason="lightning:holding", meta=meta)

        # =============================================
        # 미보유: [2026-03-09] selector 신뢰 즉시매수
        # =============================================
        if state not in (self._ST_IDLE, self._ST_WATCH):
            self._reset_state(ctx)
            state = self._ST_IDLE

        # 일일 발사 제한 (안전장치 유지)
        daily_key = f"lt_shots_{time.strftime('%Y%m%d')}"
        daily_shots = int(ctx.get_var(daily_key, 0))
        meta["daily_shots"] = daily_shots
        if daily_shots >= self._MAX_DAILY_SHOTS:
            return Decision(signal="hold", reason="lightning:daily_limit", meta=meta)

        # 자본 확인 (안전장치 유지)
        capital = 0.0
        try:
            c = getattr(ctx, "usable_capital", None) or getattr(ctx, "allocated_capital", None)
            capital = float(c or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LIGHTNING] 자본 확인: %s", exc, exc_info=True)
        min_order = float(params.get("min_order_usdt", Q.min_order))
        if capital < min_order:
            return Decision(signal="hold", reason="lightning:insufficient_capital", meta=meta)

        # [FIX] 재진입 방어: 쿨다운 + 가격상승 체크
        lt_reentry_cd = float(params.get("reentry_cooldown_sec", 300))
        last_exit_ts = float(ctx.get_var("lt_last_exit_ts", 0.0))
        last_exit_price = float(ctx.get_var("lt_last_exit_price", 0.0))
        if last_exit_ts > 0 and (now - last_exit_ts) < lt_reentry_cd:
            meta["reentry_blocked"] = True
            meta["cooldown_remaining"] = round(lt_reentry_cd - (now - last_exit_ts), 0)
            return Decision(signal="hold", reason="lightning:reentry_cooldown", meta=meta)
        if last_exit_price > 0 and price >= last_exit_price * 1.005:
            meta["reentry_blocked"] = True
            meta["exit_price"] = round(last_exit_price, 2)
            meta["price_elevation_pct"] = round((price / last_exit_price - 1) * 100, 2)
            return Decision(signal="hold", reason="lightning:reentry_price_elevated", meta=meta)

        # selector가 모멘텀/ATR/BB를 이미 검증 → 즉시 buy
        meta["selector_fast_entry"] = True
        self._set_state(ctx, self._ST_ACTIVE)
        ctx.set_var("lt_active_ts", now)
        ctx.set_var(daily_key, daily_shots + 1)
        return Decision(signal="buy", reason="lightning:selector_entry", meta=meta)

    def _legacy_staged_entry(
        self, ctx, price, params, meta, *, regime, state,
        momentum_pct, short_momentum, effective_threshold,
        ai_confidence, min_conf, watch_timeout, confirm_wait,
        probe_ratio, tp_pct, sl_pct, atr_pct, daily_key,
        daily_shots, market, now, history,
    ) -> Decision:
        """기존 다단계 진입 로직 (WATCH→PROBE→ACTIVE).
        selector_fast_entry 도입으로 현재 미사용.
        """
        # ── BTC 레짐 기반 행동 조정 ──
        btc_action: Dict[str, Any] = {}
        try:
            from app.monitor.btc_leading_signal import get_btc_leading_detector
            _btc = get_btc_leading_detector()
            if _btc:
                btc_action = _btc.get_strategy_action("LIGHTNING")
                meta["btc_regime"] = btc_action.get("regime", "TREND")
                meta["btc_action"] = btc_action.get("entry", "normal")
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[LIGHTNING] BTC 레짐 기반 행동 조정: %s", exc, exc_info=True)

        # SHOCK: 사이즈 50% 축소 + contrarian 역행 진입
        if regime == "SHOCK":
            shock_size = float(btc_action.get("size_mult", 0.5))
            meta["shock_size_mult"] = shock_size
            probe_ratio *= shock_size
            meta["probe_ratio"] = probe_ratio
            return self._try_shock_entry(ctx, price, params, meta)

        # ── RECOVERY 국면: BTC 반등 확인 후 진입 ──
        if regime == "RECOVERY":
            should_delay, delay_sec = self._check_btc_delay()
            if should_delay:
                meta["recovery_delay_sec"] = delay_sec
                return Decision(signal="hold", reason="lightning:recovery_delay", meta=meta)

        # ── AI Confidence Gate ──
        if ai_confidence < min_conf:
            return Decision(signal="hold", reason=f"lightning:low_confidence({ai_confidence:.2f}<{min_conf:.2f})", meta=meta)

        # ── BB Squeeze 감지 (돌파 임박 사전 시그널) ──
        bb_squeeze = False
        try:
            if len(history) >= 20:
                bb = indicators.bollinger_bands(history, 20, 2.0)
                if bb and bb["lower"] > 0 and bb["upper"] > 0:
                    bb_width = bb["bandwidth"] * 100
                    meta["bb_width_pct"] = round(bb_width, 2)
                    if bb_width < 2.0:
                        bb_squeeze = True
                        meta["bb_squeeze"] = True
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LIGHTNING] BB Squeeze 감지: %s", exc, exc_info=True)

        # ── IDLE → WATCH ──
        if state == self._ST_IDLE:
            watch_trigger = short_momentum > 0.5 and momentum_pct > 0
            if not watch_trigger and bb_squeeze and momentum_pct > -0.3:
                watch_trigger = True
                meta["watch_trigger"] = "bb_squeeze"
            if watch_trigger:
                self._set_state(ctx, self._ST_WATCH)
                ctx.set_var("lt_watch_ts", now)
                ctx.set_var("lt_watch_peak_mom", momentum_pct)
                meta["watch_started"] = True
                return Decision(signal="hold", reason="lightning:watch_start", meta=meta)
            return Decision(signal="hold", reason="lightning:wait", meta=meta)

        # ── WATCH → PROBE (돌파 확인) ──
        if state == self._ST_WATCH:
            watch_ts = float(ctx.get_var("lt_watch_ts", now))
            watch_peak = float(ctx.get_var("lt_watch_peak_mom", 0.0))
            elapsed = now - watch_ts
            meta["watch_elapsed_sec"] = round(elapsed)

            if momentum_pct > watch_peak:
                ctx.set_var("lt_watch_peak_mom", momentum_pct)
                watch_peak = momentum_pct

            # 타임아웃
            if elapsed >= watch_timeout:
                self._reset_state(ctx)
                return Decision(signal="hold", reason="lightning:watch_timeout", meta=meta)

            # 모멘텀 소멸
            if short_momentum < -0.3:
                self._reset_state(ctx)
                return Decision(signal="hold", reason="lightning:watch_momentum_lost", meta=meta)

            # 돌파 확인 → PROBE
            if momentum_pct >= effective_threshold:
                exec_quality = self._check_execution_quality(ctx)
                meta["exec_quality"] = exec_quality

                if exec_quality["score"] < -1.0:
                    self._reset_state(ctx)
                    return Decision(signal="hold", reason="lightning:watch_sell_pressure", meta=meta)

                # BTC 급변동 지연 체크 (감도 120초로 상향)
                should_delay, delay_sec = self._check_btc_delay()
                if should_delay and delay_sec >= 120:
                    self._reset_state(ctx)
                    meta["btc_delay_sec"] = delay_sec
                    return Decision(signal="hold", reason="lightning:btc_delay", meta=meta)

                self._record_stat("probe")
                self._set_state(ctx, self._ST_PROBE)
                ctx.set_var("lt_probe_ts", now)
                ctx.set_var("lt_probe_price", price)
                ctx.set_var("lt_probe_ratio", probe_ratio)
                ctx.set_var(daily_key, daily_shots + 1)
                meta["lt_phase"] = "PROBE"
                meta["probe_ratio"] = probe_ratio
                meta["size_scale"] = probe_ratio
                send_signal_telegram(
                    f"⚡ [LIGHTNING v2] {market} Probe ({probe_ratio:.0%})\n"
                    f"• 모멘텀: {momentum_pct:.2f}% (T:{effective_threshold:.2f}%)\n"
                    f"• ATR: {atr_pct:.1f}% | 국면: {regime}\n"
                    f"• TP: {tp_pct:.1f}% / SL: {sl_pct:.1f}%"
                )
                return Decision(signal="buy", reason=f"lightning:probe({momentum_pct:.2f}%)", meta=meta)

            return Decision(signal="hold", reason="lightning:watching", meta=meta)

        return Decision(signal="hold", reason="lightning:monitoring", meta=meta)
