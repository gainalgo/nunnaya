# ============================================================
# Upbit FOCUS Manager — standalone 5-State manager (spot long_only)
# ------------------------------------------------------------
# Guide §3.1: no StrategyPlugin, standalone manager. Own tick loop +
#   own budget + own coin selection. Separate class from Bybit FocusManager
#   (INV-2: do not touch Bybit FOCUS).
#
# 5-State: IDLE → SELECTING → WATCHING → POSITIONED → COOLDOWN
# Entry = GreenPen PA/Zone (long_only). Exit = cycle_tp(TP/SL).
#   ※ longhold/triage final wiring is in stage 4 (A·SLArbiter first) — simple SL here.
# Safety: paper ON by default, enabled OFF by default. State persisted under runtime/upbit/.
# ============================================================
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FocusState(str, Enum):
    IDLE = "IDLE"
    SELECTING = "SELECTING"
    WATCHING = "WATCHING"
    POSITIONED = "POSITIONED"
    COOLDOWN = "COOLDOWN"


@dataclass
class SpotGazuaConfig:
    enabled: bool = True              # ★ ON by default (2026-06-17 owner: "everything ON, except paper"). paper=True so 0 real orders.
    paper: bool = False               # ★ LIVE (real orders) — 2026-06-21 owner "all 3 Live" (GAZUA recovery method armed). Reverses old keep-paper stance. Per-server paper=True revert possible via UI/runtime.
    budget: float = 0.0           # 0 = auto (available balance). Currency-neutral — KRW(Upbit/Bithumb)/USDT(Bybit spot)
    max_positions: int = 3
    max_daily_plans: int = 5
    risk_pct: float = 10.0
    # ── conviction-proportional sizing ── not blindly 1/N, weighted by confidence score.
    #   (Bybit _compute_entry_budget philosophy: strong signal=full slot, weak=partial. per_slot=cap)
    conv_sizing_enabled: bool = True    # OFF=old even 1/N (slot cap full)
    conv_size_floor: float = 0.5        # slot fraction (0~1) used by a pass-floor (entry_conf_threshold) signal. 1=same as weighting OFF
    min_conf: float = 0.4
    entry_conf_threshold: float = 0.85
    primary_tf: str = "240"
    top_n: int = 10
    scan_interval_sec: float = 60.0
    # Markets excluded from scan (comma-separated) — pass turnover but keep tripping in analysis/candles,
    #   excluded operationally to keep logs clean. ★ Not a trading blacklist (no loss penalty) — blocks the request itself.
    scan_exclude: str = "KRW-APENFT"
    # ── Exchange warning-listed coin handling (selected in Entry tab) ── badge always shown, only entry block toggled.
    block_warning_coins: bool = True    # Block entry on investment-warning coins (delisting risk) (bot+manual). ON by default.
    block_caution_coins: bool = False   # Block entry on caution coins (sharp price moves etc). OFF by default = display only.
    cooldown_sec: float = 600.0
    tp1_mult: float = 1.2             # for coins (guide §9.6)
    tp2_mult: float = 2.5
    sl_mult: float = 0.8
    min_rr: float = 1.5
    min_tp_distance_pct: float = 0.3  # fee-guard
    trailing_pct: float = 1.5
    partial_pct: float = 50.0
    stale_hold_hours: float = 0.0     # 0 = disabled (skeptical of time cuts — memory lesson)
    # ★ TP method — retail rotation (fixed %) vs ATR swing (volatility multiple)
    #   owner philosophy: take profit small and often net of fees, accumulate like a retail trader → near % TP by default.
    #   ATR method (use_pct_tp=False) pushes TP +6~13% away on volatile coins so it never gets hit.
    use_pct_tp: bool = True            # default fixed % (rotation)
    tp1_pct: float = 1.2              # entry +1.2% → half partial take-profit
    tp2_pct: float = 2.5             # entry +2.5% → full
    sl_pct: float = 1.0              # entry -1.0%
    # ── spot exit final wiring (§4.2 · via A·SLArbiter · ★ON by default, observe in paper) ──────
    #   On SL hit, instead of selling immediately: "longhold" — hold the sell if BTC is healthy,
    #   release on entry-price recovery. Possible because spot has no liquidation (INV-3).
    #   ※ In a BTC downtrend, falls back to normal SL sell automatically (avoid catching a falling knife; inside A).
    longhold_enabled: bool = True      # ★ ON by default (2026-06-17 owner) — observe behavior in paper
    longhold_release_pct: float = 0.0  # 0=ATR dynamic (ATR%×1.5, clamp 1~8%) / >0=fixed %
    longhold_max_hold_hours: float = 72.0  # max longhold duration (avoid being stuck forever). 0=unlimited
    # ── §4 GAZUA recovery method: DCA (averaging down) — lower avg cost before SL → longhold if deep (2026-06-21 owner) ──
    #   Origin = plugin_gazua._common_dca_check (validated values). Spot has no liquidation so average-down→longhold→recovery harvest works.
    #   From initial entry, every step% add add_ratio×initial principal (pyramiding per round), up to max_depth% = auto 8 steps.
    #   ★ In live this means real-order averaging (adding to a losing position) — defenses dca_max_pos_mult (all-in block) and dca_abs_sl_pct (true floor) are required.
    dca_enabled: bool = True            # core of the recovery method. 0=OFF (simple SL/longhold only)
    dca_step_pct: float = 0.5           # add once for every N% below initial entry (GAZUA validated value)
    dca_add_ratio: float = 0.25         # one add = initial principal × this ratio (base before pyramiding)
    dca_max_depth_pct: float = 4.0      # average down only to this depth (auto step count = depth/step = 8). beyond = longhold
    dca_pyramid_step: float = 0.20      # add-multiplier +0.20 per round (gradual)
    dca_pyramid_max: float = 2.5        # add-multiplier cap
    dca_max_pos_mult: float = 3.0       # total invested per coin ≤ slot budget × this (over-allocation safety net)
    dca_abs_sl_pct: float = -35.0       # absolute floor (vs initial entry) — below this, force-sell ignoring longhold. 0=unlimited longhold
    # ★ Falling-knife gate (2026-06-23) — DCA would average down even mid-freefall → 2x size breaks SL/longhold
    #   (measured loss: manual force-close avg -3041/trade, post-DCA fast SL -7544 @ roe -1.9%). Entry has a momentum_reversal guard
    #   but DCA did not, so knife-catching inverts the risk/reward. → If the prior 5M bar is dropping hard, defer averaging this tick
    #   (until the bottom settles). Keep the good pullback DCA (stalled knife), block only the falling-knife catch.
    dca_stabilize_gate_enabled: bool = True   # short-term stabilization check before DCA. 0=OFF (old unconditional averaging)
    dca_stabilize_strong_atr: float = 1.0     # if prior 5M bar drop ≥ this×ATR, judge it a falling knife → defer averaging
    dca_min_gap_sec: float = 60.0             # min seconds between averaging adds — caps a fast multi-step drop from firing several adds within one bar before the ATR-relative gate catches up (it is too late on high-ATR pumped coins). 0=off
    # ── entry quality gates (§② · live-restore gates · ★ON by default, observe in paper) ─────
    #   owner diagnosis: entries are at the top/late stage → don't add more cuts, add *entry room*. (feedback_bad_entry_not_fixed_by_cut)
    #   ★ 2026-06-17 owner "everything ON" — paper means 0 real orders, observe improvement per restart. 0=that gate OFF.
    headroom_gate_pct: float = 1.0     # minimum headroom % to overhead resistance. 0=OFF. (top-chasing block gate)
    atr_sl_floor_mult: float = 0.8     # min SL distance = mult×ATR (widen if fixed %SL is narrower). 0=OFF. (avoid instant death on 1-min noise for small coins)
    overext_range_pos_pct: float = 0.85 # late-stage block: block if above this fraction of the 24H range top. 0=OFF. (★no ADX exemption)
    overext_min_move_pct: float = 8.0  # min 24H move |%| to judge late-stage (exclude small moves)
    blowoff_move_pct: float = 30.0     # parabolic block: block if 24H |move|≥this % and chasing. 0=OFF. (covers the overext ADX gap)
    # ── guard_score (§② G1 · ADX+trend-conf conviction score · ★ON by default=display, gate only when threshold>0) ──
    #   65-80 sweet / 80+ lagging → no blind floor. G1=observe (scanner score column), gate is G4(threshold).
    guard_score_mode_enabled: bool = True  # score computation/display ON. (threshold=0 means no entry block)
    # ★ G4 (2026-06-17): threshold gate ON. Block SIDEWAYS junk setups (≈+25), pass only good spots (trend+PA≈65).
    #   "Don't enter without an uptrend" (owner observation) enforced = not entering is also a strategy.
    guard_score_threshold: float = 50.0    # min entry score. 0=gate OFF. >0=block below (2026-06-17 45→50: mini-backtest 54%→high quality, owner's elite few).
    guard_score_total_cap: float = 80.0    # ±cap clamp — caps the "80+ lagging" top (sweet upper bound). 0=unlimited.
    # ── ② exit guard: multi_be_lock (lock SL upward in stages off peak = ratchet profit protection) ──
    #   Each time peak profit crosses a stage, lock SL upward only (never down). Inactive while in loss.
    #   ★ Protection not a cut — Bybit-validated (38 trades 34 wins). fee-aware (BE lock=entry+cushion). Fixed lock levels (+0.3/1.0/2.0%).
    multi_be_lock_enabled: bool = True
    multi_be_lock_stage1_pct: float = 0.25   # peak≥0.25% → SL=BE+cushion
    multi_be_lock_stage2_pct: float = 1.0    # peak≥1.0%  → SL=entry+0.3%
    multi_be_lock_stage3_pct: float = 2.0    # peak≥2.0%  → SL=entry+1.0%
    multi_be_lock_stage4_pct: float = 3.0    # peak≥3.0%  → SL=entry+2.0%
    multi_be_lock_fee_cushion_pct: float = 0.1  # Upbit round-trip fee (0.05%×2) cushion
    # ★ ATR adaptive (2026-06-18) — for majors (low-vol BTC/ETH) 0.25% peak is noise → be_lock fires on noise → BE cut
    #   (the culprit of the 0%-win-rate rotation on Bybit spot). When ON, arming floor = max(stage1_pct, ATR%×mult)=start only above noise.
    multi_be_lock_atr_adaptive: bool = False     # OFF by default (ON on major exchanges after paper observation)
    multi_be_lock_atr_mult: float = 2.0          # arming floor = ATR% × this (larger = locking starts later)
    # ── ② exit guard: be_stall intelligent (peak stall + momentum rollover → take-profit cut) ──
    #   ★ As a cut it has 5 safety layers (DESIGN_upbit_be_stall_intelligent): time window [min,max], fee guard,
    #   momentum (cut only when against is clear), neutral=conservative HOLD, inactive in loss/longhold. paper observe ON.
    be_stall_enabled: bool = True
    be_stall_sec: float = 60.0                  # min peak stall (cut candidate if it doesn't rise for this long)
    be_stall_max_since_peak_sec: float = 1800.0  # stale cutoff (don't fire on peaks older than this — prevents the FARTCOIN incident)
    be_stall_neutral_exit: bool = False         # time-cut even on neutral momentum? default False=conservative (cut only on clear against)
    be_stall_rsi_strong: float = 55.0           # LONG: RSI≥ = our side
    be_stall_rsi_weak: float = 45.0             # LONG: RSI≤ = against side
    # ── fees (net PnL · entered directly by owner 2026-06-17) ──────────────────────────
    #   charged on buy+sell each → round-trip = fee_rate_pct × 2. journal PnL/ROE and unrealized PnL are net (fee-deducted).
    #   Upbit KRW market standard 0.05%/side. May differ with coupons/events, so owner adjusts directly in the UI.
    fee_rate_pct: float = 0.05                  # one-side (buy or sell) fee rate %. 0=ignore fees (gross).
    # ── paper slippage (2026-06-24 owner) — model paper to fill *unfavorably* like live ──
    #   paper fills at the signal price instantly with no client call → 0 slippage = optimistic (false profit). By this amount
    #   assume buy=more expensive / sell=cheaper so paper PnL ≈ live. One-way bps (5=0.05%). 0=old behavior (ignore slippage).
    #   For thin alts, 10~20 is realistic. No effect in LIVE (real fill price as-is).
    paper_slippage_bps: float = 5.0
    # ── manual buy (quick-trade) position handling (2026-06-17 owner) ──────────────────────
    #   A quick-trade buy = registered in self.positions → shown in panel (regardless of slot count, no bot slot consumed).
    #   False (default, hands-off): bot doesn't touch it — no auto SL/TP, human harvests directly via close button (profit/loss).
    #   True (bot-managed): manual buys are auto-managed like normal positions with SL/TP1/TP2, longhold, and exit guards.
    manual_manage_enabled: bool = False
    # ── CONTRARIAN 2nd entry source (2026-06-18 · plan_contrarian_on_spot · ★OFF by default) ──────
    #   Opposite regime from FOCUS (trend-following) — OFF in an uptrend, enters only in neutral/down (FOCUS-churn markets).
    #   Adds entry only; execution = _execute_entry mirror (manual=False), exit = inherited automatically from _manage_all_positions
    #   (longhold/triage/be_stall = master-plan §4 spot exit modules, already implemented). Separate slot/budget.
    #   long_only (spot). Does not touch the conviction-1-shot FOCUS slot (separate slot). default OFF=live-safe.
    contrarian_enabled: bool = False            # contrarian entry source ON/OFF (OFF by default)
    contrarian_max_positions: int = 1           # contrarian separate slot (combined exposure with FOCUS max_positions)
    contrarian_coin_up_th: float = 3.0          # entry qualification: coin 24h move − BTC move ≥ this % (relative strength)
    contrarian_coin_up_cap: float = 15.0        # parabolic block: exclude if coin 24h |move| ≥ this % (pump trap). 0=OFF
    contrarian_regime_gate: bool = True         # True=don't enter in an uptrend (BTC UP), neutral/down only. False=always
    contrarian_budget: float = 0.0              # contrarian entry budget. 0=contrarian_budget_pct% of equity / >0=fixed amount
    contrarian_budget_pct: float = 10.0         # ratio % of equity when contrarian_budget=0
    contrarian_tp_pct: float = 1.5              # contrarian TP1 (partial) entry +%
    contrarian_tp2_pct: float = 3.0             # contrarian TP2 (full) entry +%
    contrarian_sl_pct: float = 1.5             # contrarian SL entry -%
    # ── *copy* of the futures FOCUS entry gates (2026-06-18 · plan_spot_full_chain) ──
    #   Futures core (focus_manager) untouched; only validated gates copy-ported into spot_entry_guards.py.
    #   ★ 2026-06-18 owner "whatever is ON in FOCUS is ON in spot too" → futures-ON guards default ON (config_version 8 migration).
    #     paper=True so 0 real orders (observe). Only momentum_deriv is also OFF in futures → keep OFF.
    #   ★ 2026-06-21 owner: after LIVE switch, this AND-chain of candle-timing gates blocked auto-entry (0 entries, all manual) → relaxed to default OFF.
    #     Quality/top defense kept via guard_score + headroom/overext/blowoff. (ON was right for paper observation but live=must re-tune. 0 entries=broken.)
    #   ① gap_check — distance to overhead N-bar high < required gap → block (no entry just below the ceiling)
    gap_check_enabled: bool = False              # [2026-06-21 owner] OFF for live auto-entry (candle gate)
    gap_check_tf: str = "60"                     # gap-measurement TF (5/15/30/60M) — [2026-06-20] 15→60(H1): the 3h-high anchor blocked range markets constantly (802 cases) → H1×12=12h structural resistance. Unrelated to futures (spot-only)
    gap_check_lookback_bars: int = 12            # last N bars (60×12=12h structural resistance)
    gap_check_breakout_exempt: bool = True       # [2026-06-20] exempt gap on new-high breakout — a breakout isn't top-chasing (breakout coins are always right near their own high, so the anchor alone can't clear it). Pump tops are caught by headroom/late-stage/micro
    gap_check_min_pct: float = 0.3               # minimum required gap %
    gap_check_atr_adaptive: bool = True          # volatility-adaptive (required gap = max(min, ATR%×mult))
    gap_check_atr_mult: float = 0.7
    gap_check_atr_cap_pct: float = 1.5
    #   ② micro_1m — 1M bar direction/volume/RSI "right now" timing (defer on counter-bar/exhaustion/overheat)
    micro_1m_check_enabled: bool = False         # [2026-06-21 owner] OFF for live auto-entry (candle gate)
    micro_1m_body_min_pct: float = 0.05          # [2026-06-20] 0.0→0.05: noise dojis with |body|<0.05% pass regardless of color (fixes 1m_candle_against 0-entry). 0=old (color only). Applied via v9 migration.
    micro_1m_vol_decline_bars: int = 3           # consecutive volume-decline bars
    micro_1m_rsi_long_max: float = 70.0          # LONG 1M RSI overheat cap
    micro_1m_rsi_short_min: float = 30.0         # (unused in spot, kept for compat)
    #   ③ momentum_reversal — block strong prior-5M against-move (ATR multiple) (no falling-knife entry)
    momentum_reversal_enabled: bool = False      # [2026-06-21 owner] OFF for live auto-entry (candle gate)
    momentum_reversal_strong_atr: float = 1.0    # strong against-move threshold (×5M ATR)
    momentum_reversal_lookback_bars: int = 3     # cumulative against-move check bars
    #   ④ raw_body — block if prior 5M N-bar open→close net energy is against the entry (Phase 2)
    raw_body_enabled: bool = False               # [2026-06-21 owner] OFF for live auto-entry (candle gate)
    raw_body_lookback: int = 3
    raw_body_min_net_pct: float = 0.3            # [2026-06-20] 0.05→0.3: 0.05 blocked even noise drift (−0.45% etc) and wiped entries (137 tries 0 entries). Block only clear −0.3%+ selling = higher frequency (fixes the futures-copy miscalibration, spot-only)
    #   ⑤ momentum_deriv — block if 5M RSI/MACD rate-of-change accelerates against the entry (Phase 2)
    momentum_deriv_enabled: bool = False
    momentum_deriv_lookback: int = 5
    momentum_deriv_rsi_slope: float = 2.0        # RSI change threshold
    momentum_deriv_macd_slope: float = 0.0       # MACD hist change threshold
    momentum_deriv_require_both: bool = True      # True=only when both RSI+MACD are against (conservative)
    #   ⑥ mtf_align — block if higher/short TF structure is clearly against (Phase 3, spot had score only)
    mtf_align_enabled: bool = False              # [2026-06-21 owner] OFF for live auto-entry (candle gate)
    mtf_align_tfs: str = "240,30,15"             # TFs to check (add D='D' when exchange-supported)
    #   ⑦ entry_expectation — block on insufficient reward (reach potential) / excessive risk (loss width) (Phase 3, reuse shared util)
    entry_expectation_enabled: bool = True
    entry_expectation_min_reward_pct: float = 0.8   # block if reward < this % (insufficient reach potential)
    entry_expectation_max_risk_pct: float = 6.0     # block if risk > this % (excessive loss width)
    # ★ [2026-06-20] guard_score-pass candidates are exempt from the EE gate (mirrors futures focus_manager.py:16685 — not _guard_score_pass).
    #   Spot _scan_and_maybe_enter candidates are all guard_score(50)-selected, but EE was re-blocking them with a *fake zone-RR*
    #   (to the nearest H1 resistance) separate from the use_pct_tp fixed % (TP+2.5/SL-1.0=real RR 2.5) (bypass dropped when porting commit 2e7f404). room=headroom 1%+gap defends separately.
    entry_expectation_bypass_guard_score: bool = True  # exempt guard_score-pass from EE gate (★ON · skip RR gate for score-pass · futures mirror. OFF=old behavior)
    #   ⑧ microtiming_5m — three 5M RSI/MACD/BB inflections; defer this tick if below 2/3 (Phase 4, defer=WAIT)
    microtiming_5m_enabled: bool = False         # [2026-06-21 owner] OFF for live auto-entry (candle gate)
    microtiming_5m_min_score: int = 2               # min inflection score to pass (0~3)
    microtiming_5m_rsi_long_threshold: float = 35.0  # RSI oversold inflection threshold
    microtiming_5m_bb_low_pct: float = 20.0          # BB lower-band threshold %
    microtiming_5m_bb_recover_pct: float = 30.0      # BB recovery threshold %
    # ====================================================================
    # BEGIN_FUTURES_672_MIRROR — futures FOCUS core 672 (verbatim copy of futures FocusConfig)
    #   2026-06-18 owner: "scrap the half-baked version and re-set up with the 672 config/defaults".
    #   ★ Crystallization of trial and error — not one value touched (0 hand-typing, extracted directly from futures source, comments and all).
    #   Currently data-sealed only (unwired) → re-wire score/guard/gate/exit to these fields chapter by chapter,
    #   removing the old spot-native approximations. Exchange unit conflicts (turnover USD↔KRW, blacklist etc) verified separately.
    # ====================================================================
    rr_ratio: float = 3.0
    max_daily_sl: int = 100        # [2026-04-25 owner decision] halt at 10 SLs (50% capital cap, more lenient)
    entry_tf: str = "5"            # M5 = "5"
    cycle_tp1_mult: float = 1.8     # Safe basis: TP1:SL=1.64:1 (old 0.5=1:1 → eaten by fees)
    cycle_tp2_mult: float = 3.0     # Safe basis: TP2:SL=2.73:1 (runner target)
    cycle_sl_mult: float = 1.1      # [2026-04-25 Long Hold System] SL farther (1.1→2.5, 5% capital risk per trade)
    partial_exit_pct: float = 50.0
    adx_filter_enabled: bool = True        # ADX-based entry filter
    min_adx_entry: int = 5                # [2026-06-21 owner] 17→5: relax ADX threshold for live auto-entry (unblock SIDEWAYS)
    # ★ [2026-06-19 owner] dedicated TF for the ADX entry gate — primary_tf(H4=240) is a 5-day box so volatile coins are
    #   forever SIDEWAYS/low-ADX. The score (base conviction) is already 6-TF and sees short-term trend → only the gate uses a short TF.
    adx_entry_tf: str = "60"              # ADX entry-gate TF (H1). "30"/"15" for shorter. ""=use primary_tf
    adx_entry_breakout_exempt: bool = True   # even at low ADX, pass if the last closed bar broke the last N-bar high (box-recovery entry)
    adx_entry_breakout_lookback: int = 12    # breakout-check lookback bars (in adx_entry_tf)
    dormant_adx_threshold: int = 15       # DORMANT basis: at or below this = no trend
    min_conviction: float = 35.0          # 2026-05-19 Phase 6 Step 1: 50→70 (old 10pt 7pt = elite admission bar; 1823-trade analysis found old conv 7+ captured 80% of big profit)
    phase3_context_bonus_enabled: bool = True  # Phase 3 time-of-day (±4) + coin (+2) bonus ON/OFF
    scanner_entry: bool = True            # [2026-04-25 default promotion] standardize multi-slot scanner entry (False→True)
    scanner_min_adx: int = 25             # [2026-04-18 evening] 25→18 ($1/min target, Anti-Knife+H4 ensure quality)
    scanner_min_conviction: float = 50.0  # 2026-05-19 Phase 6 Step 1: 30→70 (old 7pt = elite admission)
    scanner_max_exposure_pct: float = 90.0  # max % of balance used
    scanner_m30_primary_conflict_penalty: float = 1.0   # conviction multiplier on PRI(H1) vs 30M trend conflict (1.0 = no penalty)
    scanner_m30_direction_conflict_penalty: float = 1.0 # conviction multiplier on direction(LONG/SHORT) vs 30M trend conflict
    entry_mode: str = 'score'                # "score" | "reverse"
    scanner_min_turnover_24h: float = 1_000_000.0   # liquidity threshold (24h $1M)
    scanner_min_price_usdt: float = 0.10            # dust filter (5.0 → 0.10, allows strong-signal entries like APE/CHIP)
    scanner_top_n: int = 20                         # tracked coin count (10 → 20)
    scanner_blacklist: list = field(default_factory=lambda: ['CLUSDT', 'XAGUSDT', 'XAUTUSDT'])  # permanently blocked coins (e.g. ["CLUSDT"] — Bybit terms not accepted, etc)
    max_same_direction: int = 2            # max positions in the same direction (rest forced opposite) — Auto default
    auto_first_dir_lock: bool = False       # [2026-04-26 owner decision] lock first-shot direction in Auto mode
    regime_reversal_pause_enabled: bool = False      # old "transition = expensive-tuition zone" → replaced by pulse guards
    regime_reversal_ema_gap_threshold_pct: float = 0.3  # BTC EMA gap < 0.3% (convergence)
    regime_reversal_adx_threshold: float = 20.0      # ADX < 20 (trend weakening)
    regime_reversal_pause_min: float = 15.0          # entry-pause time (minutes)
    conv_sizing_low_threshold: float = 35.0  # [2026-05-17 100pt ×10] 5→50. conv≤50 → budget × 0.5
    conv_sizing_high_threshold: float = 65.0 # [2026-05-17 100pt ×10] 9→90. conv≥90 → budget × 1.5
    conv_risk_scale_enabled: bool = False    # ON/OFF (must turn on to fire)
    conv_risk_peak_conv: float = 65.0        # sweet-spot start (inverted-U peak) — linear rise from entry-threshold to here
    conv_risk_peak_mult: float = 1.5         # sweet-spot risk multiplier
    conv_risk_chop_conv: float = 80.0        # late-stage line (above this = lagging/late)
    conv_risk_chop_mult: float = 0.6         # late-stage risk-cut multiplier
    conv_risk_floor_mult: float = 0.5        # below entry-threshold (anomalous) safety multiplier
    conv_risk_max_mult: float = 2.0          # factor safety cap (prevent runaway)
    btc_trend_conv_bonus_enabled: bool = True
    btc_trend_conv_bonus: float = 20.0       # [2026-05-17 100pt ×10] 2→20
    multi_be_lock_atr_adaptive_enabled: bool = True   # True=ATR-multiple mode / False=fixed-% mode (uses stage_pct above)
    multi_be_lock_atr_tf: str = "60"                  # ATR calc TF (H1)
    multi_be_lock_atr_period: int = 14                # ATR period
    multi_be_lock_stage1_atr_mult: float = 0.3        # (adaptive mode) stage1 trigger = ATR% × 0.3 (BTC~0.3%, HYPE~0.9%)
    multi_be_lock_stage2_atr_mult: float = 0.7        # (adaptive mode) stage2 trigger = ATR% × 0.7
    multi_be_lock_stage3_atr_mult: float = 1.4        # (adaptive mode) stage3 trigger = ATR% × 1.4
    multi_be_lock_stage4_atr_mult: float = 2.2        # (adaptive mode) stage4 trigger = ATR% × 2.2
    multi_be_lock_atr_min_stage1_trigger_pct: float = 0.2  # [2026-06-04 owner] 0.3→0.2 (validated, lower stage1 floor)
    multi_be_lock_atr_max_stage1_trigger_pct: float = 3.0
    be_lock_grace_sec: float = 60.0
    be_lock_smart_rsi_check: bool = True       # ① RSI in profit direction → hold off firing
    be_lock_smart_candle_check: bool = True    # ② last N bars consecutively in profit direction → hold off firing
    be_lock_smart_rsi_long_min: float = 55.0   # LONG: RSI ≥ this = running
    be_lock_smart_candle_count: int = 3        # last N bars (5M) consecutively in profit direction
    smart_manual_entry_enabled: bool = True            # ON/OFF (when OFF, hide L⏳/S⏳ buttons)
    smart_manual_entry_default_timeout_sec: float = 300.0   # wait time (sec). UI inputs in minutes → ×60
    portfolio_sl_rate_enabled: bool = True
    portfolio_sl_rate_window_min: int = 5         # last N minutes window
    portfolio_sl_rate_threshold: int = 3          # fire if M+ SL hits within the window
    portfolio_sl_rate_pause_min: int = 15         # new-entry block time when fired
    btc_b12_combined_cap_enabled: bool = True
    btc_b12_combined_cap_max: int = 2             # cross_delta + #4_btc_bonus combined cap
    parent_roe_guard_enabled: bool = True
    parent_max_roe_loss_pct: float = 31.0          # [2026-05-13 owner re-tune] 31% — matches UI label/help. lev 5→SL 6.2% / lev 7→4.43% / lev 10→3.1%. lose at most 1/3 of margin (conservative)
    adaptive_cooldown: bool = False         # increase cooldown per loss streak
    emergency_tp_tiers: bool = False       # [2026-04-26 Long Hold default] BTC crash auto-close OFF
    lock_market: str = ''          # fixed coin (5-28 owner: gold-only first row) — empty = auto scan
    dynamic_trailing: bool = True         # [2026-04-26 Long Hold default] OFF (manual SL is the owner's)
    breakeven_trigger_pct: float = 0.4     # [2026-04-25 default promotion] fast BE lock (0.4→0.3)
    trailing_preserve_pct: float = 50.0    # [2026-04-18 night] 50→40 (more room on large profit, fixes giving back more than taken)
    trailing_small_profit_preserve_pct: float = 40.0  # [2026-04-18 night] 40→60 (grip small profit tightly)
    trailing_accel_pct: float = 5.0       # [2026-04-18 night] 5→3 (tighten slowly on big profit → keep the runner)
    trailing_tp_enabled: bool = True
    trailing_tp_min_progress: float = 0.5   # fire when peak/TP1 progress ≥ this
    trailing_tp_follow_low: float = 0.93     # follow ratio at 40~60% progress (TP at 93% of peak)
    trailing_tp_follow_mid: float = 0.90     # at 60~80% progress
    trailing_tp_follow_high: float = 0.87    # at 80%+ progress
    be_stall_exit_enabled: bool = True      # [2026-04-26 Long Hold default] OFF (can't exit without profit)
    be_stall_exit_sec: float = 60.0          # 23s pivot + 7s margin. coin volatility: 25(high) ~ 35(low).
    be_stall_intelligent_enabled: bool = True  # [2026-05-15 owner] momentum-linked (MACD/RSI/BB) intelligent — live implementation
    be_stall_intelligent_rsi_strong: float = 55.0    # LONG: RSI >= 55 = our side / SHORT: RSI <= 45
    be_stall_intelligent_rsi_weak: float = 45.0      # LONG: RSI <= 45 = against side / SHORT: RSI >= 55
    min_tp_distance_enabled: bool = True
    microtiming_5m_defer_sec: float = 600.0          # re-eval interval after WAIT (default 10 min)
    microtiming_5m_max_defers: int = 3               # max defers (default 3 → natural expiry, no BLOCK count buildup)
    microtiming_5m_phase_k_exempt: bool = True       # exempt Phase K (regime transition) entries — prioritize catching market transitions
    micro_1m_candle_check: bool = True             # ① check last 1M bar direction
    micro_1m_candle_trend_exempt_adx: float = 0.0  # ADX above this = strong trend → exempt 1M bar direction check (0=disabled)
    micro_1m_volume_check: bool = True             # ② check 1M volume decline
    micro_1m_rsi_check: bool = True                # ③ check 1M RSI extremes
    pre_be_stall_exit_mode: str = 'AUTO'                # [2026-04-25 Long Hold System] "AUTO"→"OFF" (be patient until BE)
    pre_be_stall_min_profit_pct: float = 0.10          # fire only at +0.10%+ (lock in small profit)
    pre_be_stall_sec: float = 240.0                     # [2026-04-25 default promotion] owner "91s too early" (60→240)
    pre_be_stall_volatility_threshold_pct: float = 2.0 # ATR% branch for range/spike under AUTO (per entry coin)
    pre_be_stall_max_since_peak_sec: float = 1800.0    # max time after peak (default 30 min)
    pre_be_loss_guard_enabled: bool = False            # default OFF — owner must enable (live after paper observation)
    pre_be_loss_guard_peak_max_pct: float = 0.10       # peak ≤ this = floundering (can't lift off) target
    pre_be_loss_guard_trigger_loss_pct: float = 0.5    # small cut if pushed -this(%) vs entry (half of SL -1~2%)
    pre_be_loss_guard_min_hold_sec: float = 60.0       # min hold after entry (extra safety beyond grace)
    pre_be_loss_guard_max_age_sec: float = 7200.0      # [2026-06-09 owner] 1800→7200(2h) — floundering -63(>30min) didn't fire on stale rule; entry-based so safe from old-peak incidents
    overextension_enabled: bool = True                 # live ON (owner 2026-06-07 "live not paper")
    overextension_range_pos_pct: float = 0.85          # LONG: above this fraction of 24H range top / SHORT: bottom (1-this)↓
    overextension_min_move_pct: float = 8.0            # 24H move must be |%| ≥ this to judge 'late-stage' (exclude small moves)
    overextension_penalty: float = 10.0                # conviction penalty points
    overextension_adx_exempt: float = 30.0             # ADX above this = strong breakout → exempt penalty (0=no exemption)
    blowoff_filter_enabled: bool = False               # ON/OFF (must turn on to fire)
    blowoff_penalty: float = 20.0                      # base penalty (at move_pct point)
    blowoff_extreme_pct: float = 80.0                  # 24h |move| ≥ this % = extreme (max penalty)
    blowoff_max_penalty: float = 40.0                  # max penalty at extreme
    blowoff_chase_only: bool = True                    # True = penalize same-direction (chase) only / fade (opposite) exempt
    headroom_penalty_enabled: bool = True              # [2026-06-10 owner "you input it and turn it on"] fleet default ON
    headroom_sr_penalty: float = 6.0                   # penalty for LONG just under resistance / SHORT just over support
    headroom_sr_near_pct: float = 1.5                  # within this % to resistance/support = no room
    headroom_rsi_penalty: float = 6.0                  # penalty for LONG overbought / SHORT oversold entry
    headroom_rsi_overbought: float = 70.0              # LONG: RSI above this = nowhere to go
    headroom_rsi_oversold: float = 30.0                # SHORT: RSI below this = nowhere to go
    headroom_bb_penalty: float = 4.0                   # penalty for LONG at BB top / SHORT at BB bottom
    headroom_bb_hi_pctb: float = 0.80                  # %b above this = band top (LONG no room)
    headroom_bb_lo_pctb: float = 0.20                  # %b below this = band bottom (SHORT no room)
    inflection_setup_enabled: bool = False             # when ON, add ㉒ Inflect item to guard score
    inflection_setup_weight: float = 20.0              # W: max scale of the inflection modifier
    inflection_setup_cap: float = 20.0                 # output clamp ±cap
    inflection_setup_base: float = 0.45                # base: baseline adjustment from position alone (when momentum 0)
    inflection_setup_slope_scale: float = 0.40         # slope15m tanh normalization scale (%)
    retest_setup_enabled: bool = False                 # when ON, add ㉓ Retest item to guard score
    retest_setup_weight: float = 12.0                  # max retest bonus size (× retracement quality)
    retest_setup_turn_bonus: float = 4.0               # extra bonus when turning in-direction after retracement
    retest_retr_lo: float = 0.30                       # min retracement ratio (below = top-chase, no signal)
    retest_retr_hi: float = 0.90                       # ideal retracement upper bound (>+0.3 = too-deep failure)
    retest_pivot_width: int = 2                        # pivot high/low left-right width
    retest_fail_pct: float = 0.005                     # retest fails if it strays this % from the breakout level
    awaken_sl_enabled: bool = False                    # awakening-SL adaptation ON/OFF (must enable)
    awaken_sl_mode: str = "both"                       # atr / structure / both (SL distance basis, owner's 3 modes)
    awaken_atr_ratio: float = 1.3                      # awakening judgment — current ATR / past ATR ratio
    awaken_atr_lookback: int = 20                      # past ATR average bars (primary_tf=H4)
    awaken_max_sl_mult: float = 2.5                    # max SL multiple (prevent infinite expansion)
    awaken_require_day_align: bool = True              # only Day (coin D1) in-trend qualifies to hold (exclude against/undecided)
    awaken_swing_lookback: int = 10                    # swing search bars for structure point (the awakening's footing)
    awaken_atr_buffer: float = 0.5                     # ATR buffer multiple on the structure point
    conviction_ceiling_enabled: bool = False           # default OFF — owner must enable (after checking proper value with the tool)
    conviction_ceiling_start: float = 65.0             # conviction above this = late-stage candidate
    conviction_ceiling_target: float = 50.0            # cap late-stage to this score (push below the 65 gate)
    conviction_ceiling_adx_exempt: float = 30.0        # ADX above this = riding the wall → exempt (0=no exemption)
    guard_score_total_cap_enabled: bool = False        # clamp on the sum of guard bonuses ON/OFF
    conviction_ceiling_post_guards: bool = False       # True = apply the late-stage cap *after* base+guard sum (block bonus revival)
    final_bypass_use_base: bool = False                # True = compare score-absorb bypass against base conviction (inflation-agnostic)
    entry_grace_period_sec: float = 0.0                 # disable fast-cut guards for N sec after entry. 0=OFF, 300=5min recommended.
    market_bias_grace_exit_enabled: bool = False        # default OFF — enable together with entry_grace_period_sec
    news_grace_exit_enabled: bool = False               # default OFF
    news_grace_exit_threshold: float = 0.5              # force exit if |sentiment| >= threshold (1.0=extreme)
    long_hold_timeout_enabled: bool = False     # [2026-04-25 Long Hold System] instant-cut side effect when tier1/2=0 → all OFF
    hard_roe_cap_enabled: bool = True           # default OFF (Long Hold consistency)
    hard_roe_cap_roe_pct: float = -50.0          # fire threshold ROE % (default -50, effectively rarely fires)
    override_slot_enabled: bool = True
    override_min_conviction: float = 55.0        # [2026-05-17 100pt ×10] 8→80. Override Slot entry conviction threshold
    override_min_adx: float = 40.0               # ★ 2026-05-11 owner option A (50→40 relaxed)
    override_min_mtf_align: int = 4              # all TFs aligned (Phase 2B)
    override_min_b12_n: int = 7                  # 7+ of 8 coins agree (Phase 2B)
    override_require_btc_trend_match: bool = True  # BTC trend match required (Phase 2B)
    override_max_extra_slots: int = 3            # max +3 (expansion cap)
    override_locked_slot_min_hours: float = 6.0  # ★ window(h) — count only slots locked at least this long (user knob)
    override_size_cap_pct: float = 8.0           # 8% of capital (less than half of the usual 20%)
    override_max_sl_distance_pct: float = 5.0    # SL 5% (1/4 of the usual 20%)
    override_breakeven_trigger_pct: float = 0.3  # BE locks fast (0.3%)
    override_hard_roe_cut_pct: float = -10.0     # ★ Hard ROE -10% instant cut (against betrayal)
    momentum_reversal_medium_atr: float = 0.5          # 5m 1-bar against ≥ ATR×0.5 → medium against
    momentum_reversal_strong_weight: float = -30.0     # [2026-05-17 100pt ×10] -3→-30. strong-against penalty
    momentum_reversal_medium_weight: float = -20.0     # [2026-05-17 100pt ×10] -2→-20. medium-against penalty
    long_hold_timeout_tier1_min: float = 5.0           # [2026-05-13] T1 Never-Green: 5 min + peak<0.01% — cut if never green since entry (loss-side mirror of BE Stall)
    long_hold_timeout_tier1_peak_pct: float = 0.01     # effectively "never green"
    long_hold_timeout_tier2_min: float = 15.0          # [2026-05-13] T2 "block the 5-min graduation amnesty": 15 min + peak<0.05% — slightly green but no further progress
    long_hold_timeout_tier2_peak_pct: float = 0.05
    long_hold_timeout_tier3_min: float = 30.0          # [2026-05-13] T3 BE-distant: 30 min + peak<0.2% — didn't reach half the BE trigger (0.4%) = no momentum
    long_hold_timeout_tier3_peak_pct: float = 0.2      # [2026-05-13 new] add a peak condition to tier3 too (drop the absolute time-cut)
    expectation_progress_exit_enabled: bool = True    # progress-based exit (replaces LHT time-cuts)
    expectation_progress_t1_min: float = 240.0          # T1: after N minutes
    expectation_progress_t1_pct: float = 30.0          # T1: cut if target progress < M%
    expectation_progress_t2_min: float = 480.0         # T2: after N minutes
    expectation_progress_t2_pct: float = 50.0          # T2: cut if target progress < M%
    expectation_progress_neg_cut_enabled: bool = True
    expectation_progress_neg_cut_pct: float = -50.0    # progress at or below this (e.g. -50 = 50% progressed against the target)
    expectation_progress_neg_cut_min: float = 30.0     # cut when held this long + above condition met
    entry_expectation_gate_enabled: bool = True       # ★ #1: block on RR/risk/reward threshold
    entry_expectation_min_rr: float = 1.0              # [2026-05-20 owner operation] spec 1.5 → owner relaxed to 1.0 (entry bar too high)
    breadth_strong_n: int = 8              # STRONG threshold: N/10 coins in unison = strong tsunami
    breadth_mid_n: int = 6                 # MID threshold: N/10 = medium tsunami
    breadth_aligned_strong: float = 12.0   # in-flow (follows the flow) STRONG bonus (opportunity) — owner "actively contrarian"
    breadth_aligned_mid: float = 6.0       # in-flow MID bonus
    breadth_counter_strong: float = -25.0  # against-flow (fights the flow = falling knife) STRONG penalty (block)
    breadth_counter_mid: float = -7.0      # against-flow MID penalty
    regime_counter_strong_cap_enabled: bool = True   # cap conviction in the against direction on STRONG against ON
    regime_counter_strong_cap: float = 50.0          # cap value (below scanner/guard threshold → no entry + score clearly drops)
    coin_decouple_enabled: bool = True          # per-coin decoupling SHORT release ON/OFF (2026-06-12 owner "default ON")
    coin_decouple_long_penalty: float = 12.0    # penalty on the against leg (knife-catching) when decoupled
    coin_decouple_min_strength: float = 0.5     # min coin 6TF conviction (0~1) — exclude weak wobble (avoid the rubber-ball)
    coin_decouple_btc_cache_sec: float = 120.0  # BTC 6TF direction cache TTL (sec) — computed once per scan
    mom_decouple_enabled: bool = False           # momentum-decouple conviction release ON/OFF (acts on real trades the moment it's on)
    mom_decouple_weight: float = 30.0            # W: conviction adjustment scale (inflection formula). ~30 to flip a 50-pt gap (sim)
    mom_decouple_cap: float = 35.0              # output clamp ±cap
    mom_decouple_base: float = 0.45             # base: baseline adjustment from position alone (momentum 0). same as inflection
    mom_decouple_up_thr: float = 0.40           # min |momentum up| — below this it's 'not a turn' (noise) and doesn't fire
    mom_decouple_div_thr: float = 0.20          # min coin divergence vs BTC momentum — exclude market-wide pullback (per-coin isolation)
    mom_decouple_pos_hi: float = 0.60           # SHORT-release position floor (top) — prevent bottom shorts
    mom_decouple_pos_lo: float = 0.40           # LONG-release position cap (bottom) — prevent top longs
    mom_decouple_btc_cache_sec: float = 60.0    # BTC 5m momentum cache TTL (sec) — computed once per scan
    macro_compass_enabled: bool = False             # default OFF (ON after paper validation)
    macro_recovering_conv_delta: float = 0.0        # RECOVERING LONG bonus / SHORT penalty size (default 0=paper observe)
    macro_recovering_require_di_adx: bool = True     # dead-cat defense: LONG bonus only with +DI>-DI flip + ADX≥threshold together
    macro_recovering_min_adx: float = 20.0          # min ADX to confirm recovery (trend alive)
    reversal_score: float = 10.0
    d1_trend_weight: float = 1.0   # D1 (daily) weight — big-picture direction (owner 2026-06-03)
    h4_trend_weight: float = 1.8   # H4 (4h) weight — tunable in UI (owner 2026-06-03)
    h1_trend_weight: float = 1.5
    m30_trend_weight: float = 1.2
    m15_trend_weight: float = 1.5
    m5_trend_weight: float = 1.0
    cr_speed_sign_guard_enabled: bool = False
    cr_blowoff_extreme_guard_enabled: bool = False
    cr_blowoff_extreme_ratio: float = 4.0   # speed/ATR above this = late-stage (lower = judges late-stage more often)
    cr_trend_agree_guard_enabled: bool = False
    cr_trend_agree_lookback: int = 20   # candle count for big-trend judgment (wider than 5 candles — whole-chart direction)
    breadth_dir_chg1h_pct: float = 0.3    # 1-hour change-rate threshold % (primary)
    breadth_dir_ema_pct: float = 0.10     # 5-min EMA spread threshold % (secondary)
    entry_flip_require_alignment: bool = True         # ★ #2: block if FLIP direction is against both H1+30M
    entry_auto_flip_enabled: bool = False              # ★ auto-FLIP permanently disabled after the ICP incident
    gap_check_atr_adaptive_enabled: bool = True    # volatility-adaptive ON (block top-chasing in a swinging market)
    gap_proximity_exit_enabled: bool = False   # exit on approaching top/bottom (default OFF)
    gap_proximity_exit_tf: str = "15"          # approach-exit basis TF: 5 / 15 / 30 / 60
    gap_proximity_exit_pct: float = 0.2        # approach threshold % (exit when within this)
    entry_volatility_gate_enabled: bool = True         # verify reach feasibility just before entry (default ON)
    entry_volatility_lookback_tf: str = "5"            # range-measurement TF (5-min bar)
    entry_volatility_lookback_bars: int = 12           # last N bars (12×5min = 1 hour)
    entry_volatility_min_reach_ratio: float = 0.6      # enter only if recent range/reward distance ≥ this (0.6 = need 60% of reward in volatility)
    trend_reversal_enabled: bool = False              # auto-close on H4 trend reversal (default OFF)
    bb_macd_sw_enabled: bool = False                  # SIDEWAYS BB-unfavorable + MACD-weakening auto-close (default OFF)
    bb_macd_sw_min_hold_hours: float = 2.0            # min hold to fire (h)
    bb_macd_sw_pnl_low: float = -2.0                  # fire pnl lower bound (%)
    bb_macd_sw_pnl_high: float = 0.5                  # fire pnl upper bound (%)
    caution_sideways_profit_secure_enabled: bool = False  # range + profit auto take-profit (default OFF)
    caution_min_hold_sec: float = 1800.0              # min hold to fire (sec, 30 min)
    caution_fee_rate: float = 0.00055                 # fee rate (taker per side)
    caution_min_profit_multiplier: float = 3.0        # min net profit = fee × N
    quick_tp_enabled: bool = False                    # time-based fast TP (default OFF, conflicts with Long Hold)
    quick_tp_min_hold_hours: float = 8.0              # min hold to fire (h)
    quick_tp_min_pnl_pct: float = 1.0                 # min pnl to fire (%)
    btc_crash_threshold_pct: float = -5.0             # BTC crash auto-close threshold (%, needs emergency_tp_tiers, default OFF)
    btc_emergency_pause_enabled: bool = True          # BTC sharp-move detection ON/OFF
    btc_emergency_pause_threshold_pct: float = 2.0    # [2026-04-26] default 2% — fires often, trader intuition
    btc_emergency_pause_window_min: float = 5.0       # [2026-04-26] default 5 min — fast reaction
    btc_emergency_mode: str = "trend_aligned"         # "trend_aligned" / "pause" / "close_all"
    btc_emergency_aggressive_entry: bool = True       # accelerate trend-direction entry on empty slots ON/OFF
    btc_emergency_aligned_duration_min: float = 120.0 # trend-alignment hold time (min, default 2h)
    min_sl_pct: float = 0.005                         # [2026-04-26 owner fix] min SL distance 0.5% (was 0.1% — entry==sl after rounding = instant-death incident)
    max_sl_distance_pct: float = 20.0                 # max SL distance (%, 99=effectively disabled)
    max_atr_pct: float = 5.0                          # ATR cap (%, protect volatile coins)
    cycle_min_rr: float = 1.0                         # min TP/SL RR ratio (1.0=guard disabled)
    thesis_invalidation_enabled: bool = False   # [2026-04-25 Long Hold System] structure-change cut OFF (recovery patience)
    thesis_invalidation_min_hold_h: float = 1.0 # min hold time (hours)
    thesis_invalidation_max_peak_pct: float = 0.3  # peak profit at or below this = "no progress"
    sl_dodge_enabled: bool = False            # SL-retreat disabled
    sl_dodge_proximity_pct: float = 1.5       # fire when within this % of SL
    sl_dodge_retreat_pct: float = 1.5         # 1.5% of price per retreat
    sl_dodge_max_count: int = 3               # max 3 retreats
    sl_dodge_max_total_pct: float = 5.0       # max total 5% retreat vs original SL
    day_direction_enabled: bool = True
    day_direction_hour_kst: float = 9.0         # evaluate daily at N:00 KST (default 09:00)
    day_direction_btc_adx_min: float = 18.0     # BTC H4 ADX < N → NEUTRAL (weak trend)
    day_direction_conv_delta: float = 5.0       # dominant direction conviction +N (opposite -N) — 0=observe only
    h4_pa_snapshot_enabled: bool = True
    h4_pa_snapshot_hours_kst: str = "1,5,9,13,17,21"  # CSV — KST hours at 4-hour intervals (H4 candle close times)
    morning_shield_enabled: bool = True        # 06:00 KST: tighten SL on profitable positions
    morning_guard_enabled: bool = False         # 07:00-09:30 KST: raise entry conviction
    morning_shield_lock_pct: float = 50.0      # profit ratio to preserve when profit >= 1% (%)
    morning_guard_conviction_boost: float = 20.0  # [2026-05-17 100pt ×10] 2→20. extra to morning conviction threshold
    morning_guard_end_hour_kst: float = 9.5    # Guard end time (9.5 = 09:30 KST)
    event_shield_enabled: bool = True          # event window: block new entry + tighten held SL
    event_shield_times_kst: str = ""           # event times CSV ("2026-06-10 21:30, 2026-06-11 03:00") — KST
    event_shield_window_min: float = 20.0      # window around event time (after the event = this value)
    event_shield_lead_min: float = 5.0         # [2026-06-08 owner] slippage lead — before the event use (window+lead) min = react ahead of the crowd (±20)
    event_shield_lock_pct: float = 70.0        # [2026-06-08 owner "stronger"] preserve ratio when profit≥1% / 0.3%↑ always BE
    event_shield_auto_fetch: bool = True       # [2026-06-08 owner] auto-fetch ForexFactory USD High impact (union with manual input)
    auto_tp_enabled: bool = False              # trailing harvest ON/OFF (OR with existing trailing/TP/SL)
    auto_tp_usdt: float = 1.0                  # arm threshold — when net profit exceeds this, arm 'protect this profit' (not the harvest line)
    auto_tp_peak_giveback_pct: float = 0.3     # after arming, harvest when net profit gives back this ratio off peak (0.3 = 30% giveback · owner "tightly")
    auto_sl_pct_enabled: bool = False          # auto-cut at N% loss (OR with existing SL · usually keep OFF)
    auto_sl_pct: float = 2.0                   # cut loss rate (%)
    dual_direction_observe: bool = True        # Phase 1 observe ON (no entry change · collect data only)
    gate_ledger_enabled: bool = True           # B: per-gate pass/reject tally ('why was it silent' control panel). Observe-only · doesn't touch entry · 100% local (no cross-server Tick). 2026-06-21 owner control panel — default ON (paper-observe principle, instrument for diagnosing 0 entries).
    dual_observe_auto_off_weak: bool = False   # C: auto-OFF observe on weak servers (RAM≤threshold) (lower F4 load). observe=record-only so entry unchanged.
    dual_direction_enabled: bool = False       # decide entry direction by two-way evaluation (OFF=signal direction = existing)
    erosion_guard_enabled: bool = True        # [2026-04-25 Long Hold System] peak-erosion cut OFF
    erosion_guard_peak_pct: float = 0.5        # min peak return (%) — must have reached this to fire
    erosion_guard_ratio: float = 0.3           # erosion ratio — fire if it falls to 30% or less of peak
    coin_repeat_brake_enabled: bool = True     # brake on repeated entry into the same coin
    coin_repeat_free_count: int = 0            # [2026-04-26 Long Hold default] cooldown immediately after the first
    coin_repeat_cooldown_base: float = 600.0   # brake base unit (sec) — 3-min steps
    coin_repeat_window_hours: float = 24.0     # count window (hours)
    sl_decay_enabled: bool = True             # [2026-04-25 Long Hold System] ★ must be OFF (opposite of Long Hold)
    sl_decay_2h_ratio: float = 0.7             # after 2h, SL distance to 70% of original
    sl_decay_3h_ratio: float = 0.5             # after 3h, SL distance to 50% of original
    coin_loss_cap_enabled: bool = True         # block entry when 24h cumulative loss exceeded
    coin_loss_cap_amount: float = 200.0        # [2026-04-26 Long Hold default] owner decision (coin-qualification spirit)
    coin_loss_cap_window_hours: float = 24.0   # rolling window (hours)
    per_coin_size_cap_enabled: bool = True
    per_coin_size_cap_pct: float = 30.0        # [2026-05-08 default] 30% of capital
    post_trade_pause_enabled: bool = True
    post_trade_pause_profit_sec: float = 300.0     # wait 5 min after take-profit
    post_trade_pause_loss_sec: float = 600.0       # wait 10 min after stop-loss (longer reflection) — legacy fallback
    post_trade_pause_fastreject_sec: float = 900.0 # wait 15 min after fast_reject (clear timing failure)
    post_trade_pause_loss_sliding_enabled: bool = True   # ON by default (owner decision)
    post_trade_pause_loss_tier1_pct: float = 0.5
    post_trade_pause_loss_tier1_sec: float = 60.0
    post_trade_pause_loss_tier2_pct: float = 2.0
    post_trade_pause_loss_tier2_sec: float = 300.0
    post_trade_pause_loss_tier3_pct: float = 5.0
    post_trade_pause_loss_tier3_sec: float = 1800.0
    post_trade_pause_loss_tier4_pct: float = 10.0
    post_trade_pause_loss_tier4_sec: float = 3600.0
    post_trade_pause_loss_tier5_sec: float = 14400.0     # ≥ tier4_pct (holding cell)
    consecutive_loss_pause_enabled: bool = False  # ★ [2026-06-06 owner] OFF by default — time-cut blocks recovery LONG (SOL conv96.9 paused case). SL/max_daily_sl handle loss runaway.
    consecutive_loss_pause_count: int = 5       # N consecutive losses (3→5 relaxed — less sensitive even when on)
    consecutive_loss_pause_min: int = 10        # pause M minutes (30→10 relaxed — short even when on)
    regime_direction_fail_enabled: bool = True
    regime_direction_fail_window_hours: float = 4.0  # regime window (default 4H = one H4 bar)
    regime_direction_fail_max: int = 3               # allowed failures (block that direction when exceeded)
    drawdown_shield_use_cash_only: bool = True
    drawdown_shield_caution_pct: float = 5.0    # cumulative drawdown CAUTION threshold (%)
    drawdown_shield_defend_pct: float = 10.0    # cumulative DEFEND threshold (%)
    drawdown_shield_crisis_pct: float = 20.0    # cumulative CRISIS threshold (%)
    drawdown_shield_caution_usd: float = 30.0   # daily drawdown CAUTION threshold ($)
    drawdown_shield_defend_usd: float = 60.0    # daily DEFEND threshold ($)
    drawdown_shield_crisis_usd: float = 100.0   # daily CRISIS threshold ($)
    drawdown_shield_caution_pen: float = -10.0  # CAUTION conviction penalty (negative)
    drawdown_shield_defend_pen: float = -20.0   # DEFEND penalty
    drawdown_shield_crisis_pen: float = -30.0   # CRISIS penalty
    dm_streak_block_hours: float = 1.0           # block time on reaching N losses (hours), 0=permanent
    dm_streak_block_opposite: bool = False       # [2026-04-25 owner "prevent stubbornness"] block opposite direction too on a streak. default OFF (keep SHORT-flip opportunity) / mirror of Profit Exit Block.
    direction_exhaustion_enabled: bool = True
    direction_exhaustion_window_sec: float = 900       # 15-min observation window
    direction_exhaustion_profit_count: int = 2         # N consecutive take-profits = exhausted (2 recommended)
    direction_exhaustion_block_sec: float = 1800       # 30-min hard block on that direction
    profit_exit_block_enabled: bool = True
    profit_exit_block_hours: float = 1.0               # [2026-04-25 default promotion] re-capture opportunity fast (12→1)
    profit_exit_block_min_pnl: float = 0.5             # exclude profit at or below this as noise (barely clears fees)
    profit_exit_block_min_consecutive: int = 3         # [2026-04-25 Long Hold System] relaxed to 4-win streak (3→4)
    profit_exit_block_block_opposite: bool = True     # opposite direction allowed by default (preserve FLIP opportunity)
    adx_slope_check_enabled: bool = False   # [2026-04-25 default promotion] culprit of 4-21 trade death (True→False) ★
    adx_slope_lookback_bars: int = 3                   # vs how many H4 bars ago (3 bars = 12 hours)
    adx_slope_decline_threshold_pct: float = 2.0       # skip if ADX dropped N%+ vs 3 bars ago (absorb noise)
    regime_transition_enabled: bool = False             # ★ 2026-05-11 owner decision — activate Phase K ("strong roots")
    regime_transition_paper_mode: bool = False         # ★ 2026-05-11 owner decision — live trading (paper validated, this agent 88% / sibling 94.4%)
    regime_transition_size_mult: float = 0.3           # week 1 floor 0.3 → week 2 cap 0.5 (this agent recommends Q4 FIXED CAP)
    regime_transition_tp_mult: float = 0.7             # ultra-short TP (harvest before regime hardens)
    regime_transition_sl_mult: float = 0.8             # tight SL (fast cut on regime misjudgment)
    regime_transition_adx_decline_ratio: float = 0.95  # adx_now < adx_peak_4h * 0.95 (5% drop)
    regime_transition_ema_gap_threshold_pct: float = 0.3  # BTC |EMA20-EMA50|/price < 0.3%
    regime_transition_min_conviction: float = 55.0     # [2026-05-17 100pt ×10] 8→80. above scanner_min_conviction
    regime_transition_min_mtf_align: int = 3           # 3+ of H4/H1/30M PA-direction aligned
    regime_transition_last_change_age_min: float = 180.0  # need 3h since regime change (prevent consecutive flips)
    regime_transition_daily_fail_limit: int = 3        # N daily failures → auto-OFF for 24h
    regime_transition_weekly_fail_limit: int = 5       # N weekly failures → OFF for 1 week (added by sibling)
    s3_gate_enabled: bool = True                       # [2026-04-25 default promotion] standardize Fee-Aware Gate (False→True)
    s3_gate_paper_mode: bool = False                   # [2026-04-25 default promotion] live real block (True→False)
    s3_gate_min_net_ev_usdt: float = 0.0               # block if net_ev <= this (default 0 = breakeven)
    s3_gate_fee_multiplier: float = 2.0                # fee × N safety margin (round-trip ×2)
    s3_gate_slippage_bps: float = 5.0                  # slippage estimate (basis points, 5bp = 0.05%)
    s3_gate_link_multiplier: float = 1.3               # LINK gambling-trait guard (threshold × 1.3)
    orderbook_depth_sizing_enabled: bool = False       # default OFF — owner must enable
    orderbook_depth_max_slippage_pct: float = 0.3      # count as "fillable" only up to quotes within this %
    orderbook_depth_min_fill_ratio: float = 0.5        # skip entry if capacity/intent < this ratio (too thin)
    fast_reject_v2_enabled: bool = False               # default OFF
    fast_reject_v2_max_sec: float = 30.0               # check within 30 sec of entry
    fast_reject_v2_peak_threshold_pct: float = 0.05    # peak < 0.05% (effectively 0)
    fast_reject_v2_pnl_pct: float = -0.05              # also satisfies pnl <= -0.05%
    reentry_cooldown_v2_enabled: bool = True           # ★default ON (2026-06-21 AXS rotation fix) — block re-entry into the just-closed coin for N min. Wired in _scan_and_maybe_enter.
    reentry_cooldown_v2_min: float = 45.0              # block re-entry into the same coin (market) for 45 min (2026-04-23 data: 96% further loss within 30 min of first close. 45 min = 96% defense + margin). Other coins free.
    pa_double_confirm_enabled: bool = False            # default OFF
    pa_double_confirm_window_sec: float = 60.0         # re-confirm same-direction PA within 60 sec
    regime_direction_lock_enabled: bool = False
    regime_direction_lock_freeze_sec: float = 3600.0   # 30-min freeze after regime change
    regime_direction_lock_neutral_block: bool = False   # block both directions when NEUTRAL (REST)
    regime_lock_use_slope: bool = False                 # check EMA20 slope (off = slope passes)
    regime_lock_use_distance: bool = False             # [2026-04-25 default promotion] relax distance (True→False)
    regime_lock_use_cross: bool = False                 # check EMA20 vs EMA50 cross (off = cross passes — core relaxation)
    imminent_flip_enabled: bool = True
    imminent_flip_ema_gap_pct: float = 0.3            # BTC |EMA20-EMA50|/price*100 threshold
    imminent_flip_use_30m: bool = True                # 30M secondary signal (False = H1 alone)
    imminent_flip_adx_rise_min: float = 2.0           # ADX rise vs the last lookback bars
    imminent_flip_gap_lookback: int = 3               # bars to compare gap-narrowing / ADX-rise
    same_coin_flip_cooldown_enabled: bool = True
    same_coin_flip_cooldown_min: int = 60             # 60 min (block TAO opposite entry after 47 min)
    raw_body_guard_enabled: bool = True
    raw_body_guard_lookback: int = 3                  # last 3 bars (5m)
    raw_body_guard_min_net_pct: float = 0.0           # net-strength threshold with >0 (0=sign only)
    momentum_deriv_guard_enabled: bool = True
    momentum_deriv_guard_tf: str = "5"                # 5m bar (same TF as raw_body)
    momentum_deriv_guard_lookback: int = 3            # 2026-05-19 peer-insight fix: 5→3 (25→15 min, dilute crash afterimages like 5/18 -3%). Corrects an inherent limit of time-based derivatives.
    momentum_deriv_guard_rsi_min_slope: float = 3.0   # 2026-05-19 peer-insight fix: 2.0→3.0 (block only strong against, resolve noise false positives)
    momentum_deriv_guard_macd_min_slope: float = 0.0  # MACD hist change (0=sign only)
    momentum_deriv_guard_require_both: bool = False    # 2026-05-18 fix: prevent MACD Δ≈0 noise false positives (ENAUSDT blocked 4×). Block only when both RSI+MACD are against.
    mtf_momentum_align_enabled: bool = True
    mtf_momentum_align_tfs: str = "240,60,30,15,5"    # [2026-05-21 owner] CSV 5-tier (H4=240, H1=60, 30M=30, 15M=15, 5M=5) — H1 60%-range is shallow → weight big-picture H4 + mid 15M consensus
    mtf_momentum_align_lookback: int = 3              # comparison window per TF
    mtf_momentum_align_min_aligned: int = 1           # 2026-05-19 peer-insight fix: 2→1 (let a strong H1 signal pass alone; resolve missing recovery signals like H1 +14.8 where 5/18 crash afterimages linger in TF30/TF5). Other guards (BB/momentum_deriv/microtiming/core) all live so weak spots don't pass alone.
    mtf_momentum_align_use_macd: bool = True          # include MACD too (False = RSI only)
    mtf_momentum_align_rsi_slope_thr: float = 0.5     # RSI Δ sign threshold (ignore small changes)
    cfid_enabled: bool = True
    cfid_tf: str = "60"                               # H1 (inflection points most stable on H1)
    cfid_ema_gap_thr_pct: float = 0.4                 # EMA20-50 gap / price * 100 threshold
    cfid_volume_spike_ratio: float = 1.5              # last N-bar vol avg / prior N-bar vol avg
    cfid_adx_change_min: float = 1.0                  # ADX change-rate absolute value
    cfid_lookback: int = 5                            # comparison window
    cfid_bypass_momentum_deriv: bool = True           # enable momentum_deriv BLOCK bypass
    cfid_bypass_mtf_align: bool = True                # enable mtf_momentum_align BLOCK bypass
    leading_entry_mode: str = "OFF"                   # "OFF" / "CFID" / "PATTERN"
    cfid_leading_min_strength: float = 70.0           # CFID strength threshold (1~100)
    cfid_leading_size_pct: float = 5.0                # entry size % of equity (small)
    cfid_leading_bypass_microtiming: bool = True      # bypass 5m gate
    cfid_leading_bypass_bb_regime: bool = True        # bypass BB_REGIME peak block
    pattern_leading_size_pct: float = 5.0             # entry size % of equity
    pattern_leading_min_5step_score: int = 6          # threshold of 5step (out of 12) (S5 retest inclusion recommended)
    pattern_leading_max_sr_pct: float = 1.0           # sr_near_S/R distance % (near support/resistance)
    pattern_leading_min_mtf_align: int = 2            # mtf_align aligned TF count (1~4)
    pattern_leading_bypass_microtiming: bool = True
    pattern_leading_bypass_bb_regime: bool = True
    phase6_combo_a_bonus: int = 25                    # combo A bonus
    phase6_combo_a_sr_min: int = 5                    # combo A: min sr_s (8=near only, 5=through mid)
    phase6_combo_a_mtf_min: int = 1                   # combo A: min mtf_s (actual mtf_s range ±2. 2=all 4 TFs aligned, 1=H1+30M aligned, 0=alignment-agnostic)
    phase6_combo_b_bonus: int = 35                    # combo B bonus
    phase6_combo_b_strength_min: int = 50             # combo B: min cfid_strength (70=strong only, 50=medium)
    phase6_combo_c_bonus: int = 15                    # combo C bonus
    phase6_combo_c_5step_min: int = 7                 # combo C: min 5step score (10=full, 7=strong spot)
    phase6_combo_d_bonus: int = 15                    # combo D bonus
    phase6_combo_d_news_abs_min: int = 6              # combo D: min |news_raw| (10=strong, 6=medium)
    phase6_combo_e_enabled: bool = True               # E: intuition bonus ON/OFF
    phase6_combo_e_bonus_base: int = 50               # E: base bonus (when confidence ≥ 80%)
    phase6_combo_e_bonus_max: int = 90                # E: max bonus (when confidence 0%)
    phase6_combo_e_rsi_overbought: float = 70.0       # E: RSI overbought threshold (SHORT trigger condition)
    phase6_combo_e_rsi_oversold: float = 30.0         # E: RSI oversold threshold (LONG trigger condition)
    phase6_combo_e_bb_high_pct: float = 99.0          # E: BB top threshold % (SHORT trigger condition)
    phase6_combo_e_bb_low_pct: float = 1.0            # E: BB bottom threshold % (LONG trigger condition)
    phase6_combo_f_enabled: bool = True               # F: bar-flow bonus ON/OFF
    combo_f_dedupe_enabled: bool = True               # remove combo_f direction double-count (F1 MTF·F2 M5) (2026-06-12 owner "default ON")
    phase6_combo_f_mtf_partial_bonus: int = 10        # F1: MTF partial-alignment bonus
    phase6_combo_f_mtf_full_bonus: int = 20           # F1: MTF full-alignment bonus
    phase6_combo_f_m5_dir_bonus: int = 15             # F2: M5 dominant_dir match bonus
    phase6_combo_f_m5_body_bonus: int = 15            # F3: M5 avg body satisfied bonus
    phase6_combo_f_m5_body_threshold: float = 0.3     # F3: M5 average body pct threshold (strong bar flow)
    charge_exit_enabled: bool = True                  # auto-close on score recovery ON/OFF
    charge_exit_min_pnl_pct: float = 0.0              # profit condition (trigger only above this pnl%). default 0 = pnl > 0
    charge_exit_conv_delta: float = 5.0               # conv recovery threshold (close when baseline + N rise)
    manual_entry_require_combo_f_pass: bool = False    # manual-entry combo F check ON/OFF
    manual_entry_combo_f_min: int = 20                 # combo F min score (10~50 recommended)
    bb_block_threshold_pct: float = 95.0              # LONG > this = hardblock (SHORT < 100-this = hardblock mirror)
    bb_penalty_threshold_pct: float = 85.0            # LONG > this = conv penalty (SHORT < 100-this = penalty mirror)
    bb_penalty_amount: float = 10.0                   # penalty amount (in 100-pt units)
    bb_block_trend_bypass_adx: float = 30.0          # ★ [2026-06-06 owner] ① ADX ≥ this = strong trend = BB wall-riding → bypass BB-extreme block (0=disabled). Lifts the "BB touch = reversal" rule of thumb in trending markets.
    bb_trend_bypass_require_di: bool = True           # ★ ② direction confirmed — for SHORT, wall-ride only with -DI>+DI (real downtrend). Filters out 'about to bounce (reversal)'. If DI unreadable, ① only.
    bb_trend_bypass_macd_min: float = 0.0            # ★ ③ MACD momentum allowance (0=disabled). If >0, wall-ride only when entry-direction MACD hist strength is at least this (accelerating) — tighten further.
    macro_exit_enabled: bool = False                 # default OFF (exit guard — ON after validation/sim). RISK_ON+holding SHORT / RISK_OFF+holding LONG = against
    macro_exit_breadth_min: int = 8                  # regime certainty — fire only on breadth STRONG (N/10 unison) (defend against false fire alarms)
    macro_exit_sl_cushion_pct: float = 0.15          # pull SL to this % from current price (nearest exit; breakeven on bounce, instant exit if it doesn't rise)
    macro_exit_strong_coin_exempt: bool = True       # per-coin-strength exemption ON (don't cut a profitable position even on macro-against)
    macro_exit_exempt_min_roe: float = 0.0           # exempt if profit exceeds this price-ROE% (default 0=any profit exempts)
    coin_state_machine_enabled: bool = True           # classify + log + stamp into entry dict
    coin_state_apply_conv_adjust: bool = False          # apply conviction adjustment (default OFF = after validation)
    coin_state_accel_conv_adj: float = 0.0              # [2026-05-17 100pt ×10] ACCEL adjustment (default 0)
    coin_state_steady_conv_adj: float = -10.0           # [2026-05-17 100pt ×10] -1→-10. STEADY adjustment
    coin_state_decel_conv_adj: float = -20.0            # [2026-05-17 100pt ×10] -2→-20. DECEL adjustment
    coin_state_flip_imminent_conv_adj: float = 10.0     # [2026-05-17 100pt ×10] +1→+10. FLIP_IMMINENT adjustment
    tight_trail_after_be_enabled: bool = True
    tight_trail_max_slippage_pct: float = 0.2         # cut if it drops N%p off peak (FLOOR = min threshold)
    tight_trail_min_peak_pct: float = 0.4             # apply only when peak is at least this (exclude small peaks)
    tight_trail_atr_adaptive_enabled: bool = True
    tight_trail_atr_tf: str = "5"                     # 5m ATR
    tight_trail_atr_period: int = 14
    tight_trail_atr_multiplier: float = 0.3           # atr_pct × 0.3 = adaptive slippage
    tight_trail_atr_cap_pct: float = 0.6              # cap (protect very volatile spots)
    trend_adaptive_exit_enabled: bool = False         # when ON, adapt the exit trail to coin ADX (acts on real trades)
    trend_adaptive_exit_adx_strong: float = 30.0      # ADX at/above = runner → relax trail (let it run)
    trend_adaptive_exit_adx_weak: float = 18.0        # ADX at/below = chopper → tighten trail (scalp)
    trend_adaptive_exit_runner_factor: float = 0.6    # runner factor (<1, preserve↓/slip↑ = let it run more)
    trend_adaptive_exit_chop_factor: float = 1.4      # chopper factor (>1, preserve↑/slip↓ = bank it fast)
    trend_adaptive_exit_adx_cache_sec: float = 30.0   # coin ADX cache TTL (sec) — avoid fetch every tick
    tf_round_tpsl_enabled: bool = True               # explicit force ON (incl. LIVE). default decided by auto_paper
    tf_round_anchor_tf: str = "240"                   # anchor/trade TF (H4 = textbook primary)
    tf_round_atr_period: int = 14
    tf_round_tp_atr_mult: float = 1.0                 # TP1 = ATR × 1.0 (=100% round)
    tf_round_tp2_atr_mult: float = 2.0                # TP2 = ATR × 2.0 (memory note body — H4 TP1 $15 / TP2 $30, owner correction 2026-05-28: 4441+30=4471)
    tf_round_sl_ratio: float = 0.333                 # SL distance = TP1 × ⅓ (RR 1:3)
    tf_round_anchor_lookback: int = 2                 # kline fetch headroom
    tf_round_anchor_offset: int = 0                   # anchor = the forming H4 candle right after PA decision (memory note original, owner correction 2026-05-28: yesterday's 0→1 fix dropped)
    tf_round_hold_enabled: bool = True               # hold (short-cut off) — accompanies mode ON
    frame_guard_enabled: bool = True
    frame_guard_range_tf: str = "240"                # range basis TF (240=H4, "D"=Daily)
    frame_guard_range_bars: int = 6                  # last N bars (H4 6 bars = 24h rolling)
    frame_guard_long_max_pos: float = 0.50           # max allowed LONG position (0.5=lower 50%)
    frame_guard_option_b_enabled: bool = True           # explicit force ON (incl. LIVE)
    frame_guard_long_max_pos_b: float = 0.60             # option B LONG (0.6=up to lower 60%)
    frame_guard_trend_aligned_long_max_pos: float = 0.70    # strong-trend LONG (0.7=up to lower 70%)
    frame_guard_trend_slope_pct: float = 1.5             # n-bar change-rate basis (24h 1.5%+ = strong trend)
    frame_guard_cooldown_enabled: bool = True
    frame_guard_cooldown_sec: float = 90.0           # silent-skip period for the same (market, direction) after a block
    h4_pulse_only_enabled: bool = True              # explicit force ON (incl. LIVE)
    h4_pulse_window_min: int = 60                    # entry-allowed minutes after H4 close [2026-05-27 30→60 — multi-coin + time offset + test frequency, review 45 if noisy]
    preclose_entry_enabled: bool = False             # ON/OFF (owner must enable to fire)
    preclose_min_elapsed_pct: float = 88.0           # H4 forming-bar elapsed-rate threshold (88% = from ~29 min before close)
    preclose_size_ratio: float = 0.5                 # ratio vs regular size (before close confirmation = half)
    preclose_wick_ratio_min: float = 1.5             # pin-bar acceptance: (wick opposite the direction)/body ≥ this
    preclose_body_dir_required: bool = True          # use body direction + close position (top/bottom 30%) condition
    preclose_max_per_day: int = 5                    # daily pre-entry cap (separate counter)
    preclose_min_conviction: float = 50.0            # pre-entry qualification — base conviction floor (soft-score time = pre-guard, so base basis)
    preclose_topup_enabled: bool = False             # close-confirmation top-up ON/OFF
    preclose_topup_min_pnl_pct: float = 0.0          # confirmation basis — pnl ≥ N% after H4 close (default 0 = anything but a loss)
    preclose_topup_max_chase_pct: float = 1.0        # cancel top-up if price advanced favorably over N% (prevent late chase)
    preclose_topup_require_candle_dir: bool = True   # top-up only if the last closed H4 bar is the entry direction
    preclose_topup_grace_min: float = 60.0           # top-up-allowed window after H4 close (min) — expires past it (keep half)
    anchor_fasttrack_enabled: bool = True           # explicit force ON (incl. LIVE)
    anchor_fasttrack_max_proximity: float = 0.33     # within ⅓ of TP1 distance = fast-track fires
    pa_completion_enabled: bool = True              # explicit force ON (incl. LIVE)
    pa_completion_huikkang_min_ratio: float = 1.5    # ไส้หลัง body ≥ prior average × this ratio
    pa_completion_lookback_bars: int = 3             # compute prior N-bar body average for ไส้หลัง (3 bars)
    pa_completion_sig_max_ratio: float = 1.0         # Sig body ≤ ไส้หลัง body × this ratio (1.0 = must be smaller than ไส้หลัง)
    guard_score_pa_completion_ok: float = 30.0       # PA Pat 1/2/3 complete (Sig + ไส้หลัง) ⭐ owner's core
    guard_score_pa_completion_none: float = -10.0    # no PA pattern (5-28 owner relaxed: -25 → -10, auto-penalizing every time is excessive)
    guard_score_d1_pa_ok: float = 25.0               # D1 PA formed + direction match (big picture)
    guard_score_d1_pa_none: float = -5.0             # no D1 PA or direction mismatch (5-28 owner relaxed: -15 → -5, D1 PA itself is rare)
    guard_score_btc_aligned: float = 15.0            # BTC day_direction match (LONG+LONG / SHORT+SHORT)
    guard_score_btc_opposite: float = -15.0          # against BTC direction
    guard_score_adx_strong: float = 10.0             # ADX ≥ 30 (strong trend)
    guard_score_adx_weak: float = -5.0               # ADX < 20 (weak trend)
    guard_score_adx_strong_requires_trend: bool = False
    guard_score_vol_big_align: float = 10.0          # volume big + direction match (2x+ vs 5-min average)
    guard_score_trend_high_conf: float = 10.0        # H4 trend confidence ≥ 75% (strong trend)
    guard_score_trend_low_conf: float = -5.0         # H4 trend confidence < 50%
    guard_score_rsi_extreme: float = 10.0            # 5M RSI extreme + inflection (LONG: <30+rising / SHORT: >70+falling)
    final_30m15m_check_enabled: bool = True          # block entry if both 30M+15M are against
    final_30m15m_bypass_conviction: float = 55.0      # exempt final_30m15m block at this conviction or above (0=OFF)
    final_30m15m_bypass_include_regime: bool = False  # True=include macro-against in score absorb / False=exclude (existing)
    final_d1_bypass_conviction: float = 50.0          # exempt final_d1 block at this conviction or above (0=OFF, e.g. 78)
    final_5m_simple_check_enabled: bool = True       # check 5M RSI/MACD/BB agreement with entry direction
    final_5m_simple_min_score: int = 2               # pass if N+ of 3 agree (max 3)
    final_5m_bb_trend_bypass_enabled: bool = False
    final_d1_alignment_check_enabled: bool = True    # block entry on D1 against-direction
    final_align_regime_override_enabled: bool = True   # when macro is certain, final alignment gate prefers macro direction (default ON)
    final_d1_recent5_override_enabled: bool = False
    final_d1_recent5_drop_pct: float = 1.0   # if last 5 daily bars change ≤ -this(%), ignore UPTREND label and pass SHORT (e.g. 1.0)
    d1_reality_demote_enabled: bool = False
    d1_reality_demote_drop_pct: float = 1.0   # if last 5 daily bars change ≤ -this(%), demote UPTREND→SIDEWAYS (e.g. 1.0)
    entry_guard_set: str = "green"   # green / yellow / both / minimal
    exit_guard_set: str = "green"    # green / yellow / both / minimal
    exit_5m_emergency_enabled: bool = True          # explicit force ON (incl. LIVE)
    exit_5m_rsi_overbought: float = 70.0             # LONG exit RSI threshold (overbought)
    exit_5m_rsi_oversold: float = 30.0               # SHORT exit RSI threshold (oversold)
    exit_5m_bb_top_pct: float = 90.0                 # LONG exit BB position threshold (top)
    exit_5m_bb_bottom_pct: float = 10.0              # SHORT exit BB position threshold (bottom)
    exit_5m_min_score: int = 2                       # exit if N of 3 (RSI/MACD/BB) satisfied
    guard_score_h4_pulse_in: float = 20.0            # inside H4 pulse window (60 min after close)
    guard_score_h4_pulse_out: float = -3.0           # outside H4 pulse window (5-28 owner relaxed: -10 → -3, time-spot 25% met — outside is normal)
    guard_score_h1_pa_in: float = 15.0               # H1 PA pulse pass (inside window + PA recognized)
    guard_score_h1_pa_out: float = -2.0              # H1 PA pulse not passed (5-28 owner relaxed: -5 → -2, H1 PA itself is rare)
    guard_score_frame_aligned: float = 15.0          # Frame Guard trend aligned (strong UPTREND+LONG etc)
    guard_score_frame_neutral: float = 5.0           # Frame Guard neutral spot (passes B default)
    guard_score_frame_opposite: float = -20.0        # Frame Guard opposite side (top/bottom picking)
    regime_align_cap_enabled: bool = False           # cap on the sum of trend-alignment (Frame+Trend+AltBTC) ON/OFF (cap=relax · dedupe=remove entirely)
    regime_align_cap: float = 15.0                   # sum clamp limit (±value). adjust after observing SHORT avg 32→? via guard_eval
    guard_dir_dedupe_enabled: bool = True            # remove direction double-count (Frame/Trend/AltBTC/BTC-align) on the guard side (2026-06-12 owner "default ON")
    guard_score_anchor_close: float = 20.0           # Anchor proximity ≤ 0.33 (cycle start)
    guard_score_anchor_far: float = -10.0            # Anchor proximity > 1.0 (round missed)
    guard_score_day_box_edge: float = 10.0           # near Day Box edge (reversal spot)
    guard_score_day_box_inside: float = -8.0         # inside Day Box lock box (5-28 owner relaxed: -15 → -8)
    guard_score_microtiming_ok: float = 10.0         # microtiming 5M trigger met
    guard_score_microtiming_no: float = -5.0         # microtiming 5M trigger not met
    guard_score_raw_body_align: float = 5.0          # raw_body 3-bar direction matches entry
    guard_score_raw_body_against: float = -8.0       # raw_body 3-bar direction against (5-28 owner relaxed: -15 → -8)
    guard_score_momentum_deriv_align: float = 5.0    # momentum_deriv RSI/MACD match
    guard_score_momentum_deriv_against: float = -5.0   # momentum_deriv against (5-28 owner relaxed: -10 → -5)
    flow_reversal_signal_enabled: bool = False         # paper auto_paper=True, LIVE auto OFF
    flow_reversal_signal_auto_paper: bool = True       # auto ON in paper mode
    flow_reversal_bonus_full: float = 30.0             # 5/5 conditions met (strong signal)
    flow_reversal_bonus_strong: float = 20.0           # 4/5 conditions
    flow_reversal_bonus_medium: float = 10.0           # 3/5 conditions
    flow_reversal_conf_decline_pct: float = 0.25       # Confidence 25%+ drop = weakening
    flow_reversal_adx_decline_pct: float = 0.25        # ADX 25%+ drop = weakening
    flow_reversal_lookback_samples: int = 6            # compare vs 5 min ago (30s scan × 6 = 180s ≈ 3-min lookback min)
    alt_btc_alignment_enabled: bool = True             # always evaluate (safe)
    alt_btc_aligned_bonus: float = 10.0                # alt + BTC same direction = worth holding
    alt_btc_opposite_penalty: float = -10.0            # alt - BTC opposite = recommend a quick exit
    day_box_guard_enabled: bool = True              # explicit force ON (incl. LIVE)
    day_box_window_hours: float = 4.0                # box-formation time from 09:00 KST
    day_box_lock_min_hours: float = 3.5              # ping-pong judgment possible after this point (block incomplete lock)
    day_box_max_atr_ratio: float = 0.8               # box range / day_h4_atr_pct ≤ this = ping-pong candidate
    day_box_min_touches: int = 2                     # N+ touches at both poles = ping-pong confirmed
    day_box_touch_eps_pct: float = 0.05              # pole-proximity epsilon ε (in %, 0.05 = 0.05%)
    day_box_edge_pct: float = 0.05                   # top/bottom 5% zone = "near" (SHORT top 95%+/LONG bottom 5%-)
    day_box_breakout_pct: float = 0.10               # box-breakout judgment (in %, 0.1 = 0.1%)
    h1_pa_pulse_enabled: bool = True                # explicit force ON (incl. LIVE)
    h1_pa_pulse_window_min: int = 15                 # entry-allowed minutes after H1 close (≈ H1 ¼)
    h1_pa_pulse_lookback_bars: int = 2               # recognize H1 PA within the last N bars (incl. forming)
    h1_pa_pulse_min_confidence: float = 0.5          # min PASignal.confidence (0.0~1.0)
    h1_pa_pulse_require_day_dir: bool = True         # force H4 day_direction alignment (NEUTRAL = passes)
    regime_lock_mode: str = 'OFF'                      # [2026-04-25 default promotion] Scanner Breadth method (B11→B12)
    b12_threshold_n: int = 6                            # need N+ to agree to decide direction (default 75% of 8)
    b12_window_sec: float = 1200.0                     # vote-tally window (last 20 min — 2026-04-23 data analysis: avg incident 19.5 min, median 18.3 min match)
    coin_reentry_penalty_enabled: bool = True
    coin_reentry_penalty_window_sec: float = 900       # 15-min window
    coin_reentry_penalty_per_count: float = 10.0       # [2026-05-17 100pt ×10] 1→10. conviction -10 accrued per re-entry
    fast_reject_enabled: bool = True           # [2026-04-25 Long Hold System] 5~15-min cut OFF (recovery patience)
    fast_reject_min_sec: float = 600.0              # 5-min min wait (prevent noise)
    fast_reject_max_sec: float = 1500.0              # delegate to trend/thesis after 15 min
    fast_reject_peak_threshold_pct: float = 0.15    # if peak below this, "never lifted off"
    fast_reject_trigger_pnl_pct: float = -0.5       # current pnl must be at or below this to fire (%)
    entry_quality_enabled: bool = True              # master switch
    eq_momentum_enabled: bool = True
    eq_momentum_count: int = 2                      # check last N bars
    eq_momentum_min_agree: int = 1                  # min K bars agree
    eq_bb_enabled: bool = True
    eq_bb_upper_pct: float = 80.0                   # block LONG: BB% > this (pullback buy)
    eq_bb_lower_pct: float = 20.0                   # block SHORT: BB% < this (bounce sell)
    eq_nbar_enabled: bool = True
    eq_nbar_count: int = 5                          # check bar count
    eq_nbar_min_ratio: float = 0.6                  # must be ≥ HH(or LH) ratio to pass
    manual_exit_penalty_enabled: bool = True
    manual_exit_penalty_hours: float = 0.0          # cooldown on loss exit (hours)
    session_profile_enabled: bool = True    # [2026-04-25 default promotion] standardize time-of-day ± (False→True)
    sess_quiet_start_kst: float = 1.0          # 01:00 KST start
    sess_quiet_end_kst: float = 6.0            # 06:00 KST end
    sess_quiet_delta: float = -10.0            # [2026-05-17 100pt ×10] -1→-10. quiet-window conviction penalty
    sess_active_start_kst: float = 21.0        # 21:00 KST start
    sess_active_end_kst: float = 24.0          # 24:00 KST end
    sess_active_delta: float = 10.0            # [2026-05-17 100pt ×10] 1→10. active-window conviction bonus
    direction_memory_enabled: bool = False  # old "prevent ETH 3-loss streak" → replaced by pulse/Frame Guard
    dm_window_count: int = 4                   # check last N
    dm_lookback_days: float = 3.0              # max lookback days
    dm_loss_count_penalty: int = 3             # penalty if K of N are losses
    dm_loss_count_delta: float = -5.0         # [2026-05-17 100pt ×10] -2→-20. penalty size
    dm_streak_block_enabled: bool = False      # old "standardize Hard Block" → dropped (misses PA-pulse spots)
    dm_streak_block: int = 2                   # [2026-04-25 Long Hold System] 4-loss streak → block (3→4, holding anyway)
    dm_cache_ttl_sec: float = 180.0            # 3-min cache (ease journal-scan load)
    btc_regime_enabled: bool = True         # [2026-04-25 default promotion] standardize BTC against-direction penalty (False→True)
    btc_regime_ema_long: int = 50
    btc_regime_trans_band_pct: float = 1.0     # price within EMA50 ±1% = TRANS candidate
    btc_regime_slope_flat_thr_pct: float = 0.3 # EMA20 slope within ±this % = judged flat
    btc_regime_cache_ttl_sec: float = 600.0    # 10-min cache
    btc_regime_bull_long_delta: float = 10.0     # BTC BULL + LONG → no bonus (each on its own)
    btc_regime_bear_long_delta: float = -20.0   # BTC BEAR + LONG → against penalty
    btc_regime_trans_delta: float = -10.0         # BTC TRANS → transition uncertainty = no penalty
    market_bias_enabled: bool = False       # old "fade the crowd" → dropped (PA pulse decides entry direction)
    mb_lookback_trades: int = 12               # last N trades
    mb_lookback_hours: float = 6.0             # max time range
    mb_dominance_threshold: float = 0.5        # crowd-skew basis (0~1)
    mb_min_total: int = 4                      # min sample count
    mb_against_delta: float = -3.0            # [2026-05-17 100pt ×10] -1→-10. penalty when entering against
    mb_cache_ttl_sec: float = 180.0            # 3-min cache
    pair_block_enabled: bool = True           # ★ OFF by default — when active, block the opposite direction
    pair_block_mode: str = 'conservative'        # "aggressive" | "conservative"
    pair_block_same_limit: int = 3             # max N same-direction within a group in aggressive mode
    coin_profit_lockin_enabled: bool = True   # ★ OFF by default
    coin_profit_lockin_window_hours: float = 4.0   # cumulative-calc window
    coin_profit_lockin_min_realized: float = 30.0  # min realized profit ($). Below this, Lock-in not applied
    coin_profit_lockin_protect_ratio: float = 0.7  # preserve-line ratio (70% = allow giving back only 30% of cumulative profit)
    coin_profit_lockin_require_be: bool = True     # activate only after BE lock (avoid H4-strategy conflict)
    pa_weight_enabled: bool = True             # ON by default (owner decision)
    pa_weight_pin_bar: int = 2                 # PIN_BAR (short-term signal, possible noise)
    pa_weight_engulfing: int = 3               # ENGULFING (2-bar reversal)
    pa_weight_star_v1: int = 5                 # STAR_V1 (3-bar reversal, strong)
    pa_weight_star_v2: int = 5                 # STAR_V2 (3-bar variant, same tier)
    pa_weight_squeeze_break: int = 3           # SQUEEZE_BREAK (volatility breakout)
    pa_weight_bos: int = 3                     # BOS_BULLISH/BEARISH (structure break)
    pa_weight_zone_bonus: int = 2              # Zone proximity extra bonus
    pa_zone_proximity_atr: float = 0.5         # within 0.5 ATR of zone = "near"
    pa_location_penalty_far: float = 0.5       # PA score × 0.5 when over 0.5 ATR from zone (Thai original emphasis)
    # END_FUTURES_672_MIRROR
    # ── base conviction (spot_conviction.py) component toggles — restore the 5 present in futures but missing from the 672 core ──
    #   default True (all active). For turning off individual components via cfg/UI. spot_conviction reads them via getattr.
    phase4_rsi_enabled: bool = True
    phase4_mtf_matrix_enabled: bool = True
    phase4_change_rate_enabled: bool = True
    phase4_sr_position_enabled: bool = True
    phase4_volume_pattern_enabled: bool = True
    # config migration version — prevents stale values in old runtime from overwriting new defaults (_load_state).
    config_version: int = 12                    # v12: gate_ledger control panel default ON ('why was it silent' · observe-only). v11: candle-timing gates OFF + ADX relaxed. v10: paper→False Live switch. v9: micro_1m_body_min_pct 0.05. v8: futures-ON guards default ON. v7: guard_score 45→50.


