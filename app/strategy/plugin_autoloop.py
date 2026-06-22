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
    _inject_candle_1m_telemetry,
    _reset_dca_state,
    _restore_longhold_flag_from_config,
    _try_convert_to_longhold,
    _unregister_longhold,
)

logger = logging.getLogger(__name__)


class AutoloopPlugin(StrategyPlugin):
    """오토로푸 전략 플러그인.

    핵심 지표는 RSI + MACD 이며, 실제 계산/부트스트랩 로직은
    app.engine.autoloop_strategy 에서 처리한다.
    """

    name: str = "autoloop"

    def decide(self, ctx: Any, price: float) -> Decision:
        params: Dict[str, Any] = {}
        try:
            ctrls = getattr(ctx, "controls", None) or {}
            if isinstance(ctrls, dict):
                params = dict((ctrls.get("strategy") or {}).get("params") or {})
        except (KeyError, AttributeError, TypeError):
            logger.warning("[%s] params 추출 실패 → 기본값 사용: %s", self.name if hasattr(self, 'name') else '?', getattr(ctx, 'market', '?'), exc_info=True)
            params = {}

        # --------------------------------------------------------
        # AI-Driven Dynamic Tuning (Autoloop) - 2026-01-30 v2
        # 전략별 AI 임계값 + Regime 적합도 기반 동적 조절
        # --------------------------------------------------------
        ai_score = 0.5
        regime = "UNKNOWN"
        if hasattr(ctx, "current_ai") and isinstance(ctx.current_ai, dict):
            brain = ctx.current_ai.get("brain", {})
            ai_score = float(brain.get("ai_prediction", 0.5))
            regime = str(brain.get("regime", "UNKNOWN")).upper()

        # 전략별 AI 조정
        ai_adjustment = adjust_ai_score_for_strategy(ai_score, strategy="autoloop", regime=regime)
        tp_scale = ai_adjustment["tp_scale"]
        sl_scale = ai_adjustment["sl_scale"]

        # tp_sl_mode: "auto" (AI/ATR 동적 조정 허용) | "manual" (사용자 고정값 잠금)
        tp_sl_mode = str(params.get("tp_sl_mode", "auto")).strip().lower()
        is_manual = tp_sl_mode == "manual"

        ai_influence = float(params.get("ai_influence", 0.15))
        if ai_influence > 0:
            factor = (ai_score - 0.5) * ai_influence
            shift = factor * 20.0
            params["rsi_buy"] = max(10.0, min(60.0, float(params.get("rsi_buy", 28.0)) + shift))
            params["rsi_sell"] = max(40.0, min(90.0, float(params.get("rsi_sell", 58.0)) + shift))
            # TP/SL 스케일 적용 (manual 모드면 건너뜀)
            if not is_manual:
                base_tp = float(params.get("tp_pct", 2.5))
                params["tp_pct"] = max(1.2, base_tp * tp_scale)
                base_sl = float(params.get("sl_pct", -2.5))
                params["sl_pct"] = min(-2.5, base_sl * sl_scale)

        # ATR Dynamic TP/SL
        history = getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or [])
        meta_atr: Dict[str, Any] = {}
        _apply_atr_dynamic_limits(ctx, params, float(price), history, meta_atr, "autoloop")

        # ── Online Calibration Overlay (Phase 3-A) ──
        if _get_calibrator and not is_manual:
            try:
                _cal = _get_calibrator()
                _cal_regime = _detect_regime(history)
                _cal_atr = indicators.atr_simplified(history)
                _cal_atr_pct = (_cal_atr / price * 100.0) if _cal_atr and price > 0 else 2.0
                _cal_adj = _cal.get_adjustments(_cal.classify_bucket(_cal_atr_pct, _cal_regime), "AUTOLOOP")
                if _cal_adj:
                    rsi_shift = _cal_adj.get("al_rsi_shift", 0.0)
                    params["rsi_buy"] = max(10.0, min(60.0, float(params.get("rsi_buy", 28.0)) + rsi_shift))
                    params["rsi_sell"] = max(40.0, min(90.0, float(params.get("rsi_sell", 58.0)) + rsi_shift))
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[AUTOLOOP_PLUGIN] Online Calibration Overlay: %s", exc, exc_info=True)

        # ── 2-A: 포지션 체크 ──
        pos_al = getattr(ctx, "position", None)
        has_pos_al = bool(pos_al and float((pos_al.get("qty") if isinstance(pos_al, dict) else getattr(pos_al, "qty", 0)) or 0) > 0)

        # ── LongHold: 서버 재시작 시 config에서 플래그 복원 ──
        if has_pos_al:
            _restore_longhold_flag_from_config(ctx)
        # ── LongHold 전환 완료 → 회복 체크 후 hold 유지 ──
        if has_pos_al and ctx.get_var("longhold_converted", False):
            if not _check_longhold_recovery(ctx, pos_al, price, "AUTOLOOP"):
                return Decision(signal="hold", reason="autoloop:longhold_active",
                                meta={"longhold": True, "longhold_ts": ctx.get_var("longhold_convert_ts", 0)})
        if not has_pos_al and ctx.get_var("longhold_converted", False):
            ctx.set_var("longhold_converted", False)
            _unregister_longhold(getattr(ctx, "market", ""))

        # ── [2026-03-09] selector 신뢰 즉시매수 ──
        # [2026-03-30] 급락 방어: selector 즉시매수 전 최소 안전장치
        if not has_pos_al:
            _safe_to_enter = True
            _block_reason = ""
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
                    logger.warning("[AUTOLOOP_PLUGIN] selector entry knife guard: %s", exc, exc_info=True)
            if _safe_to_enter:
                try:
                    _rsi = getattr(ctx, "rsi", None) or ctx.get_var("rsi_14", None)
                    if _rsi is not None and float(_rsi) < 15.0:
                        _safe_to_enter = False
                        _block_reason = f"rsi_extreme:{float(_rsi):.0f}"
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[AUTOLOOP_PLUGIN] selector entry RSI guard: %s", exc, exc_info=True)
            if not _safe_to_enter:
                return Decision(signal="hold", reason=f"autoloop:selector_entry_blocked:{_block_reason}",
                                meta={"selector_blocked": True, "block_reason": _block_reason})
            # GreenPen PA 확인
            if bool(params.get("greenpen_enabled", False)):
                from app.strategy.greenpen import check_entry_guard
                _gp = check_entry_guard("AUTOLOOP", getattr(ctx, "_tick_prices", None) or list(getattr(ctx, "price_history", []) or []), price)
                if not _gp["allow"]:
                    return Decision(signal="hold", reason=f"autoloop:gp_{_gp['reason']}", meta={"gp": _gp})
            full_meta = {"selector_fast_entry": True}
            full_meta.update(meta_atr)
            return Decision(signal="buy", reason="autoloop:selector_entry", meta=full_meta)

        # Local import to avoid early import/circular risks
        from app.engine.autoloop_strategy import decide_detail as al_decide_detail

        out = al_decide_detail(ctx, float(price), params)
        sig_raw = str(out.get("signal") or "hold")
        # Signal validation: only allow 'buy', 'sell', 'hold'
        if sig_raw not in ("buy", "sell", "hold"):
            meta = dict(out.get("meta") or {})
            meta.update(meta_atr)
            meta["signal_warning"] = f"Invalid signal '{sig_raw}' from al_decide_detail, forced to 'hold'"
            sig: Signal = "hold"
        else:
            sig: Signal = sig_raw
        reason = str(out.get("reason") or "autoloop")
        meta = dict(out.get("meta") or {})
        meta.update(meta_atr)

        # --------------------------------------------------------
        # Sell lock (pairing) — never move sell line down
        # --------------------------------------------------------
        # entry는 SL/DCA 계산에서도 사용되므로 lock 여부와 무관하게 초기화
        entry = qty = high = 0.0
        lock_mode = str(params.get("sell_lock_mode", "TRAIL_UP") or "TRAIL_UP").upper()
        lock_enabled = lock_mode not in ("OFF", "DISABLED", "NONE", "0", "FALSE")
        if lock_enabled:
            pos = getattr(ctx, "position", None)
            try:
                if isinstance(pos, dict):
                    entry = float(pos.get("entry") or pos.get("entry_price") or pos.get("avg_price") or pos.get("price") or 0.0)
                    qty = float(pos.get("qty") or pos.get("volume") or pos.get("balance") or 0.0)
                    high = float(pos.get("high_price") or pos.get("peak_price") or 0.0)
                else:
                    entry = float(
                        getattr(pos, "entry", None)
                        or getattr(pos, "entry_price", None)
                        or getattr(pos, "avg_price", None)
                        or getattr(pos, "price", None)
                        or 0.0
                    )
                    qty = float(
                        getattr(pos, "qty", None)
                        or getattr(pos, "volume", None)
                        or getattr(pos, "balance", None)
                        or 0.0
                    )
                    high = float(
                        getattr(pos, "high_price", None)
                        or getattr(pos, "peak_price", None)
                        or 0.0
                    )
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[AUTOLOOP] position 파싱 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
                entry = qty = high = 0.0

            if entry > 0 and qty > 0:
                try:
                    lock_entry = float(ctx.get_var("al_locked_entry", 0.0) or 0.0)
                except (TypeError, ValueError):
                    logger.warning("[AUTOLOOP] lock_entry 파싱 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
                    lock_entry = 0.0
                try:
                    lock_price = float(ctx.get_var("al_locked_sell_price", 0.0) or 0.0)
                except (TypeError, ValueError):
                    logger.warning("[AUTOLOOP] lock_price 파싱 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
                    lock_price = 0.0

                if lock_entry != entry:
                    # 진입가 변경 시 lock_entry와 lock_price를 원자적으로 초기화
                    lock_entry = entry
                    lock_price = 0.0
                    try:
                        ctx.set_var("al_locked_entry", float(lock_entry))
                        ctx.set_var("al_locked_sell_price", 0.0)
                    except (TypeError, ValueError):
                        logger.warning("[AUTOLOOP] lock 원자적 초기화 실패: %s — TP 계산 과거 가격 사용 위험", getattr(ctx, "market", "?"))

                try:
                    tp_pct = float(params.get("tp_pct", params.get("tp", 2.5)) or 2.5)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[AUTOLOOP] tp_pct 파싱 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
                    tp_pct = 2.5
                if tp_pct <= 0:
                    tp_pct = 0.5
                base_target = entry * (1.0 + abs(tp_pct) / 100.0)
                if base_target > lock_price:
                    lock_price = base_target

                try:
                    prev_high = float(ctx.get_var("al_high_since_entry", 0.0) or 0.0)
                except (TypeError, ValueError):
                    logger.warning("[AUTOLOOP] prev_high 파싱 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
                    prev_high = 0.0
                if high <= 0:
                    high = prev_high
                if price > high:
                    high = float(price)
                try:
                    ctx.set_var("al_high_since_entry", float(high))
                except (TypeError, ValueError):
                    logger.warning("[AUTOLOOP] 고점 추적 실패: %s — trailing TP 계산 오류 위험", getattr(ctx, "market", "?"))

                try:
                    from app.monitor.time_volatility_adjuster import get_time_volatility_multiplier
                    time_mult = get_time_volatility_multiplier()
                except (ImportError, AttributeError, TypeError):
                    logger.warning("[AUTOLOOP] time_volatility_multiplier 가져오기 실패", exc_info=True)
                    time_mult = 1.0
                try:
                    trail_base = float(params.get("trailing_pct", 1.2) or 1.2)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[AUTOLOOP] trail_base 파싱 실패: %s", getattr(ctx, "market", "?"), exc_info=True)
                    trail_base = 1.2
                # ── 2-B: Range Guard — 횡보 구간에서 trailing을 넓혀 휩쏘 방지 ──
                if not is_manual and regime == "RANGE":
                    trail_base = trail_base * 1.5  # 횡보 시 trailing 50% 확대
                trail_pct = trail_base * time_mult
                if high > 0 and trail_pct > 0:
                    trail_price = high * (1.0 - trail_pct / 100.0)
                    if trail_price > lock_price:
                        lock_price = trail_price

                meta["sell_lock_price"] = lock_price
                try:
                    ctx.set_var("al_locked_entry", float(lock_entry))
                    ctx.set_var("al_locked_sell_price", float(lock_price))
                except (TypeError, ValueError) as exc:
                    logger.warning("[AUTOLOOP_PLUGIN] sell lock persist: %s", exc, exc_info=True)

                _al_sl_pct_raw = float(params.get("sl_pct", -2.5) or -2.5)
                if _al_sl_pct_raw > 0:
                    _al_sl_pct_raw = -_al_sl_pct_raw
                _al_sl_thresh = entry * (1.0 + _al_sl_pct_raw / 100.0) if entry > 0 else 0.0
                if sig == "sell" and price < lock_price and (_al_sl_thresh <= 0 or price > _al_sl_thresh):
                    sig = "hold"
                    reason = "autoloop:locked_sell_hold"
            else:
                try:
                    ctx.set_var("al_locked_entry", 0.0)
                    ctx.set_var("al_locked_sell_price", 0.0)
                    ctx.set_var("al_high_since_entry", 0.0)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[AUTOLOOP_PLUGIN] sell lock reset: %s", exc, exc_info=True)

        # lock OFF이어도 SL/DCA에서 entry가 필요하므로 추출
        if not lock_enabled and entry <= 0 and has_pos_al:
            try:
                _pos = getattr(ctx, "position", None)
                if isinstance(_pos, dict):
                    entry = float(_pos.get("entry") or _pos.get("entry_price") or _pos.get("avg_price") or 0.0)
                elif _pos is not None:
                    entry = float(getattr(_pos, "entry", 0) or getattr(_pos, "avg_price", 0) or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[AUTOLOOP] entry 추출 실패 (lock OFF): %s", getattr(ctx, "market", "?"), exc_info=True)
                entry = 0.0

        # Inject AI score + regime into telemetry
        meta["regime"] = regime
        if "telemetry" in meta and isinstance(meta["telemetry"], dict):
            meta["telemetry"]["ai_score"] = ai_score

            _inject_candle_1m_telemetry(ctx, meta["telemetry"])

        # ── AUTOLOOP SL 확인 (2틱 연속 — 노이즈 방어) + DCA ──
        _al_sl_pct_for_dca = float(params.get("sl_pct", -2.5) or -2.5)
        if _al_sl_pct_for_dca > 0:
            _al_sl_pct_for_dca = -_al_sl_pct_for_dca
        _al_sl_thresh_dca = entry * (1.0 + _al_sl_pct_for_dca / 100.0) if entry > 0 else 0.0
        if has_pos_al and sig == "sell" and entry > 0 and price <= _al_sl_thresh_dca:
            _al_sl_confirm_need = int(params.get("sl_confirm_ticks", 2))
            _al_sl_streak = int(ctx.get_var("al_sl_streak", 0)) + 1
            ctx.set_var("al_sl_streak", _al_sl_streak)
            meta["sl_streak"] = _al_sl_streak
            meta["sl_confirm_need"] = _al_sl_confirm_need
            if _al_sl_streak < _al_sl_confirm_need:
                return Decision(signal="hold", reason="autoloop:sl_confirming", meta=meta)
            ctx.set_var("al_sl_streak", 0)
            # DCA 물타기 먼저 시도
            dca_result = _common_dca_check(ctx, price, entry, params, "al", meta)
            if dca_result is not None:
                return dca_result
            # ── DCA 불가 → SL → LongHold 전환 시도 ──
            _lh_market = getattr(ctx, "market", "")
            _lh_result = _try_convert_to_longhold(ctx, _lh_market, "AUTOLOOP", entry, price, meta)
            if _lh_result is not None:
                return _lh_result
        elif has_pos_al:
            ctx.set_var("al_sl_streak", 0)
        if not has_pos_al:
            _reset_dca_state(ctx, "al")

        # 주문 보정 (매수/매도 시에만)
        if sig in ("buy", "sell"):
            market = getattr(ctx, "market", "BTCUSDT")
            amount = meta.get("amount", Q.min_order)
            order_price = price
            amount, order_price = adjust_order_amount_and_price(amount, order_price, market)
            meta["amount"] = amount
            meta["price"] = order_price
        return Decision(signal=sig, reason=reason, meta=meta)
