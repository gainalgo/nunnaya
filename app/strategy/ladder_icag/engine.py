# ============================================================
# ICAG Engine — Core tick logic
#   anchor → zone → bias → order targets
# ============================================================
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import ICAGConfig
from .state import ICAGMarketState, ICAGPortfolioState
from .atr import get_market_atr, get_market_vwap, get_current_price

logger = logging.getLogger(__name__)


class ICAGEngine:

    def __init__(self, cfg: Optional[ICAGConfig] = None):
        self.cfg = cfg or ICAGConfig()

    # ============================================================
    # 1) ANCHOR
    # ============================================================
    def compute_anchor(
        self,
        state: ICAGMarketState,
        price: float,
    ) -> float:
        """Compute the anchor price for a market (dual-speed EMA)."""
        cfg = self.cfg

        # --- raw anchor ---
        if state.position_qty > 0 and state.position_avg_price > 0:
            vwap = state.vwap if state.vwap > 0 else price
            raw = cfg.anchor_avg_weight * state.position_avg_price + cfg.anchor_vwap_weight * vwap
        else:
            raw = state.vwap if state.vwap > 0 else price

        state.anchor_raw = raw

        # --- EMA smoothing (dual-speed) ---
        old_anchor = state.anchor_price
        if old_anchor <= 0:
            state.anchor_price = raw
            return raw

        deviation_pct = abs(price - old_anchor) / old_anchor * 100.0

        if deviation_pct > cfg.anchor_fast_trigger_pct:
            # fast mode: flash crash / pump
            alpha = min(cfg.anchor_fast_alpha_max, deviation_pct / 100.0 * 4.0)
        else:
            alpha = cfg.anchor_ema_alpha

        new_anchor = old_anchor * (1.0 - alpha) + raw * alpha

        # --- jump limiter ---
        max_jump = old_anchor * (cfg.anchor_max_jump_pct / 100.0)
        if abs(new_anchor - old_anchor) > max_jump:
            direction = 1.0 if new_anchor > old_anchor else -1.0
            new_anchor = old_anchor + direction * max_jump

        state.anchor_price = new_anchor
        return new_anchor

    # ============================================================
    # 2) ZONE
    # ============================================================
    def compute_zone(
        self,
        state: ICAGMarketState,
        price: float,
    ) -> str:
        """Determine current zone: CORE / EXPANSION / RISK_CUT."""
        cfg = self.cfg
        anchor = state.anchor_price
        atr = state.atr

        if anchor <= 0 or atr <= 0:
            return "CORE"

        distance = abs(price - anchor)
        core_width = atr * cfg.core_width_atr
        expansion_width = atr * cfg.expansion_width_atr

        # hard cut band
        cut_distance = anchor * (cfg.cut_pct / 100.0)

        if distance > expansion_width or distance > cut_distance:
            zone = "RISK_CUT"
        elif distance > core_width:
            zone = "EXPANSION"
        else:
            zone = "CORE"

        state.zone = zone
        return zone

    # ============================================================
    # 3) INVENTORY RATIO & BIAS
    # ============================================================
    def compute_bias(
        self,
        state: ICAGMarketState,
        price: float,
        portfolio: Optional[ICAGPortfolioState] = None,
    ) -> str:
        """Compute BUY / SELL / BALANCED bias with hysteresis."""
        cfg = self.cfg
        now = time.time()

        # --- inventory ratio ---
        if state.budget_allocated > 0 and price > 0:
            max_qty = state.budget_allocated / state.anchor_price if state.anchor_price > 0 else state.budget_allocated / price
            inv_ratio = state.position_qty / max_qty if max_qty > 0 else 0.0
        else:
            inv_ratio = 0.0
        inv_ratio = max(0.0, min(1.0, inv_ratio))
        state.inv_ratio = inv_ratio

        # --- dynamic bias bands ---
        vol_factor = 0.0
        if price > 0 and state.atr > 0:
            vol_factor = max(0.005, min(0.03, state.atr / price))

        buy_zone_upper = 0.35 - vol_factor
        sell_zone_lower = 0.65 + vol_factor

        # --- position aging adjustment ---
        time_bias = self._aging_bias(state)
        sell_zone_lower -= time_bias  # lower threshold = easier to enter SELL bias

        # --- hysteresis ---
        hyst = cfg.bias_hysteresis
        old_bias = state.bias
        cooldown_ok = (now - state.bias_last_change_ts) >= cfg.bias_cooldown_sec

        new_bias = old_bias

        # hard caps first
        if inv_ratio >= cfg.inventory_cap_ratio:
            new_bias = "SELL"
        elif state.zone == "RISK_CUT":
            new_bias = "SELL" if state.position_qty > 0 else "BALANCED"
        elif price > state.anchor_price * (1 + cfg.buy_disable_pct / 100.0):
            # above anchor + buy_disable → no buying
            if inv_ratio > sell_zone_lower:
                new_bias = "SELL"
            else:
                new_bias = "BALANCED"
        elif cooldown_ok:
            if old_bias == "BALANCED":
                if inv_ratio < buy_zone_upper - hyst:
                    new_bias = "BUY"
                elif inv_ratio > sell_zone_lower + hyst:
                    new_bias = "SELL"
            elif old_bias == "BUY":
                if inv_ratio > buy_zone_upper + hyst:
                    new_bias = "BALANCED"
                if inv_ratio > sell_zone_lower + hyst:
                    new_bias = "SELL"
            elif old_bias == "SELL":
                if inv_ratio < sell_zone_lower - hyst:
                    new_bias = "BALANCED"
                if inv_ratio < buy_zone_upper - hyst:
                    new_bias = "BUY"

        # --- portfolio-level override ---
        if portfolio and new_bias == "BUY":
            if portfolio.global_buy_throttle <= 0:
                new_bias = "BALANCED"

        # --- budget utilization check ---
        if state.budget_allocated > 0:
            util = state.budget_used / state.budget_allocated
            if util >= cfg.budget_max_utilization and new_bias == "BUY":
                new_bias = "BALANCED"

        if new_bias != old_bias:
            state.bias_last_change_ts = now
        state.bias = new_bias
        return new_bias

    # ============================================================
    # 4) UNDERWATER MODE
    # ============================================================
    def compute_underwater_mode(
        self,
        state: ICAGMarketState,
        price: float,
    ) -> str:
        """Determine underwater recovery mode."""
        cfg = self.cfg

        if state.position_qty <= 0 or state.anchor_price <= 0:
            state.underwater_mode = "NORMAL"
            return "NORMAL"

        underwater_pct = (state.anchor_price - price) / state.anchor_price * 100.0

        if underwater_pct < cfg.defensive_pct:
            mode = "NORMAL"
        elif underwater_pct < cfg.dca_rescue_pct:
            mode = "DEFENSIVE"
        elif underwater_pct < cfg.capitulation_pct:
            mode = "DCA_RESCUE"
        else:
            mode = "CAPITULATION"

        state.underwater_mode = mode
        return mode

    # ============================================================
    # 5) STEP SIZE
    # ============================================================
    def compute_step(
        self,
        state: ICAGMarketState,
        price: float,
        zone: str,
        order_usdt: float = 0.0,
    ) -> float:
        """Compute adaptive step size in price units."""
        cfg = self.cfg
        atr = state.atr

        if atr <= 0 or price <= 0:
            return price * cfg.min_step_pct / 100.0

        # base step from ATR
        atr_pct = atr / price
        step_pct = max(cfg.min_step_pct / 100.0, atr_pct * cfg.base_k)

        # zone multiplier
        if zone == "CORE":
            step_pct *= cfg.core_k_mult
        elif zone == "EXPANSION":
            step_pct *= cfg.expansion_k_mult
        elif zone == "RISK_CUT":
            step_pct *= cfg.risk_cut_k_mult

        # fee-aware floor: round-trip must be profitable
        min_profit_pct = (cfg.fee_rate * 2 + cfg.slippage_buffer)
        if order_usdt > 0 and cfg.min_profit_usdt > 0:
            min_profit_pct = max(min_profit_pct, cfg.min_profit_usdt / order_usdt)
        step_pct = max(step_pct, min_profit_pct)

        step_abs = price * step_pct
        return step_abs

    # ============================================================
    # 6) ORDER TARGETS
    # ============================================================
    def generate_targets(
        self,
        state: ICAGMarketState,
        price: float,
        order_usdt: float,
        portfolio: Optional[ICAGPortfolioState] = None,
    ) -> Dict[str, Any]:
        """Generate buy/sell target prices and quantities.

        Returns dict with:
            buy_targets:  [(price, qty), ...]
            sell_targets: [(price, qty), ...]
            diagnostics:  {zone, bias, anchor, step, ...}
        """
        cfg = self.cfg
        anchor = state.anchor_price
        zone = state.zone
        bias = state.bias
        step = self.compute_step(state, price, zone, order_usdt)

        if step <= 0 or anchor <= 0:
            return {"buy_targets": [], "sell_targets": [], "diagnostics": {"error": "no_step_or_anchor"}}

        # --- order counts by bias ---
        max_core = cfg.max_orders_core
        max_exp = cfg.max_orders_expansion
        total_max = max_core + max_exp

        if bias == "BUY":
            buy_levels = total_max
            sell_levels = max(1, total_max // 3)
        elif bias == "SELL":
            buy_levels = max(1, total_max // 3)
            sell_levels = total_max
        else:  # BALANCED — paired: buy N, sell N
            buy_levels = total_max
            sell_levels = min(total_max, max(1, total_max // 2))

        # --- portfolio throttle ---
        if portfolio and portfolio.global_buy_throttle < 1.0:
            buy_levels = max(1, int(buy_levels * portfolio.global_buy_throttle))

        # --- RISK_CUT override ---
        if zone == "RISK_CUT":
            buy_levels = 0
            sell_levels = min(3, sell_levels)

        # --- underwater mode override ---
        if state.underwater_mode == "DEFENSIVE":
            buy_levels = 0
        elif state.underwater_mode == "DCA_RESCUE":
            buy_levels = min(2, buy_levels)  # limited DCA
        elif state.underwater_mode == "CAPITULATION":
            buy_levels = 0
            sell_levels = min(2, sell_levels)

        # --- above anchor buy-disable ---
        if price > anchor * (1 + cfg.buy_disable_pct / 100.0):
            buy_levels = 0

        # --- generate buy targets (below anchor) ---
        buy_targets: List[Tuple[float, float]] = []
        for i in range(1, buy_levels + 1):
            bp = anchor - step * i
            if bp <= 0:
                break
            qty = order_usdt / bp if bp > 0 else 0.0
            if qty > 0:
                buy_targets.append((bp, qty))

        # --- generate sell targets (above anchor, only if holding) ---
        sell_targets: List[Tuple[float, float]] = []
        if state.position_qty > 0 and sell_levels > 0:
            # distribute holding across sell levels
            sell_qty_total = state.position_qty
            qty_per_level = sell_qty_total / sell_levels if sell_levels > 0 else sell_qty_total

            for i in range(1, sell_levels + 1):
                sp = anchor + step * i
                sq = min(qty_per_level, sell_qty_total)
                if sq <= 0:
                    break
                sell_targets.append((sp, sq))
                sell_qty_total -= sq
                if sell_qty_total <= 0:
                    break

        diagnostics = {
            "anchor": round(anchor, 4),
            "zone": zone,
            "bias": bias,
            "step": round(step, 4),
            "step_pct": round(step / price * 100, 4) if price > 0 else 0,
            "atr": round(state.atr, 4),
            "atr_pct": round(state.atr_pct, 4),
            "inv_ratio": round(state.inv_ratio, 4),
            "underwater_mode": state.underwater_mode,
            "buy_levels": len(buy_targets),
            "sell_levels": len(sell_targets),
            "position_qty": state.position_qty,
            "position_avg": state.position_avg_price,
            "vwap": round(state.vwap, 4),
        }

        return {
            "buy_targets": buy_targets,
            "sell_targets": sell_targets,
            "diagnostics": diagnostics,
        }

    # ============================================================
    # 7) FULL TICK
    # ============================================================
    def on_tick(
        self,
        state: ICAGMarketState,
        price: float,
        order_usdt: float,
        portfolio: Optional[ICAGPortfolioState] = None,
    ) -> Dict[str, Any]:
        """Full tick cycle: refresh ATR/VWAP → anchor → zone → bias → targets."""
        now = time.time()

        # --- refresh market data (throttled by cache in atr module) ---
        atr_abs, atr_pct = get_market_atr(
            state.symbol,
            period=self.cfg.atr_period,
            timeframe_minutes=self.cfg.atr_timeframe_minutes,
        )
        state.atr = atr_abs
        state.atr_pct = atr_pct

        vwap = get_market_vwap(state.symbol, hours=self.cfg.vwap_period_hours)
        if vwap > 0:
            state.vwap = vwap

        # --- pipeline ---
        self.compute_anchor(state, price)
        self.compute_zone(state, price)
        self.compute_bias(state, price, portfolio)
        self.compute_underwater_mode(state, price)
        targets = self.generate_targets(state, price, order_usdt, portfolio)

        state.last_tick_ts = now

        return targets

    # ============================================================
    # 8) FILL HANDLERS
    # ============================================================
    def on_buy_fill(
        self,
        state: ICAGMarketState,
        fill_price: float,
        fill_qty: float,
    ) -> Dict[str, Any]:
        """Process a buy fill: update position, generate paired sell."""
        cfg = self.cfg

        # update position
        old_total = state.position_qty * state.position_avg_price
        new_total = old_total + fill_price * fill_qty
        state.position_qty += fill_qty
        state.position_avg_price = new_total / state.position_qty if state.position_qty > 0 else 0.0
        if state.position_entry_ts <= 0:
            state.position_entry_ts = time.time()
        state.budget_used += fill_price * fill_qty
        state.buy_count += 1
        state.trade_count += 1
        state.last_fill_ts = time.time()

        # record fill
        state.fill_history.append({
            "side": "buy",
            "price": fill_price,
            "qty": fill_qty,
            "ts": time.time(),
        })

        # --- paired sell target ---
        step_pct = max(
            cfg.min_step_pct / 100.0,
            cfg.fee_rate * 2 + cfg.slippage_buffer,
        )
        if state.atr > 0 and fill_price > 0:
            atr_step = state.atr / fill_price * cfg.base_k * cfg.tp_step_multiplier
            step_pct = max(step_pct, atr_step)

        sell_price = fill_price * (1 + step_pct)

        return {
            "action": "pair_sell",
            "sell_price": sell_price,
            "sell_qty": fill_qty,
            "step_pct": round(step_pct * 100, 4),
        }

    def on_sell_fill(
        self,
        state: ICAGMarketState,
        fill_price: float,
        fill_qty: float,
    ) -> Dict[str, Any]:
        """Process a sell fill: update position, compute profit, generate paired buy."""
        cfg = self.cfg

        # profit calculation (simplified)
        profit = (fill_price - state.position_avg_price) * fill_qty if state.position_avg_price > 0 else 0.0
        fee = fill_price * fill_qty * cfg.fee_rate * 2  # roundtrip fee estimate
        net_profit = profit - fee
        state.realized_pnl += net_profit

        # update position
        state.position_qty = max(0.0, state.position_qty - fill_qty)
        if state.position_qty <= 0:
            state.position_avg_price = 0.0
            state.position_entry_ts = 0.0
        state.budget_used = max(0.0, state.budget_used - fill_price * fill_qty)
        state.sell_count += 1
        state.trade_count += 1
        state.last_fill_ts = time.time()

        # record fill
        state.fill_history.append({
            "side": "sell",
            "price": fill_price,
            "qty": fill_qty,
            "ts": time.time(),
            "profit": round(net_profit, 2),
        })

        # --- paired buy target (reentry) ---
        step_pct = max(
            cfg.min_step_pct / 100.0,
            cfg.fee_rate * 2 + cfg.slippage_buffer,
        )
        if state.atr > 0 and fill_price > 0:
            atr_step = state.atr / fill_price * cfg.base_k * cfg.reentry_step_multiplier
            step_pct = max(step_pct, atr_step)

        buy_price = fill_price * (1 - step_pct)

        return {
            "action": "pair_buy",
            "buy_price": buy_price,
            "buy_qty": fill_qty,
            "step_pct": round(step_pct * 100, 4),
            "profit": round(net_profit, 2),
        }

    # ============================================================
    # helpers
    # ============================================================
    def _aging_bias(self, state: ICAGMarketState) -> float:
        """Calculate sell-pressure bias from position age."""
        cfg = self.cfg
        if state.position_entry_ts <= 0 or state.position_qty <= 0:
            return 0.0
        hold_hours = (time.time() - state.position_entry_ts) / 3600.0
        if hold_hours < 1:
            return cfg.aging_1h_bias
        elif hold_hours < 24:
            return cfg.aging_24h_bias
        elif hold_hours < 72:
            return cfg.aging_72h_bias
        else:
            return cfg.aging_72h_plus_bias