@dataclass
class SpotGazuaPosition:
    market: str
    direction: str                    # always "LONG" (spot)
    entry_price: float
    qty: float
    tp1: float
    tp2: float
    sl: float
    atr_used: float
    entry_ts: float
    partial_done: bool = False
    trailing_high: float = 0.0
    krw_spent: float = 0.0
    paper: bool = False
    order_uuid: str = ""
    close_retry_count: int = 0
    tp1_order_uuid: str = ""   # server-side limit-sell (half) order ID
    tp2_order_uuid: str = ""   # server-side limit-sell (remainder) order ID
    longhold_active: bool = False    # §4.2 switched to longhold (SL sell on hold)
    longhold_since_ts: float = 0.0   # longhold-switch time (basis for max_hold cap)
    last_peak_ts: float = 0.0        # last trailing_high update time (for measuring be_stall stall)
    manual: bool = False             # quick-trade manual buy (excluded from bot auto-management in hands-off mode — human closes)
    source: str = "FOCUS"            # entry source — "FOCUS"(trend) / "CONTRARIAN"(against). For separate slot count and UI badge
    dca_count: int = 0               # §4 averaging-down execution count (basis for pyramiding/step limit)
    dca_initial_entry: float = 0.0   # initial entry price (basis for averaging depth/absolute floor — fixed, separate from avg cost)
    dca_base_krw: float = 0.0        # initial entry principal (base for add size; unchanged even as avg cost drops)
    dca_last_ts: float = 0.0         # epoch of the last averaging add — enforces dca_min_gap_sec spacing

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SpotGazuaPosition":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


