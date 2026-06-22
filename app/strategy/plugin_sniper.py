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
    """SNIPER v2 — 상태 기반 정밀 저격 시스템.

    6-State Machine: IDLE → WATCH → PROBE → ACTIVE → ARM_TRAIL → EXIT
    - Phase 0 (WATCH): 관측 — 조건 유지 확인 후 진입
    - Phase 1 (PROBE): 탐색 진입 — 30% 소액 매수
    - Phase 2 (ACTIVE): 확인 진입 — 반등 확인 시 나머지 70%
    - ARM_TRAIL: TP 도달 → 트레일링 시작
    - TIMEOUT/ABORT: 시간 초과 또는 실패 시 청산

    [2026-02-23] ATR/BB 기반 정밀 파라미터, reserved_selector 계약 존중.
    """

    name: str = "sniper"

    # ── 상태 상수 ──
    _ST_IDLE = "IDLE"
    _ST_WATCH = "WATCH"
    _ST_PROBE = "PROBE"
    _ST_ACTIVE = "ACTIVE"
    _ST_ARM_TRAIL = "ARM_TRAIL"

    # ── 일일 발사 제한 ──
    _MAX_DAILY_SHOTS = 7
    # 운영 하한: 최소치만 설정, 실제 TP/SL은 UI Guards에서 조절
    # [2026-03-18] 0.8/2.5 — 기본은 낮게, Strategy TP/SL Guards + Global Profit Take로 유연 운용
    _MIN_TP_PCT = 0.8
    _MIN_SL_PCT = 2.5

    # [FIX N2+N8] 성과 통계: 인스턴스 레벨 격리 + 스레드 잠금
    # SNIPER / SNIPER(S) 두 싱글턴이 같은 클래스이므로 클래스 변수를 공유함 → __init__으로 격리
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
        """외부에서 통계 조회 (API/텔레그램 등)."""
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
        """승률 기반 동적 probe 비율 (0.15~0.45). 데이터 부족 시 None 반환."""
        self._ensure_daily_stats()
        s = self._stats
        total_exits = s["win"] + s["loss"]
        if total_exits < 3:
            return None  # 데이터 부족 → None (params fallback 사용)

        win_rate = s["win"] / total_exits
        # 승률 연동: 낮으면 보수적, 높으면 공격적
        # 45% 미만 → 0.20 (보수적), 60% 이상 → 0.40 (공격적)
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
        # [2026-03-07] SNIPER(S) scope_start_ts 경과시간 이월 저장
        # swap-out으로 코인이 교체되더라도 이전 완화 타이머 경과를 보존
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
        """매도 시 재진입 판단용 변수 기록."""
        ctx.set_var("sniper_last_exit_ts", now)
        ctx.set_var("sniper_exit_ai_score", ai_score)
        ctx.set_var("sniper_exit_count", int(ctx.get_var("sniper_exit_count", 0)) + 1)
        # [FIX #4] exit 시 profile 저장 → 다른 변종(SNIPER↔SNIPER(S)) 재진입 시 구분
        ctx.set_var("sniper_exit_profile", profile)

    def _check_execution_quality(self, ctx: Any, history: list) -> Dict[str, Any]:
        """WATCH 단계: 체결 강도 + depth imbalance 체크."""
        result: Dict[str, Any] = {"vol_surge": False, "depth_bullish": False, "score": 0.0}
        try:
            # 체결 강도: 최근 5틱 거래량 vs 이전 10틱 평균
            vol_hist = list(getattr(ctx, "volume_history", []) or [])
            if len(vol_hist) >= 15:
                recent_vol = sum(vol_hist[-5:]) / 5
                baseline_vol = sum(vol_hist[-15:-5]) / 10
                if baseline_vol > 0 and recent_vol > baseline_vol * 1.5:
                    result["vol_surge"] = True
                    result["score"] += 2.0
                elif baseline_vol > 0 and recent_vol > baseline_vol * 1.2:
                    result["score"] += 1.0

            # Depth imbalance: bid > ask = 매수 우위
            depth_bid = float(getattr(ctx, "depth_bid_usdt", 0) or 0)
            depth_ask = float(getattr(ctx, "depth_ask_usdt", 0) or 0)
            if depth_bid == 0 and depth_ask == 0:
                # controls에서 가져오기
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
                    result["score"] -= 2.0  # 매도 압력 우위
        except (KeyError, AttributeError, TypeError, ValueError) as _e:
            # [FIX N12] 체결 품질 검사 실패 시 중립 점수 반환 (매도 압력 감지 불가)
            logging.getLogger("sniper.exec_quality").warning("exec_quality check failed: %s", _e, exc_info=True)
        return result

    def _make_sell_meta(self, meta: Dict[str, Any], price: float, market: str) -> Dict[str, Any]:
        amount = meta.get("amount", Q.min_order)
        amount, order_price = adjust_order_amount_and_price(amount, price, market)
        meta["amount"] = amount
        meta["price"] = order_price
        meta["force_exit"] = True
        # [FIX N3] use_limit은 meta에 이미 params 기반으로 설정됨 — 덮어쓰지 않음
        # meta["use_limit"]은 decide() 시작부에서 params["use_limit"]으로 채워짐
        meta["fallback_to_market"] = True  # use_limit=True 시 미체결 → 시장가 폴백
        return meta

    def decide(self, ctx: Any, price: float) -> Decision:
        ctrls = getattr(ctx, "controls", None) or {}
        if isinstance(ctrls, dict):
            params = dict((ctrls.get("strategy") or {}).get("params") or {})
        else:
            params = getattr(ctx, "strategy_params", None) or {}
        market = str(getattr(ctx, "market", "") or getattr(ctx, "code", ""))
        now = time.time()

        # ── 파라미터 로드 (reserved_selector 계약 존중) ──
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
        # [PROTECTED] 기본값 True - 변동성 기반 동적 조정 핵심 기능
        atr_auto = bool(params.get("atr_auto", True))

        # v2 파라미터 (selector가 계산, 플러그인은 존중)
        # 동적 probe 비율: 승률 기반 자동 조절 (데이터 3건 미만이면 selector 값 사용)
        dynamic_ratio = self._calc_dynamic_probe_ratio()
        probe_ratio = dynamic_ratio if dynamic_ratio is not None else float(params.get("probe_ratio", 0.2))
        confirm_ratio = 1.0 - probe_ratio
        watch_sec = float(params.get("watch_sec", 180))
        confirm_window_sec = float(params.get("confirm_window_sec", 300))
        time_stop_min = float(params.get("time_stop_min", 60))
        param_atr_pct = float(params.get("atr_pct", 0.0))
        # 국면 모드
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

        # SNIPER(s) 전용 진입 완화:
        # 기존 SNIPER는 그대로 두고, precision_scope 슬롯만 AI/RSI 진입 문턱을 점진 완화한다.
        effective_ai_min = ai_min_score
        # [2026-03-07] RSI gate: 하드코딩 30 → params 주입 가능 (기본 38)
        # 셀렉터가 RSI 55까지 허용하는데 플러그인이 30으로 자르면
        # 선정된 후보 대부분이 실행 단계에서 탈락하는 근본 병목 해소
        effective_rsi_entry_max = float(params.get("rsi_entry_max", 38.0))
        if is_scope_snipers:
            # Scope 슬롯은 완화되더라도 AI 하한 50% 아래로는 내리지 않는다.
            scope_ai_floor = 0.50
            effective_ai_min = float(params.get("ai_min_score_scope", min(ai_min_score, 0.55)))
            effective_rsi_entry_max = float(params.get("rsi_entry_max_scope", 42.0))
            scope_start_ts = float(ctx.get_var("snipers_scope_start_ts", 0.0) or 0.0)
            if scope_start_ts <= 0:
                # [2026-03-07] scope_start_ts 이월: swap-out으로 코인이 교체되어도
                # 이전 슬롯의 경과시간을 이어받아 20분 완화 타이머가 리셋되지 않음
                _prev_elapsed = float(ctx.get_var("snipers_scope_elapsed_carry", 0.0) or 0.0)
                scope_start_ts = now - _prev_elapsed
                ctx.set_var("snipers_scope_start_ts", scope_start_ts)
            scope_wait_min = max(0.0, (now - scope_start_ts) / 60.0)
            # [FIX #9] 매 틱마다 elapsed carry 갱신 → 크래시 시에도 타이머 보존
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

        # ── AI / RSI 조회 ──
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
                logger.warning("[SNIPER] ── AI / RSI 조회 ──: %s", exc, exc_info=True)

        if ai_score == 0.5 and rsi == 50.0:
            try:
                brain = getattr(ctx, "current_ai", {}).get("brain", {})
                ai_score = float(brain.get("ai_prediction", 0.5))
                rsi = float(brain.get("rsi", 50.0))
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[SNIPER] ── AI / RSI 조회 ──: %s", exc, exc_info=True)

        meta["ai_score"] = ai_score
        meta["selected_timeframe"] = selected_tf

        # [2026-03-07] RSI 3-tick smoothing: 순간 RSI 노이즈 완충
        # RSI는 틱마다 3~10pt 흔들려 선정 시점(RSI 28) → 실행 시점(RSI 35)으로
        # 변해 하드 게이트에서 탈락하는 문제 해소
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

        # ── 가격 히스토리 ──
        history = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
        if not history or len(history) < 3:
            return Decision(signal="hold", reason="sniper:insufficient_data", meta=meta)

        # ── 국면 모드 결정 ──
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
                logger.warning("[SNIPER] cycle_mode EMA 판정 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
                effective_cycle_mode = "UP" if rsi >= 50 else "DOWN"
        meta["cycle_mode"] = cycle_mode
        meta["cycle_mode_effective"] = effective_cycle_mode

        # ── ATR 기반 threshold 조정 (selector 값 없을 때만) ──
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
                logger.warning("[SNIPER] ── ATR 기반 threshold 조정 (selector 값 없을 때만) ──: %s", exc, exc_info=True)

        # ── ATR 기반 동적 TP/SL (SNIPER / SNIPER(s) 공통) ──
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
                logger.warning("[SNIPER] ── ATR 기반 동적 TP/SL (SNIPER / SNIPER(s) 공통) ──: %s", exc, exc_info=True)

        # 국면 모드 후처리
        if effective_cycle_mode == "UP":
            tp_pct = max(self._MIN_TP_PCT, tp_pct)
            sl_pct = max(self._MIN_SL_PCT, sl_pct)
        elif effective_cycle_mode == "DOWN":
            tp_pct = max(self._MIN_TP_PCT, min(tp_pct, 1.8))
            sl_pct = max(self._MIN_SL_PCT, min(sl_pct, 5.0))
            trail_tp = True
            trail_dist_pct = max(0.3, min(trail_dist_pct, 1.0))

        # 최종 안전 하한/상한 (legacy/runtime 값 방어)
        tp_pct = max(self._MIN_TP_PCT, min(tp_pct, 30.0))  # [FIX N11] 상한 30% 캡 (TP 무한대 방지)
        sl_pct = max(self._MIN_SL_PCT, sl_pct)

        meta["tp_pct"] = round(tp_pct, 4)
        meta["sl_pct"] = round(sl_pct, 4)
        meta["trail_tp"] = trail_tp
        meta["trail_dist_pct"] = round(trail_dist_pct, 4)
        meta["entry_threshold_pct"] = round(entry_threshold, 4)

        # ── 현재 상태 로드 ──
        state = self._get_state(ctx)
        pos = getattr(ctx, "position", None)
        has_pos = False
        try:
            has_pos = pos is not None and float(pos.get("qty", 0) or 0) > 0
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[SNIPER] position qty 파싱 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
            has_pos = False

        # 서버 재시작 안전장치: 포지션 있는데 상태 IDLE → ACTIVE로 복구
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
        # 보유 중: PROBE / ACTIVE / ARM_TRAIL 상태 처리
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

            # ── PROBE 상태: 확인 대기 ──
            if state == self._ST_PROBE:
                probe_ts = float(ctx.get_var("sniper_probe_ts", now))
                probe_price = float(ctx.get_var("sniper_probe_price", entry_price))
                elapsed = now - probe_ts
                meta["probe_elapsed_sec"] = round(elapsed)
                meta["sniper_phase"] = "PROBE"

                # ── 3분봉 저점 확인 후 CONFIRM 전략 ──
                # 조건: probe 진입 후 180초(3분봉 1개) 경과 + 현재가 >= 진입가 → 저점 확인
                confirm_ok = False

                if elapsed >= 180.0 and price >= probe_price:
                    confirm_ok = True
                    meta["confirm_trigger"] = "3min_hold"

                # 보조: EMA 골든크로스 + 60초 + 진입가 대비 +0.5% (강한 추세)
                if not confirm_ok and elapsed >= 60.0 and len(history) >= 12:
                    try:
                        ema_f = indicators.ema(history, 5)
                        ema_s = indicators.ema(history, 12)
                        if ema_f and ema_s and ema_f > ema_s and price > probe_price * 1.005:
                            confirm_ok = True
                            meta["confirm_trigger"] = "ema_golden"
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.warning("[SNIPER] 보조: EMA 골든크로스 + 60초 + 진입가 대비 +0.5%% (강한 추세): %s", exc, exc_info=True)

                if confirm_ok and elapsed < confirm_window_sec:
                    # → ACTIVE: 나머지 예산 매수
                    self._record_stat("confirm")
                    self._set_state(ctx, self._ST_ACTIVE)
                    ctx.set_var("sniper_active_ts", now)
                    meta["sniper_phase"] = "CONFIRM"
                    probe_ratio_eff = float(ctx.get_var("sniper_probe_ratio", probe_ratio) or probe_ratio)
                    # SNIPER(s) DCA를 위해 confirm에서 일부 예산을 남길 수 있다.
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
                        f"🎯🎯 [SNIPER v2] {market} 확인 진입!\n"
                        f"• Probe +{(price - probe_price) / probe_price * 100:.2f}%\n"
                        f"• {meta.get('confirm_trigger', 'OK')}\n"
                        f"• 추가 {confirm_ratio_eff:.0%} 매수"
                    )
                    return Decision(signal="buy", reason="sniper:confirm", meta=meta)

                # timeout: confirm_window 초과 → probe 포기, abort sell
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

            # ── ACTIVE / ARM_TRAIL 상태 ──
            meta["sniper_phase"] = state

            # 1) ARM_TRAIL: 트레일링 모드
            if state == self._ST_ARM_TRAIL:
                peak = float(ctx.get_var("sniper_peak_pct", profit_pct))
                if profit_pct > peak:
                    ctx.set_var("sniper_peak_pct", profit_pct)
                    peak = profit_pct
                meta["trail_peak_pct"] = peak

                # ATR 기반 trail 간격 동적 조정
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

            # 2) ACTIVE 상태: TP/SL/Time-stop/RSI exit
            # TP 도달 → ARM_TRAIL 전환 (바로 청산 안 함)
            if trail_tp and profit_pct >= tp_pct:
                self._set_state(ctx, self._ST_ARM_TRAIL)
                ctx.set_var("sniper_peak_pct", profit_pct)
                meta["arm_trail_at"] = round(profit_pct, 3)
                return Decision(signal="hold", reason="sniper:arm_trail", meta=meta)

            # TP 도달 (trail 비활성 시)
            if not trail_tp and profit_pct >= tp_pct:
                self._record_stat("win")
                self._reset_state(ctx)
                self._mark_exit(ctx, now, ai_score, profile=profile)
                self._check_contrarian_opportunity(market, price, rsi, ai_score, "tp")
                meta = self._make_sell_meta(meta, price, market)
                return Decision(signal="sell", reason="sniper:tp", meta=meta)

            # ── DCA 물타기 (SNIPER / SNIPER(s) 공통) ──
            dca_initial_entry = float(ctx.get_var("sniper_dca_initial_entry", 0.0))
            if dca_initial_entry <= 0:
                dca_initial_entry = entry_price
                ctx.set_var("sniper_dca_initial_entry", dca_initial_entry)
            # [FIX #6] entry_price가 0이면 DCA 계산 불가 → skip
            if dca_initial_entry <= 0:
                dca_initial_entry = 0.0  # 아래 drop_from_initial 계산 방어

            dca_step_pct = float(params.get("dca_step_pct", 0.5))
            if dca_step_pct <= 0:
                dca_step_pct = 0.5  # [FIX #10] 음수/0 방어 → 기본값 복원
            dca_add_ratio = float(params.get("dca_add_ratio", 0.5))
            _sl_for_depth = abs(float(params.get("sl_pct", sl_pct) or sl_pct))
            _default_depth = round(min(3.0, _sl_for_depth * 0.75), 1)
            dca_max_depth_pct = float(params.get("dca_max_depth_pct", _default_depth))
            dca_count = int(ctx.get_var("sniper_dca_count", 0))
            max_dca_steps = int(dca_max_depth_pct / dca_step_pct) if dca_step_pct > 0 else 0

            # 유동성 판단: volume_history 기반 (최근 거래량 평균)
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

            # 유동성별 DCA 조정
            if dca_liq_label == "low":
                # 저유동성: 최대 2회, step 넓힘 x2, 비율 축소 x0.6
                max_dca_steps = min(max_dca_steps, 2)
                dca_step_pct = dca_step_pct * 2.0
                dca_add_ratio = dca_add_ratio * 0.6
            elif dca_liq_label == "high":
                # 고유동성: 역피라미딩 배율 증가 가능
                pass

            # 역피라미딩: 단계가 깊을수록 비율 증가 (1x → 1.25x → ... → max 3x)
            pyramid_mult = min(1.0 + dca_count * 0.25, 3.0)  # [FIX N7] 상한 3x 캡 (무제한 확대 방지)
            effective_ratio = round(dca_add_ratio * pyramid_mult, 4)

            drop_from_initial = ((dca_initial_entry - price) / dca_initial_entry * 100) if dca_initial_entry > 0 else 0.0  # [FIX #6] div/0 방어
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
                liq_tag = " ⚠️저유동" if dca_liq_label == "low" else ""
                send_signal_telegram(
                    f"📊 [SNIPER DCA] {market} 물타기 #{dca_count + 1}/{max_dca_steps}{liq_tag}\n"
                    f"• 초기가 대비 -{drop_from_initial:.2f}%\n"
                    f"• 추가매수 {effective_ratio:.0%} (역피라미딩 x{pyramid_mult:.2f})\n"
                    f"• 평단: {entry_price:,.0f} → 현재: {price:,.0f}"
                )
                return Decision(signal="buy", reason="sniper:dca", meta=meta)

            # [2026-03-08] 즉시매수 보호: 매수 직후 3분간 SL/timeout/RSI exit 차단 (TP만 허용)
            # 급매수 직후 미세 하락/노이즈에 의한 즉시 손절 방지
            _buy_grace_sec = float(params.get("instant_buy_grace_sec", 180.0))
            _active_ts_grace = float(ctx.get_var("sniper_active_ts", 0.0))
            _in_buy_grace = (_active_ts_grace > 0 and (now - _active_ts_grace) < _buy_grace_sec)
            if _in_buy_grace:
                meta["buy_grace_remaining_sec"] = round(_buy_grace_sec - (now - _active_ts_grace))

            # SL (연속 3틱 확인 — 노이즈 방어)
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

            # RSI Exit (과매수) — 매수 grace 중에는 차단
            if rsi_exit_enabled and rsi >= 70 and not _in_buy_grace:
                self._record_stat("win" if profit_pct > 0 else "loss")
                self._reset_state(ctx)
                self._mark_exit(ctx, now, ai_score, profile=profile)
                meta = self._make_sell_meta(meta, price, market)
                return Decision(signal="sell", reason="sniper:rsi_exit", meta=meta)

            # TIME-STOP: 횡보 타임아웃 (수수료 루프 방지)
            active_ts = float(ctx.get_var("sniper_active_ts", 0.0))
            # [FIX #7] 수동 편성/복구 시 active_ts 미설정 → position entry_ts로 fallback
            if active_ts <= 0:
                active_ts = float((pos or {}).get("entry_ts", 0) or (pos or {}).get("ts", 0) or 0)
                if active_ts > 0:
                    ctx.set_var("sniper_active_ts", active_ts)  # 이후 틱에서 재계산 방지
            if active_ts > 0:
                hold_minutes = (now - active_ts) / 60.0
                meta["hold_minutes"] = round(hold_minutes, 1)
                if hold_minutes >= time_stop_min and abs(profit_pct) < 0.5 and profit_pct <= 0:
                    # [FIX] 수익 중 타임아웃이면 win으로 기록 (이전엔 항상 loss 기록)
                    self._record_stat("win" if profit_pct > 0 else "loss")
                    self._reset_state(ctx)
                    self._mark_exit(ctx, now, ai_score, profile=profile)
                    meta = self._make_sell_meta(meta, price, market)
                    meta["timeout_reason"] = f"{hold_minutes:.0f}min_flat"
                    return Decision(signal="sell", reason="sniper:timeout", meta=meta)

            # 추세 보호: 상승 중이면 조기 청산 차단 (단, 최대 보호 시간 초과 시 강제 우회)
            trend_protect = bool(params.get("trend_protect_enabled", True))
            max_protect_hours = float(params.get("max_trend_protect_hours", 48.0))  # [FIX M7] 무기한 보호 방지
            # hold_minutes는 active_ts 블록 내에서만 정의되므로 안전하게 재계산
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
                    logger.warning("[SNIPER] hold_minutes는 active_ts 블록 내에서만 정의되므로 안전하게 재계산: %s", exc, exc_info=True)

            # 저격 매도 (near_high exit)
            if exit_enabled:
                exit_high = 0.0
                try:
                    exit_high = float(ctx.get_rolling_high(float(exit_lookback)) or 0.0)
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[SNIPER] rolling_high 조회 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
                    exit_high = 0.0
                if exit_high <= 0.0:
                    exit_bars = min(exit_lookback, len(history))
                    exit_recent = history[-exit_bars:] if exit_bars > 0 else history
                    exit_high = max(exit_recent) if exit_recent else float(price)
                exit_target = exit_high * (1 - exit_threshold / 100)
                meta["exit_high_price"] = exit_high
                meta["exit_target_price"] = exit_target
                # near_high exit: TP Guards 하한(1.2%) 이상일 때만 허용
                # 이전: 수익률 무관 → 0.6%에서도 매도 → TP Guards 우회
                _near_high_min_pct = max(self._MIN_TP_PCT, float(params.get("near_high_min_profit_pct", self._MIN_TP_PCT)))
                if price >= exit_target and profit_pct >= _near_high_min_pct:
                    self._record_stat("win" if profit_pct > 0 else "loss")
                    self._reset_state(ctx)
                    self._mark_exit(ctx, now, ai_score, profile=profile)
                    meta = self._make_sell_meta(meta, price, market)
                    return Decision(signal="sell", reason="sniper:near_high", meta=meta)

            return Decision(signal="hold", reason="sniper:holding", meta=meta)

        # =============================================
        # 미보유: IDLE / WATCH 상태 처리 (Entry 파이프라인)
        # =============================================
        # 상태 클린업: 포지션 없는데 PROBE/ACTIVE 상태면 IDLE로 복귀
        if state not in (self._ST_IDLE, self._ST_WATCH):
            self._reset_state(ctx)
            state = self._ST_IDLE

        if not entry_enabled:
            return Decision(signal="hold", reason="sniper:entry_disabled", meta=meta)

        # [2026-03-07] 수동 배치(buy_now=True) → 모든 게이트 우회, 즉시 매수
        # 수동 배치는 strategy_router.py에서 이미 시장가 매수를 시도하지만,
        # FSM 실패·페이퍼 모드 등 fallback 경로에서 SniperPlugin이 처리한다.
        if bool(params.get("buy_now", False)):
            meta["buy_now"] = True
            return Decision(signal="buy", reason="sniper:buy_now_manual", meta=meta)

        # 쿨다운 / 재진입 제어
        auto_reentry = bool(params.get("auto_reentry", False))
        last_exit_ts = float(ctx.get_var("sniper_last_exit_ts", 0.0))
        cooldown_sec = expiry_min * 60
        # [FIX #4] 다른 변종(SNIPER↔SNIPER(S))이 남긴 쿨다운은 무시
        _exit_profile = str(ctx.get_var("sniper_exit_profile", "") or "").upper()
        if _exit_profile and _exit_profile != profile:
            last_exit_ts = 0.0  # 다른 변종의 exit → 쿨다운/재진입 제한 비적용
        if last_exit_ts > 0:
            if not auto_reentry:
                # auto_reentry=False → 최대 2회 재진입, AI 점수 10%p 이상 개선 시만
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
                # AI 충분히 개선됨 → 쿨다운 후 재진입 허용
            if (now - last_exit_ts) < cooldown_sec:
                meta["cooldown_remaining"] = cooldown_sec - (now - last_exit_ts)
                return Decision(signal="hold", reason="sniper:cooldown", meta=meta)

        # 일일 발사 횟수 제한
        daily_key = f"sniper_shots_{time.strftime('%Y%m%d')}"
        daily_shots = int(ctx.get_var(daily_key, 0))
        meta["daily_shots"] = daily_shots
        if daily_shots >= self._MAX_DAILY_SHOTS:
            return Decision(signal="hold", reason="sniper:daily_limit", meta=meta)

        # AI Gate — [FIX] 하드 게이트 → 소프트 grace zone
        # AI도 수시로 변동하므로 약간 미달 시 RSI가 극과매도면 보완 허용
        _ai_grace_pct = float(params.get("ai_grace_pct", 10.0))  # 기본 10% grace
        _ai_hard_floor = effective_ai_min * (1 - _ai_grace_pct / 100.0)
        if ai_gate_enabled and ai_score < effective_ai_min:
            meta["ai_required"] = round(effective_ai_min, 4)
            meta["ai_hard_floor"] = round(_ai_hard_floor, 4)
            # hard floor 미만 → 무조건 탈락 (ex: 0.50 * 0.90 = 0.45)
            if ai_score < _ai_hard_floor:
                meta["ai_blocked"] = True
                return Decision(signal="hold", reason="sniper:ai_gate", meta=meta)
            # grace zone (hard_floor <= ai < ai_min): RSI 극과매도면 통과
            if rsi < 30:
                meta["ai_grace_pass"] = True
                meta["ai_grace_reason"] = "rsi_deeply_oversold"
            else:
                meta["ai_blocked"] = True
                meta["ai_grace_fail"] = True
                return Decision(signal="hold", reason="sniper:ai_gate_grace", meta=meta)

        # RSI Entry 필터 — [FIX] 하드 게이트 → 소프트 grace zone
        # RSI는 수시로 변동하므로 살짝 초과(grace_zone) 시 다른 지표로 보완 허용
        # grace_zone 내에서는 AI가 충분히 높거나 RSI가 하락 추세면 통과
        _rsi_grace_pct = float(params.get("rsi_grace_pct", 15.0))  # 기본 15% grace
        _rsi_hard_cap = effective_rsi_entry_max * (1 + _rsi_grace_pct / 100.0)
        if rsi_entry_enabled and rsi > effective_rsi_entry_max:
            meta["rsi_required_max"] = round(effective_rsi_entry_max, 2)
            meta["rsi_hard_cap"] = round(_rsi_hard_cap, 2)
            # hard cap 초과 → 무조건 탈락 (ex: 38 * 1.15 ≈ 43.7)
            if rsi > _rsi_hard_cap:
                meta["rsi_blocked"] = True
                return Decision(signal="hold", reason="sniper:rsi_entry", meta=meta)
            # grace zone (entry_max < rsi <= hard_cap): 보완 조건 체크
            _rsi_falling = len(_rsi_buf) >= 3 and _rsi_buf[-1] < _rsi_buf[-2]  # RSI 하락 중
            _ai_strong = ai_score >= (effective_ai_min + 0.10)  # AI가 하한 + 10%p 이상
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

        # 자본 확인
        capital = 0.0
        try:
            c = getattr(ctx, "usable_capital", None)
            if c is None:
                c = getattr(ctx, "allocated_capital", None)
            capital = float(c or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[SNIPER] 자본 확인 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
            capital = 0.0

        min_order = float(params.get("min_order_usdt", Q.min_order))
        if capital < min_order:
            return Decision(signal="hold", reason="sniper:insufficient_capital", meta=meta)

        # 저점 근접 조건 확인 — 5분봉 기준 rolling low 우선 사용
        # entry_lookback_min을 분(minutes) 단위로 해석해 타임스탬프 기반 최저가 계산
        # → 스캐너(5분봉)와 동일한 시간 축으로 통일
        entry_low = 0.0
        try:
            entry_low = ctx.get_rolling_low(float(entry_lookback))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[SNIPER] → 스캐너(5분봉)와 동일한 시간 축으로 통일: %s", exc, exc_info=True)
        if entry_low <= 0:
            # fallback: 기존 틱 기반
            entry_bars = min(entry_lookback, len(history))
            entry_recent = history[-entry_bars:] if entry_bars > 0 else history
            entry_low = min(entry_recent) if entry_recent else float(price)
        entry_target = entry_low * (1 + entry_threshold / 100)
        meta["entry_low_price"] = entry_low
        meta["entry_target_price"] = entry_target
        meta["distance_pct"] = (price - entry_low) / entry_low * 100 if entry_low > 0 else 999

        near_low = price <= entry_target

        # EMA 크로스 검증 (선택적)
        ema_cross_enabled = bool(params.get("ema_cross_enabled", False))
        if ema_cross_enabled and near_low and len(history) >= 50:
            try:
                ema_fast = indicators.ema(history, 12)
                ema_slow = indicators.ema(history, 26)
                if ema_fast and ema_slow and ema_fast <= ema_slow:
                    meta["ema_cross_blocked"] = True
                    return Decision(signal="hold", reason="sniper:no_golden_cross", meta=meta)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[SNIPER] EMA 크로스 검증 (선택적): %s", exc, exc_info=True)

        # ── Bottom Probability Score (BPS) — 저점 확률 점수 ──
        # 6개 지표를 합산해 현재 가격이 저점 구간일 확률을 0~100점으로 점수화
        bps = 0.0
        bps_detail: dict = {}
        try:
            # 1) RSI (0~30pts): 과매도 깊이
            if rsi < 25:
                bps += 30; bps_detail["rsi"] = 30
            elif rsi < 30:
                bps += 22; bps_detail["rsi"] = 22
            elif rsi < 35:
                bps += 14; bps_detail["rsi"] = 14
            elif rsi < 42:
                bps += 6;  bps_detail["rsi"] = 6

            # 2) MACD 히스토그램 턴 (0~20pts): 음수에서 반등
            if len(history) >= 36:
                _ml, _sl, _hist = indicators.macd(list(history))
                _ml2, _sl2, _hist2 = indicators.macd(list(history)[:-3])
                if _hist is not None and _hist2 is not None:
                    if _hist2 < 0 and _hist > _hist2:   # 음수에서 상승 중
                        bps += 20; bps_detail["macd"] = 20
                    elif _hist > _hist2:                 # 상승 중 (양수 구간도)
                        bps += 10; bps_detail["macd"] = 10

            # 3) BB z-score (0~20pts): 밴드 하단 이탈 깊이
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

            # 4) 꼬리캔들 회복 (0~15pts): 저점 찍고 소폭 반등
            if len(history) >= 5:
                _recent_min = min(list(history)[-5:])
                if _recent_min > 0 and float(price) > _recent_min:
                    _bounce = (float(price) - _recent_min) / _recent_min * 100
                    if 0.05 <= _bounce <= 1.5:   # 너무 많이 오른 건 제외
                        bps += 15 if _bounce >= 0.3 else 8
                        bps_detail["tail"] = round(_bounce, 3)

            # 5) 거래량 급증 (0~10pts): 항복 매도 신호
            _vols = list(getattr(ctx, "volume_history", []) or [])  # [FIX #2] AttributeError 방어
            if len(_vols) >= 10:
                _recent_v = _vols[-1]
                _avg_v = sum(_vols[-20:-1]) / max(1, len(_vols[-20:-1]))
                if _avg_v > 0:
                    _vr = _recent_v / _avg_v
                    if _vr >= 2.0:
                        bps += 10; bps_detail["vol"] = round(_vr, 2)
                    elif _vr >= 1.5:
                        bps += 5;  bps_detail["vol"] = round(_vr, 2)

            # 6) AI 신뢰도 보너스 (0~5pts)
            if ai_score >= 0.70:
                bps += 5; bps_detail["ai"] = 5
            elif ai_score >= 0.60:
                bps += 2; bps_detail["ai"] = 2

        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[SNIPER] 6) AI 신뢰도 보너스 (0~5pts): %s", exc, exc_info=True)

        bps = min(100.0, bps)
        meta["bps"] = round(bps, 1)
        meta["bps_detail"] = bps_detail

        # ── IDLE → PROBE Fast Entry (BPS 기반 즉시 진입, WATCH 생략) ──
        fast_entry_enabled = bool(params.get("fast_entry_enabled", True))
        fast_entry_bps_min = float(params.get("fast_entry_bps_min", 55.0))

        # [2026-03-07] SNIPER(S) BPS Fire 점진 완화:
        # 최고 75에서 시작 → 30분에 걸쳐 서서히 하락 → 최저 fast_entry_bps_min(55)
        # 슬롯 배치 직후에는 높은 기준(75)을 요구하고,
        # 시간이 흐르면서 기준을 낮춰 진입 기회를 넓힌다.
        if is_scope_snipers:
            _bps_start = float(params.get("scope_bps_fire_start", 75.0))
            _bps_floor = float(params.get("scope_bps_fire_floor", fast_entry_bps_min))
            _bps_decay_min = float(params.get("scope_bps_fire_decay_min", 30.0))  # 30분에 걸쳐 감소
            _scope_elapsed = float(meta.get("scope_wait_min", 0.0))
            if _bps_decay_min > 0 and _scope_elapsed < _bps_decay_min:
                _ratio = min(1.0, _scope_elapsed / _bps_decay_min)
                fast_entry_bps_min = _bps_start - (_bps_start - _bps_floor) * _ratio
            else:
                fast_entry_bps_min = _bps_floor
            meta["bps_fire_threshold"] = round(fast_entry_bps_min, 1)

        # GreenPen PA 확인 (greenpen_enabled=True 일 때만)
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
                f"⚡ [SNIPER BPS Fast] {market} 즉시 Probe ({probe_ratio:.0%}) | BPS {bps:.0f}pt\n"
                f"• 현재가: {price:,.0f} | RSI: {rsi:.1f} | AI: {ai_score:.0%}\n"
                f"• {entry_lookback}분 최저가: {entry_low:,.0f}\n"
                f"• TP: {tp_pct}% / SL: {sl_pct}%"
            )
            return Decision(signal="buy", reason="sniper:probe", meta=meta)

        # ── IDLE → WATCH 전환 (Phase 0: 관측 시작) ──
        if state == self._ST_IDLE:
            if near_low:
                self._set_state(ctx, self._ST_WATCH)
                ctx.set_var("sniper_watch_ts", now)
                ctx.set_var("sniper_watch_low", price)
                meta["watch_started"] = True
                return Decision(signal="hold", reason="sniper:watch_start", meta=meta)
            return Decision(signal="hold", reason="sniper:wait", meta=meta)

        # ── WATCH 상태: 관측 윈도우 ──
        if state == self._ST_WATCH:
            watch_ts = float(ctx.get_var("sniper_watch_ts", now))
            watch_low = float(ctx.get_var("sniper_watch_low", price))
            elapsed = now - watch_ts
            meta["watch_elapsed_sec"] = round(elapsed)

            # 조건 이탈: 저점에서 벗어남
            # [2026-03-07] WATCH abort tolerance: entry_target 위 마진 허용
            # 빠른 반등 시 entry_target을 약간 초과해도 즉사하지 않음
            # BPS/confidence가 높을수록 마진 확대 (강한 시그널은 반등이 진짜일 수 있음)
            _deploy_conf = float(params.get("deploy_confidence", 0) or 0)
            if _deploy_conf >= 60.0 or bps >= 65.0:
                _abort_margin = 1.008   # 0.8% — 강한 시그널
            elif bps >= 50.0:
                _abort_margin = 1.005   # 0.5%
            else:
                _abort_margin = 1.003   # 0.3%
            _abort_price = entry_target * _abort_margin
            if price > _abort_price:
                self._reset_state(ctx)
                meta["abort_margin"] = round((_abort_margin - 1.0) * 100, 2)
                return Decision(signal="hold", reason="sniper:watch_abort", meta=meta)

            # 적응형 watch_sec: RSI 낮을수록, AI 높을수록 관측 시간 단축 (최소 30초)
            try:
                _rsi_factor = max(0.0, min(1.0, (rsi - 20.0) / 30.0))   # RSI 20→0, 50→1
                _ai_factor = max(0.0, min(1.0, (0.8 - ai_score) / 0.4))  # AI 0.8→0, 0.4→1
                _compress = 1.0 - 0.6 * (1.0 - (_rsi_factor + _ai_factor) / 2.0)
                effective_watch_sec = max(30.0, watch_sec * _compress)
            except (TypeError, ValueError):
                logger.warning("[SNIPER] WATCH 압축 계산 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
                effective_watch_sec = watch_sec
            meta["effective_watch_sec"] = round(effective_watch_sec)

            # 관측 시간 미충족
            if elapsed < effective_watch_sec:
                # 관측 중 더 낮은 가격 추적
                if price < watch_low:
                    ctx.set_var("sniper_watch_low", price)
                return Decision(signal="hold", reason="sniper:watching", meta=meta)

            # 관측 통과! → 체결 품질 + 모멘텀 확인 후 PROBE 진입
            exec_quality = self._check_execution_quality(ctx, history)
            meta["exec_quality"] = exec_quality

            momentum_ok = False
            if len(history) >= 5:
                recent_5 = history[-5:]
                if recent_5[-1] >= recent_5[0]:
                    momentum_ok = True
            if price >= watch_low:
                momentum_ok = True

            # 매도 압력 우위면 진입 차단
            if exec_quality["score"] < -1.0:
                self._reset_state(ctx)
                meta["watch_blocked"] = "sell_pressure"
                return Decision(signal="hold", reason="sniper:watch_sell_pressure", meta=meta)

            if momentum_ok:
                # → PROBE: 소액 진입
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
                    f"🔭 [SNIPER v2] {market} Probe 진입 ({probe_ratio:.0%})\n"
                    f"• 현재가: {price:,.0f}\n"
                    f"• {entry_lookback}분 최저가: {entry_low:,.0f}\n"
                    f"• 관측 {elapsed:.0f}초 통과\n"
                    f"• TP: {tp_pct}% / SL: {sl_pct}%"
                    + (f"\n• {filter_str}" if filter_str else "")
                )
                return Decision(signal="buy", reason="sniper:probe", meta=meta)
            else:
                # 모멘텀 없음: 관측 리셋
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
        """SNIPER 익절 후 역행 매수 기회 체크, Reserved Queue 등록 및 텔레그램 알림.
        
        조건: RSI <= 35 AND AI >= 0.5 시에만 역행 기회로 판정
        """
        try:
            # 역행 매수 조건 (안전장치)
            if rsi > 35:
                return  # RSI 높으면 역행 X
            if ai_score < 0.5:
                return  # AI 낮으면 역행 X

            # Reserved Queue에 CONTRARIAN 후보로 자동 등록
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
                        "suggested_budget_usdt": 50,  # 기본 50 USDT
                        "recommended_params": {
                            "tp_pct": 5.0,
                            "sl_pct": -3.0,
                            "trail_tp": True,
                            "trail_dist_pct": 2.0,
                            "min_score": 2,
                        },
                        "reason": f"SNIPER {exit_reason} 익절 → 역행 기회",
                    }
                    reserved_queue.push(candidate)
                    registered = True
                except (KeyError, IndexError, TypeError) as exc:
                    logger.warning("[SNIPER] Reserved Queue에 CONTRARIAN 후보로 자동 등록: %s", exc, exc_info=True)
            reg_msg = " ✅ Reserved 등록됨" if registered else ""
            send_signal_telegram(
                f"🔄 [역행 매수 기회] {market}\n"
                f"• SNIPER {exit_reason} 익절 → 매도 압력 발생\n"
                f"• RSI: {rsi:.1f} (과매도)\n"
                f"• AI: {ai_score:.0%}\n"
                f"• 현재가: {price:,.0f}\n"
                f"• 💡 있으면 팔고, 없으면 사라!{reg_msg}"
            )
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[SNIPER] Reserved Queue에 CONTRARIAN 후보로 자동 등록: %s", exc, exc_info=True)

