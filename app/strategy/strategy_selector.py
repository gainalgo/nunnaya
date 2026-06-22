# ============================================================
# File: app/strategy/strategy_selector.py
# Autocoin OS v3-H — Strategy Selector
# (EMA Smoothing + Hysteresis)
# ============================================================

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Dict, Any, Tuple
import math
import time

from app.engine.hyper_engine_context import HyperEngineContext
from app.strategy import indicators
logger = logging.getLogger(__name__)

STRATEGIES = ("autocoin", "pingpong", "ladder", "lightning", "gazua")

@dataclass
class SelectionResult:
    chosen: str
    ema_scores: Dict[str, float]
    confidence: float
    reason: Dict[str, Any]

class StrategySelector:
    """
    중앙 판단자 (Central Decision Maker)

    역할:
    - 시장 상태(Context) → 전략 점수 계산
    - 점수는 EMA로 누적
    - Bias는 히스테리시스로 안정화
    """

    def __init__(self):
        self.alpha = 0.2               # EMA smoothing factor
        self.min_hold_seconds = 30     # 최소 유지 시간
        self.switch_margin = 5.0       # 전략 전환 임계 차이

    # --------------------------------------------------
    def select(self, ctx: HyperEngineContext) -> SelectionResult:
        scores, features = self._score_all(ctx)

        # EMA 누적
        ctx.update_ema(scores, self.alpha)

        ranked = sorted(
            ctx.ema_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )

        best, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        confidence = best_score - second_score

        now = time.time()

        # 히스테리시스
        if ctx.bias is None:
            ctx.bias = best
            ctx.bias_ts = now
        else:
            held = now - (ctx.bias_ts or now)
            if best != ctx.bias:
                if held >= self.min_hold_seconds and confidence >= self.switch_margin:
                    ctx.bias = best
                    ctx.bias_ts = now

        ctx.confidence = round(confidence, 2)
        ctx.selection_scores = ctx.ema_scores.copy()
        ctx.selection_reason = {
            "features": features,
            "alpha": self.alpha,
            "min_hold_sec": self.min_hold_seconds,
            "switch_margin": self.switch_margin,
        }

        return SelectionResult(
            chosen=ctx.bias,
            ema_scores=ctx.ema_scores.copy(),
            confidence=ctx.confidence,
            reason=ctx.selection_reason,
        )

    # --------------------------------------------------
    def _score_all(self, ctx: HyperEngineContext) -> Tuple[Dict[str, float], Dict[str, Any]]:
        # NOTE:
        # - price_buffer/history 에 비정상(0, 음수, NaN/inf) 값이 섞이면
        #   change_pct 계산에서 ZeroDivisionError가 발생할 수 있다.
        # - 실제로 TICK_LOOP_FATAL "float division by zero" 의 주요 원인.
        # - 여기서 1차 방어(필터링)를 수행하여 TickLoop가 죽지 않도록 한다.

        # 너무 긴 히스토리를 매 tick마다 스캔하지 않도록, 최근 구간만 사용
        raw_prices = list(ctx.price_buffer)[-200:]

        prices: list[float] = []
        for p in raw_prices:
            try:
                fp = float(p)
            except (TypeError, ValueError) as exc:
                logger.warning("[strategy_selector] %s: %s", '너무 긴 히스토리를 매 tick마다 스캔하지 않도록, 최근 구간만 사용 except-> continue', exc, exc_info=True)
                continue
            if (not math.isfinite(fp)) or fp <= 0.0:
                continue
            prices.append(fp)

        n = len(prices)

        if n < 20:
            return {s: 0.0 for s in STRATEGIES}, {"note": "insufficient data"}

        last = prices[-1]
        p0 = prices[0]

        # p0가 0이면 division-by-zero; 위에서 필터링했지만 안전하게 한 번 더 방어
        if p0 <= 0.0:
            return {s: 0.0 for s in STRATEGIES}, {"note": "invalid base price (p0<=0)"}

        change_pct = ((last - p0) / p0) * 100.0

        avg = sum(prices) / n
        var = sum((p - avg) ** 2 for p in prices) / n
        vol = var ** 0.5

        mom = prices[-1] - prices[-2]
        window = prices[-50:] if n >= 50 else prices
        rng = max(window) - min(window)

        scores = {s: 0.0 for s in STRATEGIES}
        scores["autocoin"] += 10.0

        # PingPong: range-bound
        scores["pingpong"] += 20.0 + max(0.0, 15.0 - abs(change_pct))

        # Ladder: gentle trend
        scores["ladder"] += min(25.0, abs(change_pct) * 2.0)

        # Lightning: volatility spike
        scores["lightning"] += min(30.0, (vol / max(1.0, avg)) * 1000.0)
        scores["lightning"] += min(20.0, abs(mom) / max(1.0, avg) * 2000.0)

        # Gazua: strong bullish trend
        if change_pct > 0:
            scores["gazua"] += min(40.0, change_pct * 2.5)
        if mom > 0:
            scores["gazua"] += 10.0
        if change_pct < 0:
            scores["gazua"] -= 15.0

        # --------------------------------------------------------
        # [PATCH] Performance-based Penalty (Anti-Whipsaw)
        # --------------------------------------------------------
        # 최근 거래에서 손실이 많다면, 추세 추종형(돌파) 전략의 점수를 깎아서
        # 횡보/역추세 전략(PingPong)이나 관망으로 유도한다.
        history = getattr(ctx, "trade_history", [])
        recent_losses = 0
        if history:
            # 최근 5회 거래 확인
            for _, profit, _ in list(history)[-5:]:
                if profit < 0:
                    recent_losses += 1
        
        if recent_losses >= 3:
            # 최근 5번 중 3번 이상 손실이면 Lightning/Gazua 점수 대폭 차감 (50% 페널티)
            scores["lightning"] *= 0.5
            scores["gazua"] *= 0.5

        # --------------------------------------------------------
        # Squeeze Expansion Detection (Gazua Boost)
        # --------------------------------------------------------
        # 볼린저 밴드 스퀴즈가 해소되면서(Expansion) 상방으로 튀면 Gazua 전략을 강력 추천한다.
        sq_res = indicators.bollinger_squeeze(prices, length=20, k=2.0, lookback=20)
        is_squeeze = False
        if sq_res:
            _, is_squeeze = sq_res

        was_squeeze = bool(ctx.get_var("selector_was_squeeze", False))
        squeeze_expansion = None

        if was_squeeze and not is_squeeze:
            # Squeeze 해소 (Expansion)
            if mom > 0:
                scores["gazua"] += 50.0  # 강력한 매수 추세 신호로 간주
                squeeze_expansion = "bull"
                
                # ----------------------------------------------------
                # Notification Trigger
                # ----------------------------------------------------
                # 텔레그램 알림 큐에 메시지 추가
                msg = f"🚀 [Squeeze Breakout] {ctx.market} Volatility Expansion Detected! (Mom: {mom:.2f}%)"
                ctx.notifications.append({"ts": time.time(), "level": "INFO", "message": msg})

        ctx.set_var("selector_was_squeeze", is_squeeze)

        features = {
            "change_pct": round(change_pct, 4),
            "vol": round(vol, 4),
            "momentum": round(mom, 4),
            "range": round(rng, 4),
            "n": n,
            "squeeze_expansion": squeeze_expansion,
        }

        return scores, features