class SpotGazuaManager:
    """Upbit spot long-only FOCUS — standalone manager."""

    _quote_currency = "KRW"   # quote currency — key for balance/budget lookup. Bybit spot overrides to USDT.

    def __init__(self, system: Any = None, client: Any = None, *, state_path: Optional[str] = None):
        self.system = system
        self._lock = threading.RLock()

        if client is None:
            from app.integrations.upbit_trade import UpbitTradeClient
            client = UpbitTradeClient(
                os.getenv("UPBIT_ACCESS_KEY", ""), os.getenv("UPBIT_SECRET_KEY", "")
            )
        self.client = client

        self.config = SpotGazuaConfig()
        # ★ via-A(SLArbiter) switch (DESIGN_A §A.4). OFF=0 change to the current direct-SL behavior.
        #   Route via A if either env FOCUS_SL_ARBITER_ENABLED or longhold_enabled is on.
        self._sl_arbiter_on = str(os.getenv("FOCUS_SL_ARBITER_ENABLED", "")).strip().lower() in (
            "1", "true", "yes", "on"
        )
        self.state = FocusState.IDLE
        self.positions: List[SpotGazuaPosition] = []
        self.daily_plans_used = 0
        self.daily_sl_count = 0
        self._day_stamp = ""
        self.last_scan_ts = 0.0
        self._last_contra_scan_ts = 0.0   # CONTRARIAN against-scan timer (separate from FOCUS)
        self.cooldown_until = 0.0
        self._recent_exit: Dict[str, float] = {}   # market -> last close ts (basis for re-entry cooldown v2)
        self._paper_seq = 0
        # ★ [2026-06-21 owner] near-miss block control — record gate blocks after a guard_score pass.
        #   Mirrors futures focus_manager._record_near_miss. Spot is long-only (no SHORT) so 'shield' only:
        #   a blocked buy rising afterward (regrettable block) = over-block signal. deque=recent in memory, /nearmiss judges after the fact.
        self._recent_near_miss: deque = deque(maxlen=30)
        self._nm_enrich_box: Dict[str, Any] = {"ts": 0.0, "data": None}   # enrichment 25s response cache (avoid the kline wall)

        if state_path is None:
            try:
                from app.core.runtime_paths import RuntimePaths
                state_path = RuntimePaths(exchange="upbit").custom("upbit_focus_config.json")
            except Exception:
                state_path = os.path.join("runtime", "upbit", "upbit_focus_config.json")
                os.makedirs(os.path.dirname(state_path), exist_ok=True)
        self.state_path = state_path
        # trade journal (JSONL) — for the Overall Status / Trade Journal widgets
        self.journal_path = os.path.join(os.path.dirname(state_path), "upbit_focus_journal.jsonl")
        # ★ [2026-06-20] journal-dedicated lock — serialize write(append)/delete(rewrite)/read.
        #   Previously no lock/fsync, so concurrent closes, dashboard deletes, or 2-process appends corrupted the file (fragment records = missing entries).
        #   Reusing self._lock (the heavy tick RLock) would block deletes behind the 16-coin scan, so a separate lightweight Lock is used.
        self._journal_lock = threading.Lock()
        # account-summary TTL cache (so status polling doesn't hit accounts() every time)
        self._acct_cache: Dict[str, Any] = {}
        self._acct_cache_ts = 0.0

        # ★ [2026-06-21] GateLedger — "why was it silent today" (per-gate pass/reject) spot control panel.
        #   Reuses futures focus_gate_ledger.GateLedger as-is (exchange-agnostic · observe-only · doesn't touch entry by a single byte).
        #   ★ 100% local — no cross-server Tick (exchange Tick is sacrosanct). Persisted per-exchange under runtime
        #     (spot_gate_stats.json) → 0 conflict for 3 spot instances on one box. Records only when config.gate_ledger_enabled.
        self._gate_ledger = None
        try:
            from app.manager.focus_gate_ledger import GateLedger
            _gl_flush = float(os.getenv("FOCUS_GATE_LEDGER_FLUSH_SEC", "60") or "60")
            _gl_path = os.path.join(os.path.dirname(self.state_path), "spot_gate_stats.json")
            self._gate_ledger = GateLedger(flush_path=_gl_path, flush_sec=_gl_flush)
        except Exception as _gl_exc:
            logger.debug("[SPOT_GAZUA] GateLedger init skipped: %s", _gl_exc)

        self._load_state()
        # ★ scan_exclude exchange consistency — the shared default (KRW-APENFT) leaks to USDT exchanges (Bybit spot) → clean up.
        #   Markets that don't match this exchange's quote currency (e.g. a KRW- market on a USDT exchange) are harmless but confusing → remove.
        if self._sanitize_scan_exclude():
            self._save_state()
        # On migration, persist the new values immediately (v1 from next restart — prevent re-firing)
        if getattr(self, "_migrated_v1", False):
            self._save_state()

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def _exclude_market_ok(self, mk: str) -> bool:
        """Is this in this exchange's quote-currency market format (USDT suffix / KRW- prefix)?"""
        m = str(mk).upper()
        return m.endswith("USDT") if self._quote_currency == "USDT" else m.startswith("KRW-")

    def _sanitize_scan_exclude(self) -> bool:
        """Remove markets in scan_exclude that don't match this exchange. True if changed."""
        raw = str(self.config.scan_exclude or "")
        if not raw.strip():
            return False
        kept = [m.strip() for m in raw.split(",") if m.strip() and self._exclude_market_ok(m.strip())]
        new = ",".join(kept)
        if new != raw:
            logger.info("[SPOT_GAZUA] scan_exclude reconcile(%s): %r → %r", self._quote_currency, raw, new)
            self.config.scan_exclude = new
            return True
        return False

    # ── main tick ───────────────────────────────────────────
    def tick(self, btc_price: float = 0.0) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"state": "DISABLED"}
        with self._lock:
            self._maybe_reset_daily()
            self._manage_all_positions()

            now = time.time()
            if now < self.cooldown_until and not self.positions:
                self.state = FocusState.COOLDOWN
                return {"state": self.state.value, "positions": 0}

            # ★ Manual (quick-trade) positions don't consume bot slots — "regardless of slot count" (owner 2026-06-17).
            #   Bot auto-entry slots count bot positions only → manual holdings don't block bot scanning.
            #   FOCUS slots = bot entries excluding manual and CONTRARIAN (against has its own slot).
            bot_positions = sum(1 for p in self.positions
                                if not getattr(p, "manual", False)
                                and getattr(p, "source", "FOCUS") != "CONTRARIAN")
            can_scan = (
                bot_positions < self.config.max_positions
                and self.daily_plans_used < self.config.max_daily_plans
                and now - self.last_scan_ts >= self.config.scan_interval_sec
            )
            if can_scan:
                self.last_scan_ts = now
                self.state = FocusState.SELECTING
                self._scan_and_maybe_enter()

            # ★ CONTRARIAN 2nd entry source — separate slot/budget/regime-gate. FOCUS path untouched.
            #   Doesn't touch the conviction-1-shot FOCUS (separate slot). OFF in uptrend (neutral/down = FOCUS-churn markets only).
            if self.config.contrarian_enabled:
                contra_positions = sum(1 for p in self.positions
                                       if getattr(p, "source", "FOCUS") == "CONTRARIAN")
                if (contra_positions < self.config.contrarian_max_positions
                        and self.daily_plans_used < self.config.max_daily_plans
                        and now - self._last_contra_scan_ts >= self.config.scan_interval_sec):
                    self._last_contra_scan_ts = now
                    self._scan_contrarian_and_maybe_enter()

            return {
                "state": self.state.value,
                "positions": len(self.positions),
                "daily_plans_used": self.daily_plans_used,
            }

    # ── scan + entry ─────────────────────────────────────────
    def _ledger_record(self, market: str, gate: str, passed: bool) -> None:
        """Tally one gate (observe-only, doesn't touch the entry flow). Records only when config.gate_ledger_enabled.
        Never propagates exceptions — the control panel must not break entry/exit."""
        try:
            if self._gate_ledger is not None and getattr(self.config, "gate_ledger_enabled", False):
                self._gate_ledger.record(str(market or "-"), str(gate or "-"), passed=bool(passed))
        except Exception:
            pass

    def _scan_and_maybe_enter(self) -> None:
        # ★ GateLedger callback — only when ON does selector→scanner tally per-coin gate pass/reject.
        #   When OFF, pass None → 0 extra cost on the scan path (0 behavior change).
        _rec = (self._ledger_record
                if (self._gate_ledger is not None and getattr(self.config, "gate_ledger_enabled", False))
                else None)
        try:
            from app.manager.spot_focus_coin_selector import select_spot_focus_coin
            result = select_spot_focus_coin(
                self.system, self.client,
                primary_tf=self.config.primary_tf, top_n=self.config.top_n,
                min_conf=self.config.min_conf, exclude=self.config.scan_exclude,
                headroom_gate_pct=self.config.headroom_gate_pct,
                overext_range_pos_pct=self.config.overext_range_pos_pct,
                overext_min_move_pct=self.config.overext_min_move_pct,
                blowoff_move_pct=self.config.blowoff_move_pct,
                guard_score_mode_enabled=self.config.guard_score_mode_enabled,
                guard_score_threshold=self.config.guard_score_threshold,
                guard_score_total_cap=self.config.guard_score_total_cap,
                block_warning=self.config.block_warning_coins,
                block_caution=self.config.block_caution_coins,
                record=_rec,
            )
        except Exception as exc:
            logger.warning("[SPOT_GAZUA] scan error: %s", exc)
            self.state = FocusState.IDLE
            return

        if not result:
            self.state = FocusState.IDLE
            return

        market = result.get("market", "")
        if not market or any(p.market == market for p in self.positions):
            self.state = FocusState.IDLE
            return

        # ★ Re-entry cooldown (v2) — block re-entry into the *same coin* just closed (prevent rotation). default OFF (opt-in).
        #   Other coins free → 0 hit to entry frequency. 2026-04-23 data: re-entry within N min of first close is the core of rotation.
        if getattr(self.config, "reentry_cooldown_v2_enabled", False):
            _ex_ts = self._recent_exit.get(market, 0.0)
            _cd_sec = float(getattr(self.config, "reentry_cooldown_v2_min", 45.0) or 0.0) * 60.0
            if _ex_ts > 0 and _cd_sec > 0 and (time.time() - _ex_ts) < _cd_sec:
                logger.info("[SPOT_GAZUA] %s re-entry cooldown — closed %.0f min ago (<%.0f min), skip",
                            market, (time.time() - _ex_ts) / 60.0, _cd_sec / 60.0)
                self.state = FocusState.IDLE
                return

        conf = float(result.get("confidence", 0) or 0)
        if conf < self.config.entry_conf_threshold:
            try:
                from app.manager.spot_focus_entry_signal import confirm_entry
                ok, reason = confirm_entry(
                    self.client, market, "LONG",
                    conf=conf, threshold=self.config.entry_conf_threshold,
                )
            except Exception as exc:
                ok, reason = False, f"confirm_error:{exc}"
            if not ok:
                logger.info("[SPOT_GAZUA] %s entry held: %s", market, reason)
                self.state = FocusState.WATCHING
                return

        # ★ ADX entry gate (ported from the futures ADX state machine) — reject low-ADX/SIDEWAYS junk, before other gates.
        #   When adx_filter_enabled (default True) is ON, primary_tf ADX < min_adx_entry → hold in WATCHING.
        #   Insufficient data/error = fail-open (pass). Same pattern as the existing 8 gates (return on block).
        try:
            from app.manager.spot_guard_chain import adx_entry_gate
            _adx_ok, _adx_why = adx_entry_gate(self.client, market, self.config)
            if not _adx_ok:
                self._ledger_record(market, "ADX(entry-gate)", passed=False)
                self._record_near_miss(market, result.get("conviction_score", result.get("final_score", result.get("guard_score", 0))), "ADX", str(_adx_why), float(result.get("price") or 0))
                logger.info("[SPOT_GAZUA] ⛔ %s ADX entry gate blocked: %s", market, _adx_why)
                self.state = FocusState.WATCHING
                return
        except Exception as _adx_exc:
            logger.debug("[SPOT_GAZUA] adx_entry_gate fail-open: %s", _adx_exc)

        # ★ Phase 1: copy of the futures entry-timing gates (gap/micro_1m/momentum_reversal) — final check on the selected candidate.
        #   All default OFF → only enabled ones act (when off, pass instantly · 0 fetch). Hold this tick on block.
        _gate_price = float(result.get("price") or 0) or self._get_price(market)
        _gate_atr = float(result.get("atr") or 0) or self._estimate_atr(market, _gate_price)
        try:
            from app.manager.spot_entry_guards import (
                check_gap, check_micro_1m, check_momentum_reversal, check_raw_body, check_momentum_deriv,
                check_mtf_align, check_entry_expectation, check_microtiming_5m,
            )
            _checks = [
                ("timing:gap", check_gap(self.client, market, "LONG", _gate_price, _gate_atr, self.config)),
                ("timing:micro_1m", check_micro_1m(self.client, market, "LONG", self.config)),
                ("timing:momentum_reversal", check_momentum_reversal(self.client, market, "LONG", self.config)),
                ("timing:raw_body", check_raw_body(self.client, market, "LONG", self.config)),
                ("timing:momentum_deriv", check_momentum_deriv(self.client, market, "LONG", self.config)),
                ("timing:mtf_align", check_mtf_align(self.client, market, "LONG", self.config)),
            ]
            # ★ [2026-06-20] EE gate — guard_score-pass candidates (all candidates on this path) are exempt (futures mirror). Stops the fake zone-RR re-block.
            #   Run EE only when off (keep code for non-guard_score/fallback). room=headroom+gap · real RR=fixed % defends separately.
            if not getattr(self.config, "entry_expectation_bypass_guard_score", True):
                _checks.append(("timing:entry_expectation", check_entry_expectation(self.client, market, "LONG", _gate_price, _gate_atr, self.config)))
            _checks.append(("timing:microtiming_5m", check_microtiming_5m(self.client, market, "LONG", self.config)))
            for _label, (_ok, _why) in _checks:
                if not _ok:
                    self._ledger_record(market, _label, passed=False)
                    self._record_near_miss(market, result.get("conviction_score", result.get("final_score", result.get("guard_score", 0))), _label, str(_why), _gate_price)
                    logger.info("[SPOT_GAZUA] ⛔ %s entry timing gate blocked: %s", market, _why)
                    self.state = FocusState.WATCHING
                    return
        except Exception as _g_exc:
            logger.debug("[SPOT_GAZUA] entry guards fail-open: %s", _g_exc)

        self._execute_entry(result)

    # ── near-miss block control (post-judgment of gate blocks after a score pass · long-only shield) ──────
    def _record_near_miss(self, market: str, score: Any, gate: str, reason: str, price: float = 0.0) -> None:
        """Final-stage gate block after a guard_score pass = near-miss record (mirrors futures _record_near_miss).
        Spot has no SHORT so no 'spear' (opposite direction) → pure 'shield' evaluation. A blocked buy rising afterward (regrettable block)
        = over-block signal (a clue to relax that gate). deque (memory) only — record only, unrelated to entry/guards. Failure is harmless."""
        try:
            _px = float(price or 0.0)
            if _px <= 0:
                try:
                    _px = float(self._get_price(market) or 0.0)
                except Exception:  # noqa: BLE001
                    _px = 0.0
            self._recent_near_miss.append({
                "symbol": (market or "").upper(), "direction": "LONG",
                "score": round(float(score or 0.0), 1),
                "gate": str(gate or "?")[:40], "reason": str(reason or "")[:95],
                "ts": time.time(), "price": _px,
            })
        except Exception:  # noqa: BLE001
            pass

    def get_near_miss_enriched(self) -> List[Dict[str, Any]]:
        """near-miss deque + post-judgment of current/5/15/30/60-min return vs the block price (long-only).
        Mirrors futures strategy_focus_router._enrich_near_miss. 25s response cache to avoid the kline wall (owner's rate-limit lesson).
        verdict: age<5 observing / ret>+0.10% regrettable block (over-block) / ret≤+0.05% good block / in between neutral."""
        now = time.time()
        box = getattr(self, "_nm_enrich_box", None)
        if not isinstance(box, dict):
            box = self._nm_enrich_box = {"ts": 0.0, "data": None}
        if box.get("data") is not None and (now - float(box.get("ts") or 0.0)) < 25.0:
            return box["data"]

        _price_cache: Dict[str, float] = {}
        _kline_cache: Dict[tuple, list] = {}

        def _cur(sym: str) -> float:
            if sym in _price_cache:
                return _price_cache[sym]
            try:
                px = float(self._get_price(sym) or 0.0)
            except Exception:  # noqa: BLE001
                px = 0.0
            _price_cache[sym] = px if px > 0 else 0.0
            return _price_cache[sym]

        def _ret(px0: float, px1: float):
            if px0 <= 0 or px1 <= 0:   # long-only: simple rise % (no SHORT sign flip)
                return None
            return round((px1 / px0 - 1.0) * 100.0, 3)

        def _close_at(sym: str, ts0: float, target_ts: float) -> float:
            if ts0 <= 0 or target_ts <= 0 or target_ts > now:
                return 0.0
            age_min = max(0.0, (now - ts0) / 60.0)
            limit = max(24, min(144, int(age_min / 5.0) + 18))
            ck = (sym, limit)
            raw = _kline_cache.get(ck)
            if raw is None:
                try:
                    raw = self.client.get_kline(sym, interval="5", limit=limit) or []
                except Exception:  # noqa: BLE001
                    raw = []
                _kline_cache[ck] = raw
            for row in raw:
                try:
                    ts = float(row[0])
                    if ts > 10_000_000_000:
                        ts = ts / 1000.0
                    close = float(row[4])
                except (IndexError, TypeError, ValueError):
                    continue
                if close > 0 and (ts + 300.0) >= target_ts:
                    return close
            return 0.0

        out: List[Dict[str, Any]] = []
        for n in list(self._recent_near_miss):
            sym = str(n.get("symbol") or "").upper()
            ts0 = float(n.get("ts") or 0.0)
            age_min = round((now - ts0) / 60.0, 1) if ts0 else 0.0
            block_price = float(n.get("price") or 0.0)
            cur = _cur(sym)
            ret_now = _ret(block_price, cur)
            if ret_now is None:
                vkey, vlabel = ("unknown", "pending")
            elif age_min < 5.0:
                vkey, vlabel = ("watching", "observing")
            elif ret_now > 0.10:
                vkey, vlabel = ("missed_entry", "regrettable block")
            elif ret_now <= 0.05:
                vkey, vlabel = ("good_block", "good block")
            else:
                vkey, vlabel = ("neutral", "neutral")
            rec = {
                "symbol": sym, "direction": "LONG", "score": n.get("score"),
                "reason": n.get("reason"), "gate": n.get("gate") or "?",
                "ts": ts0, "age_min": age_min,
                "block_price": block_price, "price": block_price,
                "current_price": cur, "ret_now_pct": ret_now,
                "verdict": vkey, "verdict_label": vlabel,
            }
            for h in (5, 15, 30, 60):
                if age_min < h or block_price <= 0:
                    rec[f"ret_{h}m_pct"] = None
                else:
                    rec[f"ret_{h}m_pct"] = _ret(block_price, _close_at(sym, ts0, ts0 + h * 60.0))
            out.append(rec)
        out.sort(key=lambda r: r.get("age_min") or 0)
        box["ts"] = now
        box["data"] = out
        return out

    def _compute_targets(self, entry: float, atr: float, *,
                         tp1_pct: Optional[float] = None,
                         tp2_pct: Optional[float] = None,
                         sl_pct: Optional[float] = None):
        """Compute TP1/TP2/SL.
        use_pct_tp=True → fixed % off entry (retail rotation, owner's settings as-is).
        False → ATR volatility multiple (cycle_tp, Bybit swing). ※default % — no hidden rules.
        ★ When tp1_pct/tp2_pct/sl_pct overrides are given (CONTRARIAN), force the % path regardless of use_pct_tp — against-only targets.
          (FOCUS calls give no override → behavior 100% unchanged.)
        """
        from app.strategy.greenpen.cycle_tp import CycleTargets, compute_cycle_targets
        _override = tp1_pct is not None
        if self.config.use_pct_tp or _override:
            _tp1 = tp1_pct if tp1_pct is not None else self.config.tp1_pct
            _tp2 = tp2_pct if tp2_pct is not None else self.config.tp2_pct
            _sl = sl_pct if sl_pct is not None else self.config.sl_pct
            tp1_d = entry * (_tp1 / 100.0)
            sl_d = entry * (_sl / 100.0)
            # ★ §② ATR SL floor — widen with ATR if the fixed-%SL is narrower than ATR (avoid small-coin instant death). OFF=as-is.
            from app.manager.spot_entry_quality import atr_floored_sl_distance
            sl_d = atr_floored_sl_distance(entry, sl_d, atr, atr_sl_floor_mult=self.config.atr_sl_floor_mult)
            tp1 = entry + tp1_d
            tp2 = entry * (1 + _tp2 / 100.0)
            sl = entry - sl_d
            return CycleTargets(
                tp1=round(tp1, 8), tp2=round(tp2, 8), sl=round(sl, 8),
                rr_ratio=round(tp1_d / max(sl_d, 1e-12), 2),
                atr_used=atr, direction="LONG",
            )
        return compute_cycle_targets(
            entry, "LONG", atr,
            tp1_mult=self.config.tp1_mult, tp2_mult=self.config.tp2_mult,
            sl_mult=self.config.sl_mult, min_rr=self.config.min_rr,
            min_tp_distance_pct=self.config.min_tp_distance_pct,
        )

    def _execute_entry(self, result: Dict[str, Any]) -> None:
        from app.strategy.greenpen.cycle_tp import compute_position_size

        market = result.get("market", "")
        price = float(result.get("price") or 0) or self._get_price(market)
        if price <= 0:
            logger.warning("[SPOT_GAZUA] no price for %s — skip entry", market)
            self.state = FocusState.IDLE
            return
        atr = float(result.get("atr") or 0) or price * 0.02

        targets = self._compute_targets(price, atr)

        budget = self._effective_budget()                       # slot cap (total assets ÷ max_positions)
        # ★ conviction-proportional — from Phase2 on, prefer the base+modifier final score (0~100).
        #   Old-version/CONTRARIAN fallback uses GreenPen confidence (0~1) as-is.
        _score100 = result.get("conviction_score", result.get("final_score", None))
        if _score100 is not None:
            conv01 = max(0.0, min(1.0, float(_score100 or 0) / 100.0))
        else:
            conv01 = float(result.get("confidence", 0) or 0)
        conv_f = self._conv_size_factor(conv01)
        budget *= conv_f                                        # strong signal=full cap, weak=partial
        sl_dist = abs(price - targets.sl)
        sizing = compute_position_size(
            budget, self.config.risk_pct, sl_dist, price,
            # Slot split is already handled in _effective_budget (total assets ÷ max_positions) →
            # use unlimited (>10) here to avoid re-splitting. budget = already 'per-slot share × score multiplier'.
            max_daily_plans=999,
        )
        krw_spend = sizing.qty * price
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        if krw_spend < min_krw:
            krw_spend = min_krw
        if budget > 0 and krw_spend > budget:
            krw_spend = budget
        if krw_spend < min_krw:
            logger.info("[SPOT_GAZUA] budget %.0f < min order %.0f — skip %s", budget, min_krw, market)
            self.state = FocusState.IDLE
            return
        qty = krw_spend / price

        paper = bool(self.config.paper)
        order_uuid = ""
        if paper:
            # ★ [2026-06-24] paper slippage — assume buys fill unfavorably (more expensive) → less qty for the same budget.
            #   Brings paper PnL close to live (false profit if slippage ignored). Irrelevant to the LIVE branch (uses real fill price).
            _slip = max(0.0, float(getattr(self.config, "paper_slippage_bps", 0.0))) / 10000.0
            if _slip > 0:
                price *= (1.0 + _slip)
                qty = krw_spend / price
                targets = self._compute_targets(price, atr)
            order_uuid = f"PAPER-{self._paper_seq}"
            self._paper_seq += 1
            logger.info("[SPOT_GAZUA][PAPER] BUY %s krw=%.0f qty=%.8f @ %.2f (sl=%.2f tp1=%.2f tp2=%.2f)",
                        market, krw_spend, qty, price, targets.sl, targets.tp1, targets.tp2)
        else:
            try:
                od = self.client.market_buy(market, krw_spend)
                order_uuid = od.get("uuid", "")
                exec_qty = float(od.get("executed_volume", 0) or 0)
                fill_price = float(od.get("avg_price", 0) or 0)
                # ★ A market buy may fill across several quotes (e.g. two fills of 18,029 + 452,316 KRW).
                #   Use wait_order to wait until fully filled (state=done), then finalize the *final avg/qty*.
                #   Avoid drawing SL/TP off a partial-fill avg and missing the rest (owner pointed this out).
                if order_uuid:
                    try:
                        od2 = self.client.wait_order(uuid=order_uuid, market=market, timeout_sec=10.0, poll_interval=0.5)
                        exec_qty = float(od2.get("executed_volume", 0) or 0) or exec_qty
                        fill_price = float(od2.get("avg_price", 0) or 0) or fill_price
                    except Exception as q_exc:
                        logger.warning("[SPOT_GAZUA] wait_order reconcile %s failed: %s", market, q_exc)
                if exec_qty > 0:
                    qty = exec_qty            # actual filled qty (fee-reflected) — sell consistency
                if fill_price > 0:
                    price = fill_price        # ★ actual fill avg → basis for entry_price/TP/SL
                    # Recompute TP/SL off the real avg (correct order-estimate vs slippage difference)
                    targets = self._compute_targets(price, atr)
                logger.info("[SPOT_GAZUA] BUY %s krw=%.0f uuid=%s qty=%.8f @avg %.4f (sl=%.4f tp1=%.4f tp2=%.4f)",
                            market, krw_spend, order_uuid, qty, price, targets.sl, targets.tp1, targets.tp2)
            except Exception as exc:
                logger.error("[SPOT_GAZUA] BUY FAILED %s: %s", market, exc)
                self.state = FocusState.IDLE
                return

        pos = SpotGazuaPosition(
            market=market, direction="LONG", entry_price=price, qty=qty,
            tp1=targets.tp1, tp2=targets.tp2, sl=targets.sl, atr_used=targets.atr_used,
            entry_ts=time.time(), trailing_high=price, krw_spent=krw_spend,
            paper=paper, order_uuid=order_uuid,
        )
        # ★ live: pre-place TP1/TP2 as exchange limit-sell orders (avoid missing polling spikes)
        if not paper:
            self._place_tp_orders(pos)
        self.positions.append(pos)
        self.daily_plans_used += 1
        self.state = FocusState.POSITIONED
        self._ledger_record(market, "ENTRY", passed=True)   # control-panel funnel tail — one actual entry
        self._record_journal("ENTRY", pos, price, reason="GreenPen entry")
        self._save_state()

    # ── CONTRARIAN 2nd entry source ───────────────────────
    #   Adds entry only — exit is managed for all positions by _manage_all_positions regardless of source (auto-inherited).
    def _contrarian_budget(self) -> float:
        """Contrarian entry budget. contrarian_budget>0=fixed amount / 0=contrarian_budget_pct% of equity.
        Capped by real available balance + 99.5% buffer (fees/slippage) — same convention as _effective_budget."""
        cfg = self.config
        try:
            if cfg.paper:
                equity = 1_000_000.0
                held = sum(float(p.krw_spent or 0) for p in self.positions)
                free = max(0.0, equity - held)
            else:
                free = float(self.client.get_balance(self._quote_currency))
                held = sum(float(p.krw_spent or 0) for p in self.positions)
                equity = (float(cfg.budget) if cfg.budget > 0 else free + held)
            amt = (float(cfg.contrarian_budget) if cfg.contrarian_budget > 0
                   else equity * (float(cfg.contrarian_budget_pct) / 100.0))
            return max(0.0, min(amt, free) * 0.995)
        except Exception:
            return 0.0

    def _scan_contrarian_and_maybe_enter(self) -> None:
        try:
            from app.manager.spot_focus_coin_selector import select_spot_contrarian_coin
            result = select_spot_contrarian_coin(
                self.system, self.client,
                top_n=self.config.top_n, exclude=self.config.scan_exclude,
                coin_up_th=self.config.contrarian_coin_up_th,
                coin_up_cap=self.config.contrarian_coin_up_cap,
                regime_gate=self.config.contrarian_regime_gate,
                block_warning=self.config.block_warning_coins,
                block_caution=self.config.block_caution_coins,
            )
        except Exception as exc:
            logger.warning("[SPOT_CONTRA] scan error: %s", exc)
            return
        if not result:
            return
        market = result.get("market", "")
        # Already holding the same market (FOCUS/against/manual alike) → no duplicate entry.
        if not market or any(p.market == market for p in self.positions):
            return
        self._execute_contrarian_entry(result)

    def _execute_contrarian_entry(self, result: Dict[str, Any]) -> None:
        """Contrarian entry = _execute_entry mirror. Difference: against budget/targets + source="CONTRARIAN".
        manual=False (default) → _manage_all_positions auto-manages via longhold/triage/be_stall (inherited)."""
        market = result.get("market", "")
        price = float(result.get("price") or 0) or self._get_price(market)
        if price <= 0:
            logger.warning("[SPOT_CONTRA] no price for %s — skip entry", market)
            return
        atr = self._estimate_atr(market, price) or price * 0.02

        def _targets(p):
            return self._compute_targets(
                p, atr, tp1_pct=self.config.contrarian_tp_pct,
                tp2_pct=self.config.contrarian_tp2_pct, sl_pct=self.config.contrarian_sl_pct)
        targets = _targets(price)

        budget = self._contrarian_budget()
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        krw_spend = budget
        if krw_spend < min_krw:
            logger.info("[SPOT_CONTRA] budget %.0f < min order %.0f — skip %s", budget, min_krw, market)
            return
        qty = krw_spend / price

        paper = bool(self.config.paper)
        order_uuid = ""
        if paper:
            # ★ [2026-06-24] paper slippage — assume buys fill unfavorably (more expensive). Irrelevant to the LIVE branch.
            _slip = max(0.0, float(getattr(self.config, "paper_slippage_bps", 0.0))) / 10000.0
            if _slip > 0:
                price *= (1.0 + _slip)
                qty = krw_spend / price
                targets = _targets(price)
            order_uuid = f"PAPER-{self._paper_seq}"
            self._paper_seq += 1
            logger.info("[SPOT_CONTRA][PAPER] BUY %s krw=%.0f qty=%.8f @ %.2f (sl=%.2f tp1=%.2f tp2=%.2f)",
                        market, krw_spend, qty, price, targets.sl, targets.tp1, targets.tp2)
        else:
            try:
                od = self.client.market_buy(market, krw_spend)
                order_uuid = od.get("uuid", "")
                exec_qty = float(od.get("executed_volume", 0) or 0)
                fill_price = float(od.get("avg_price", 0) or 0)
                if order_uuid:
                    try:
                        od2 = self.client.wait_order(uuid=order_uuid, market=market, timeout_sec=10.0, poll_interval=0.5)
                        exec_qty = float(od2.get("executed_volume", 0) or 0) or exec_qty
                        fill_price = float(od2.get("avg_price", 0) or 0) or fill_price
                    except Exception as q_exc:
                        logger.warning("[SPOT_CONTRA] wait_order reconcile %s failed: %s", market, q_exc)
                if exec_qty > 0:
                    qty = exec_qty
                if fill_price > 0:
                    price = fill_price
                    targets = _targets(price)   # recompute TP/SL off the real avg
                logger.info("[SPOT_CONTRA] BUY %s krw=%.0f uuid=%s qty=%.8f @avg %.4f (sl=%.4f tp1=%.4f tp2=%.4f)",
                            market, krw_spend, order_uuid, qty, price, targets.sl, targets.tp1, targets.tp2)
            except Exception as exc:
                logger.error("[SPOT_CONTRA] BUY FAILED %s: %s", market, exc)
                return

        pos = SpotGazuaPosition(
            market=market, direction="LONG", entry_price=price, qty=qty,
            tp1=targets.tp1, tp2=targets.tp2, sl=targets.sl, atr_used=targets.atr_used,
            entry_ts=time.time(), trailing_high=price, krw_spent=krw_spend,
            paper=paper, order_uuid=order_uuid, source="CONTRARIAN",
        )
        if not paper:
            self._place_tp_orders(pos)
        self.positions.append(pos)
        self.daily_plans_used += 1
        self.state = FocusState.POSITIONED
        self._record_journal("ENTRY", pos, price, reason="CONTRARIAN entry")
        self._save_state()

    # ── position management (exit) ──────────────────────────────────
    def _manage_all_positions(self) -> None:
        if not self.positions:
            return
        from app.strategy.greenpen.cycle_tp import (
            CycleTargets, should_full_exit, should_partial_exit,
        )

        closed: List[SpotGazuaPosition] = []
        for pos in list(self.positions):
            price = self._get_price(pos.market)
            if not price:
                continue
            if price > pos.trailing_high:
                pos.trailing_high = price
                pos.last_peak_ts = time.time()   # basis for be_stall stall measurement
            elif pos.last_peak_ts <= 0:
                pos.last_peak_ts = pos.entry_ts  # init loaded/old-version positions

            # ★ hands-off (watch) manual position — skip all bot auto-management (SL/TP/be_lock/be_stall/longhold).
            #   But keep real-balance-0 (external/manual close) detection → clean up if sold directly on the exchange. Human closes (close button).
            if getattr(pos, "manual", False) and not self.config.manual_manage_enabled:
                if not pos.paper:
                    try:
                        from app.integrations.upbit_trade import base_currency
                        bal = float(self.client.get_balance(base_currency(pos.market), include_locked=True))
                        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
                        if bal * price < min_krw:
                            logger.info("[SPOT_GAZUA] %s hands-off manual position — external/manual close detected, cleaning up", pos.market)
                            self._record_journal("EXIT", pos, price, reason="external/manual close")
                            closed.append(pos)
                    except Exception as exc:
                        logger.debug("[SPOT_GAZUA] %s hands-off balance reconcile failed (ignored): %s", pos.market, exc)
                continue

            # ★ Manual position switched hands-off→bot-managed: if targets are 0, compute SL/TP once off avg to avoid instant close.
            if getattr(pos, "manual", False) and self.config.manual_manage_enabled and pos.sl <= 0:
                _atr = self._estimate_atr(pos.market, pos.entry_price) or pos.entry_price * 0.02
                _t = self._compute_targets(pos.entry_price, _atr)
                pos.sl, pos.tp1, pos.tp2, pos.atr_used = _t.sl, _t.tp1, _t.tp2, _t.atr_used
                if not pos.paper:
                    self._place_tp_orders(pos)
                logger.info("[SPOT_GAZUA] %s manual position switched to bot management — SL/TP computed (sl=%.4f tp1=%.4f)",
                            pos.market, pos.sl, pos.tp1)
                self._save_state()

            # ★ §4.2 longhold coin recovers → release (return to normal management). live/paper common.
            if pos.longhold_active:
                self._maybe_release_longhold(pos, price)

            # ★ ② multi_be_lock — lock SL upward in stages off peak (profit protection). Not applied during longhold.
            if not pos.longhold_active:
                self._apply_multi_be_lock(pos)

            # ★ live: real-balance reconcile — if 0 (or dust), treat as external (manual) close, clean up.
            #   Bug where, if the owner sold directly on the exchange (human harvest), the bot trusted memory and held forever.
            if not pos.paper:
                try:
                    from app.integrations.upbit_trade import base_currency
                    # ★ include_locked=True required: server-side TP limits lock the coin, so
                    #   looking at available only sees 0 → misjudges external close → kills its own position (11-case incident).
                    #   A manual close has both available+locked at 0, so it's still detected correctly.
                    bal = float(self.client.get_balance(base_currency(pos.market), include_locked=True))
                    min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
                    if bal * price < min_krw:
                        for uid in (pos.tp1_order_uuid, pos.tp2_order_uuid):
                            if uid:
                                try:
                                    self.client.cancel_order(uuid=uid, market=pos.market)
                                except Exception:
                                    pass
                        logger.info("[SPOT_GAZUA] %s real balance %.8f (₩%.0f<%.0f) — external/manual close detected, cleaning up position",
                                    pos.market, bal, bal * price, min_krw)
                        self._record_journal("EXIT", pos, price, reason="external/manual close")
                        closed.append(pos)
                        continue
                except Exception as exc:
                    logger.debug("[SPOT_GAZUA] %s balance reconcile failed (ignored): %s", pos.market, exc)
                # ★ exchange sync — if the owner moves the TP in the app, the bot follows that price
                self._sync_from_exchange(pos, price)

            # ★ ② be_stall intelligent — peak stall + momentum rollover → take-profit cut (live/paper common)
            _bs = self._check_be_stall(pos, price)
            if _bs:
                if not pos.paper:   # cancel server-side TP, then market order
                    for uid in (pos.tp1_order_uuid, pos.tp2_order_uuid):
                        if uid:
                            try:
                                self.client.cancel_order(uuid=uid, market=pos.market)
                            except Exception:
                                pass
                    pos.tp1_order_uuid = ""
                    pos.tp2_order_uuid = ""
                if self._sell_all(pos, _bs):
                    self._record_journal("EXIT", pos, price, reason=_bs)
                    closed.append(pos)
                else:
                    pos.close_retry_count += 1
                    if pos.close_retry_count >= 5:
                        closed.append(pos)
                    self._save_state()
                continue

            # ★ §4 GAZUA averaging-down (DCA) — lower avg cost before SL (inactive during longhold/hands-off). live=real order.
            if self._maybe_dca(pos, price):
                continue   # avg/target updated → re-evaluate next tick

            # ★ §4 absolute floor — at/below abs_sl vs initial entry = force-sell ignoring longhold (block infinite averaging/longhold)
            if self._dca_abs_floor_breached(pos, price):
                if not pos.paper:
                    for uid in (pos.tp1_order_uuid, pos.tp2_order_uuid):
                        if uid:
                            try:
                                self.client.cancel_order(uuid=uid, market=pos.market)
                            except Exception:
                                pass
                    pos.tp1_order_uuid = ""
                    pos.tp2_order_uuid = ""
                pos.longhold_active = False
                if self._sell_all(pos, "abs_sl"):
                    self.daily_sl_count += 1
                    self._record_journal("EXIT", pos, price,
                                         reason=f"DCA absolute floor {self.config.dca_abs_sl_pct:.0f}% force-sell")
                    closed.append(pos)
                else:
                    pos.close_retry_count += 1
                    if pos.close_retry_count >= 5:
                        closed.append(pos)
                    self._save_state()
                continue

            # ★ live + server-side TP orders placed → confirm fills + poll SL (spike-immune)
            if not pos.paper and (pos.tp1_order_uuid or pos.tp2_order_uuid):
                if self._manage_live_tp_orders(pos, price):
                    closed.append(pos)
                continue

            targets = CycleTargets(
                tp1=pos.tp1, tp2=pos.tp2, sl=pos.sl,
                rr_ratio=0.0, atr_used=pos.atr_used, direction="LONG",
            )

            reason = should_full_exit(
                price, pos.entry_price, "LONG", targets,
                trailing_high=pos.trailing_high if pos.partial_done else 0.0,
                trailing_pct=self.config.trailing_pct,
            )
            if reason:
                # ★ §4.2: an SL hit goes through A arbitration (longhold possible). Profit side (TP2/trail) sells as-is.
                if "SL hit" in reason and not self._resolve_sl_exit(pos, price, reason):
                    continue  # switched to longhold — sell on hold
                if self._sell_all(pos, reason):
                    if "SL hit" in reason:
                        self.daily_sl_count += 1
                    self._record_journal("EXIT", pos, price, reason=reason)
                    closed.append(pos)
                else:
                    # sell failed → keep position, retry next tick (avoid orphan).
                    pos.close_retry_count += 1
                    if pos.close_retry_count >= 5:
                        logger.error("[SPOT_GAZUA] %s sell failed 5× — manual cleanup needed (orphan)", pos.market)
                        closed.append(pos)
                    self._save_state()
                continue

            pe = should_partial_exit(
                price, pos.entry_price, "LONG", targets,
                partial_pct=self.config.partial_pct, already_partial=pos.partial_done,
            )
            if pe:
                sold_q = pos.qty * (pe.exit_pct / 100.0)
                if self._sell_partial(pos, pe.exit_pct):
                    self._book_partial(pos, price, pe.exit_pct / 100.0, sold_qty=sold_q)  # ★journal+principal split (partial_done set)
                    pos.sl = max(pos.sl, pe.new_sl)  # breakeven guarantee (ratchet — be_lock doesn't lower the lock)
                    self._save_state()
                continue

            if self.config.stale_hold_hours > 0:
                hh = (time.time() - pos.entry_ts) / 3600.0
                pnl_pct = (price / pos.entry_price - 1) * 100 if pos.entry_price > 0 else 0
                if hh >= self.config.stale_hold_hours and -1.0 < pnl_pct < 0.5:
                    self._sell_all(pos, f"stale {hh:.0f}h {pnl_pct:.1f}%")
                    self._record_journal("EXIT", pos, price, reason=f"stale {hh:.0f}h")
                    closed.append(pos)

        for pos in closed:
            if pos in self.positions:
                self.positions.remove(pos)
        if closed:
            _now_c = time.time()
            for _cp in closed:
                self._recent_exit[_cp.market] = _now_c   # basis time for re-entry cooldown (v2)
            self.cooldown_until = _now_c + self.config.cooldown_sec
            if not self.positions:
                self.state = FocusState.COOLDOWN
            self._save_state()

    # ── manual force-close (human harvest) ─────────────────────────────────
    def force_close(self, market: str) -> Dict[str, Any]:
        """Sell one bot-managed position in full, then finalize/journal/persist (paper/live common).
        Quick-trade (/order) is LIVE-only and external-balance-based so it can't close a paper position —
        this directly closes the position the bot holds in memory. Uses the same lock as tick."""
        with self._lock:
            pos = next((p for p in self.positions if p.market == market), None)
            if pos is None:
                return {"ok": False, "error": f"no {market} position"}
            price = self._get_price(market) or pos.entry_price
            if not self._sell_all(pos, "manual force-close"):
                return {"ok": False, "error": "sell failed — retry needed"}
            self._record_journal("EXIT", pos, price, reason="manual force-close")
            if pos in self.positions:
                self.positions.remove(pos)
            self.cooldown_until = time.time() + self.config.cooldown_sec
            if not self.positions:
                self.state = FocusState.COOLDOWN
            self._save_state()
            return {"ok": True, "market": market, "exit": round(price, 8)}

    def clean_slate(self, backup: bool = True) -> Dict[str, Any]:
        """Paper clean-slate for this exchange: close every position, wipe the journal, release cooldown.
        In-process (no external file lock). force_close() takes its own lock, so it is called OUTSIDE
        this method's lock to avoid re-entrancy. Returns {closed, journal_removed}."""
        closed = 0
        for mkt in [p.market for p in list(self.positions)]:
            try:
                if self.force_close(mkt).get("ok"):
                    closed += 1
            except Exception as exc:
                logger.warning("[SPOT_GAZUA] clean_slate close %s failed: %s", mkt, exc)
        removed = 0
        with self._lock:
            jp = getattr(self, "journal_path", None)
            try:
                if jp and os.path.exists(jp):
                    with open(jp, "r", encoding="utf-8", errors="ignore") as f:
                        removed = sum(1 for ln in f if ln.strip())
                    if backup:
                        import shutil
                        import time as _t
                        shutil.copy2(jp, f"{jp}.{_t.strftime('%Y%m%d_%H%M%S')}.bak")
                    with open(jp, "w", encoding="utf-8") as f:
                        f.flush()
                        os.fsync(f.fileno())
            except OSError as exc:
                logger.warning("[SPOT_GAZUA] clean_slate journal wipe failed: %s", exc)
        try:
            self.release_cooldown()
        except Exception:
            pass
        return {"closed": closed, "journal_removed": removed}

    def release_cooldown(self) -> Dict[str, Any]:
        """Manually release cooldown/daily limits — resume immediately from COOLDOWN (owner clicks the 'stuck badge').
        Resets the daily plan limit (max_daily_plans), SL limit, and post-trade cooldown at once, state→IDLE.
        ★ *Manual* override of live overtrade/churn protection (for observation/resume). Today's count to 0 until the next daily reset."""
        with self._lock:
            before = {"state": self.state.value,
                      "plans": f"{self.daily_plans_used}/{self.config.max_daily_plans}",
                      "cooldown_min": round(max(0.0, self.cooldown_until - time.time()) / 60.0, 1)}
            self.cooldown_until = 0.0
            self.daily_plans_used = 0
            if hasattr(self, "daily_sl_count"):
                self.daily_sl_count = 0
            if self.state == FocusState.COOLDOWN:
                self.state = FocusState.IDLE   # the scan loop resumes IDLE→SELECTING
            self._save_state()
            logger.info("[SPOT_GAZUA] 🔓 cooldown manually released — %s (plans/SL/cooldown reset → IDLE)", before)
            return {"ok": True, "before": before, "after": "IDLE (resumed)"}

    # ── §4.2 spot exit final wiring (via A·SLArbiter, longhold) ──────────────
    def _spot_freeze_active(self, pos: "SpotGazuaPosition", price: float) -> bool:
        """Spot longhold qualification = A's freeze_active input.
        True only when longhold_enabled + BTC healthy + hold cap not exceeded.
        In a BTC downtrend, False → A does a normal SL sell (avoid catching a falling knife, INV-3)."""
        if not self.config.longhold_enabled:
            return False
        # longhold max-hold cap (avoid being stuck forever) — over it, normal sell on the next SL tick
        cap_h = self.config.longhold_max_hold_hours
        if cap_h > 0:
            since = pos.longhold_since_ts or pos.entry_ts
            if (time.time() - since) / 3600.0 >= cap_h:
                return False
        # BTC regime gate (reuse the pure strategy_helpers helper — ctx-agnostic)
        try:
            from app.strategy.strategy_helpers import _check_btc_regime_for_longhold
            return bool(_check_btc_regime_for_longhold())
        except Exception as exc:
            logger.debug("[SPOT_GAZUA] BTC regime judgment failed → longhold not allowed: %s", exc)
            return False

    def _longhold_release_pct(self, pos: "SpotGazuaPosition") -> float:
        """Longhold release threshold (%). 0=ATR dynamic (ATR%×1.5, clamp 1~8%), >0=fixed."""
        if self.config.longhold_release_pct > 0:
            return float(self.config.longhold_release_pct)
        atr = self._estimate_atr(pos.market, pos.entry_price)
        if atr > 0 and pos.entry_price > 0:
            return max(1.0, min(8.0, (atr / pos.entry_price) * 100.0 * 1.5))
        return 2.0

    def _resolve_sl_exit(self, pos: "SpotGazuaPosition", price: float, reason: str) -> bool:
        """Handle an SL hit — ask A to decide sell/longhold.
        Returns: True=sell now / False=longhold (sell on hold).
        If not routed via A (switch OFF), always True = 0 change to the current direct-SL behavior."""
        if not (self._sl_arbiter_on or self.config.longhold_enabled):
            return True
        # ★ A profit-protection SL (be_lock etc, sl≥entry) is not a longhold target — locks in profit (not a falling knife).
        if pos.entry_price > 0 and pos.sl >= pos.entry_price:
            return True
        from app.manager.sl_arbiter import SLProposal, arbitrate, EXIT_NOW
        freeze = self._spot_freeze_active(pos, price)
        dec = arbitrate(
            has_liquidation=False,  # Upbit spot = no liquidation (same as the adapter selector)
            proposals=[SLProposal("cycle_tp", EXIT_NOW, reason=reason)],
            freeze_active=freeze, current_sl=pos.sl,
        )
        if dec.action == EXIT_NOW:
            if pos.longhold_active:  # release longhold (cap exceeded etc) then sell
                pos.longhold_active = False
            return True
        # HOLD = switch to longhold (sell on hold). Record first-switch time/journal.
        if not pos.longhold_active:
            pos.longhold_active = True
            pos.longhold_since_ts = time.time()
            rel = self._longhold_release_pct(pos)
            logger.info("[SPOT_GAZUA] 🔒 longhold switch %s @%.4f (sl=%.4f, release threshold +%.2f%%, BTC healthy)",
                        pos.market, price, pos.sl, rel)
            self._record_journal("LONGHOLD", pos, price, reason=f"SL→longhold (release +{rel:.2f}%)")
            self._save_state()
        return False

    # ── §4 GAZUA recovery method: DCA (averaging down) ─────────────────────────────
    def _maybe_dca(self, pos: "SpotGazuaPosition", price: float) -> bool:
        """For every step% drop below the initial entry, add a buy once to lower the avg (pyramiding).
        Origin = mirror of plugin_gazua._common_dca_check (validated). Inactive during longhold/hands-off.
        Returns True = an add-buy was executed (avg/targets updated → re-evaluate this tick)."""
        cfg = self.config
        if not getattr(cfg, "dca_enabled", False):
            return False
        if pos.manual or pos.longhold_active:
            return False
        if price <= 0 or pos.entry_price <= 0:
            return False
        step = float(getattr(cfg, "dca_step_pct", 0.5) or 0.5)
        if step <= 0:
            return False
        # Lazy-capture initial entry/principal — even as avg drops, depth/floor basis stays fixed to the *initial price*
        if pos.dca_initial_entry <= 0:
            pos.dca_initial_entry = pos.entry_price
            pos.dca_base_krw = float(pos.krw_spent or 0) or (pos.qty * pos.entry_price)
        initial = pos.dca_initial_entry
        max_steps = int(float(getattr(cfg, "dca_max_depth_pct", 4.0)) / step) if step > 0 else 0
        drop = (initial - price) / initial * 100.0 if initial > 0 else 0.0
        next_level = (pos.dca_count + 1) * step
        if not (pos.dca_count < max_steps and drop >= next_level and price < initial):
            return False
        # ★ DCA spacing (2026-06-25) — at most one add per dca_min_gap_sec. A fast multi-step drop on a
        #   high-ATR pumped coin (e.g. a freshly +40% coin) can otherwise fire several adds within a single
        #   bar before the ATR-relative falling-knife gate's cumulative drop reaches its threshold — the gate
        #   catches a single sharp candle, not a staircase of small drops, so it is "too late" on such coins.
        _gap = float(getattr(cfg, "dca_min_gap_sec", 60.0))
        if _gap > 0 and pos.dca_last_ts > 0 and (time.time() - pos.dca_last_ts) < _gap:
            return False
        # size = initial principal × add_ratio × pyramiding multiplier
        pyr = min(1.0 + pos.dca_count * float(getattr(cfg, "dca_pyramid_step", 0.20)),
                  float(getattr(cfg, "dca_pyramid_max", 2.5)))
        add_krw = float(pos.dca_base_krw or pos.krw_spent or 0) * float(getattr(cfg, "dca_add_ratio", 0.25)) * pyr
        # budget cap — total invested per coin ≤ slot budget × mult (block over-allocation)
        cap = self._effective_budget() * float(getattr(cfg, "dca_max_pos_mult", 3.0))
        if cap > 0 and (float(pos.krw_spent or 0) + add_krw) > cap:
            add_krw = max(0.0, cap - float(pos.krw_spent or 0))
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        if add_krw < min_krw:
            return False   # budget exhausted → can't add more, hand off naturally to longhold
        # ★ Falling-knife gate — if the prior 5M bar is crashing, defer averaging this tick (until the bottom settles).
        #   Blocks the case where freefall knife-catching only grows size and inverts SL/longhold risk-reward (good pullback DCA passes).
        try:
            from app.manager.spot_entry_guards import check_dca_stabilized
            _stab_ok, _stab_why = check_dca_stabilized(self.client, pos.market, cfg, live_price=price)
            if not _stab_ok:
                logger.info("[SPOT_GAZUA] %s averaging deferred — %s", pos.market, _stab_why)
                return False
        except Exception as _stab_exc:
            logger.debug("[SPOT_GAZUA] dca_stabilize fail-open: %s", _stab_exc)
        ok = self._book_addbuy(pos, price, add_krw)
        if ok:
            pos.dca_last_ts = time.time()   # start the spacing window from this add
        return ok

    def _book_addbuy(self, pos: "SpotGazuaPosition", ref_price: float, add_krw: float) -> bool:
        """Reflect one averaging-down fill *in the ledger* — update qty/krw_spent/avg + recompute targets +
        (live) cancel/re-place server-side TP + journal 'DCA' (not a close). ★Invariant: avg = total principal ÷ total qty."""
        add_price = ref_price
        add_qty = add_krw / add_price if add_price > 0 else 0.0
        if add_qty <= 0:
            return False
        if pos.paper:
            # ★ [2026-06-24] paper slippage — assume DCA buys also fill unfavorably (more expensive) → realistic averaged cost.
            _slip = max(0.0, float(getattr(self.config, "paper_slippage_bps", 0.0))) / 10000.0
            if _slip > 0 and add_price > 0:
                add_price *= (1.0 + _slip)
                add_qty = add_krw / add_price
            logger.info("[SPOT_GAZUA][PAPER] DCA#%d %s +krw=%.0f qty=%.8f @ %.4f",
                        pos.dca_count + 1, pos.market, add_krw, add_qty, add_price)
        else:
            try:
                od = self.client.market_buy(pos.market, add_krw)
                uuid = str(od.get("uuid", "") or "")
                exq = float(od.get("executed_volume", 0) or 0)
                fp = float(od.get("avg_price", 0) or 0)
                if uuid:
                    try:
                        od2 = self.client.wait_order(uuid=uuid, market=pos.market, timeout_sec=10.0, poll_interval=0.5)
                        exq = float(od2.get("executed_volume", 0) or 0) or exq
                        fp = float(od2.get("avg_price", 0) or 0) or fp
                    except Exception as q_exc:
                        logger.warning("[SPOT_GAZUA] DCA wait_order %s failed: %s", pos.market, q_exc)
                if exq > 0:
                    add_qty = exq
                if fp > 0:
                    add_price = fp
                add_krw = add_qty * add_price   # re-finalize principal off the actual fill
                logger.info("[SPOT_GAZUA] DCA#%d %s +krw=%.0f qty=%.8f @avg %.4f",
                            pos.dca_count + 1, pos.market, add_krw, add_qty, add_price)
            except Exception as exc:
                logger.error("[SPOT_GAZUA] DCA BUY FAILED %s: %s", pos.market, exc)
                return False
        # ★ accounting update (invariant: avg = total principal/total qty)
        new_qty = pos.qty + add_qty
        new_cost = float(pos.krw_spent or 0) + add_krw
        if new_qty <= 0:
            return False
        pos.qty = new_qty
        pos.krw_spent = new_cost
        pos.entry_price = new_cost / new_qty
        pos.dca_count += 1
        # recompute TP/SL off the new avg
        atr = self._estimate_atr(pos.market, pos.entry_price)
        t = self._compute_targets(pos.entry_price, atr)
        pos.tp1, pos.tp2, pos.sl, pos.atr_used = t.tp1, t.tp2, t.sl, t.atr_used
        # live: avg/qty changed → cancel + re-place server-side TP orders (else it sells the old qty)
        if not pos.paper:
            for uid in (pos.tp1_order_uuid, pos.tp2_order_uuid):
                if uid:
                    try:
                        self.client.cancel_order(uuid=uid, market=pos.market)
                    except Exception:
                        pass
            pos.tp1_order_uuid = ""
            pos.tp2_order_uuid = ""
            self._place_tp_orders(pos)
        self._record_journal("DCA", pos, add_price, qty=add_qty,
                             reason=f"averaging#{pos.dca_count} +₩{add_krw:.0f} avg→{pos.entry_price:.4f}")
        self._save_state()
        return True

    def _dca_abs_floor_breached(self, pos: "SpotGazuaPosition", price: float) -> bool:
        """At/below the absolute floor (dca_abs_sl_pct) vs initial entry = force-sell signal ignoring longhold.
        0 (or positive) = unlimited longhold (no floor)."""
        floor_pct = float(getattr(self.config, "dca_abs_sl_pct", 0.0) or 0.0)
        if floor_pct >= 0:
            return False
        initial = pos.dca_initial_entry or pos.entry_price
        if initial <= 0:
            return False
        return price <= initial * (1.0 + floor_pct / 100.0)

    def _maybe_release_longhold(self, pos: "SpotGazuaPosition", price: float) -> None:
        """When a longhold coin recovers to entry+release-threshold, release longhold → return to normal management."""
        if not pos.longhold_active or pos.entry_price <= 0:
            return
        profit_pct = (price / pos.entry_price - 1) * 100.0
        if profit_pct >= self._longhold_release_pct(pos):
            pos.longhold_active = False
            logger.info("[SPOT_GAZUA] 🔓 longhold released %s @%.4f (+%.2f%% recovered) — back to normal management",
                        pos.market, price, profit_pct)
            self._record_journal("LONGHOLD_RELEASE", pos, price, reason=f"+{profit_pct:.2f}% recovered")
            self._save_state()

    # ── ② exit guard: multi_be_lock (lock SL upward in stages off peak) ──────
    def _apply_multi_be_lock(self, pos: "SpotGazuaPosition") -> None:
        """Each time peak profit crosses a stage, lock SL upward only (ratchet). Inactive while in loss.
        ★ Protective — never lowers SL (max). Fixed lock levels (BE+cushion / +0.3 / +1.0 / +2.0%)."""
        cfg = self.config
        if not cfg.multi_be_lock_enabled or pos.entry_price <= 0:
            return
        e = pos.entry_price
        peak_pct = (pos.trailing_high / e - 1) * 100.0 if pos.trailing_high > 0 else 0.0
        # ★ ATR-adaptive arming floor — for majors (low-vol), 0.25% peak is noise so BE locks instantly → noise cut (Bybit rotation).
        #   When ON, be_lock starts only above the ATR% noise → on lock, price floats well above SL, preventing a noise cut.
        _arm = cfg.multi_be_lock_stage1_pct
        if getattr(cfg, "multi_be_lock_atr_adaptive", False) and pos.atr_used > 0:
            _atr_pct = pos.atr_used / e * 100.0
            _arm = max(_arm, _atr_pct * float(getattr(cfg, "multi_be_lock_atr_mult", 2.0)))
        if peak_pct < _arm:
            return   # before stage 1 (or the ATR noise floor) — keep original SL (incl. while in loss)
        if peak_pct >= cfg.multi_be_lock_stage4_pct:
            target, lvl = e * 1.02, "+2.0%"
        elif peak_pct >= cfg.multi_be_lock_stage3_pct:
            target, lvl = e * 1.01, "+1.0%"
        elif peak_pct >= cfg.multi_be_lock_stage2_pct:
            target, lvl = e * 1.003, "+0.3%"
        else:   # stage1 → breakeven + fee cushion
            target, lvl = e * (1 + cfg.multi_be_lock_fee_cushion_pct / 100.0), "BE"
        if target > pos.sl:   # upward only (ratchet)
            pos.sl = round(target, 8)
            logger.info("[SPOT_GAZUA] BE lock %s peak%.2f%% → SL↑ %s(%.4f)",
                        pos.market, peak_pct, lvl, pos.sl)
            self._save_state()

    # ── ② exit guard: be_stall intelligent (peak stall + momentum rollover → take-profit cut) ──
    def _check_be_stall(self, pos: "SpotGazuaPosition", price: float) -> Optional[str]:
        """Stall near peak + clearly against momentum → return take-profit cut reason (else None).
        5 safety layers: time window · fee guard · momentum · neutral-conservative · inactive in loss/longhold. (DESIGN §2.2)"""
        cfg = self.config
        if not cfg.be_stall_enabled or pos.longhold_active or pos.entry_price <= 0:
            return None
        peak_pct = (pos.trailing_high / pos.entry_price - 1) * 100.0 if pos.trailing_high > 0 else 0.0
        pnl_pct = (price / pos.entry_price - 1) * 100.0
        last_peak = pos.last_peak_ts or pos.entry_ts
        stall_sec = time.time() - last_peak
        # ① time window [min, max] — stale-peak cutoff
        if not (cfg.be_stall_sec <= stall_sec <= cfg.be_stall_max_since_peak_sec):
            return None
        # ② fee guard + ★inactive while in loss — be_stall is a take-profit (peak-giveback prevention) guard.
        #   Cut only when current pnl is in profit (≥0.15%, above fees). ★Removed the old 'or peak≥0.30%' hole:
        #   firing on *current loss* (-%) just because peak was crossed once kills the DCA recovery method and turns it into rotation
        #   (2026-06-21 AXS: fired at peak+0.49% pnl-0.95% → stop-loss right after averaging → re-entry repeated 9×/48min).
        #   Loss-zone exits are handled by DCA→longhold→abs_sl(-35%). (DESIGN §2.2)
        if pnl_pct < 0.15:
            return None
        # ③ intelligent momentum (5m)
        try:
            raw5 = self.client.get_kline(pos.market, interval="5", limit=40)
            closes5 = [float(r[4]) for r in raw5 if len(r) >= 5]
        except Exception:
            closes5 = []
        from app.manager.spot_exit_guards import score_momentum_long
        for_s, against_s, detail = score_momentum_long(
            closes5, rsi_strong=cfg.be_stall_rsi_strong, rsi_weak=cfg.be_stall_rsi_weak,
        )
        if for_s >= 2 and against_s == 0:
            pos.last_peak_ts = time.time()   # our side → hold + reset timer
            return None
        against_clear = (against_s >= 2 and for_s == 0)
        # ④ neutral fallback = conservative HOLD (no cut if neutral_exit=False)
        if not against_clear and not cfg.be_stall_neutral_exit:
            pos.last_peak_ts = time.time()   # neutral → hold (reset timer)
            return None
        kind = "intel" if against_clear else "time"
        return f"be_stall {kind} {int(stall_sec)}s peak+{peak_pct:.2f}% pnl+{pnl_pct:.2f}% ({detail})"

    def _sell_all(self, pos: SpotGazuaPosition, reason: str) -> bool:
        """Sell in full. True on success / False on failure (keep position, for retry)."""
        if pos.paper:
            logger.info("[SPOT_GAZUA][PAPER] SELL ALL %s qty=%.8f (%s)", pos.market, pos.qty, reason)
            logger.info("[SPOT_GAZUA] CLOSE %s reason=%s", pos.market, reason)
            return True
        try:
            self.client.market_sell(pos.market, pos.qty)
            logger.info("[SPOT_GAZUA] CLOSE %s reason=%s", pos.market, reason)
            return True
        except Exception as exc:
            # insufficient_funds_ask: recorded qty > real balance (fees/rounding). Re-query real balance and retry.
            if "insufficient" in str(exc).lower():
                try:
                    from app.integrations.upbit_trade import base_currency
                    bal = float(self.client.get_balance(base_currency(pos.market)))
                    if bal > 0:
                        self.client.market_sell(pos.market, bal)
                        pos.qty = 0.0
                        logger.info("[SPOT_GAZUA] CLOSE %s reason=%s (real balance %.8f retry succeeded)",
                                    pos.market, reason, bal)
                        return True
                    logger.warning("[SPOT_GAZUA] %s real balance 0 — already closed, cleaning up position", pos.market)
                    return True
                except Exception as e2:
                    logger.error("[SPOT_GAZUA] sell_all %s real-balance retry failed: %s", pos.market, e2)
            logger.error("[SPOT_GAZUA] sell_all %s FAILED: %s", pos.market, exc)
            return False

    def _sell_partial(self, pos: SpotGazuaPosition, pct: float) -> bool:
        """Partial sell. True on success / False on failure (defer partial_done, retry)."""
        q = pos.qty * (pct / 100.0)
        if pos.paper:
            logger.info("[SPOT_GAZUA][PAPER] SELL %.0f%% %s qty=%.8f", pct, pos.market, q)
            pos.qty -= q
            return True
        try:
            self.client.market_sell(pos.market, q)
            pos.qty -= q
            return True
        except Exception as exc:
            logger.error("[SPOT_GAZUA] partial sell %s FAILED: %s", pos.market, exc)
            return False

    # ── 🕊️ amnesty — adopt holding-cell coins ──────────────────
    #   "coins betray every time but give a chance every time" (owner 2026-06-16)
    #   Pull orphans that exist on the exchange but are stuck outside the bot into bot management — *only ones the human picks*.
    #   ★ Never auto-adopt — prevents the incident of selling off the owner's other coins.
    def _estimate_atr(self, market: str, ref_price: float) -> float:
        """ATR approximation for an adopted coin's TP/SL. On failure, avg×2%."""
        try:
            raw = self.client.get_kline(market, interval=self.config.primary_tf, limit=15)
            trs, pc = [], None
            for r in raw:
                h, l, c = float(r[2]), float(r[3]), float(r[4])
                tr = (h - l) if pc is None else max(h - l, abs(h - pc), abs(l - pc))
                trs.append(tr); pc = c
            if trs:
                return sum(trs) / len(trs)
        except Exception:
            pass
        return ref_price * 0.02

    def list_orphans(self) -> List[Dict[str, Any]]:
        """Exchange-held coins not in the bot's positions = amnesty candidates (info only)."""
        out: List[Dict[str, Any]] = []
        try:
            accts = self.client.accounts()
        except Exception as exc:
            logger.warning("[SPOT_GAZUA] orphan lookup failed: %s", exc)
            return out
        held = {p.market for p in self.positions}
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        for a in accts:
            cur = str(a.get("currency", "")).upper()
            if cur in (self._quote_currency, ""):
                continue
            bal = float(a.get("balance", 0) or 0)
            if bal <= 0:
                continue
            market = self._normalize_market(cur)
            if market in held:
                continue
            price = self._get_price(market) or 0.0
            value = bal * price
            if value < min_krw:
                continue  # dust = can't sell even on the exchange → amnesty pointless
            avg = float(a.get("avg_buy_price", 0) or 0)
            pnl = (price / avg - 1) * 100 if avg > 0 else 0.0
            out.append({
                "market": market, "currency": cur, "balance": bal,
                "avg_buy_price": avg, "current_price": price,
                "value_krw": value, "pnl_pct": pnl,
            })
        return out

    def adopt_orphan(self, market: str) -> Dict[str, Any]:
        """Amnesty — adopt an exchange holding into bot management (avg-based TP/SL + place server-side TP)."""
        from app.integrations.upbit_trade import base_currency
        market = str(market).upper().strip()
        if self.config.paper:
            return {"ok": False, "error": "paper mode — amnesty possible after switching to live"}
        if any(p.market == market for p in self.positions):
            return {"ok": False, "error": "already bot-managed"}
        base = base_currency(market)
        try:
            bal = float(self.client.get_balance(base))
        except Exception as exc:
            return {"ok": False, "error": f"balance lookup failed: {exc}"}
        price = self._get_price(market) or 0.0
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        if bal <= 0 or price <= 0 or bal * price < min_krw:
            return {"ok": False, "error": f"insufficient holding/dust (₩{bal * price:.0f})"}
        # avg = account avg_buy_price (else current price)
        avg = 0.0
        try:
            for a in self.client.accounts():
                if str(a.get("currency", "")).upper() == base:
                    avg = float(a.get("avg_buy_price", 0) or 0)
                    break
        except Exception:
            pass
        entry = avg if avg > 0 else price
        atr = self._estimate_atr(market, entry)
        targets = self._compute_targets(entry, atr)
        pos = SpotGazuaPosition(
            market=market, direction="LONG", entry_price=entry, qty=bal,
            tp1=targets.tp1, tp2=targets.tp2, sl=targets.sl, atr_used=targets.atr_used,
            entry_ts=time.time(), trailing_high=max(entry, price), krw_spent=bal * entry,
            paper=False, order_uuid="ADOPTED",
        )
        self._place_tp_orders(pos)
        self.positions.append(pos)
        if self.state == FocusState.IDLE:
            self.state = FocusState.POSITIONED
        self._record_journal("ENTRY", pos, entry, reason="🕊️ amnesty adoption")
        self._save_state()
        logger.info("[SPOT_GAZUA] 🕊️ amnesty adoption %s qty=%.8f avg=%.4f (sl=%.4f tp1=%.4f tp2=%.4f)",
                    market, bal, entry, targets.sl, targets.tp1, targets.tp2)
        return {"ok": True, "market": market, "entry": entry, "qty": bal,
                "sl": targets.sl, "tp1": targets.tp1, "tp2": targets.tp2}

    # ── exchange sync (follow manual TP moves) ──────────────────
    def _sync_from_exchange(self, pos: "SpotGazuaPosition", price: float) -> None:
        """Read open sell orders + balance from the exchange and sync the bot's tp1/tp2/qty.
        If the owner moves a TP in the app (cancel→re-order), the bot follows that price.
        ※ SL has no exchange order (polled), so it's not synced — keep the bot's internal value."""
        from app.integrations.upbit_trade import base_currency
        try:
            opens = self.client.open_orders(pos.market, side="ask")
        except Exception as exc:
            logger.debug("[SPOT_GAZUA] %s open_orders lookup failed (ignored): %s", pos.market, exc)
            return
        sells = []
        for o in (opens or []):
            try:
                pr = float(o.get("price") or 0)
                uid = str(o.get("uuid") or "")
                if pr > 0 and uid:
                    sells.append((pr, uid))
            except (TypeError, ValueError):
                continue
        sells.sort(key=lambda x: x[0])  # price ascending: lower=TP1, higher=TP2
        changed = False
        if len(sells) >= 2:
            (p1, u1), (p2, u2) = sells[0], sells[-1]
            if abs(p1 - pos.tp1) > 1e-9 or u1 != pos.tp1_order_uuid:
                changed = True
            if abs(p2 - pos.tp2) > 1e-9 or u2 != pos.tp2_order_uuid:
                changed = True
            pos.tp1, pos.tp1_order_uuid = p1, u1
            pos.tp2, pos.tp2_order_uuid = p2, u2
        elif len(sells) == 1:
            p2, u2 = sells[0]
            if abs(p2 - pos.tp2) > 1e-9 or u2 != pos.tp2_order_uuid or pos.tp1_order_uuid:
                changed = True
            pos.tp2, pos.tp2_order_uuid = p2, u2
            # ★ TP1 slot gone: just clearing it would miss a fill (partial take-profit) (journal/principal-unbooked bug).
            #   If filled, book it (_book_partial); if manually canceled/merged, just clear the slot.
            if pos.tp1_order_uuid and not pos.partial_done:
                try:
                    od1 = self.client.get_order(uuid=pos.tp1_order_uuid, market=pos.market)
                    if str(od1.get("state", "")).lower() == "done":
                        self._book_partial(pos, pos.tp1, self.config.partial_pct / 100.0,
                                           sold_qty=float(od1.get("executed_volume", 0) or 0))
                        pos.sl = max(pos.sl, pos.entry_price)
                        logger.info("[SPOT_GAZUA] %s TP1 fill detected (sync) — booked the half take-profit", pos.market)
                        changed = True
                except Exception:
                    pass
            pos.tp1_order_uuid = ""   # clear slot (booked, or manually canceled/merged)
        # If 0 sell orders, don't touch — _manage_live_tp_orders judges fill/cancel
        # Sync qty from balance — ★[2026-06-21] *down only*. Shrink qty only when balance is less than the bot's estimate
        #   (external/partial-sell detected). Do NOT absorb when balance is *higher*: adding a coin the bot didn't buy
        #   (orphan/manual buy/rotation residue) to qty leaves krw_spent (principal) unchanged so avg = principal÷inflated qty collapses (e.g. AXS 1876→1050) →
        #   on close, (sell − fake avg)×inflated qty = a fake windfall record (the root of PnL inflation). External holdings go through amnesty (adopt).
        try:
            bal = float(self.client.get_balance(base_currency(pos.market), include_locked=True))
            if 0 < bal < pos.qty * 0.99:
                pos.qty = bal
                changed = True
        except Exception:
            pass
        # ★ heal orphaned partial take-profit — TP1 slot empty but partial_done=False + only TP2 alive and
        #   principal (krw_spent) excessive vs held qty (=old bug failed to book TP1) → correct once by the actual sold ratio.
        if (not pos.partial_done and not pos.tp1_order_uuid and pos.tp2_order_uuid
                and pos.entry_price > 0 and pos.qty > 0):
            implied = float(pos.krw_spent or 0) / pos.entry_price       # original qty by principal
            if implied > pos.qty * 1.25:                                # 25%+ over held = partial fill unbooked
                sold_frac = max(0.0, min(0.95, 1.0 - pos.qty / implied))
                logger.info("[SPOT_GAZUA] %s orphaned partial take-profit detected (%.0f%%) — correcting ledger", pos.market, sold_frac * 100)
                self._book_partial(pos, pos.tp1 or pos.entry_price, sold_frac)
                changed = True
        if changed:
            logger.info("[SPOT_GAZUA] %s exchange sync — tp1=%.4f tp2=%.4f qty=%.8f",
                        pos.market, pos.tp1, pos.tp2, pos.qty)
            self._save_state()

    # ── server-side limit TP (polling-spike immune) ──────────────────
    def _place_tp_orders(self, pos: SpotGazuaPosition) -> None:
        """Right after entry, pre-place TP1 (half)/TP2 (remainder) limit sells on the exchange.
        On failure, clear the uuids to fall back to polling exit (fail-safe)."""
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        half = pos.qty * (self.config.partial_pct / 100.0)
        rest = pos.qty - half
        try:
            if half * pos.tp1 >= min_krw and rest * pos.tp2 >= min_krw:
                od1 = self.client.limit_sell(pos.market, pos.tp1, half)
                pos.tp1_order_uuid = str(od1.get("uuid", "") or "")
                od2 = self.client.limit_sell(pos.market, pos.tp2, rest)
                pos.tp2_order_uuid = str(od2.get("uuid", "") or "")
                logger.info("[SPOT_GAZUA] server-side TP placed %s: TP1 %.4f×%.6f + TP2 %.4f×%.6f",
                            pos.market, pos.tp1, half, pos.tp2, rest)
            else:
                # half below min order (5000) → all in one TP2
                od2 = self.client.limit_sell(pos.market, pos.tp2, pos.qty)
                pos.tp2_order_uuid = str(od2.get("uuid", "") or "")
                logger.info("[SPOT_GAZUA] server-side TP (full TP2) placed %s: %.4f×%.6f",
                            pos.market, pos.tp2, pos.qty)
        except Exception as exc:
            pos.tp1_order_uuid = ""
            pos.tp2_order_uuid = ""
            logger.error("[SPOT_GAZUA] TP limit order failed %s: %s — falling back to polling exit", pos.market, exc)

    def _manage_live_tp_orders(self, pos: SpotGazuaPosition, price: float) -> bool:
        """live: confirm server-side TP fills + poll SL. True when fully closed."""
        # 1) TP1 filled → half take-profit + SL to breakeven
        if pos.tp1_order_uuid and not pos.partial_done:
            try:
                od = self.client.get_order(uuid=pos.tp1_order_uuid, market=pos.market)
                if str(od.get("state", "")).lower() == "done":
                    filled = float(od.get("executed_volume", 0) or 0)
                    self._book_partial(pos, pos.tp1, self.config.partial_pct / 100.0, sold_qty=filled)  # ★journal+principal split
                    pos.qty = max(0.0, pos.qty - filled)
                    pos.sl = max(pos.sl, pos.entry_price)  # move to breakeven (ratchet — be_lock doesn't lower)
                    pos.tp1_order_uuid = ""
                    logger.info("[SPOT_GAZUA] TP1 filled %s — half take-profit (%.6f), SL→breakeven %.4f",
                                pos.market, filled, pos.sl)
                    self._save_state()
            except Exception as exc:
                logger.warning("[SPOT_GAZUA] TP1 order lookup %s failed: %s", pos.market, exc)
        # 2) TP2 filled → full close complete
        if pos.tp2_order_uuid:
            try:
                od = self.client.get_order(uuid=pos.tp2_order_uuid, market=pos.market)
                if str(od.get("state", "")).lower() == "done":
                    logger.info("[SPOT_GAZUA] TP2 filled %s — full close complete", pos.market)
                    self._record_journal("EXIT", pos, pos.tp2, reason="TP2 fill")
                    pos.tp2_order_uuid = ""
                    return True
            except Exception as exc:
                logger.warning("[SPOT_GAZUA] TP2 order lookup %s failed: %s", pos.market, exc)
        # 3) SL polling (Upbit spot has no stop support) → on hit, cancel open TP + market-sell the remainder
        if price <= pos.sl:
            # ★ §4.2: A arbitration — if longhold-qualified, hold the sell (keep TP limits = take profit on recovery).
            if not self._resolve_sl_exit(pos, price, f"SL hit: {price:.4f} <= {pos.sl:.4f}"):
                return False
            for uid in (pos.tp1_order_uuid, pos.tp2_order_uuid):
                if uid:
                    try:
                        self.client.cancel_order(uuid=uid, market=pos.market)
                    except Exception as c_exc:
                        logger.warning("[SPOT_GAZUA] TP order cancel %s failed: %s", pos.market, c_exc)
            pos.tp1_order_uuid = ""
            pos.tp2_order_uuid = ""
            if self._sell_all(pos, f"SL hit: {price:.4f} <= {pos.sl:.4f}"):
                self.daily_sl_count += 1
                self._record_journal("EXIT", pos, price, reason="SL hit")
                return True
            pos.close_retry_count += 1
            if pos.close_retry_count >= 5:
                logger.error("[SPOT_GAZUA] %s SL sell failed 5× — manual cleanup needed (orphan)", pos.market)
                return True
            self._save_state()
            return False
        return False

    # ── budget / price ─────────────────────────────────────────
    def _effective_budget(self) -> float:
        """Per-slot budget = total assets ÷ max_positions (even spread across coins).
        Total assets (equity) = auto (budget=0): available KRW + held position principal (krw_spent)
                              / manual (budget>0): that fixed total.
        ★ Since equity includes held principal (krw_spent), equity stays constant as slots fill
          → per_slot stays ~1/N = true even spread (it would shrink if based on remaining balance).
        Capped by actual available KRW (spot can't buy beyond balance) + 99.5% (fee/slippage buffer,
        KRW-XLM 200138 insufficient_funds lesson)."""
        slots = max(1, int(self.config.max_positions))
        try:
            if self.config.paper:
                # ★ [2026-06-25] honor config.budget in paper too (per-exchange paper allocation) —
                #   was hardcoded 1M, silently ignoring the amount the owner set per exchange.
                equity = float(self.config.budget) if self.config.budget > 0 else 1_000_000.0
                held = sum(float(p.krw_spent or 0) for p in self.positions)
                free = max(0.0, equity - held)
            else:
                free = float(self.client.get_balance(self._quote_currency))
                held = sum(float(p.krw_spent or 0) for p in self.positions)
                equity = (float(self.config.budget) if self.config.budget > 0
                          else free + held)
            per_slot = equity / slots
            return max(0.0, min(per_slot, free) * 0.995)
        except Exception:
            return 0.0

    def _conv_size_factor(self, conf01: float) -> float:
        """confidence(0~1) → size multiplier vs slot cap (floor~1.0).
        Conviction-proportional sizing: pass-floor (entry_conf_threshold) signal=floor, full (1.0)=full slot.
        Linear normalization [lo,1.0]→[floor,1.0]. If conv_sizing_enabled=False, 1.0 (even 1/N).
        The Upbit version of Bybit conviction-weighted sizing (_compute_entry_budget)."""
        cfg = self.config
        if not cfg.conv_sizing_enabled:
            return 1.0
        floor_f = max(0.0, min(1.0, float(cfg.conv_size_floor)))
        lo = float(cfg.entry_conf_threshold)
        if lo >= 1.0:
            return 1.0
        t = max(0.0, min(1.0, (float(conf01) - lo) / (1.0 - lo)))
        return floor_f + (1.0 - floor_f) * t

    def _get_price(self, market: str) -> float:
        try:
            return float(self.client.get_price(market))
        except Exception:
            return 0.0

    # ── daily counters ─────────────────────────────────────────
    @staticmethod
    def _trading_day(ts: Optional[float] = None) -> str:
        """Trading-day epoch = 07:00 KST (same as Bybit). Trades before 07:00 belong to the prior day.
        Server localtime=KST, so the date after a -7h offset = the 07:00 boundary."""
        t = time.time() if ts is None else float(ts or 0)
        return time.strftime("%Y-%m-%d", time.localtime(t - 7 * 3600))

    def _maybe_reset_daily(self) -> None:
        # ★ 07:00 KST epoch (unified with Bybit). Old gmtime (UTC midnight = 09:00 KST) → 07:00.
        stamp = self._trading_day()
        if stamp != self._day_stamp:
            self._day_stamp = stamp
            self.daily_plans_used = 0
            self.daily_sl_count = 0

    # ── persistence (atomic write) ────────────────────────────────
    def _save_state(self) -> None:
        try:
            data = {
                "config": asdict(self.config),
                "state": {
                    "focus_state": self.state.value,
                    "positions": [p.to_dict() for p in self.positions],
                    "daily_plans_used": self.daily_plans_used,
                    "daily_sl_count": self.daily_sl_count,
                    "day_stamp": self._day_stamp,
                    "cooldown_until": self.cooldown_until,
                    "paper_seq": self._paper_seq,
                },
            }
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as exc:
            logger.error("[SPOT_GAZUA] save_state FAILED: %s", exc)

    def _load_state(self) -> None:
        try:
            if not os.path.exists(self.state_path):
                return
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            cfg_in = data.get("config") or {}
            for k, v in cfg_in.items():
                if hasattr(self.config, k):
                    setattr(self.config, k, v)
            # ★ budget_krw → budget rename migration (old-state compat · preserve setting). Currency-neutral name.
            if "budget_krw" in cfg_in and not cfg_in.get("budget"):
                try:
                    self.config.budget = float(cfg_in.get("budget_krw") or 0.0)
                except (TypeError, ValueError):
                    pass
            # ── config migration (one-time correction where stale OFF values in old runtime override new ON defaults) ──
            #   2026-06-17 owner "everything ON by default, except paper". Old files have no config_version (=0) →
            #   force entry-quality/longhold gates to the current dataclass defaults (ON) once, then bump the version.
            #   ※ After that it's v1 and won't re-fire — values the owner tuned via UI are preserved.
            loaded_ver = int(cfg_in.get("config_version", 0) or 0)
            _CUR_VER = SpotGazuaConfig().config_version  # current code version (2)
            if loaded_ver < _CUR_VER:
                _d = SpotGazuaConfig()
                # Force only the fields newly turned ON in each version, cumulatively (owner UI-tuned values preserved from that version on).
                _bump_fields = []
                if loaded_ver < 1:   # v1: entry-quality/longhold gates ON
                    _bump_fields += ["longhold_enabled", "headroom_gate_pct", "atr_sl_floor_mult",
                                     "overext_range_pos_pct", "blowoff_move_pct"]
                if loaded_ver < 2:   # v2: guard_score display ON
                    _bump_fields += ["guard_score_mode_enabled"]
                if loaded_ver < 3:   # v3: guard_score threshold gate + 80 cap ON
                    _bump_fields += ["guard_score_threshold", "guard_score_total_cap"]
                if loaded_ver < 4:   # v4: multi_be_lock (profit-protection SL lock) ON
                    _bump_fields += ["multi_be_lock_enabled"]
                if loaded_ver < 5:   # v5: be_stall intelligent (momentum-precise cut) ON
                    _bump_fields += ["be_stall_enabled"]
                if loaded_ver < 7:   # v7: guard_score threshold 45→50 (mini-backtest basis · elite few)
                    _bump_fields += ["guard_score_threshold"]
                if loaded_ver < 8:   # v8: copied futures-ON guards default ON (owner "whatever is ON in FOCUS is ON in spot too")
                    _bump_fields += ["gap_check_enabled", "micro_1m_check_enabled", "momentum_reversal_enabled",
                                     "raw_body_enabled", "mtf_align_enabled", "entry_expectation_enabled",
                                     "microtiming_5m_enabled"]
                if loaded_ver < 9:   # v9: micro_1m noise-doji exemption (0.05) — fixes the false-block that rejected tiny dojis by color alone
                    _bump_fields += ["micro_1m_body_min_pct"]
                if loaded_ver < 10:  # v10: spot GAZUA Live switch — force paper→False (owner "all 3 Live", recovery-method DCA armed). Per-server paper revert via UI preserved after.
                    _bump_fields += ["paper"]
                if loaded_ver < 11:  # v11: candle-timing gates OFF + ADX threshold relaxed for live auto-entry (owner 2026-06-21 "if over-blocking makes entry impossible there's no coin to catch"). Applied on each server restart. UI re-tuning preserved after.
                    _bump_fields += ["gap_check_enabled", "micro_1m_check_enabled", "momentum_reversal_enabled",
                                     "raw_body_enabled", "mtf_align_enabled", "microtiming_5m_enabled", "min_adx_entry"]
                if loaded_ver < 12:  # v12: gate_ledger control panel default ON (owner 2026-06-21 "add a near-miss control panel"). Observe-only · doesn't touch entry · local. UI toggle preserved after.
                    _bump_fields += ["gate_ledger_enabled"]
                for k in _bump_fields:
                    setattr(self.config, k, getattr(_d, k))
                self.config.config_version = _CUR_VER
                logger.info("[SPOT_GAZUA] config v%d→v%d migration — applied default ON: %s",
                            loaded_ver, _CUR_VER, ", ".join(_bump_fields) or "(none)")
                self._migrated_v1 = True   # saved at the end of __init__ (flag name kept)
            st = data.get("state") or {}
            fs = st.get("focus_state", "IDLE")
            self.state = FocusState(fs) if fs in FocusState._value2member_map_ else FocusState.IDLE
            self.positions = [SpotGazuaPosition.from_dict(p) for p in st.get("positions", [])]
            # ★ mode consistency: in LIVE mode, remove any mixed-in virtual (paper) positions (they don't really exist)
            if not self.config.paper:
                _before = len(self.positions)
                self.positions = [p for p in self.positions if not p.paper]
                if len(self.positions) < _before:
                    logger.info("[SPOT_GAZUA] LIVE mode — removed %d loaded virtual (paper) positions",
                                _before - len(self.positions))
            self.daily_plans_used = int(st.get("daily_plans_used", 0) or 0)
            self.daily_sl_count = int(st.get("daily_sl_count", 0) or 0)
            self._day_stamp = st.get("day_stamp", "")
            self.cooldown_until = float(st.get("cooldown_until", 0.0) or 0.0)
            self._paper_seq = int(st.get("paper_seq", 0) or 0)
            logger.info("[SPOT_GAZUA] state loaded: %d positions, %d plans used (paper=%s, enabled=%s)",
                        len(self.positions), self.daily_plans_used, self.config.paper, self.config.enabled)
        except Exception as exc:
            logger.error("[SPOT_GAZUA] load_state FAILED: %s", exc)

    # ── trade journal (JSONL append-only) ──────────────────────────
    def _book_partial(self, pos: "SpotGazuaPosition", exit_price: float,
                      sold_fraction: float, sold_qty: Optional[float] = None) -> None:
        """Book one partial take-profit (TP1) *in the ledger* — journal EXIT (only the sold-ratio principal) + deduct remaining principal + partial_done.
        ★ Prevent double-count: deduct krw_spent by the sold ratio (sold_fraction) → later TP2/full close books only remaining principal as PnL.
        ★ Dup guard (partial_done): once only, called from sync/manage_live/polling. qty/SL/uuid handled by the caller."""
        if pos.partial_done:
            return
        sf = max(0.0, min(1.0, float(sold_fraction)))
        part_cost = float(pos.krw_spent or 0) * sf
        self._record_journal("EXIT", pos, exit_price, reason="TP1 partial take-profit",
                             qty=sold_qty, cost_override=part_cost)
        pos.krw_spent = max(0.0, float(pos.krw_spent or 0) - part_cost)
        pos.partial_done = True
        self._save_state()

    def _record_journal(self, event: str, pos: "SpotGazuaPosition", price: float,
                        reason: str = "", qty: Optional[float] = None,
                        cost_override: Optional[float] = None) -> None:
        """Record one entry/exit to the journal. No trade impact even on failure (best-effort).
        Spot long_only: ROE% = price change %. pnl_krw = principal (krw_spent) × ROE.
        ★ cost_override: a partial take-profit computes PnL on *only the sold-ratio* principal (using full krw_spent double-counts)."""
        try:
            q = float(qty if qty is not None else pos.qty)
            entry = float(pos.entry_price or 0)
            is_exit = (event == "EXIT" and entry > 0)
            # ★★ [2026-06-23 fix] Stamp the re-entry cooldown (v2) basis time on *every exit path*.
            #   Previously set only in _manage_all_positions(1759) → LIVE SL/TP exits go through
            #   _manage_live_tp_orders/_resolve_sl_exit and were missed → the 45-min cooldown couldn't
            #   see LIVE exits, causing re-entry rotation right after SL (LAYER measured). _record_journal is the single
            #   funnel every exit passes, so stamping here covers paper/live·SL/TP/manual.
            if event == "EXIT":
                try:
                    self._recent_exit[pos.market] = time.time()
                except Exception:
                    pass
            # ★ PnL₩ = principal (krw_spent) × price change % − round-trip fee (net, owner-entered fee_rate_pct 2026-06-17).
            #   The old (exit-avg)×qty hits ₩0/inflated values when qty is polluted by balance sync → principal-based is stable.
            #   Fees: buy side = principal×rate, sell side = sell proceeds (principal×ratio)×rate. Deduct both for true net.
            cost = float(cost_override) if cost_override is not None else float(pos.krw_spent or 0)
            fee_r = max(0.0, float(getattr(self.config, "fee_rate_pct", 0.0))) / 100.0
            if is_exit:
                # ★ [2026-06-24] paper slippage — assume sells fill unfavorably (cheaper). Every exit goes through this funnel
                #   so it covers SL/TP/manual/partial. LIVE has price=real fill so it's unaffected (slip not applied).
                if bool(getattr(self.config, "paper", False)):
                    _eslip = max(0.0, float(getattr(self.config, "paper_slippage_bps", 0.0))) / 10000.0
                    if _eslip > 0:
                        price = price * (1.0 - _eslip)
                ratio = price / entry
                gross_krw = cost * (ratio - 1.0)
                fee_krw = cost * fee_r + (cost * ratio) * fee_r   # buy + sell round-trip
                pnl_krw = round(gross_krw - fee_krw, 2)
                roe = round((pnl_krw / cost) * 100, 3) if cost > 0 else 0.0
            else:
                gross_krw, fee_krw, pnl_krw, roe = 0.0, 0.0, 0.0, 0.0
            rec = {
                "ts": time.time(), "strategy": "FOCUS", "event": event,
                "market": pos.market, "direction": "LONG",
                "entry": round(entry, 8), "exit": round(price, 8) if event == "EXIT" else None,
                "qty": round(q, 8), "pnl_krw": pnl_krw, "roe_pct": roe,
                "gross_pnl_krw": round(gross_krw, 2), "fee_krw": round(fee_krw, 2),
                "hold_sec": round(time.time() - pos.entry_ts, 1),
                "reason": reason, "paper": bool(pos.paper),
            }
            # ★ [2026-06-20] lock + flush + fsync — block missing entries from concurrent write/delete races
            #   and crash partial-writes (fragment records) (mirrors the futures TradeJournal pattern). Write one whole line.
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            with self._journal_lock:
                with open(self.journal_path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as exc:
            logger.debug("[SPOT_GAZUA] journal write failed (ignored): %s", exc)

    def read_journal(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Recent trade records (newest first). Empty list if no file."""
        try:
            if not os.path.exists(self.journal_path):
                return []
            with self._journal_lock:
                with open(self.journal_path, encoding="utf-8") as f:
                    lines = f.readlines()
            out = []
            for ln in lines[-max(limit, 1):]:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        continue
            out.reverse()
            return out
        except Exception as exc:
            logger.debug("[SPOT_GAZUA] journal read failed: %s", exc)
            return []

    def delete_journal(self, ts: float) -> Dict[str, Any]:
        """Delete one journal record (ts match). Rewrite the file without the ts line. Trade-unrelated (record only)."""
        try:
            if not os.path.exists(self.journal_path):
                return {"ok": False, "error": "no journal"}
            # ★ [2026-06-20] lock + atomic (temp→replace) rewrite — block file corruption from an append cutting in
            #   during delete or a "w" truncate being interrupted (fragment records = one cause of missing entries).
            with self._journal_lock:
                with open(self.journal_path, encoding="utf-8") as f:
                    lines = f.readlines()
                kept, removed = [], 0
                for ln in lines:
                    s = ln.strip()
                    if not s:
                        continue
                    try:
                        row_ts = float(json.loads(s).get("ts", 0) or 0)
                    except Exception:
                        kept.append(ln); continue           # preserve unparseable lines
                    if abs(row_ts - float(ts)) < 1e-6:
                        removed += 1
                    else:
                        kept.append(ln)
                if removed == 0:
                    return {"ok": False, "error": "record not found"}
                _tmp = self.journal_path + ".tmp"
                with open(_tmp, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(_tmp, self.journal_path)   # atomic rename
            logger.info("[SPOT_GAZUA] journal delete ts=%s (%d records)", ts, removed)
            return {"ok": True, "removed": removed}
        except Exception as exc:
            logger.warning("[SPOT_GAZUA] journal delete failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def journal_summary(self, daily_days: int = 30) -> Dict[str, Any]:
        """Journal aggregation — cumulative/today PnL, win rate, daily PnL (for the bar chart)."""
        rows = self.read_journal(limit=5000)
        exits = [r for r in rows if r.get("event") == "EXIT"]
        today = self._trading_day()  # ★ trading-day epoch 07:00 KST (unified with Bybit)
        total_pnl = round(sum(float(r.get("pnl_krw", 0) or 0) for r in exits), 2)
        today_pnl = 0.0
        wins = 0
        daily: Dict[str, float] = {}
        for r in exits:
            day = self._trading_day(r.get("ts"))
            pk = float(r.get("pnl_krw", 0) or 0)
            daily[day] = round(daily.get(day, 0.0) + pk, 2)
            if day == today:
                today_pnl += pk
            if pk > 0:
                wins += 1
        n = len(exits)
        days_sorted = sorted(daily.keys())[-max(daily_days, 1):]
        return {
            "total_pnl_krw": total_pnl,
            "today_pnl_krw": round(today_pnl, 2),
            "trades": n,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "daily": [{"day": d, "pnl_krw": daily[d]} for d in days_sorted],
        }

    # ── account summary (for the Overall Status card, TTL cache) ─────────────
    def account_summary(self, ttl: float = 5.0) -> Dict[str, Any]:
        """Available KRW + coin holdings value + total assets (₩). Read-only · cached."""
        now = time.time()
        if self._acct_cache and (now - self._acct_cache_ts) < ttl:
            return self._acct_cache
        krw_free = 0.0
        holdings = 0.0
        try:
            # Available quote currency (KRW/USDT) + non-quote coins via ★one batch ticker call (no per-coin calls
            #   — with many coins in a LIVE unified account, status would stall and surface 'Failed to fetch').
            coin_bals: Dict[str, float] = {}
            for a in self.client.accounts():
                cur = str(a.get("currency", "")).upper()
                bal = float(a.get("balance", 0) or 0) + float(a.get("locked", 0) or 0)
                if cur == self._quote_currency:
                    krw_free += bal
                elif bal > 0:
                    coin_bals[cur] = coin_bals.get(cur, 0.0) + bal
            if coin_bals:
                markets = [self._normalize_market(c) for c in coin_bals]
                price_map: Dict[str, float] = {}
                try:
                    for t in (self.client.get_tickers(markets) or []):
                        price_map[str(t.get("market", ""))] = float(t.get("trade_price", 0) or 0)
                except Exception:
                    pass
                for c, bal in coin_bals.items():
                    holdings += bal * (price_map.get(self._normalize_market(c)) or 0.0)
        except Exception as exc:
            logger.debug("[SPOT_GAZUA] account_summary failed: %s", exc)
            return self._acct_cache or {"krw_free": 0.0, "holdings_krw": 0.0, "equity_krw": 0.0}
        out = {
            "krw_free": round(krw_free, 0),
            "holdings_krw": round(holdings, 0),
            "equity_krw": round(krw_free + holdings, 0),
        }
        self._acct_cache = out
        self._acct_cache_ts = now
        return out

    # ── quick trade (manual instant market order) ─────────────────────────
    def _normalize_market(self, market: str) -> str:
        """Normalize a manually-entered market — per-exchange override point. Upbit: 'BTC'→'KRW-BTC'.
        (USDT exchanges like Bybit spot override 'BTC'→'BTCUSDT' in BybitSpotGazuaManager)"""
        m = str(market).upper().strip()
        if not m.startswith("KRW-"):
            m = f"KRW-{m}"
        return m

    def quick_order(self, market: str, side: str, *, krw: float = 0.0, qty: float = 0.0,
                    pct: float = 0.0) -> Dict[str, Any]:
        """Dashboard quick trade — instant market order unrelated to bot management.
        KRW mode: buy=KRW amount, sell=qty (0=full real balance).
        % mode (pct>0): buy=available KRW×%, sell=real balance×% — convert authoritatively from exchange balance. Blocked in paper."""
        from app.integrations.upbit_trade import base_currency
        market = self._normalize_market(market)
        s = str(side).lower()
        pct = max(0.0, min(100.0, float(pct or 0.0)))
        if self.config.paper:
            return {"ok": False, "error": "paper mode — quick order possible after switching to live"}
        try:
            if s in ("buy", "bid", "long"):
                # ★ Block manual buys on exchange warning-listed coins (Entry setting toggle). Sells always allowed (escape).
                wf = {}
                try:
                    wf = self.client.get_market_warnings().get(market, {})
                except Exception:
                    pass
                if self.config.block_warning_coins and wf.get("warning"):
                    return {"ok": False, "error": f"{market} investment-warning coin — buy blocked (can disable in Entry settings)"}
                if self.config.block_caution_coins and wf.get("caution"):
                    return {"ok": False, "error": f"{market} caution ({','.join(wf.get('kinds', []))}) — buy blocked"}
                if pct > 0:
                    free = float(self.client.get_balance(self._quote_currency))
                    amt = min(free * pct / 100.0, free * 0.9995)  # fee room
                else:
                    amt = float(krw)
                min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
                if amt < min_krw:
                    return {"ok": False, "error": f"buy amount ₩{amt:.0f} < min order ₩{min_krw:.0f}"}
                od = self.client.market_buy(market, amt)
                # ★ Register manual buys in self.positions too → shown in panel (regardless of slot count). Bot-management is toggled.
                pos = self._register_manual_position(market, amt, od)
                managed = bool(self.config.manual_manage_enabled)
                logger.info("[SPOT_GAZUA] 🟢 quick buy %s ₩%.0f%s → position registered (%s)",
                            market, amt, f" ({pct:.0f}%)" if pct > 0 else "",
                            "bot-managed" if managed else "hands-off")
                return {"ok": True, "side": "buy", "market": market, "krw": round(amt, 0),
                        "managed": managed, "registered": bool(pos), "order": od}
            else:
                if pct > 0:
                    q = float(self.client.get_balance(base_currency(market))) * pct / 100.0
                else:
                    q = float(qty)
                    if q <= 0:
                        q = float(self.client.get_balance(base_currency(market)))
                if q <= 0:
                    return {"ok": False, "error": "no holding to sell"}
                od = self.client.market_sell(market, q)
                logger.info("[SPOT_GAZUA] 🔴 quick sell %s qty=%.8f%s", market, q, f" ({pct:.0f}%)" if pct > 0 else "")
                return {"ok": True, "side": "sell", "market": market, "qty": q, "order": od}
        except Exception as exc:
            logger.error("[SPOT_GAZUA] quick_order %s %s FAILED: %s", market, side, exc)
            return {"ok": False, "error": str(exc)}

    def _register_manual_position(self, market: str, krw_spend: float, order: Dict[str, Any]):
        """Quick-trade manual buy → register in self.positions (panel display · regardless of slot count).
        Fill avg/qty are finalized to full fill via wait_order (same pattern as _execute_entry).
        manual_manage_enabled=True → compute SL/TP and place server TP (bot auto-management).
        False (hands-off) → SL/TP=0 (display '—'), no bot auto-close — human closes (close button)."""
        order = order if isinstance(order, dict) else {}
        order_uuid = str(order.get("uuid", "") or "")
        exec_qty = float(order.get("executed_volume", 0) or 0)
        fill_price = float(order.get("avg_price", 0) or 0)
        # A market buy may fill across several quotes → wait for full fill, then finalize the final avg/qty.
        if order_uuid:
            try:
                od2 = self.client.wait_order(uuid=order_uuid, market=market, timeout_sec=10.0, poll_interval=0.5)
                exec_qty = float(od2.get("executed_volume", 0) or 0) or exec_qty
                fill_price = float(od2.get("avg_price", 0) or 0) or fill_price
            except Exception as q_exc:
                logger.warning("[SPOT_GAZUA] quick buy wait_order reconcile %s failed: %s", market, q_exc)
        price = fill_price or self._get_price(market)
        qty = exec_qty if exec_qty > 0 else (krw_spend / price if price > 0 else 0.0)
        if price <= 0 or qty <= 0:
            logger.warning("[SPOT_GAZUA] manual position register skip %s (price=%.4f qty=%.8f)", market, price, qty)
            return None
        with self._lock:
            # If a position for the same market exists, prevent a duplicate row (exchange holdings sum but keep 1 panel row).
            if any(p.market == market for p in self.positions):
                logger.info("[SPOT_GAZUA] %s existing position — skip duplicate manual-buy registration", market)
                return None
            managed = bool(self.config.manual_manage_enabled)
            if managed:
                atr = self._estimate_atr(market, price) or price * 0.02
                t = self._compute_targets(price, atr)
                sl, tp1, tp2, atr_used = t.sl, t.tp1, t.tp2, t.atr_used
            else:
                sl = tp1 = tp2 = atr_used = 0.0
            pos = SpotGazuaPosition(
                market=market, direction="LONG", entry_price=price, qty=qty,
                tp1=tp1, tp2=tp2, sl=sl, atr_used=atr_used,
                entry_ts=time.time(), trailing_high=price, krw_spent=krw_spend,
                paper=False, order_uuid=order_uuid, manual=True,
            )
            if managed:
                self._place_tp_orders(pos)   # bot-managed: place TP1/TP2 server-side limit sells
            self.positions.append(pos)
            self.state = FocusState.POSITIONED
            self._record_journal("ENTRY", pos, price,
                                 reason="manual buy (bot-managed)" if managed else "manual buy (hands-off)")
            self._save_state()
            return pos

    # ── UI / Router ─────────────────────────────────────────
    def update_config(self, config: Optional[Dict[str, Any]] = None, **kw) -> Dict[str, Any]:
        """Accepts both a dict (Bybit FOCUS pattern) and kwargs."""
        with self._lock:
            merged: Dict[str, Any] = dict(config) if isinstance(config, dict) else {}
            merged.update(kw)
            prev_paper = self.config.paper
            for k, v in merged.items():
                if not hasattr(self.config, k):
                    continue
                # ★ Coerce by the dataclass current value's type — safe for query params (all strings) and UI input.
                #   (Required to receive 672-mirror fields via the generic router: "17"→17, "true"→True, etc)
                cur = getattr(self.config, k)
                try:
                    if isinstance(cur, bool):
                        v = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "on")
                    elif isinstance(cur, int):          # bool already handled above
                        v = int(float(v))
                    elif isinstance(cur, float):
                        v = float(v)
                    elif isinstance(cur, list):
                        v = v if isinstance(v, list) else [s.strip() for s in str(v).split(",") if s.strip()]
                    elif isinstance(cur, str):
                        v = str(v)
                except (ValueError, TypeError):
                    continue                            # can't convert → ignore (keep existing value)
                setattr(self.config, k, v)
            # ★ On paper↔live switch, clean up mode-mismatched positions (prevent mixing)
            if "paper" in merged and bool(merged["paper"]) != bool(prev_paper):
                if not self.config.paper:  # paper→live: discard virtual positions (they don't really exist)
                    removed = [p.market for p in self.positions if p.paper]
                    self.positions = [p for p in self.positions if not p.paper]
                    if removed:
                        logger.info("[SPOT_GAZUA] LIVE switch — cleaned up virtual (paper) positions: %s", removed)
                else:  # live→paper: bot keeps really managing live positions (by pos.paper), warn only
                    live = [p.market for p in self.positions if not p.paper]
                    if live:
                        logger.warning("[SPOT_GAZUA] PAPER switch — live positions %s remain (bot keeps managing them). Check the Upbit app", live)
            self._save_state()
            return asdict(self.config)

    def get_status(self, *, with_account: bool = True) -> Dict[str, Any]:
        poss = []
        upnl_krw = 0.0
        fee_r = max(0.0, float(getattr(self.config, "fee_rate_pct", 0.0))) / 100.0
        for p in self.positions:
            d = p.to_dict()
            cur = self._get_price(p.market)
            d["current_price"] = cur
            # net: price change % − round-trip fee % (buy fee_r + sell fee_r×ratio). Uses owner-entered fee_rate_pct.
            if cur and p.entry_price > 0:
                ratio = cur / p.entry_price
                gross_pct = (ratio - 1.0) * 100.0
                fee_pct = (fee_r + fee_r * ratio) * 100.0
                d["pnl_pct"] = round(gross_pct - fee_pct, 2)
                upnl_krw += (cur - p.entry_price) * p.qty - (p.entry_price + cur) * p.qty * fee_r
            else:
                d["pnl_pct"] = 0.0
            # progress to TP1 (%) — entry→tp1 range (negative = below avg)
            if cur and p.tp1 and p.tp1 > p.entry_price:
                d["progress_pct"] = round((cur - p.entry_price) / (p.tp1 - p.entry_price) * 100, 1)
            else:
                d["progress_pct"] = 0.0
            d["hold_sec"] = round(time.time() - p.entry_ts, 1)
            poss.append(d)
        jr = self.journal_summary()
        out = {
            "enabled": self.config.enabled,
            "paper": self.config.paper,
            "state": self.state.value,
            "positions": poss,
            "unrealized_krw": round(upnl_krw, 0),
            "daily_plans_used": self.daily_plans_used,
            "daily_sl_count": self.daily_sl_count,
            "today_pnl_krw": jr["today_pnl_krw"],
            "total_pnl_krw": jr["total_pnl_krw"],
            # ★ [2026-06-21] GateLedger snapshot — "why was it silent" control panel (per-gate pass/reject). Only when ON.
            "gate_stats": (self._gate_ledger.snapshot()
                           if (getattr(self, "_gate_ledger", None) is not None
                               and getattr(self.config, "gate_ledger_enabled", False))
                           else None),
            "config": asdict(self.config),
        }
        # ★ [2026-06-20] recommended budget for manual (quick-trade) entry — the bot slot budget as-is (_effective_budget, total assets÷slots).
        #   UI pre-fills it as the default buy amount (owner: "recommend the budget on manual entry too"). No new size logic.
        try:
            out["rec_budget"] = round(self._effective_budget())
        except Exception:
            out["rec_budget"] = 0
        if with_account and not self.config.paper:
            out["account"] = self.account_summary()
        return out


# ── Upbit spot = one peer of SpotGazuaManager (a sibling on par with Bithumb/Bybit/Binance) ──────────
#   Defaults (UpbitTradeClient · runtime/upbit/ · KRW) are provided by SpotGazuaManager → thin subclass.
class UpbitGazuaManager(SpotGazuaManager):
    """Upbit spot long-only FOCUS. SpotGazuaManager body as-is (default client/state is Upbit)."""
    pass
