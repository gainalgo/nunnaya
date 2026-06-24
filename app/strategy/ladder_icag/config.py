# ============================================================
# ICAG Configuration — all tunable parameters in one place
# ============================================================
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ICAGConfig:
    # --- ATR ---
    atr_period: int = 14
    atr_timeframe_minutes: int = 5

    # --- Anchor ---
    anchor_avg_weight: float = 0.70       # position avg price weight
    anchor_vwap_weight: float = 0.30      # rolling VWAP weight
    anchor_ema_alpha: float = 0.15        # slow anchor smoothing
    anchor_fast_alpha_max: float = 0.60   # fast anchor (flash crash)
    anchor_fast_trigger_pct: float = 5.0  # % deviation to trigger fast mode
    anchor_max_jump_pct: float = 3.0      # max anchor move per tick (%)
    vwap_period_hours: int = 24

    # --- Zones ---
    core_width_atr: float = 1.2
    expansion_width_atr: float = 3.0
    cut_pct: float = 6.0                  # hard risk-cut band (%)

    # --- Step / Spacing ---
    base_k: float = 1.0
    min_step_pct: float = 0.25            # absolute floor (%)
    core_k_mult: float = 0.9              # tighter in core (profit 66)
    expansion_k_mult: float = 1.4
    risk_cut_k_mult: float = 1.8

    # --- Inventory ---
    inventory_cap_ratio: float = 0.90     # hard buy disable
    budget_max_utilization: float = 0.75
    buy_disable_pct: float = 2.0          # above anchor (%)

    # --- Bias hysteresis ---
    bias_hysteresis: float = 0.03         # 3% band to prevent flip-flop
    bias_cooldown_sec: int = 60

    # --- Orders ---
    max_orders_core: int = 3              # max orders in core zone (6→3)
    max_orders_expansion: int = 0         # expansion zone disabled (4→0, core only)
    requote_cooldown_sec: int = 60        # grid recompute cooldown (15s→60s)

    # --- Fee-aware profit ---
    fee_rate: float = 0.0005              # 0.05% per trade
    slippage_buffer: float = 0.0005       # 0.05%
    min_profit_usdt: float = 200.0         # minimum profit per round-trip

    # --- Position aging (time decay) ---
    aging_1h_bias: float = 0.0            # fresh: no extra sell pressure
    aging_24h_bias: float = 0.05
    aging_72h_bias: float = 0.15
    aging_72h_plus_bias: float = 0.25

    # --- Deep underwater thresholds ---
    defensive_pct: float = 5.0            # switch to defensive mode
    dca_rescue_pct: float = 10.0          # DCA rescue mode
    capitulation_pct: float = 20.0        # alert user (no auto exec)

    # --- Pair-grid fill ---
    tp_step_multiplier: float = 1.0       # tp_step = step_pct * multiplier
    reentry_step_multiplier: float = 1.0

    # --- Portfolio-level ---
    portfolio_util_throttle: float = 0.80   # reduce buy intensity
    portfolio_util_block: float = 0.90      # block all new buys
    cluster_risk_throttle: int = 3          # N markets in RISK_CUT
    cluster_risk_severe: int = 5
    global_max_orders: int = 25             # account-wide order cap (80→25)

    # --- BTC guard integration ---
    btc_drop_buy_block_pct: float = -2.0    # BTC -2% (5m) = block
    btc_drop_buy_reduce_pct: float = -1.0   # BTC -1% = reduce 70%
