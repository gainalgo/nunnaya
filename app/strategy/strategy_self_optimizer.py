# ============================================================
# File: app/strategy/strategy_self_optimizer.py
# ------------------------------------------------------------
# StrategySelfOptimizer
# - A layer that improves long-term strategy quality based on signals after risk adjustment.
# - Currently lightweight, but structurally extensible.
# ============================================================

from __future__ import annotations
from typing import Dict, Any
import time

from .strategy_types import (
    StrategyPolicy,
    StrategySignal,
    StrategyBrainOutput,
)


class StrategySelfOptimizer:
    """
    Lightweight optimization module for long-term strategy tuning.
    For now it is a simple pass-through, but it is kept as a layer
    so it can be structurally extended.
    """

    # --------------------------------------------------------
    # Main optimization function
    # --------------------------------------------------------
    def refine(
        self,
        market: str,
        price: float,
        policy: StrategyPolicy,
        brain: StrategyBrainOutput,
        signal: StrategySignal,
        context: Any = None
    ) -> StrategySignal:
        """
        Apply win-rate-based automatic parameter tuning logic.
        """
        if context is None:
            return signal

        # Compute win rate
        wins = getattr(context, "win_count", 0)
        losses = getattr(context, "loss_count", 0)
        total = wins + losses

        # Only adjust once enough data has accumulated (minimum 5 trades)
        if total >= 5:
            win_rate = wins / total
            params = policy.get("params", {})

            # Case 1: win rate too low (< 30%) -> conservative entry, tighter stop loss
            if win_rate < 0.3:
                # Lower the RSI buy threshold so we only enter when more oversold
                current_rsi_buy = float(params.get("rsi_buy", 28.0))
                if current_rsi_buy > 20.0:
                    params["rsi_buy"] = current_rsi_buy - 0.05  # lower slowly

                # Tighten the stop loss (SL) a bit (e.g. -3.0 -> -2.9)
                current_sl = float(params.get("sl", -3.0))
                if current_sl < -1.0:
                    params["sl"] = min(-1.0, current_sl * 0.99)

            # Case 2: win rate very high (> 70%) -> aggressive operation
            elif win_rate > 0.7:
                # Raise the take profit (TP) target
                current_tp = float(params.get("tp", 1.2))
                if current_tp < 5.0:
                    params["tp"] = current_tp * 1.001

            # Case 3: learn the base bet size from the win rate (Learning)
            # Increase base size when win rate is good, decrease it when bad to manage risk
            current_scale = float(params.get("base_size_scale", 1.0))
            if win_rate > 0.6:
                # If win rate >= 60%, increase gradually (max 2.0x; engine still checks the capital cap)
                params["base_size_scale"] = min(2.0, current_scale * 1.02)
            elif win_rate < 0.4:
                # If win rate < 40%, decrease (minimum 0.2x)
                params["base_size_scale"] = max(0.2, current_scale * 0.98)

        # --------------------------------------------------------
        # Profit Trend Tuning (Recent 10 trades)
        # --------------------------------------------------------
        history = getattr(context, "trade_history", [])
        if len(history) >= 5:
            # Sum of returns over the most recent 10 trades (or fewer)
            recent = list(history)[-10:]
            profit_sum = sum(item[2] for item in recent)  # item[2] is profit_pct

            # Trend very good (cumulative return > 5%) -> narrow DCA/pyramiding spacing (aggressive)
            if profit_sum > 5.0:
                step_pct = float(params.get("step_pct", 1.0))
                if step_pct > 0.5:
                    params["step_pct"] = step_pct * 0.99

            # Trend bad (cumulative return < -2%) -> lower take profit (TP) to encourage a quick exit
            elif profit_sum < -2.0:
                tp = float(params.get("tp", 1.0))
                if tp > 0.5:
                    params["tp"] = tp * 0.99

            # If the profit trend is good, give the bet size an extra boost
            if profit_sum > 10.0:
                bs = float(params.get("base_size_scale", 1.0))
                params["base_size_scale"] = min(2.5, bs * 1.05)

            # Trend very bad (cumulative return < -5%) -> cooldown (pause trading temporarily)
            if profit_sum < -5.0:
                last_trade_ts = recent[-1][0]
                now = time.time()
                # If within 1 hour of the last trade, apply a cooldown (prevent consecutive losses)
                if (now - last_trade_ts) < 3600:
                    current_block = float(getattr(context, "entry_block_until_ts", 0.0) or 0.0)
                    if now > current_block:
                        # Block entries for 30 minutes
                        context.entry_block_until_ts = now + 1800.0
                        context.entry_block_reason = f"optimizer:bad_trend({profit_sum:.1f}%)"

        # --------------------------------------------------------
        # Enforce Cooldown
        # --------------------------------------------------------
        now = time.time()
        block_until = float(getattr(context, "entry_block_until_ts", 0.0) or 0.0)
        if now < block_until and signal.signal == "buy":
            return StrategySignal("hold")

        return signal
