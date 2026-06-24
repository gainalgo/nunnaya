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
    Central Decision Maker

    Role:
    - Market state (Context) -> compute strategy scores
    - Scores are accumulated via EMA
    - Bias is stabilized with hysteresis
    """

    def __init__(self):
        self.alpha = 0.2               # EMA smoothing factor
        self.min_hold_seconds = 30     # minimum hold time
        self.switch_margin = 5.0       # strategy switch threshold difference

    # --------------------------------------------------
    def select(self, ctx: HyperEngineContext) -> SelectionResult:
        scores, features = self._score_all(ctx)

        # EMA accumulation
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

        # hysteresis
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
        # - If abnormal values (0, negative, NaN/inf) are mixed into
        #   price_buffer/history, a ZeroDivisionError can occur in the
        #   change_pct calculation.
        # - This is actually a major cause of TICK_LOOP_FATAL "float division by zero".
        # - We perform a first line of defense (filtering) here so the TickLoop does not die.

        # Use only the most recent segment so we don't scan an overly long history every tick
        raw_prices = list(ctx.price_buffer)[-200:]

        prices: list[float] = []
        for p in raw_prices:
            try:
                fp = float(p)
            except (TypeError, ValueError) as exc:
                logger.warning("[strategy_selector] %s: %s", 'price parse failed (non-numeric value) except-> continue', exc, exc_info=True)
                continue
            if (not math.isfinite(fp)) or fp <= 0.0:
                continue
            prices.append(fp)

        n = len(prices)

        if n < 20:
            return {s: 0.0 for s in STRATEGIES}, {"note": "insufficient data"}

        last = prices[-1]
        p0 = prices[0]

        # If p0 is 0 -> division-by-zero; already filtered above, but guard once more to be safe
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
        # If recent trades have many losses, penalize trend-following (breakout) strategy
        # scores to steer toward range/counter-trend strategies (PingPong) or standing aside.
        history = getattr(ctx, "trade_history", [])
        recent_losses = 0
        if history:
            # Check the last 5 trades
            for _, profit, _ in list(history)[-5:]:
                if profit < 0:
                    recent_losses += 1

        if recent_losses >= 3:
            # If 3+ of the last 5 are losses, heavily cut Lightning/Gazua scores (50% penalty)
            scores["lightning"] *= 0.5
            scores["gazua"] *= 0.5

        # --------------------------------------------------------
        # Squeeze Expansion Detection (Gazua Boost)
        # --------------------------------------------------------
        # When a Bollinger Band squeeze resolves (Expansion) and breaks upward, strongly recommend the Gazua strategy.
        sq_res = indicators.bollinger_squeeze(prices, length=20, k=2.0, lookback=20)
        is_squeeze = False
        if sq_res:
            _, is_squeeze = sq_res

        was_squeeze = bool(ctx.get_var("selector_was_squeeze", False))
        squeeze_expansion = None

        if was_squeeze and not is_squeeze:
            # Squeeze resolved (Expansion)
            if mom > 0:
                scores["gazua"] += 50.0  # treated as a strong bullish trend signal
                squeeze_expansion = "bull"
                
                # ----------------------------------------------------
                # Notification Trigger
                # ----------------------------------------------------
                # Add message to the Telegram notification queue
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
