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
    _common_dca_check,
    _evaluate_reversal_buy_guard,
    _inject_candle_1m_telemetry,
    _reset_dca_state,
    _try_convert_to_longhold,
    send_signal_telegram,
)

logger = logging.getLogger(__name__)


class ContrarianPlugin(StrategyPlugin):
    """CONTRARIAN 전략 플러그인.

    시장 하락 시 역행하는 코인을 감지하여 매수.
    - Relative Strength (BTC 대비 상대 강도)
    - Correlation (상관관계)
    - 시장 하락 + 개별 상승 감지

    [CREATED 2026-01-26]
    [ENHANCED 2026-01-26]
    - Trailing TP 추가
    - 유동성 필터 추가
    - 동적 TP/SL (ATR 기반)
    - 2단계 진입 (RSI 확인)
    """

    name: str = "contrarian"

    def decide(self, ctx: Any, price: float) -> Decision:
        from app.core.contrarian_scanner import get_contrarian_scanner

        params: Dict[str, Any] = {}
        try:
            ctrls = getattr(ctx, "controls", None) or {}
            if isinstance(ctrls, dict):
                params = dict((ctrls.get("strategy") or {}).get("params") or {})
        except (KeyError, AttributeError, TypeError):
            logger.warning("[%s] params 추출 실패 → 기본값 사용: %s", self.name if hasattr(self, 'name') else '?', getattr(ctx, 'market', '?'), exc_info=True)
            params = {}

        # 기본 파라미터
        # [2026-02-04] CONTRARIAN - params 값 사용 (하드코딩 제거)
        min_score = int(params.get("min_score", 2))
        tp_pct = float(params.get("tp", params.get("tp_pct", 15.0)))  # 기본 TP: 15%
        sl_pct = float(params.get("sl", params.get("sl_pct", -5.0)))  # 기본 SL -5.0% (역행 전략 여유폭)
        if sl_pct > 0:
            sl_pct = -sl_pct
        cooldown_sec = float(params.get("cooldown_sec", 600))
        max_hold_sec = float(params.get("max_hold_sec", 86400))  # 기본 24시간 (장기보유)

        # [NEW] Trailing TP 파라미터
        trail_tp_enabled = bool(params.get("trail_tp_enabled", True))
        trail_dist_pct = float(params.get("trail_dist_pct", 3.0))

        # [NEW] 유동성 필터 파라미터
        min_volume_usdt = float(params.get("min_volume_usdt", 100_000))  # 100K USDT

        # [NEW] 동적 TP/SL 파라미터
        use_atr = bool(params.get("use_atr", True))
        atr_tp_mult = float(params.get("atr_tp_mult", 2.5))
        atr_sl_mult = float(params.get("atr_sl_mult", 1.5))

        # [NEW] 2단계 진입 파라미터
        rsi_filter_enabled = bool(params.get("rsi_filter", True))
        rsi_max = float(params.get("rsi_max", 50))  # [2026-02-03] RSI 50 이하로 강화 (이미 많이 오른 코인 제외)

        # EMA 크로스 진입 필터 (기본 비활성 — 급락 역행 시 기회 손실 방지)
        ema_cross_enabled = bool(params.get("ema_cross_enabled", False))
        ema_fast = int(params.get("ema_fast", 5))
        ema_slow = int(params.get("ema_slow", 20))
        user_sell_only = bool(params.get("user_sell_only", False))

        # market: 우선순위 - ctx.market > ctx.code > "BTCUSDT"
        market = str(getattr(ctx, "market", "") or getattr(ctx, "code", "") or "BTCUSDT")

        # 스캐너 조회
        scanner = get_contrarian_scanner()
        is_signal, candidate = scanner.is_contrarian_signal(market, min_score=min_score)

        meta: Dict[str, Any] = {
            "strategy": "contrarian",
            "min_score": min_score,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "trail_tp_enabled": trail_tp_enabled,
            "use_atr": use_atr,
            "rsi_filter": rsi_filter_enabled,
        }

        if candidate:
            cand_benchmark_ret = getattr(candidate, "benchmark_ret_pct", None)
            if cand_benchmark_ret is None:
                cand_benchmark_ret = getattr(candidate, "btc_ret_pct", 0.0)
            meta.update({
                "coin_ret_pct": candidate.coin_ret_pct,
                # benchmark_ret_pct is the current field name (btc_ret_pct is legacy).
                "benchmark_ret_pct": cand_benchmark_ret,
                "btc_ret_pct": cand_benchmark_ret,
                "rs": candidate.rs,
                "rs_diff": candidate.rs_diff,
                "corr": candidate.corr,
                "score": candidate.score,
                "rank": candidate.rank,
            })
            # [2026-02-23] 조기 감지 정보
            meta["rs_momentum"] = getattr(candidate, "rs_momentum", 0.0)
            meta["acceleration"] = getattr(candidate, "acceleration", 0.0)
            meta["early_signal"] = getattr(candidate, "early_signal", False)
            early_reasons = getattr(candidate, "early_reasons", [])
            meta["early_reasons"] = early_reasons if isinstance(early_reasons, list) else []

        if scanner._cache:
            meta["market_down"] = scanner._cache.market_down
            cache_benchmark_ret = getattr(scanner._cache, "benchmark_ret_pct", None)
            if cache_benchmark_ret is None:
                cache_benchmark_ret = getattr(scanner._cache, "btc_ret_pct", 0.0)
            # Keep both keys for UI/backward compatibility.
            meta["benchmark_ret_pct_global"] = cache_benchmark_ret
            meta["btc_ret_pct_global"] = cache_benchmark_ret

        # 텔레메트리 주입
        _inject_candle_1m_telemetry(ctx, meta)

        # [NEW] 동적 TP/SL 계산 (ATR 기반)
        history = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
        if use_atr and len(history) >= 14:
            atr = indicators.atr_simplified(history, 14)
            if atr and price > 0:
                dynamic_tp = abs((atr * atr_tp_mult) / price * 100.0)
                dynamic_sl = -abs((atr * atr_sl_mult) / price * 100.0)
                # 사용자 설정이 없으면 동적 값 사용
                if "tp" not in params:
                    tp_pct = max(tp_pct, dynamic_tp)  # 더 큰 값 사용
                    meta["dynamic_tp"] = dynamic_tp
                if "sl" not in params:
                    sl_pct = min(sl_pct, dynamic_sl)  # 더 작은 값 사용
                    meta["dynamic_sl"] = dynamic_sl

        # 포지션 확인
        pos = getattr(ctx, "position", None)
        has_pos = (pos is not None and float(pos.get("qty", 0.0) or 0.0) > 0)

        now = time.time()

        # ── [2026-03-09] selector 신뢰 즉시매수: 포지션 없으면 selector가 이미 검증 → buy ──
        if not has_pos:
            # GreenPen PA + Structure 확인
            if bool(params.get("greenpen_enabled", False)):
                from app.strategy.greenpen import check_entry_guard
                _gp = check_entry_guard("CONTRARIAN", getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or []), price)
                if not _gp["allow"]:
                    return Decision(signal="hold", reason=f"contrarian:gp_{_gp['reason']}", meta={"gp": _gp})
            meta["selector_fast_entry"] = True
            return Decision(signal="buy", reason="contrarian:selector_entry", meta=meta)

        # ============================================================
        # 포지션 보유 중: TP/SL/Trailing/타임스탑 체크
        # ============================================================
        if has_pos:
            entry = float(
                pos.get("entry", 0.0)
                or pos.get("avg_price", 0.0)
                or pos.get("entry_price", 0.0)
                or getattr(ctx, "avg_buy_price", 0.0)
                or 0.0
            )
            entry_ts = float(ctx.get_var("contrarian_entry_ts", 0.0))

            if entry > 0:
                profit_pct = (price - entry) / entry * 100.0
                meta["profit_pct"] = profit_pct
                qty = float(pos.get("qty") or 0.0)
                profit_usdt = (price - entry) * qty
                meta["profit_usdt"] = profit_usdt

                # --------------------------------------------------------
                # SL: 기본 sl_pct (기본 -5%) + ATR 기반 동적 SL
                # extreme_sl(-70%) 제거 — 사실상 파산 허용 수준이었음
                # --------------------------------------------------------

                # 일반 SL (기본 sl_pct 또는 사용자 지정)
                if profit_pct <= sl_pct:
                    # ── CONTRARIAN DCA: SL 매도 전에 물타기 시도 ──
                    dca_result = _common_dca_check(ctx, price, entry, params, "ct", meta)
                    if dca_result is not None:
                        return dca_result
                    # ── DCA 불가 → LongHold 전환 시도 (역발상 특성상 회복 가능성 높음) ──
                    _lh_market = getattr(ctx, "market", "")
                    _lh_result = _try_convert_to_longhold(ctx, _lh_market, "CONTRARIAN", entry, price, meta)
                    if _lh_result is not None:
                        return _lh_result
                    send_signal_telegram(
                        f"🛑 [CONTRARIAN] {market} SL Hit!\n"
                        f"• Loss: {profit_pct:.2f}%\n"
                        f"• SL: {sl_pct:.1f}% (기본/사용자)\n"
                        f"• PnL: {profit_usdt:,.0f} USDT"
                    )
                    self._reset_state(ctx, now)
                    amount = meta.get("amount", Q.min_order)
                    order_price = price
                    amount, order_price = adjust_order_amount_and_price(amount, order_price, market)
                    meta["amount"] = amount
                    meta["price"] = order_price
                    meta["force_exit"] = True
                    meta["fallback_to_market"] = True
                    return Decision(signal="sell", reason="contrarian:sl", meta=meta)

                # ATR 기반 동적 SL (사용자 설정 시)
                if use_atr and len(history) >= 14 and "dynamic_sl" in meta:
                    dynamic_sl_val = meta["dynamic_sl"]
                    if profit_pct <= dynamic_sl_val:
                        send_signal_telegram(
                            f"📉 [CONTRARIAN] {market} Dynamic SL Hit!\n"
                            f"• Loss: {profit_pct:.2f}%\n"
                            f"• Dynamic SL: {dynamic_sl_val:.1f}% (ATR 기반)\n"
                            f"• PnL: {profit_usdt:,.0f} USDT"
                        )
                        self._reset_state(ctx, now)
                        # 주문 보정 (market 변수 재사용)
                        amount = meta.get("amount", Q.min_order)
                        order_price = price
                        amount, order_price = adjust_order_amount_and_price(amount, order_price, market)
                        meta["amount"] = amount
                        meta["price"] = order_price
                        meta["force_exit"] = True
                        meta["fallback_to_market"] = True
                        return Decision(signal="sell", reason="contrarian:dynamic_sl", meta=meta)

                # --------------------------------------------------------
                # [NEW] Trailing TP 로직
                # --------------------------------------------------------
                if trail_tp_enabled:
                    trailing_active = bool(ctx.get_var("ct_trailing_active", False))
                    trail_peak = float(ctx.get_var("ct_trail_peak", 0.0))
                    trail_stop = float(ctx.get_var("ct_trail_stop", 0.0))

                    meta["trailing_active"] = trailing_active

                    if trailing_active:
                        # 최고가 갱신
                        if price > trail_peak:
                            trail_peak = price
                            new_stop = trail_peak * (1.0 - trail_dist_pct / 100.0)
                            if new_stop > trail_stop:
                                trail_stop = new_stop
                            ctx.set_var("ct_trail_peak", trail_peak)
                            ctx.set_var("ct_trail_stop", trail_stop)

                        meta["trail_peak"] = trail_peak
                        meta["trail_stop"] = trail_stop

                        # Trail Stop 도달 → 매도
                        if price <= trail_stop:
                            final_profit_pct = (price - entry) / entry * 100.0
                            final_profit_usdt = (price - entry) * qty
                            send_signal_telegram(
                                f"📈 [CONTRARIAN] {market} Trailing TP!\n"
                                f"• Profit: +{final_profit_pct:.2f}%\n"
                                f"• PnL: +{final_profit_usdt:,.0f} USDT\n"
                                f"• Peak: {trail_peak:,.0f} → Exit: {price:,.0f}\n"
                                f"• 역행 매매 성공! 🎯"
                            )
                            self._reset_state(ctx, now)
                            # 주문 보정
                            market = getattr(ctx, "market", "BTCUSDT")
                            amount = meta.get("amount", Q.min_order)
                            order_price = price
                            amount, order_price = adjust_order_amount_and_price(amount, order_price, market)
                            meta["amount"] = amount
                            meta["price"] = order_price
                            meta["force_exit"] = True
                            meta["fallback_to_market"] = True
                            return Decision(signal="sell", reason="contrarian:trailing_tp", meta=meta)

                        return Decision(signal="hold", reason="contrarian:trailing_active", meta=meta)

                    elif profit_pct >= tp_pct:
                        # TP 도달 → 트레일링 활성화
                        trail_peak = price
                        trail_stop = trail_peak * (1.0 - trail_dist_pct / 100.0)
                        ctx.set_var("ct_trailing_active", True)
                        ctx.set_var("ct_trail_peak", trail_peak)
                        ctx.set_var("ct_trail_stop", trail_stop)

                        send_signal_telegram(
                            f"🚀 [CONTRARIAN] {market} Trailing Activated!\n"
                            f"• Profit: +{profit_pct:.2f}%\n"
                            f"• Trail Stop: {trail_stop:,.0f} (-{trail_dist_pct:.1f}%)\n"
                            f"• 더 오르면 따라가고, 떨어지면 익절!"
                        )

                        meta["trailing_active"] = True
                        meta["trail_peak"] = trail_peak
                        meta["trail_stop"] = trail_stop
                        return Decision(signal="hold", reason="contrarian:trailing_armed", meta=meta)

                else:
                    # Trailing 비활성화 시 단순 TP
                    if profit_pct >= tp_pct:
                        send_signal_telegram(
                            f"📈 [CONTRARIAN] {market} TP Hit!\n"
                            f"• Profit: +{profit_pct:.2f}%\n"
                            f"• PnL: +{profit_usdt:,.0f} USDT\n"
                            f"• 역행 매매 성공! 🎯"
                        )
                        self._reset_state(ctx, now)
                        # 주문 보정
                        market = getattr(ctx, "market", "BTCUSDT")
                        amount = meta.get("amount", Q.min_order)
                        order_price = price
                        amount, order_price = adjust_order_amount_and_price(amount, order_price, market)
                        meta["amount"] = amount
                        meta["price"] = order_price
                        meta["force_exit"] = True
                        meta["fallback_to_market"] = True
                        return Decision(signal="sell", reason="contrarian:tp", meta=meta)

                # 타임스탑 체크
                if entry_ts > 0 and (now - entry_ts) >= max_hold_sec:
                    send_signal_telegram(
                        f"⏰ [CONTRARIAN] {market} Time Stop!\n"
                        f"• Held: {(now - entry_ts) / 60:.0f}분\n"
                        f"• Profit: {profit_pct:.2f}%"
                    )
                    self._reset_state(ctx, now)
                    # 주문 보정
                    market = getattr(ctx, "market", "BTCUSDT")
                    amount = meta.get("amount", Q.min_order)
                    order_price = price
                    amount, order_price = adjust_order_amount_and_price(amount, order_price, market)
                    meta["amount"] = amount
                    meta["price"] = order_price
                    meta["force_exit"] = True
                    meta["fallback_to_market"] = True
                    return Decision(signal="sell", reason="contrarian:timestop", meta=meta)
            else:
                meta["entry_missing"] = True
                return Decision(signal="hold", reason="contrarian:no_entry_price", meta=meta)

            return Decision(signal="hold", reason="contrarian:monitoring", meta=meta)

        # ============================================================
        # 포지션 없음: 매수 판단 (트레일링 상태 리셋)
        # ============================================================
        ctx.set_var("ct_trailing_active", False)
        ctx.set_var("ct_trail_peak", 0.0)
        ctx.set_var("ct_trail_stop", 0.0)

        # 쿨다운 체크
        cooldown_ts = float(ctx.get_var("contrarian_cooldown_ts", 0.0))
        if cooldown_ts > 0 and (now - cooldown_ts) < cooldown_sec:
            remaining = cooldown_sec - (now - cooldown_ts)
            meta["cooldown_remaining"] = remaining
            return Decision(signal="hold", reason="contrarian:cooldown", meta=meta)

        # 시그널 신선도 체크 — 스캔 후 시간이 너무 지나면 진입 차단
        signal_max_age_sec = float(params.get("signal_max_age_sec", 600.0))
        scanner_last_scan = float(getattr(scanner, "_last_scan", 0.0) or 0.0)
        signal_age_sec = (now - scanner_last_scan) if scanner_last_scan > 0 else float("inf")
        meta["signal_age_sec"] = round(signal_age_sec) if signal_age_sec != float("inf") else -1
        if signal_age_sec > signal_max_age_sec:
            return Decision(signal="hold", reason="contrarian:signal_stale", meta=meta)

        # 자본 확인
        capital = 0.0
        try:
            c = getattr(ctx, "usable_capital", None)
            if c is None:
                c = getattr(ctx, "allocated_capital", None)
            capital = float(c or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[CONTRARIAN] 자본 확인 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
            capital = 0.0

        min_order = float(params.get("min_order_usdt", Q.min_order))
        if capital < min_order:
            return Decision(signal="hold", reason="contrarian:insufficient_capital", meta=meta)

        # --------------------------------------------------------
        # [NEW] 유동성 필터 체크
        # --------------------------------------------------------
        volume_24h = float(meta.get("notional_quote_1m", 0.0)) * 60 * 24  # 추정
        if volume_24h < min_volume_usdt:
            # 실제 24h 거래대금이 없으면 스킵하지 않음 (데이터 없을 수 있음)
            pass  # 로그만 남기고 진행

        # --------------------------------------------------------
        # [NEW] 2단계 진입: RSI 필터
        # --------------------------------------------------------
        if rsi_filter_enabled and len(history) >= 14:
            rsi = indicators.rsi(history, 14)
            meta["rsi"] = rsi
            if rsi and rsi > rsi_max:
                meta["rsi_rejected"] = True
                return Decision(signal="hold", reason="contrarian:rsi_high", meta=meta)

        # --------------------------------------------------------
        # [2026-02-03] EMA 크로스 진입 필터 (추세 전환 확인)
        # --------------------------------------------------------
        if ema_cross_enabled and len(history) >= max(ema_fast, ema_slow):
            ema_f = indicators.ema(history, ema_fast)
            ema_s = indicators.ema(history, ema_slow)

            if ema_f and ema_s:
                is_golden_cross = ema_f > ema_s
                meta["ema_fast"] = ema_f
                meta["ema_slow"] = ema_s
                meta["ema_golden_cross"] = is_golden_cross

                if not is_golden_cross:
                    # 골든 크로스 없으면 진입 보류 (역행이어도 추세 전환 미확인)
                    return Decision(signal="hold", reason="contrarian:no_golden_cross", meta=meta)

        if bool(params.get("reversal_guard_enabled", True)):
            guard_min_score = float(params.get("reversal_guard_min_score", 2.0))
            if user_sell_only:
                guard_min_score = max(2.5, guard_min_score)
            guard_ok, guard_meta = _evaluate_reversal_buy_guard(
                history=history,
                price=float(price),
                strategy_tag="contrarian_longhold" if user_sell_only else "contrarian",
                rsi_value=float(meta.get("rsi")) if meta.get("rsi") is not None else None,
                rsi_low_static=min(float(rsi_max), 45.0),
                min_score=guard_min_score,
                require_macd_turn=bool(params.get("reversal_guard_require_macd_turn", False)),
                require_extreme_rsi=bool(params.get("reversal_guard_require_extreme_rsi", False)),
            )
            meta.update(guard_meta)
            if not guard_ok:
                return Decision(signal="hold", reason="contrarian:reversal_guard", meta=meta)

        # 역행 신호 있으면 매수
        if is_signal and candidate:
            is_early = bool(getattr(candidate, "early_signal", False))
            early_reasons = getattr(candidate, "early_reasons", [])
            benchmark_ret_pct = float(getattr(candidate, "benchmark_ret_pct", getattr(candidate, "btc_ret_pct", 0.0)) or 0.0)
            rs_text = f"{float(candidate.rs):.2f}" if (getattr(candidate, "rs", None) is not None) else "N/A"
            corr_text = f"{float(candidate.corr):.2f}" if (getattr(candidate, "corr", None) is not None) else "N/A"
            rsi_info = f"RSI: {meta.get('rsi', 'N/A'):.1f}" if meta.get('rsi') else ""

            if is_early:
                # 🔮 조기 감지 진입: 작은 포지션 + 타이트 SL
                rs_mom = getattr(candidate, "rs_momentum", 0.0)
                accel = getattr(candidate, "acceleration", 0.0)
                reasons_str = " + ".join(early_reasons) if early_reasons else "조기감지"
                send_signal_telegram(
                    f"🔮 [CONTRARIAN] {market} 조기 감지!\n"
                    f"• Score: {candidate.score}/3\n"
                    f"• Coin: {candidate.coin_ret_pct:+.2f}% vs BM: {benchmark_ret_pct:+.2f}%\n"
                    f"• RS 모멘텀: {rs_mom:+.2f} | 가속도: {accel:+.2f}\n"
                    f"• 감지 근거: {reasons_str}\n"
                    f"• {rsi_info}\n"
                    f"• TP: {tp_pct:.1f}% / SL: {sl_pct:.1f}%\n"
                    f"🔍 역행 시작 초기 포착!"
                )
                meta["entry_type"] = "early"
                meta["early_reasons_str"] = reasons_str
            else:
                send_signal_telegram(
                    f"🔄 [CONTRARIAN] {market} Buy Signal!\n"
                    f"• Score: {candidate.score}/3\n"
                    f"• Coin: {candidate.coin_ret_pct:+.2f}% vs Benchmark: {benchmark_ret_pct:+.2f}%\n"
                    f"• RS: {rs_text}\n"
                    f"• Corr: {corr_text}\n"
                    f"• {rsi_info}\n"
                    f"• TP: {tp_pct:.1f}% / SL: {sl_pct:.1f}%\n"
                    f"👉 시장 하락 속 역행 코인 발견!"
                )
                meta["entry_type"] = "confirmed"

            ctx.set_var("contrarian_entry_ts", now)
            # 주문 보정
            market = getattr(ctx, "market", "BTCUSDT")
            amount = meta.get("amount", Q.min_order)
            order_price = price
            amount, order_price = adjust_order_amount_and_price(amount, order_price, market)
            meta["amount"] = amount
            meta["price"] = order_price
            return Decision(signal="buy", reason="contrarian:early_signal" if is_early else "contrarian:signal", meta=meta)

        return Decision(signal="hold", reason="contrarian:wait", meta=meta)

    def _reset_state(self, ctx: Any, now: float) -> None:
        """트레일링 및 진입 상태 리셋"""
        ctx.set_var("contrarian_entry_ts", 0.0)
        ctx.set_var("contrarian_cooldown_ts", now)
        ctx.set_var("ct_trailing_active", False)
        ctx.set_var("ct_trail_peak", 0.0)
        ctx.set_var("ct_trail_stop", 0.0)
        _reset_dca_state(ctx, "ct")
