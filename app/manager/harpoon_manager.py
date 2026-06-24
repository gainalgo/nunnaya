"""
Harpoon — FOCUS Sub-Strategy Scalper

A strategy that repeats high-leverage scalps with M1 PA patterns near the H4
zones detected by FOCUS. It consumes FOCUS data read-only and never modifies
FOCUS state.

Architecture:
    FOCUS (the fisherman 🎣) → zones, h4_sig, ATR, state
    Harpoon (the harpoon 🔱) → M1 PA pattern detection → high-leverage scalps near zones

State Machine:
    STANDBY → ZONE_READY → STALKING → FIRED → COOLDOWN → STANDBY
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join("runtime", "harpoon_config.json")


# ---------------------------------------------------------------------------
# Enums & Dataclasses
# ---------------------------------------------------------------------------

class HarpoonState(str, Enum):
    STANDBY = "STANDBY"          # No FOCUS zone, or price is far away
    ZONE_READY = "ZONE_READY"    # Price within zone ATR×0.5
    STALKING = "STALKING"        # Waiting for an M1 PA pattern
    FIRED = "FIRED"              # Scalp entry completed
    COOLDOWN = "COOLDOWN"        # Waiting after scalp closed


@dataclass
class HarpoonConfig:
    enabled: bool = False               # ★ 2026-04-23 owner decision ① reversal: HARPOON OFF for now (same on both servers). Prevents True boot if the runtime file is lost.
    leverage: int = 20
    budget_pct: float = 10.0            # 10% of the FOCUS budget
    budget_usdt: float = 0.0            # When set directly (0=auto from FOCUS)
    tp_atr_mult: float = 0.15           # TP = ATR × 0.15
    sl_atr_mult: float = 0.10           # SL = ATR × 0.10
    risk_pct: float = 0.5              # 0.5% risk of budget per scalp
    zone_proximity_atr: float = 0.5     # Zone proximity test: ATR × 0.5
    max_scalps_per_hour: int = 5
    max_daily_scalps: int = 15
    max_consecutive_loss: int = 3       # Consecutive losses → 1 hour pause
    max_daily_loss_pct: float = 2.0     # Max daily loss %
    tick_interval_sec: float = 5.0      # STALKING/ZONE_READY tick interval
    standby_interval_sec: float = 30.0  # STANDBY tick interval
    cooldown_sec: float = 30.0          # Cooldown after a scalp
    entry_tf: str = "1"                 # M1 timeframe
    spread_max_atr_pct: float = 2.0     # Max spread (ATR %)
    server_side_tpsl: bool = True       # Server-side TP/SL required
    # ── ADX Filter (independent threshold) ──
    min_adx: int = 0                       # 0=inherit FOCUS dormant_adx_threshold, >0=Harpoon independent decision
    # ── Dynamic Trailing SL (for scalpers — tighter than FOCUS) ──
    dynamic_trailing: bool = False         # Dynamic trailing ON/OFF
    breakeven_trigger_pct: float = 0.08    # At 0.08% profit, SL→breakeven (fast lock for scalpers)
    trailing_preserve_pct: float = 50.0    # Preserve 50% of peak profit
    # ★ Stage 0 (2026-04-22 owner decision B, sibling plan v3 integration) ★
    # paper_mode + 9 defenses integrated. Same review pattern as this agent's letter #11 (Phase K/L).
    # default OFF — enabled after owner approval. paper_mode=True means 0 entries, stats only.
    paper_mode: bool = False               # ★ 2026-04-23 owner directive: paper concept removed — ON=live trading, OFF=fully halted
    # Stage 0-1: B11 regime_lock integration (FOCUS focus_mgr._get_btc_regime_lock_reason)
    respect_b11_regime_lock: bool = True   # BULL → block SHORT, BEAR → block LONG
    # Stage 0-2: strengthen min_adx (only the default of the existing min_adx field 0 → 20)
    # ↑ Keep the existing field (only used as a fallback when the user sets 0)
    min_adx_v2: int = 20                   # Stage 0-2 recommended value (used when existing min_adx=0)
    # Stage 0-3: share J v2 ADX-decline skip (FOCUS adx_slope_check_enabled)
    respect_focus_adx_slope: bool = True   # If FOCUS J v2 is on, apply the same criteria
    # Stage 0-4: Morning Guard auto-standby window
    respect_morning_guard: bool = True     # Auto-standby during 06:50~morning_guard_end
    # Stage 0-5: share coin_loss_cap
    respect_coin_loss_cap: bool = True     # Share FOCUS's 24h cumulative loss cap
    # Stage 0-6: HARPOON-only Fast-Reject (forensic 04-14 peak 0% pattern)
    fast_reject_v2_enabled: bool = True    # 60s after entry, peak 0% + loss → immediate cut
    fast_reject_v2_max_sec: float = 60.0
    fast_reject_v2_peak_threshold_pct: float = 0.05
    fast_reject_v2_pnl_pct: float = -0.05
    # Stage 0-7: 30 min re-entry cooldown after the first SL
    post_sl_cooldown_min: float = 30.0     # 0 = disabled
    # Stage 0-9: extend Morning Guard window (HARPOON only, option B)
    morning_extended_end_hour_kst: float = 10.5  # FOCUS 09:30 → HARPOON 10:30
    # Stage 0-10: strengthen entry signal (agreement of 2 PA patterns)
    pa_double_confirm_enabled: bool = False         # default OFF (review after 1 week of paper)
    pa_double_confirm_window_sec: float = 60.0

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M (2026-04-24) — Multi-Market + Budget separation, full overhaul
    #   Owner decision: turn HARPOON from a "FOCUS sub" into an
    #   "independent multi-market engine". See letter #23 plan.
    # ═══════════════════════════════════════════════════════════════════

    # [Phase M.A] Scan Universe — HARPOON's own scan targets
    scan_universe: str = "all"              # "all" / "top20" / "top50" / "custom"
    scan_blacklist: List[str] = field(default_factory=list)     # Excluded coins
    scan_whitelist: List[str] = field(default_factory=list)     # Dedicated coins (if empty, use all or universe)
    scan_min_volume_usdt_24h: float = 1_000_000.0  # Liquidity floor (min turnover)

    # [Phase M.B] Multi-position management
    max_concurrent_scalps: int = 3          # Max concurrent scalps held
    max_same_direction_scalps: int = 2      # Max in the same direction
    cooldown_per_coin_sec: float = 300.0    # Re-entry cooldown for the same coin

    # [Phase M.C] FOCUS coordination
    respect_focus_coin_lock: bool = True    # HARPOON skips coins FOCUS holds
    respect_focus_direction_lock: bool = True  # Forbid scalps opposite to FOCUS direction
    coin_exclusive_priority: str = "first_come"  # "first_come"/"focus"/"harpoon"
    focus_entry_freeze_sec: float = 30.0    # HARPOON skips for N sec right after a FOCUS entry

    # [Phase M.D] HARPOON's own signal thresholds
    min_adx_self: int = 20                   # HARPOON's own ADX threshold (separate from min_adx=0 inherit)
    min_conviction_self: float = 50.0        # [2026-05-17 100-pt scale ×10] 5→50. HARPOON's own conviction (separate from FOCUS scanner_min)
    pa_patterns_allowed: List[str] = field(default_factory=lambda: [
        "ENGULFING", "PIN_BAR", "BOS_BULLISH", "BOS_BEARISH",
        "STAR_V2", "SQUEEZE_BREAK"   # HARPOON favors these — fast reversal/breakout
    ])

    # ★ [2026-04-24 owner directive] HARPOON-Specific PA Weight Override
    # Owner intuition: "the signal Harpoon should catch ≠ FOCUS's prey"
    # When adding PA to the FOCUS conviction score, use these values if the caller is HARPOON.
    # Separate from FOCUS pa_weight (focus_config.json).
    #
    # Per-pattern preference (HARPOON short-term scalper view):
    #   PIN_BAR        — M5 short wick reject : HARPOON favors (FOCUS 1 → 2)
    #   ENGULFING      — strong momentum 2-bar : HARPOON favors (FOCUS 2 → 3)
    #   STAR_V1        — H4 big reversal       : FOCUS prey (HARPOON 1)
    #   STAR_V2        — trend reversal 3-bar  : strong for both (3)
    #   SQUEEZE_BREAK  — compression → breakout : HARPOON favors (3)
    #   BOS_BULLISH/BEARISH — support/resistance break : HARPOON favors (3)
    pa_weight_pin_bar: int = 2
    pa_weight_engulfing: int = 3
    pa_weight_star_v1: int = 1
    pa_weight_star_v2: int = 3
    pa_weight_squeeze_break: int = 3
    pa_weight_bos: int = 3
    pa_weight_zone_bonus: int = 1
    pa_zone_proximity_atr: float = 0.5
    pa_location_penalty_far: float = 0.5

    # [Phase M.E] Zone calculation source
    zone_source: str = "self"                # "self" (self-calculated) / "focus" (share FOCUS zone)
    zone_lookback_bars: int = 50             # zone calc lookback

    # [Phase M.F] ★ HARPOON Standalone Mode (2026-04-24 direct owner request)
    #   Default False: FOCUS Sub-scalper (requires FOCUS enabled=True)
    #   True: runs standalone regardless of FOCUS (HARPOON works even if FOCUS is off)
    #   Cons (owner aware): FOCUS shared guards disabled, sparse market context
    #   Pros: 24/7 full operation, fills FOCUS blind spots, pure Phase M validation
    harpoon_standalone_mode: bool = False


@dataclass
class ScalpPosition:
    market: str = ""
    direction: str = ""         # "LONG" or "SHORT"
    entry_price: float = 0.0
    qty: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    atr_used: float = 0.0
    entry_ts: float = 0.0
    scalp_id: int = 0          # serial number
    # ── Dynamic Trailing SL ──
    peak_profit_price: float = 0.0   # peak profit price
    breakeven_locked: bool = False   # SL moved to breakeven
    original_sl: float = 0.0        # original SL
    # ★ Phase M.G (2026-04-24) — timestamps for BE Stall Exit / Pre-BE Stall
    last_peak_update_ts: float = 0.0  # last time peak_profit_price was updated
    be_locked_ts: float = 0.0          # time the BE lock was set


@dataclass
class ScalpRecord:
    """Record of a completed scalp."""
    scalp_id: int = 0
    market: str = ""
    direction: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    qty: float = 0.0
    pnl_usdt: float = 0.0
    result: str = ""            # "TP" / "SL" / "MANUAL"
    duration_sec: float = 0.0
    ts: float = 0.0


# ---------------------------------------------------------------------------
# FOCUS state whitelist — FOCUS states in which Harpoon may activate
# ---------------------------------------------------------------------------
# v2: DORMANT removed — scalping also bleeds fees in ADX<15 ranging markets
# Activate from ALERT (sign the range is ending) onward
FOCUS_ACTIVE_STATES = {"ALERT", "HUNT"}


# ---------------------------------------------------------------------------
# HarpoonManager
# ---------------------------------------------------------------------------

class HarpoonManager:
    """
    FOCUS sub-strategy scalping manager.

    Consumes FOCUS zone/h4_sig/ATR read-only and repeats high-leverage scalps
    with M1 PA patterns near the zones.
    """

    def __init__(self, focus_manager: Any = None, system: Any = None):
        self.focus = focus_manager      # FocusManager (read-only)
        self.system = system
        self._lock = threading.RLock()

        # Config
        self.config = HarpoonConfig()

        # State
        self.state = HarpoonState.STANDBY
        self.current_scalp: Optional[ScalpPosition] = None
        self.target_zone: Optional[Dict] = None    # current target zone
        self.target_direction: str = ""

        # Counters
        self.scalps_this_hour: int = 0
        self.scalps_today: int = 0
        self.consecutive_losses: int = 0
        self.daily_pnl: float = 0.0
        self.total_pnl: float = 0.0
        self._next_scalp_id: int = 1

        # Timing
        self.last_tick_ts: float = 0.0
        self.cooldown_start_ts: float = 0.0
        self.hour_reset_ts: float = 0.0
        self.daily_reset_ts: float = 0.0
        self.loss_pause_until: float = 0.0   # time the pause lifts after consecutive losses

        # History
        self.recent_scalps: List[Dict] = []  # last 50 records

        # ═══════════════════════════════════════════════════════════════════
        # ★ Phase M.B (2026-04-24) — Multi-position management
        #   Keep current_scalp in parallel (no impact on existing logic). Gradual transition in Phase 4+.
        # ═══════════════════════════════════════════════════════════════════
        self.active_scalps: List[ScalpPosition] = []     # concurrently held scalps (new)
        self.last_scalp_exit_by_coin: Dict[str, float] = {}  # {MARKET: last_exit_ts} — cooldown_per_coin
        self._post_sl_cooldown_by_coin: Dict[tuple, float] = {}  # {(MKT, DIR): expire_ts} — shared post_sl_cooldown_min

        # Trade client (lazy init)
        self._client = None
        self._exit_retry_count: int = 0

        # Load persisted state
        self._load_config()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def effective_budget(self) -> float:
        """Compute Harpoon's effective budget."""
        if self.config.budget_usdt > 0:
            return self.config.budget_usdt
        # Auto-allocate from the FOCUS budget
        if self.focus:
            focus_budget = getattr(self.focus, 'budget_usdt', 0) or 0
            if focus_budget > 0:
                return focus_budget * (self.config.budget_pct / 100.0)
            # If budget=0, allocate from the system balance
            if self.system:
                try:
                    bal = getattr(self.system, '_cached_balance', {})
                    total = float(bal.get('totalWalletBalance', 0) or 0)
                    if total > 0:
                        return total * (self.config.budget_pct / 100.0)
                except (TypeError, ValueError, AttributeError):
                    pass
        return 50.0  # fallback $50

    # ------------------------------------------------------------------
    # Lazy client
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            from app.integrations.bybit_trade import BybitTradeClient
            self._client = BybitTradeClient(category="linear")
            logger.info("[HARPOON] Linear perpetual client initialized")
        return self._client

    # ------------------------------------------------------------------
    # Price access
    # ------------------------------------------------------------------

    def _get_current_price(self, market: str) -> Optional[float]:
        try:
            from app.core.hyper_price_store import price_store
            p = price_store.get_price(market)
            if p and p > 0:
                return float(p)
        except Exception:
            pass
        try:
            p = self._get_client()._linear_last_price(market)
            if p and p > 0:
                return float(p)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    def tick(self) -> Dict[str, Any]:
        """
        Harpoon main tick. Called from FocusManager's _scan_loop or a separate loop.

        Returns:
            status info dict
        """
        if not self.config.enabled:
            return {"state": "DISABLED"}

        # E-STOP check
        if self.system and bool(getattr(self.system, 'emergency_stop', False)):
            self._emergency_close()
            return {"state": "EMERGENCY_STOP"}

        with self._lock:
            now = time.time()
            self._maybe_reset_counters(now)

            # Throttle by state
            interval = self.config.standby_interval_sec if self.state == HarpoonState.STANDBY \
                else self.config.tick_interval_sec
            if (now - self.last_tick_ts) < interval:
                return {"state": self.state.value, "skipped": True}
            self.last_tick_ts = now

            # ★ FOCUS conflict defense: if FOCUS enters a real position while HARPOON holds one, close immediately
            if self.current_scalp and self._has_focus_conflict():
                logger.warning(
                    "[HARPOON] FOCUS conflict! Closing scalp %s %s immediately",
                    self.current_scalp.market, self.current_scalp.direction,
                )
                price = self._get_current_price(self.current_scalp.market) or 0
                self._execute_scalp_exit("FOCUS_CONFLICT", price)
                self.state = HarpoonState.STANDBY
                return {"state": "STANDBY", "reason": "focus_conflict_close"}

            # FOCUS linkage check
            if not self._is_focus_compatible():
                if self.state != HarpoonState.STANDBY:
                    self.state = HarpoonState.STANDBY
                return {"state": "STANDBY", "reason": "focus_incompatible"}

            # Guard check
            guard = self._check_guards(now)
            if guard:
                return {"state": self.state.value, "guard": guard}

            # State dispatch
            result = {}
            try:
                if self.state == HarpoonState.STANDBY:
                    result = self._handle_standby(now)
                elif self.state == HarpoonState.ZONE_READY:
                    result = self._handle_zone_ready(now)
                elif self.state == HarpoonState.STALKING:
                    result = self._handle_stalking(now)
                elif self.state == HarpoonState.FIRED:
                    result = self._handle_fired(now)
                elif self.state == HarpoonState.COOLDOWN:
                    result = self._handle_cooldown(now)
            except Exception as exc:
                logger.error("[HARPOON] tick error: %s", exc, exc_info=True)

            # ★ Phase M.F (2026-04-24): Multi-market / Multi-position extra handling
            #   Runs after the existing state dispatch — primary (current_scalp) as before,
            #   additional scalps handled separately below.
            try:
                # 1) Check Bybit server state for additional scalps (detect secondary exits)
                cleared_count = self._monitor_additional_scalps(now)
                if cleared_count > 0:
                    result["multi_cleared"] = cleared_count

                # 2) Try extra entries (only if max_concurrent>1 and slots remain)
                #    Skipped inside _try_extra_entries if paper_mode or FOCUS-incompatible
                if self._is_focus_compatible() and self.state not in (HarpoonState.COOLDOWN,):
                    entered_count = self._try_extra_entries(now)
                    if entered_count > 0:
                        result["multi_entered"] = entered_count
            except Exception as exc:
                logger.debug("[HARPOON M.F] tick multi-processing exception: %s", exc)

            result["state"] = self.state.value
            result["scalps_today"] = self.scalps_today
            result["daily_pnl"] = round(self.daily_pnl, 4)
            result["active_scalps_count"] = self._count_active_scalps()
            return result

    # ------------------------------------------------------------------
    # FOCUS compatibility check
    # ------------------------------------------------------------------

    def _is_focus_compatible(self) -> bool:
        """Check whether the FOCUS state is suitable for Harpoon activation.

        v2: ADX filter added — if FOCUS's ADX is below dormant_adx_threshold,
        Harpoon also halts (scalping in a ranging market = fee loss).

        ★ Phase M.F (2026-04-24 direct owner request): Standalone mode support.
           If harpoon_standalone_mode=True, bypass the FOCUS check → HARPOON runs standalone.
        """
        # ★ Standalone mode: bypass all FOCUS checks
        if getattr(self.config, "harpoon_standalone_mode", False):
            return True

        if not self.focus:
            return False
        if not getattr(self.focus, 'enabled', False):
            return False

        focus_state = getattr(self.focus, 'state', None)
        if focus_state is None:
            return False

        state_val = focus_state.value if hasattr(focus_state, 'value') else str(focus_state)

        # If FOCUS holds a position, leverage conflict → halt
        if state_val in ("POSITIONED", "CAUTION"):
            return False

        # If FOCUS is in cooldown, market unfavorable → halt
        if state_val == "COOLDOWN":
            return False

        # v2→v3: ADX filter — if Harpoon-specific min_adx > 0, use independent threshold, else inherit FOCUS
        focus_config = getattr(self.focus, 'config', None)
        if focus_config and getattr(focus_config, 'adx_filter_enabled', False):
            last_adx = getattr(self.focus, '_last_adx_value', 0)
            # Prefer Harpoon's independent threshold; if absent (0), inherit FOCUS dormant_adx_threshold
            harpoon_min = self.config.min_adx
            threshold = harpoon_min if harpoon_min > 0 else getattr(focus_config, 'dormant_adx_threshold', 15)
            if last_adx > 0 and last_adx < threshold:
                return False  # ranging market — scalping is also risky

        return state_val in FOCUS_ACTIVE_STATES

    def _has_focus_conflict(self) -> bool:
        """FOCUS actually holds a position → leverage conflict."""
        if not self.focus:
            return False  # No FOCUS = no conflict
        state_val = getattr(self.focus, 'state', None)
        if state_val and hasattr(state_val, 'value'):
            state_val = state_val.value
        return str(state_val) in ("POSITIONED", "CAUTION", "TRAILING")

    # ------------------------------------------------------------------
    # Guard checks
    # ------------------------------------------------------------------

    def _check_guards(self, now: float) -> Optional[str]:
        """Guard check. Returns a reason string on violation."""
        # Consecutive-loss pause
        if self.loss_pause_until > now:
            return f"consecutive_loss_pause_until_{int(self.loss_pause_until - now)}s"

        # Per-hour scalp limit
        if self.scalps_this_hour >= self.config.max_scalps_per_hour:
            return "max_scalps_per_hour"

        # Daily scalp limit
        if self.scalps_today >= self.config.max_daily_scalps:
            return "max_daily_scalps"

        # Daily loss limit
        budget = self.effective_budget
        if budget > 0:
            loss_pct = abs(min(0, self.daily_pnl)) / budget * 100
            if loss_pct >= self.config.max_daily_loss_pct:
                return f"max_daily_loss_{loss_pct:.1f}pct"

        return None

    # ------------------------------------------------------------------
    # ★ Stage 0 (2026-04-22 owner decision B, sibling plan v3 integration) ★
    # 9 guards right before entry + paper_mode JSONL logging
    # B11/J v2/Morning Guard/coin_loss_cap = shared with FOCUS
    # Fast-Reject v2 / re-entry cooldown / Morning Guard extension = HARPOON-specific
    # ★ Fix for this agent's letter FAIL (2026-04-22 22:42):
    #   - _get_focus_adx new method (enables Stage 0-2)
    #   - 0-3 J v2 minimal implementation (stub → active)
    # ------------------------------------------------------------------

    def _get_focus_adx(self) -> float:
        """Get FOCUS's latest H4 ADX value (for Stage 0-2/0-3).

        ★ Fix for this agent's letter FAIL (2026-04-22 22:42) — added missing method.
        FOCUS exposes it via the _last_adx_value field (already used at L355 etc.).
        """
        try:
            if not self.focus:
                return 0.0
            return float(getattr(self.focus, "_last_adx_value", 0) or 0)
        except Exception:
            return 0.0

    def _check_stage0_gates(self, market: str, direction: str,
                            atr: float, price: float) -> tuple:
        """Combined check of the 9 Stage 0 guards right before entry.

        Returns: (blocked: bool, reason: str)
        """
        cfg = self.config
        focus_mgr = self.focus  # FocusManager reference (only when present)

        # Stage 0-1: B11 regime_lock integration
        if getattr(cfg, "respect_b11_regime_lock", True) and focus_mgr is not None:
            try:
                if hasattr(focus_mgr, "_get_btc_regime_lock_reason"):
                    blocked, reason = focus_mgr._get_btc_regime_lock_reason(direction)
                    if blocked:
                        return True, f"b11_regime_lock: {reason}"
            except Exception as exc:
                logger.debug("[HARPOON] B11 check failed: %s", exc)

        # Stage 0-2: strengthen min_adx (default 0 → 20 fallback)
        try:
            adx_threshold = cfg.min_adx if cfg.min_adx > 0 else getattr(cfg, "min_adx_v2", 20)
            current_adx = self._get_focus_adx() if hasattr(self, "_get_focus_adx") else 0
            if current_adx > 0 and current_adx < adx_threshold:
                return True, f"adx_below_threshold: {current_adx:.1f} < {adx_threshold}"
        except Exception:
            pass  # On ADX fetch failure, pass (safe fallback)

        # Stage 0-3: share FOCUS J v2 (★ applies this agent's letter FAIL 4-4)
        if getattr(cfg, "respect_focus_adx_slope", True) and focus_mgr is not None:
            try:
                if getattr(focus_mgr.config, "adx_slope_check_enabled", False):
                    # If FOCUS J v2 is ON, HARPOON also skips when the market is cooling
                    # Minimal impl: block if ADX is below the J v2 threshold (FOCUS scanner_min_adx)
                    adx = self._get_focus_adx()
                    j_threshold = float(getattr(focus_mgr.config, "scanner_min_adx", 18.0))
                    if adx > 0 and adx < j_threshold:
                        return True, f"focus_j_v2_adx_cooling: {adx:.1f} < {j_threshold}"
            except Exception:
                pass

        # Stage 0-4: Morning Guard auto-standby + Stage 0-9 time extension (HARPOON only)
        if getattr(cfg, "respect_morning_guard", True) and focus_mgr is not None:
            try:
                from datetime import datetime, timezone, timedelta
                kst_now = datetime.now(tz=timezone(timedelta(hours=9)))
                hour = kst_now.hour + kst_now.minute / 60.0
                # FOCUS Morning Guard window (06:50 ~ end_hour) + HARPOON extension
                mg_enabled = getattr(focus_mgr.config, "morning_guard_enabled", True)
                if mg_enabled:
                    fg_end = float(getattr(focus_mgr.config, "morning_guard_end_hour_kst", 9.5))
                    hp_end = float(getattr(cfg, "morning_extended_end_hour_kst", fg_end))
                    end_hour = max(fg_end, hp_end)  # HARPOON more conservative (longer window)
                    if 6.83 <= hour <= end_hour:  # 06:50~
                        return True, f"morning_guard_active (KST {hour:.2f}h, end={end_hour})"
            except Exception as exc:
                logger.debug("[HARPOON] Morning Guard check failed: %s", exc)

        # Stage 0-5: share coin_loss_cap
        if getattr(cfg, "respect_coin_loss_cap", True) and focus_mgr is not None:
            try:
                if (getattr(focus_mgr.config, "coin_loss_cap_enabled", True)
                        and hasattr(focus_mgr, "_get_coin_loss_total")):
                    loss_total = focus_mgr._get_coin_loss_total(market, direction)
                    cap = float(getattr(focus_mgr.config, "coin_loss_cap_amount", 30.0))
                    if loss_total < 0 and abs(loss_total) >= cap:
                        return True, f"coin_loss_cap_exceeded: ${loss_total:.2f} >= ${cap}"
            except Exception as exc:
                logger.debug("[HARPOON] coin_loss_cap check failed: %s", exc)

        # Stage 0-7: 30 min re-entry cooldown (after HARPOON's own SL/Fast-Reject)
        cd_min = float(getattr(cfg, "post_sl_cooldown_min", 30.0))
        if cd_min > 0:
            cd_sec = cd_min * 60.0
            now_ts = time.time()
            cooldown_map = getattr(self, "_post_sl_cooldown_map", {})
            key = (market.upper(), direction.upper())
            last_sl_ts = cooldown_map.get(key, 0)
            if last_sl_ts > 0 and (now_ts - last_sl_ts) < cd_sec:
                remain = cd_sec - (now_ts - last_sl_ts)
                return True, f"post_sl_cooldown: {remain/60:.1f}min remain"

        return False, "all_passed"

    def _log_paper_entry(self, market: str, direction: str, price: float, atr: float) -> None:
        """Log a paper_mode entry attempt to JSONL (Phase K pattern)."""
        try:
            log_path = os.path.join("runtime", "harpoon_paper_log.jsonl")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            entry = {
                "ts": time.time(),
                "market": market,
                "direction": direction,
                "price": price,
                "atr": atr,
                "tp_estimate": price + atr * self.config.tp_atr_mult * (1 if direction == "LONG" else -1),
                "sl_estimate": price - atr * self.config.sl_atr_mult * (1 if direction == "LONG" else -1),
                "leverage": self.config.leverage,
                "budget_estimate": self.effective_budget,
                "paper_mode": True,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("[HARPOON] paper log failed: %s", exc)

    def _record_post_sl(self, market: str, direction: str) -> None:
        """Update the cooldown map when SL/Fast-Reject fires (Stage 0-7)."""
        if not hasattr(self, "_post_sl_cooldown_map"):
            self._post_sl_cooldown_map = {}
        self._post_sl_cooldown_map[(market.upper(), direction.upper())] = time.time()

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _handle_standby(self, now: float) -> Dict:
        """STANDBY: watch whether price approaches a zone.
        ★ Phase M.E + M.F: standalone mode computes its own zones, otherwise FOCUS zones."""
        market = self._get_focus_market()
        if not market:
            return {"reason": "no_market"}
        zones = self._get_zones_for_harpoon(market)
        if not zones:
            return {"reason": "no_zones"}

        price = self._get_current_price(market)
        if not price:
            return {"reason": "no_price"}

        atr = self._get_focus_atr()
        if not atr or atr <= 0:
            return {"reason": "no_atr"}

        proximity = atr * self.config.zone_proximity_atr

        # Find the nearest zone (both directions — zone type decides direction)
        best_zone = self._find_nearest_zone(price, "", zones, proximity)
        if best_zone:
            # ★ Zone type decides scalp direction: SUPPORT→LONG, RESISTANCE→SHORT
            z_type = best_zone.get("type", "").upper()
            if "RESISTANCE" in z_type:
                scalp_dir = "SHORT"
            else:
                scalp_dir = "LONG"

            self.target_zone = best_zone
            self.target_direction = scalp_dir
            self.state = HarpoonState.ZONE_READY
            logger.info(
                "[HARPOON] STANDBY → ZONE_READY: %s %s zone %s $%.2f~$%.2f (price=$%.2f, dist=$%.2f)",
                market, scalp_dir, z_type,
                best_zone.get("price_low", 0), best_zone.get("price_high", 0),
                price, abs(price - best_zone.get("price_low", price)),
            )
            self._save_config()
            return {"transition": "ZONE_READY", "zone": best_zone}

        return {"reason": "no_nearby_zone"}

    def _handle_zone_ready(self, now: float) -> Dict:
        """ZONE_READY: reached near the zone, start waiting for M1 PA."""
        market = self._get_focus_market()
        if not market:
            self.state = HarpoonState.STANDBY
            return {"reason": "lost_market"}

        price = self._get_current_price(market)
        if not price:
            return {"reason": "no_price"}

        atr = self._get_focus_atr()
        if not atr:
            self.state = HarpoonState.STANDBY
            return {"reason": "no_atr"}

        # Check whether price moved away from the zone
        proximity = atr * self.config.zone_proximity_atr
        if self.target_zone:
            zone_mid = (self.target_zone.get("price_low", 0) + self.target_zone.get("price_high", 0)) / 2
            if abs(price - zone_mid) > proximity * 1.5:
                logger.info("[HARPOON] ZONE_READY → STANDBY: price moved away from zone")
                self.state = HarpoonState.STANDBY
                self.target_zone = None
                return {"reason": "price_moved_away"}

        # ── M5 trend filter (same role as FOCUS's H1 check) ──
        m5_trend = self._check_m5_trend(market, self.target_direction)
        if m5_trend == "opposed":
            logger.debug(
                "[HARPOON] ZONE_READY skip: M5 trend opposed to %s — waiting",
                self.target_direction,
            )
            return {"waiting": "m5_opposed"}

        # M1 PA pattern check
        pa_signal = self._check_m1_pa(market, self.target_direction)
        if pa_signal:
            self.state = HarpoonState.STALKING
            logger.info("[HARPOON] ZONE_READY → STALKING: M1 %s detected (m5=%s)",
                        pa_signal.get("pattern"), m5_trend or "n/a")
            return {"transition": "STALKING", "pa": pa_signal}

        return {"waiting": "m1_pa"}

    def _handle_stalking(self, now: float) -> Dict:
        """STALKING: M1 PA confirmed, execute entry."""
        market = self._get_focus_market()
        if not market:
            self.state = HarpoonState.STANDBY
            return {"reason": "lost_market"}

        price = self._get_current_price(market)
        if not price:
            return {"reason": "no_price"}

        atr = self._get_focus_atr()
        if not atr:
            self.state = HarpoonState.STANDBY
            return {"reason": "no_atr"}

        # ── M5 trend re-check (right before entry) ──
        m5_trend = self._check_m5_trend(market, self.target_direction)
        if m5_trend == "opposed":
            logger.info("[HARPOON] STALKING → ZONE_READY: M5 trend opposed to %s", self.target_direction)
            self.state = HarpoonState.ZONE_READY
            return {"reason": "m5_opposed", "transition": "ZONE_READY"}

        # ★ M17 FIX: re-check M1 PA freshness before entry (prevent stale signal)
        pa_recheck = self._check_m1_pa(market, self.target_direction)
        if not pa_recheck:
            self.state = HarpoonState.ZONE_READY
            logger.info("[HARPOON] STALKING → ZONE_READY: M1 PA no longer valid")
            return {"reason": "pa_stale", "transition": "ZONE_READY"}

        # ★★★ Stage 0 (2026-04-22 owner decision B, plan v3 integration) ★★★
        # 9 guards right before entry + paper_mode final gate.
        # Same review pattern as this agent's letter #11, default safe (paper_mode=True).
        stage0_blocked, stage0_reason = self._check_stage0_gates(market, self.target_direction, atr, price)
        if stage0_blocked:
            logger.info("[HARPOON] STAGE 0 BLOCK: %s %s — %s", market, self.target_direction, stage0_reason)
            self.state = HarpoonState.COOLDOWN
            self.cooldown_start_ts = now
            return {"reason": "stage0_blocked", "detail": stage0_reason}

        # ★ paper_mode final gate (Phase K pattern: block right before entry + JSONL log only)
        if getattr(self.config, "paper_mode", True):
            self._log_paper_entry(market, self.target_direction, price, atr)
            logger.info(
                "[HARPOON] PAPER skip: %s %s @ $%.4f atr=$%.4f (no live trade, JSONL log only)",
                market, self.target_direction, price, atr,
            )
            self.state = HarpoonState.COOLDOWN
            self.cooldown_start_ts = now
            return {"reason": "paper_mode_skip", "market": market, "direction": self.target_direction}

        # Execute entry
        try:
            result = self._execute_scalp_entry(market, self.target_direction, price, atr)
            if result and result.get("success"):
                self.state = HarpoonState.FIRED
                logger.info(
                    "[HARPOON] STALKING → FIRED: %s %s @ $%.4f qty=%.6f TP=$%.4f SL=$%.4f",
                    market, self.target_direction, price,
                    result.get("qty", 0), result.get("tp", 0), result.get("sl", 0),
                )
                self._save_config()
                return {"transition": "FIRED", **result}
            else:
                # Entry failed → cooldown
                self.state = HarpoonState.COOLDOWN
                self.cooldown_start_ts = now
                return {"reason": "entry_failed", "detail": result}
        except Exception as exc:
            logger.error("[HARPOON] entry execution error: %s", exc, exc_info=True)
            self.state = HarpoonState.COOLDOWN
            self.cooldown_start_ts = now
            return {"reason": "entry_error", "error": str(exc)}

    def _handle_fired(self, now: float) -> Dict:
        """FIRED: holding a position, monitoring TP/SL."""
        if not self.current_scalp:
            self.state = HarpoonState.STANDBY
            return {"reason": "no_scalp_position"}

        market = self.current_scalp.market
        price = self._get_current_price(market)
        if not price:
            return {"reason": "no_price"}

        # ★ Check real Bybit position — prevent ghost if server-side TP/SL already closed it
        if not hasattr(self, '_last_bybit_check_ts'):
            self._last_bybit_check_ts = 0.0
        if now - self._last_bybit_check_ts >= 10.0:  # check every 10s
            self._last_bybit_check_ts = now
            try:
                from app.core.constants import BYBIT_POSITION_LIST
                client = self._get_client()
                resp = client._request("GET", BYBIT_POSITION_LIST,
                                       params={"category": "linear", "symbol": market})
                pos_list = resp.get("result", {}).get("list", [])
                has_position = any(float(p.get("size", 0)) > 0 for p in pos_list)
                if not has_position:
                    # No position on Bybit → already closed by server-side TP/SL
                    scalp = self.current_scalp
                    entry = scalp.entry_price
                    # Estimate which of TP/SL filled → use that price as exit_price
                    if scalp.direction == "LONG":
                        _tp_hit = price >= scalp.tp if scalp.tp else False
                        exit_price = scalp.tp if _tp_hit else (scalp.sl if scalp.sl else price)
                    else:
                        _tp_hit = price <= scalp.tp if scalp.tp else False
                        exit_price = scalp.tp if _tp_hit else (scalp.sl if scalp.sl else price)
                    exit_reason = "SERVER_TP" if _tp_hit else "SERVER_SL"

                    if scalp.direction == "LONG":
                        pnl = (exit_price - entry) * scalp.qty
                    else:
                        pnl = (entry - exit_price) * scalp.qty
                    fee = exit_price * scalp.qty * 0.00055 * 2
                    pnl -= fee
                    duration = now - scalp.entry_ts

                    logger.info(
                        "[HARPOON] FIRED → position gone (%s): %s %s @ $%.4f → $%.4f | PnL=$%.4f | %.1fs",
                        exit_reason, scalp.market, scalp.direction, entry, exit_price, pnl, duration,
                    )

                    # ── ScalpRecord + recent_scalps ──
                    record = ScalpRecord(
                        scalp_id=scalp.scalp_id, market=scalp.market,
                        direction=scalp.direction, entry_price=entry,
                        exit_price=exit_price, qty=scalp.qty,
                        pnl_usdt=round(pnl, 4), result=exit_reason,
                        duration_sec=round(duration, 1), ts=now,
                    )
                    self.recent_scalps.append(asdict(record))
                    if len(self.recent_scalps) > 50:
                        self.recent_scalps = self.recent_scalps[-50:]

                    # ── Counter update ──
                    self.scalps_today += 1
                    self.scalps_this_hour += 1
                    self.daily_pnl += pnl
                    self.total_pnl += pnl
                    if pnl < 0:
                        self.consecutive_losses += 1
                        # ★ Stage 0-7 hook (2026-04-22 fix for this agent's letter FAIL 4-3):
                        # On SL/Fast-Reject, record a 30 min cooldown for (market, direction)
                        try:
                            self._record_post_sl(scalp.market, scalp.direction)
                        except Exception as exc:
                            logger.debug("[HARPOON] _record_post_sl failed: %s", exc)
                    else:
                        self.consecutive_losses = 0

                    # ── Bookkeeping (journal + ledger) ──
                    try:
                        from app.manager.trade_journal import journal
                        _peak = 0.0
                        if scalp.peak_profit_price > 0 and entry > 0:
                            if scalp.direction == "LONG":
                                _peak = (scalp.peak_profit_price / entry - 1) * 100
                            else:
                                _peak = (1 - scalp.peak_profit_price / entry) * 100
                        journal.record_exit(
                            strategy="HARPOON", market=scalp.market,
                            direction=scalp.direction,
                            entry_price=entry, exit_price=exit_price, qty=scalp.qty,
                            reason=exit_reason, leverage=self.config.leverage,
                            hold_sec=duration,
                            dynamic_trailing=self.config.dynamic_trailing,
                            breakeven_locked=scalp.breakeven_locked,
                            peak_profit_pct=_peak,
                        )
                    except Exception:
                        pass
                    try:
                        if self.system and hasattr(self.system, 'ledger'):
                            self.system.ledger.append(
                                "HARPOON_EXIT", market=scalp.market,
                                direction=scalp.direction, qty=scalp.qty,
                                entry_price=entry, exit_price=exit_price,
                                pnl_usdt=round(pnl, 4), reason=exit_reason,
                                duration_sec=round(duration, 1),
                            )
                    except Exception:
                        pass

                    self._clear_current_scalp_from_active()  # Phase M.B sync
                    self.current_scalp = None
                    self.state = HarpoonState.COOLDOWN
                    self.cooldown_start_ts = now
                    self._save_config()
                    return {"exit": exit_reason, "pnl": round(pnl, 4)}
            except Exception as exc:
                logger.debug("[HARPOON] Bybit position check failed: %s", exc)

        # ── Dynamic Trailing SL (update SL before the exit check) ──
        if self.config.dynamic_trailing:
            self._apply_scalp_trailing_sl(self.current_scalp, price)

        from app.strategy.greenpen.harpoon_tp import ScalpTargets, should_scalp_exit

        targets = ScalpTargets(
            tp=self.current_scalp.tp,
            sl=self.current_scalp.sl,  # dynamic trailing may have updated it
            atr_used=self.current_scalp.atr_used,
        )

        exit_reason = should_scalp_exit(price, self.current_scalp.entry_price,
                                        self.current_scalp.direction, targets)
        if exit_reason:
            result = self._execute_scalp_exit(exit_reason, price)
            return {"exit": exit_reason, **result}

        # Timeout: market-close if held for more than 5 min
        hold_time = now - self.current_scalp.entry_ts
        if hold_time > 300:  # 5 min
            logger.warning("[HARPOON] scalp timeout (%.0fs), closing at market", hold_time)
            result = self._execute_scalp_exit("TIMEOUT", price)
            return {"exit": "TIMEOUT", **result}

        # Show current PnL
        entry = self.current_scalp.entry_price
        if self.current_scalp.direction == "LONG":
            unrealized = (price - entry) * self.current_scalp.qty
        else:
            unrealized = (entry - price) * self.current_scalp.qty

        return {"holding": True, "unrealized_pnl": round(unrealized, 4),
                "hold_sec": round(hold_time, 1)}

    # ------------------------------------------------------------------
    # Dynamic Trailing SL (scalper-only)
    # ------------------------------------------------------------------

    def _apply_scalp_trailing_sl(self, scalp: ScalpPosition, price: float):
        """Dynamic trailing SL for scalpers. Same structure as FOCUS, just tighter params.

        Stage 1: profit >= breakeven_trigger_pct → SL → entry price (breakeven lock)
        Stage 2: afterward, trail SL to preserve trailing_preserve_pct% of peak profit
        """
        # Record original SL (first time only)
        if scalp.original_sl <= 0:
            scalp.original_sl = scalp.sl

        # Compute return
        if scalp.direction == "LONG":
            pnl_pct = (price / scalp.entry_price - 1) * 100 if scalp.entry_price > 0 else 0
        else:
            pnl_pct = (1 - price / scalp.entry_price) * 100 if scalp.entry_price > 0 else 0

        # Track peak profit price
        if scalp.direction == "LONG":
            if price > scalp.peak_profit_price:
                scalp.peak_profit_price = price
        else:
            if scalp.peak_profit_price <= 0 or price < scalp.peak_profit_price:
                scalp.peak_profit_price = price

        trigger_pct = self.config.breakeven_trigger_pct
        preserve_ratio = self.config.trailing_preserve_pct / 100.0

        # ── Stage 1: breakeven lock ──
        if not scalp.breakeven_locked and pnl_pct >= trigger_pct:
            new_sl = scalp.entry_price
            should_lock = False
            if scalp.direction == "LONG" and new_sl > scalp.sl:
                should_lock = True
            elif scalp.direction == "SHORT" and new_sl < scalp.sl:
                should_lock = True

            if should_lock:
                old_sl = scalp.sl
                scalp.sl = new_sl
                scalp.breakeven_locked = True
                scalp.be_locked_ts = time.time()  # ★ Phase M.G: for BE Stall Exit
                logger.info(
                    "[HARPOON] BREAKEVEN LOCK %s %s: SL $%.2f->$%.2f (profit +%.3f%%)",
                    scalp.direction, scalp.market, old_sl, scalp.sl, pnl_pct,
                )
                self._update_scalp_bybit_sl(scalp)
                self._save_config()
                return

        # ── Stage 2: profit trailing ──
        if scalp.breakeven_locked and scalp.peak_profit_price > 0:
            if scalp.direction == "LONG":
                peak_gain = scalp.peak_profit_price / scalp.entry_price - 1
                trail_offset = peak_gain * preserve_ratio
                new_sl = scalp.entry_price * (1 + trail_offset)
                if new_sl > scalp.sl:
                    old_sl = scalp.sl
                    scalp.sl = round(new_sl, 4)
                    logger.info(
                        "[HARPOON] TRAIL %s %s: SL $%.2f->$%.2f (peak +%.3f%%, locked +%.3f%%)",
                        scalp.direction, scalp.market, old_sl, scalp.sl,
                        peak_gain * 100, trail_offset * 100,
                    )
                    self._update_scalp_bybit_sl(scalp)
                    self._save_config()
            else:  # SHORT
                peak_gain = 1 - scalp.peak_profit_price / scalp.entry_price
                trail_offset = peak_gain * preserve_ratio
                new_sl = scalp.entry_price * (1 - trail_offset)
                if new_sl < scalp.sl:
                    old_sl = scalp.sl
                    scalp.sl = round(new_sl, 4)
                    logger.info(
                        "[HARPOON] TRAIL %s %s: SL $%.2f->$%.2f (peak +%.3f%%, locked +%.3f%%)",
                        scalp.direction, scalp.market, old_sl, scalp.sl,
                        peak_gain * 100, trail_offset * 100,
                    )
                    self._update_scalp_bybit_sl(scalp)
                    self._save_config()

    def _update_scalp_bybit_sl(self, scalp: ScalpPosition):
        """Update only the SL on Bybit (keep TP). Retry once on failure."""
        for _try in range(2):
            try:
                self._get_client().set_trading_stop(
                    scalp.market, take_profit=scalp.tp, stop_loss=scalp.sl,
                )
                return
            except Exception as exc:
                logger.warning("[HARPOON] Bybit SL update failed %s (attempt %d/2): %s",
                               scalp.market, _try + 1, exc)
                if _try == 0:
                    import time as _t; _t.sleep(0.5)

    def _handle_cooldown(self, now: float) -> Dict:
        """COOLDOWN: wait after a scalp closes."""
        elapsed = now - self.cooldown_start_ts
        if elapsed >= self.config.cooldown_sec:
            self.state = HarpoonState.STANDBY
            self.target_zone = None
            logger.info("[HARPOON] COOLDOWN → STANDBY (%.0fs)", elapsed)
            return {"transition": "STANDBY"}
        return {"cooldown_remaining": round(self.config.cooldown_sec - elapsed, 1)}

    # ------------------------------------------------------------------
    # Zone helpers
    # ------------------------------------------------------------------

    def _get_focus_zones(self) -> List[Dict]:
        # ★ A2 FIX: shallow copy — prevent race
        if not self.focus:
            return []
        zones = getattr(self.focus, 'zones', []) or []
        return list(zones)

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.E (2026-04-24) — self zone calculation (Standalone mode support)
    # ═══════════════════════════════════════════════════════════════════

    def _compute_self_zones(self, market: str) -> List[Dict]:
        """HARPOON's own M1 lookback zone calculation.

        Without depending on FOCUS, cluster Swing Highs (resistance) /
        Swing Lows (support) over M1 N bars (zone_lookback_bars, default 50).

        Returns: List[Dict] with the same structure as FOCUS zones
          [{type: "SUPPORT"/"RESISTANCE", price_low, price_high, strength}]
        """
        if not market:
            return []
        try:
            cfg = self.config
            lookback = int(getattr(cfg, "zone_lookback_bars", 50))
            client = self._get_client()
            klines = client.get_kline(market, interval=getattr(cfg, "entry_tf", "1"), limit=lookback)
            if not klines or len(klines) < 10:
                return []

            # Extract OHLC (Bybit format: [ts, open, high, low, close, volume, ...])
            highs = []
            lows = []
            closes = []
            for k in klines:
                try:
                    highs.append(float(k[2]))
                    lows.append(float(k[3]))
                    closes.append(float(k[4]))
                except (IndexError, ValueError, TypeError):
                    continue
            if len(highs) < 10 or len(lows) < 10:
                return []

            # Estimate ATR (simplified: mean of high-low)
            ranges = [h - l for h, l in zip(highs, lows)]
            atr = sum(ranges) / len(ranges) if ranges else 0
            if atr <= 0:
                return []
            tolerance = atr * 0.3   # price clustering tolerance

            # Extract Swing High / Low (compare 2 bars on each side)
            swing_highs = []
            swing_lows = []
            for i in range(2, len(highs) - 2):
                if highs[i] >= max(highs[i-2], highs[i-1], highs[i+1], highs[i+2]):
                    swing_highs.append(highs[i])
                if lows[i] <= min(lows[i-2], lows[i-1], lows[i+1], lows[i+2]):
                    swing_lows.append(lows[i])

            # Price clustering (group nearby prices)
            def cluster(prices: list, tol: float) -> List[List[float]]:
                if not prices:
                    return []
                sorted_p = sorted(prices)
                clusters = [[sorted_p[0]]]
                for p in sorted_p[1:]:
                    if abs(p - clusters[-1][-1]) <= tol:
                        clusters[-1].append(p)
                    else:
                        clusters.append([p])
                # Larger clusters first (touched often = strong zone)
                clusters.sort(key=len, reverse=True)
                return clusters

            high_clusters = cluster(swing_highs, tolerance)
            low_clusters = cluster(swing_lows, tolerance)

            zones = []
            # Top 3 RESISTANCE
            for cl in high_clusters[:3]:
                if len(cl) < 2:  # only where at least 2 swings gathered
                    continue
                zones.append({
                    "type": "RESISTANCE",
                    "price_low": min(cl),
                    "price_high": max(cl),
                    "strength": min(1.0, len(cl) / 5.0),  # strength 1.0 when 5 gather
                })
            # Top 3 SUPPORT
            for cl in low_clusters[:3]:
                if len(cl) < 2:
                    continue
                zones.append({
                    "type": "SUPPORT",
                    "price_low": min(cl),
                    "price_high": max(cl),
                    "strength": min(1.0, len(cl) / 5.0),
                })
            return zones
        except Exception as exc:
            logger.debug("[HARPOON M.E] self-zone compute failed for %s: %s", market, exc)
            return []

    def _get_zones_for_harpoon(self, market: str = "") -> List[Dict]:
        """Phase M.E + M.F (2026-04-24 owner-specified):
        Compute self zones only in standalone mode. Default uses FOCUS zones.

        - harpoon_standalone_mode = True: self M1 lookback calc (independent of FOCUS)
                                          fallback: if no self result, FOCUS zones
        - harpoon_standalone_mode = False (default): FOCUS zones as-is (existing behavior)
        """
        cfg = self.config
        standalone = bool(getattr(cfg, "harpoon_standalone_mode", False))
        if standalone:
            if not market:
                market = self._get_focus_market()
            zones = self._compute_self_zones(market)
            if not zones:
                # fallback: try FOCUS zones (if FOCUS is on)
                zones = self._get_focus_zones()
            return zones
        # Default (Sub-scalper) — use FOCUS zones
        return self._get_focus_zones()

    def _get_focus_market(self) -> str:
        if not self.focus:
            return ""
        market = getattr(self.focus, 'selected_market', "") or ""
        if not market:
            # ★ lock_market fallback when FOCUS is IDLE
            cfg = getattr(self.focus, 'config', None)
            if cfg:
                market = getattr(cfg, 'lock_market', "") or ""
        return market

    def _get_focus_direction(self) -> str:
        if not self.focus:
            return ""
        # [2026-05-15] FOCUS h4_sig→primary_sig renamed — keep old-attribute fallback
        sig = getattr(self.focus, 'primary_sig', None) or getattr(self.focus, 'h4_sig', None)
        if sig and isinstance(sig, dict):
            return sig.get("direction", "")
        return ""

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.A (2026-04-24) — Multi-Market Scanner (independent scan universe)
    # ═══════════════════════════════════════════════════════════════════

    def _get_scan_universe(self) -> List[str]:
        """Return the list of markets HARPOON scans.

        Priority:
          1. whitelist non-empty → scan only the whitelist
          2. scan_universe == "custom" → whitelist required (empty list if missing)
          3. scan_universe == "top20" / "top50" → FOCUS scanner_pool or Bybit top N
          4. scan_universe == "all" → the full pool registered in the FOCUS scanner

        blacklist is excluded in the final step.
        """
        cfg = self.config
        universe_mode = (getattr(cfg, "scan_universe", "all") or "all").lower()
        whitelist = [m.upper() for m in getattr(cfg, "scan_whitelist", []) if m]
        blacklist_set = {m.upper() for m in getattr(cfg, "scan_blacklist", []) if m}

        candidates: List[str] = []

        # 1) whitelist first
        if whitelist:
            candidates = list(whitelist)
        elif universe_mode == "custom":
            # custom but empty whitelist → no scan
            return []
        else:
            # 2) Inherit FOCUS scanner pool (apply top_n)
            # FOCUS already applied liquidity/grade filters — reuse the list
            try:
                if self.focus:
                    # Reference the full pool the FOCUS scanner evaluates
                    pool = getattr(self.focus, "_scanner_market_pool", None)
                    if pool and isinstance(pool, (list, tuple)):
                        candidates = [str(m).upper() for m in pool]
                    else:
                        # fallback: cache for the scan_list endpoint
                        scan_cache = getattr(self.focus, "_last_scan_list", None)
                        if scan_cache and isinstance(scan_cache, (list, tuple)):
                            candidates = [str(item.get("market","")).upper() if isinstance(item, dict) else str(item).upper()
                                          for item in scan_cache if item]
            except Exception as exc:
                logger.debug("[HARPOON] scan universe fetch from focus failed: %s", exc)

            # top_n limit
            if universe_mode == "top20":
                candidates = candidates[:20]
            elif universe_mode == "top50":
                candidates = candidates[:50]
            # "all" has no limit

        # 3) Exclude blacklist
        if blacklist_set:
            candidates = [m for m in candidates if m not in blacklist_set]

        # 4) Dedup + drop empties
        seen = set()
        result = []
        for m in candidates:
            if m and m not in seen:
                seen.add(m)
                result.append(m)
        return result

    def _get_harpoon_candidates(self) -> List[Dict[str, Any]]:
        """Return HARPOON's scan candidate list (Phase 1 — first-pass filter only).

        Each candidate is a dict:
          {
            "market": str,
            "direction": "LONG"/"SHORT",
            "conviction": int,
            "adx": float,
            "pa_pattern": str,
            "price": float,
            "atr": float,
          }

        Filter order:
          1. scan universe (incl. blacklist/whitelist) — _get_scan_universe
          2. liquidity (min_volume_usdt_24h) — Bybit API integration in Phase 2
          3. ADX floor (min_adx_self or min_adx inherit)
          4. Conviction floor (min_conviction_self)
          5. PA pattern allow-list (pa_patterns_allowed)

        Phase 1 scope: universe + PA pattern filter only. Reuses real signals (FOCUS scanner results).
        In Phase 2~3: self PA detection + liquidity + TP/SL calculation.
        """
        cfg = self.config
        universe = self._get_scan_universe()
        if not universe:
            return []

        # Reuse the FOCUS scanner's latest already-evaluated results
        scan_list = []
        try:
            if self.focus:
                scan_list = getattr(self.focus, "_last_scan_list", None) or []
        except Exception:
            scan_list = []

        if not scan_list:
            return []

        min_adx = int(getattr(cfg, "min_adx_self", 20))
        # If min_adx=0, inherit FOCUS dormant
        if min_adx <= 0:
            min_adx = int(getattr(cfg, "min_adx", 0)) or 15
        min_conv = float(getattr(cfg, "min_conviction_self", 50.0))  # [2026-05-17 100-pt scale ×10] 5→50
        pa_allowed = set(getattr(cfg, "pa_patterns_allowed", []) or [])

        candidates = []
        for item in scan_list:
            if not isinstance(item, dict):
                continue
            mkt = str(item.get("market", "")).upper()
            if mkt not in universe:
                continue
            # Skip if no signal (HOLD)
            sig = str(item.get("signal", "")).upper()
            if sig not in ("BUY", "SELL"):
                continue
            direction = "LONG" if sig == "BUY" else "SHORT"
            # ADX / Conviction filter
            adx = float(item.get("adx", 0) or 0)
            if adx < min_adx:
                continue
            conv = int(item.get("conviction", 0) or 0)

            # ★ [2026-04-24] HARPOON-Specific PA Weight Override
            # FOCUS conviction is computed with caller="focus" weights.
            # HARPOON-favored patterns (PIN_BAR/ENGULFING/SQUEEZE/BOS) get an extra bump,
            # FOCUS-favored patterns (STAR_V1) get a deduction.
            # delta = HARPOON_w - FOCUS_w (add only the difference from what's already included).
            pa_name = str(item.get("pa_pattern", "") or "").upper()
            if pa_name and self.focus:
                try:
                    fcfg = self.focus.config
                    focus_w_map = {
                        "PIN_BAR": int(getattr(fcfg, "pa_weight_pin_bar", 1)),
                        "ENGULFING": int(getattr(fcfg, "pa_weight_engulfing", 2)),
                        "STAR_V1": int(getattr(fcfg, "pa_weight_star_v1", 3)),
                        "STAR_V2": int(getattr(fcfg, "pa_weight_star_v2", 3)),
                        "SQUEEZE_BREAK": int(getattr(fcfg, "pa_weight_squeeze_break", 2)),
                        "BOS_BULLISH": int(getattr(fcfg, "pa_weight_bos", 2)),
                        "BOS_BEARISH": int(getattr(fcfg, "pa_weight_bos", 2)),
                    }
                    harpoon_w_map = {
                        "PIN_BAR": int(getattr(cfg, "pa_weight_pin_bar", 2)),
                        "ENGULFING": int(getattr(cfg, "pa_weight_engulfing", 3)),
                        "STAR_V1": int(getattr(cfg, "pa_weight_star_v1", 1)),
                        "STAR_V2": int(getattr(cfg, "pa_weight_star_v2", 3)),
                        "SQUEEZE_BREAK": int(getattr(cfg, "pa_weight_squeeze_break", 3)),
                        "BOS_BULLISH": int(getattr(cfg, "pa_weight_bos", 3)),
                        "BOS_BEARISH": int(getattr(cfg, "pa_weight_bos", 3)),
                    }
                    # [2026-05-17 100-pt scale ×10] PA weight itself is 0~6 (same scale as FOCUS _compute_pa_weight).
                    # New conviction is 0~100 (PA_score × 5 is conviction's PA contribution).
                    # → ×5 the delta too to match the 100-pt system. (e.g. PA delta 1 → conv delta 5)
                    delta = (harpoon_w_map.get(pa_name, 0) - focus_w_map.get(pa_name, 0)) * 5
                    if delta != 0:
                        conv_adjusted = max(0.0, float(conv) + float(delta))
                        if conv_adjusted != conv:
                            logger.debug("[HARPOON] PA delta %s: %s %+d → conv %.1f→%.1f",
                                         mkt, pa_name, delta, conv, conv_adjusted)
                        conv = conv_adjusted
                except Exception as exc:
                    logger.debug("[HARPOON] PA delta calc error: %s", exc)

            if conv < min_conv:
                continue
            # PA pattern allow-list
            pa = pa_name
            if pa_allowed and pa not in pa_allowed:
                continue
            # Collect candidate
            candidates.append({
                "market": mkt,
                "direction": direction,
                "conviction": conv,
                "adx": adx,
                "pa_pattern": pa,
                "price": float(item.get("price", 0) or 0),
                "atr": float(item.get("atr", 0) or 0),
            })

        # Sort by conviction descending (strong signals first)
        candidates.sort(key=lambda x: (-x["conviction"], -x["adx"]))

        # ★ Phase M.C: apply FOCUS coordination filter
        candidates = self._filter_candidates_by_focus_coordination(candidates)

        # ★ Phase M.B: Multi-position check (only candidates that can newly enter)
        filtered = []
        for c in candidates:
            can_open, reason = self._can_open_new_scalp(c.get("market", ""), c.get("direction", ""))
            if can_open:
                filtered.append(c)
            else:
                logger.debug("[HARPOON] candidate skip: %s %s — %s",
                             c.get("market"), c.get("direction"), reason)
        return filtered

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.C (2026-04-24) — FOCUS coordination logic
    #   Owner decision: full HARPOON overhaul + safe concurrent operation with FOCUS
    # ═══════════════════════════════════════════════════════════════════

    def _get_focus_held_markets(self) -> List[tuple]:
        """Get the list of (market, direction) FOCUS holds.
        read-only helper on focus_manager side."""
        try:
            if not self.focus:
                return []
            fn = getattr(self.focus, "_get_held_markets_for_harpoon", None)
            if callable(fn):
                return fn() or []
        except Exception as exc:
            logger.debug("[HARPOON] _get_focus_held_markets failed: %s", exc)
        return []

    def _is_focus_coin_locked(self, market: str) -> bool:
        """True if FOCUS holds this coin (HARPOON skips when respect_focus_coin_lock).
        Coin-exclusive rule — one coin = one engine."""
        if not market:
            return False
        if not getattr(self.config, "respect_focus_coin_lock", True):
            return False
        held = self._get_focus_held_markets()
        mkt_u = market.upper()
        return any(m == mkt_u for (m, _d) in held)

    def _is_focus_direction_conflict(self, market: str, direction: str) -> bool:
        """True if FOCUS holds an opposite-direction position on this coin.
        Prevents two-way hedging (respect_focus_direction_lock)."""
        if not market or not direction:
            return False
        if not getattr(self.config, "respect_focus_direction_lock", True):
            return False
        held = self._get_focus_held_markets()
        mkt_u = market.upper()
        dir_u = direction.upper()
        for (m, d) in held:
            if m == mkt_u and d != dir_u:
                return True
        return False

    def _is_focus_entry_freeze_active(self) -> bool:
        """True if FOCUS entered within the last focus_entry_freeze_sec.
        HARPOON briefly skips right after a FOCUS entry — prevents race condition."""
        freeze_sec = float(getattr(self.config, "focus_entry_freeze_sec", 30.0))
        if freeze_sec <= 0:
            return False
        try:
            if not self.focus:
                return False
            fn = getattr(self.focus, "_get_most_recent_entry_ts_for_harpoon", None)
            if not callable(fn):
                return False
            last_ts = fn() or 0.0
            if last_ts <= 0:
                return False
            elapsed = time.time() - last_ts
            return elapsed < freeze_sec
        except Exception as exc:
            logger.debug("[HARPOON] _is_focus_entry_freeze_active failed: %s", exc)
            return False

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.B (2026-04-24) — Multi-position management methods
    # ═══════════════════════════════════════════════════════════════════

    def _get_active_scalps(self) -> List[ScalpPosition]:
        """Current active scalp list.
        Phase 3 (kept in parallel): if current_scalp exists, include it in the return.
        Phase 4+ switches to managing active_scalps directly."""
        result = list(self.active_scalps) if self.active_scalps else []
        if self.current_scalp and not any(s.market == self.current_scalp.market for s in result):
            result.append(self.current_scalp)
        return result

    def _count_active_scalps(self) -> int:
        """Number of currently active scalps."""
        return len(self._get_active_scalps())

    def _count_same_direction_scalps(self, direction: str) -> int:
        """Number of active scalps in the same direction."""
        if not direction:
            return 0
        dir_u = direction.upper()
        return sum(1 for s in self._get_active_scalps() if (s.direction or "").upper() == dir_u)

    def _is_coin_on_cooldown(self, market: str) -> bool:
        """Per-coin cooldown check (cooldown_per_coin_sec).
        Prevents same-coin re-entry — similar to FOCUS coin_repeat_brake but HARPOON-independent."""
        if not market:
            return False
        cooldown = float(getattr(self.config, "cooldown_per_coin_sec", 300.0))
        if cooldown <= 0:
            return False
        last_exit = self.last_scalp_exit_by_coin.get(market.upper(), 0)
        if last_exit <= 0:
            return False
        return (time.time() - last_exit) < cooldown

    def _is_coin_on_post_sl_cooldown(self, market: str, direction: str) -> bool:
        """Stage 0-7 post_sl_cooldown_min check (block the same setup after an SL).
        Works with the existing _record_post_sl."""
        if not market or not direction:
            return False
        cooldown_min = float(getattr(self.config, "post_sl_cooldown_min", 30.0))
        if cooldown_min <= 0:
            return False
        key = (market.upper(), direction.upper())
        expire_ts = self._post_sl_cooldown_by_coin.get(key, 0)
        if expire_ts <= 0:
            return False
        return time.time() < expire_ts

    def _can_open_new_scalp(self, market: str, direction: str) -> tuple:
        """Whether a new scalp entry is allowed.
        Returns: (can_open: bool, reason: str)

        Check order:
          1. max_concurrent_scalps (total concurrent count)
          2. max_same_direction_scalps (same-direction count)
          3. cooldown_per_coin_sec (per-coin cooldown)
          4. post_sl_cooldown (same setup after SL)
          5. already holding the same coin (prevent duplicate entry)
        """
        cfg = self.config
        mkt_u = (market or "").upper()
        dir_u = (direction or "").upper()

        # 1) Total concurrent scalp count
        max_concurrent = int(getattr(cfg, "max_concurrent_scalps", 3))
        current_count = self._count_active_scalps()
        if current_count >= max_concurrent:
            return False, f"max_concurrent({current_count}/{max_concurrent})"

        # 2) Same-direction count
        max_same_dir = int(getattr(cfg, "max_same_direction_scalps", 2))
        same_dir_count = self._count_same_direction_scalps(dir_u)
        if same_dir_count >= max_same_dir:
            return False, f"max_same_direction({same_dir_count}/{max_same_dir})"

        # 3) Per-coin cooldown
        if self._is_coin_on_cooldown(mkt_u):
            return False, "coin_cooldown"

        # 4) Post-SL cooldown (Stage 0-7)
        if self._is_coin_on_post_sl_cooldown(mkt_u, dir_u):
            return False, "post_sl_cooldown"

        # 5) Prevent duplicate entry — coin already held
        for s in self._get_active_scalps():
            if (s.market or "").upper() == mkt_u:
                return False, f"coin_already_held({(s.direction or '').upper()})"

        # 6) ★ Phase M.E: shared guards (FOCUS B8/B10/coin_loss_cap/manual_exit_penalty)
        shared_blocked, shared_reason = self._check_shared_guards(mkt_u, dir_u)
        if shared_blocked:
            return False, shared_reason

        return True, "ok"

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.D (2026-04-24) — Budget separation (separate FOCUS/HARPOON pool)
    # ═══════════════════════════════════════════════════════════════════

    def _get_focus_total_margin(self) -> float:
        """Get the total margin FOCUS is using (read-only)."""
        try:
            if not self.focus:
                return 0.0
            fn = getattr(self.focus, "_get_total_margin_for_harpoon", None)
            if callable(fn):
                return float(fn() or 0)
        except Exception as exc:
            logger.debug("[HARPOON] _get_focus_total_margin failed: %s", exc)
        return 0.0

    def _get_harpoon_used_margin(self) -> float:
        """Sum of total margin used by HARPOON's active scalps."""
        try:
            total = 0.0
            for s in self._get_active_scalps():
                entry_price = float(getattr(s, "entry_price", 0) or 0)
                qty = float(getattr(s, "qty", 0) or 0)
                lev = float(getattr(self.config, "leverage", 1) or 1)
                if entry_price > 0 and qty > 0 and lev > 0:
                    margin = (entry_price * qty) / lev
                    total += margin
            return total
        except Exception as exc:
            logger.debug("[HARPOON] _get_harpoon_used_margin failed: %s", exc)
            return 0.0

    def _get_harpoon_total_budget_pool(self) -> float:
        """HARPOON's own total budget pool (= existing effective_budget property).
        budget_pct or directly-set budget_usdt value."""
        return float(self.effective_budget or 0)

    def _get_harpoon_available_budget(self) -> float:
        """HARPOON remaining available budget = total_pool - used_margin."""
        pool = self._get_harpoon_total_budget_pool()
        used = self._get_harpoon_used_margin()
        return max(0.0, pool - used)

    def _compute_per_scalp_budget(self, market: str, conviction: int = 5) -> float:
        """Compute per-new-scalp budget (Multi-position aware).

        Formula: remaining HARPOON available budget / remaining slot count
          - remaining available = total_pool - used_margin (subtract what other scalps use)
          - remaining slots = max_concurrent_scalps - current active count
          - micro-adjustment considering risk_pct

        Min $5 (Bybit min order) / max remaining available
        """
        cfg = self.config
        available = self._get_harpoon_available_budget()
        if available < 5.0:
            return 0.0  # cannot enter

        max_concurrent = int(getattr(cfg, "max_concurrent_scalps", 3))
        current_count = self._count_active_scalps()
        remaining_slots = max(1, max_concurrent - current_count)

        # Distribute remaining budget evenly across remaining slots
        per_scalp = available / remaining_slots

        # ★ [2026-04-24] PA Weight integration (FOCUS conviction 0~10 → 0~16 expanded)
        #   Owner decision "PA=formula, fee=variable" → stronger PA → larger size to dilute fee
        #   tier mapping (full-size basis):
        #     conviction <  5  → skip (can't beat fees, return 0.0)
        #     [2026-05-17 100-pt ×10 migration] old 5/7/10/13/16 → 50/70/100/130/160 (simple ×10)
        #     conviction 50~70   → 0.5x  (weak signal: ADX/BB only, no PA)
        #     conviction 71~100  → 0.8x  (traditional strong signal: ADX+BB+MACD+trend)
        #     conviction 101~130 → 1.1x  (traditional + Pat 1/2 PA)
        #     conviction 131~160 → 1.4x  (traditional + Pat 3 PA + zone — full conviction)
        risk_pct = float(getattr(cfg, "risk_pct", 0.5))
        if conviction < 50:
            return 0.0  # can't clear fee BEP — skip
        elif conviction <= 70:
            conv_scale = 0.5
        elif conviction <= 100:
            conv_scale = 0.8
        elif conviction <= 130:
            conv_scale = 1.1
        else:  # 131~160
            conv_scale = 1.4
        per_scalp = per_scalp * conv_scale

        # Final: min $5, max available
        return max(5.0, min(per_scalp, available))

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.E (2026-04-24) — Shared Guards integration (share FOCUS guards)
    # ═══════════════════════════════════════════════════════════════════

    def _get_focus_shared_guard_state(self, market: str, direction: str) -> dict:
        """Get FOCUS's shared guard state (read-only).
        FOCUS._get_shared_guard_state_for_harpoon() wrapper."""
        try:
            if not self.focus:
                return {}
            fn = getattr(self.focus, "_get_shared_guard_state_for_harpoon", None)
            if callable(fn):
                return fn(market, direction) or {}
        except Exception as exc:
            logger.debug("[HARPOON] _get_focus_shared_guard_state failed: %s", exc)
        return {}

    def _check_shared_guards(self, market: str, direction: str) -> tuple:
        """Combined shared-guard check (Phase M.E).

        Checks (sharing FOCUS state):
          1. profit_exit_block (B10) — block after a same-direction profit streak
          2. direction_exhaustion (B8) — block after 2 profit exits within 15 min
          3. coin_loss_cap — block coins with -$20+ cumulative over 24h
          4. manual_exit_penalty — block a coin the owner manually closed for 1.5h
          5. (coin_repeat info is reference only, no block decision — HARPOON uses its own cooldown)

        Owner decision: HARPOON must not bypass FOCUS guards. Combine both.

        Returns: (blocked: bool, reason: str)
        """
        cfg = self.config

        # Skip if the owner explicitly turned these OFF
        if not getattr(cfg, "respect_coin_loss_cap", True) \
           and not getattr(cfg, "respect_focus_adx_slope", True) \
           and not getattr(cfg, "respect_b11_regime_lock", True):
            # All guards OFF — skip the shared guard too
            return (False, "")

        state = self._get_focus_shared_guard_state(market, direction)
        if not state:
            return (False, "")

        # 1) profit_exit_block (FOCUS B10)
        peb_blocked, peb_reason = state.get("profit_exit_blocked", (False, ""))
        if peb_blocked:
            return (True, f"shared_profit_exit_block: {peb_reason[:80]}")

        # 2) direction_exhaustion (FOCUS B8)
        de_blocked, de_reason = state.get("direction_exhaustion_blocked", (False, ""))
        if de_blocked:
            return (True, f"shared_direction_exhaustion: {de_reason[:80]}")

        # 3) coin_loss_cap (24h cumulative, per the respect_coin_loss_cap setting)
        if getattr(cfg, "respect_coin_loss_cap", True):
            cap_blocked, cap_reason = state.get("coin_loss_cap_blocked", (False, ""))
            if cap_blocked:
                return (True, f"shared_coin_loss_cap: {cap_reason[:80]}")

        # 4) manual_exit_penalty (after an owner manual exit)
        if state.get("manual_exit_penalty_active", False):
            return (True, f"shared_manual_exit_penalty: {market} manual-exit penalty active")

        return (False, "")

    def _record_scalp_exit(self, market: str, direction: str, result: str):
        """On scalp exit, record cooldown timers + clean up active_scalps.
        result: "TP" / "SL" / "MANUAL" / "server_sl" / "pre_be_stall" etc."""
        if not market:
            return
        mkt_u = market.upper()
        now = time.time()
        # Record per-coin cooldown (common to all exits)
        self.last_scalp_exit_by_coin[mkt_u] = now
        # SL-type exits add post_sl_cooldown
        result_lower = (result or "").lower()
        if direction and ("sl" in result_lower or "reject" in result_lower):
            cooldown_min = float(getattr(self.config, "post_sl_cooldown_min", 30.0))
            if cooldown_min > 0:
                key = (mkt_u, direction.upper())
                self._post_sl_cooldown_by_coin[key] = now + cooldown_min * 60
        # Remove from active_scalps
        self.active_scalps = [s for s in self.active_scalps if (s.market or "").upper() != mkt_u]

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.F (2026-04-24) — Multi-market / Multi-position real entry
    #   Design principles:
    #     - current_scalp = "primary" — keep the existing single path (full dynamic trailing etc.)
    #     - active_scalps[others] = "secondary" — rely only on Bybit server-side TP/SL
    #     - the existing state machine manages the primary
    #     - new functions do extra handling at the end of tick
    # ═══════════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.G (2026-04-24) — 3 multi-scalp guards (generic helpers)
    #   Dynamic Trailing / Fast-Reject v2 / Pre-BE Stall Exit
    #   Designed pos-generic — applicable to both Primary and Secondary
    # ═══════════════════════════════════════════════════════════════════

    def _check_fast_reject_v2_for_scalp(self, scalp: ScalpPosition, price: float, now: float) -> tuple:
        """Fast-Reject v2: peak 0% + PnL loss within N sec of entry → immediate exit.
        Directly addresses the Stage 0-6 forensic 04-14 peak 0% pattern.
        Returns: (should_exit: bool, reason: str)"""
        cfg = self.config
        if not getattr(cfg, "fast_reject_v2_enabled", True):
            return (False, "")
        max_sec = float(getattr(cfg, "fast_reject_v2_max_sec", 60.0))
        peak_thr_pct = float(getattr(cfg, "fast_reject_v2_peak_threshold_pct", 0.05))
        pnl_pct_thr = float(getattr(cfg, "fast_reject_v2_pnl_pct", -0.05))
        elapsed = now - (scalp.entry_ts or 0)
        if elapsed <= 0 or elapsed > max_sec:
            return (False, "")
        # Compute peak profit
        peak_pct = 0.0
        if scalp.peak_profit_price > 0 and scalp.entry_price > 0:
            if scalp.direction == "LONG":
                peak_pct = (scalp.peak_profit_price / scalp.entry_price - 1) * 100
            else:
                peak_pct = (1 - scalp.peak_profit_price / scalp.entry_price) * 100
        # Current pnl
        if scalp.direction == "LONG":
            pnl_pct = (price / scalp.entry_price - 1) * 100 if scalp.entry_price > 0 else 0
        else:
            pnl_pct = (1 - price / scalp.entry_price) * 100 if scalp.entry_price > 0 else 0
        # Condition: peak still below threshold AND pnl in loss
        if peak_pct < peak_thr_pct and pnl_pct <= pnl_pct_thr:
            return (True, f"fast_reject_v2_{int(elapsed)}s_peak{peak_pct:.2f}%_pnl{pnl_pct:.2f}%")
        return (False, "")

    def _check_pre_be_stall_exit_for_scalp(self, scalp: ScalpPosition, price: float, now: float) -> tuple:
        """Pre-BE Stall Exit (same concept as FOCUS, ported to HARPOON).
        Before BE lock + peak ≥ min_profit + no update for N sec → exit.
        AUTO mode: fires only when the entered coin's ATR < threshold.

        If HARPOON config has no dedicated field, reference FOCUS's pre_be_stall_* fields or apply defaults.
        Returns: (should_exit: bool, reason: str)"""
        cfg = self.config
        # No HARPOON-specific pre_be_stall setting, so apply default AUTO/0.10%/60s
        mode = (getattr(cfg, "pre_be_stall_exit_mode", "AUTO") or "AUTO").upper()
        if mode == "OFF":
            return (False, "")
        if scalp.breakeven_locked:
            return (False, "")  # after BE lock, BE Stall Exit handles it
        min_profit_pct = float(getattr(cfg, "pre_be_stall_min_profit_pct", 0.10))
        stall_sec = float(getattr(cfg, "pre_be_stall_sec", 60.0))
        vol_thr_pct = float(getattr(cfg, "pre_be_stall_volatility_threshold_pct", 2.0))
        # peak profit
        peak_pct = 0.0
        if scalp.peak_profit_price > 0 and scalp.entry_price > 0:
            if scalp.direction == "LONG":
                peak_pct = (scalp.peak_profit_price / scalp.entry_price - 1) * 100
            else:
                peak_pct = (1 - scalp.peak_profit_price / scalp.entry_price) * 100
        if peak_pct < min_profit_pct:
            return (False, "")
        # Elapsed since last peak update — scalp has no last_peak_update_ts, so use entry_ts
        # (ScalpPosition has no last_peak_update_ts field — simplified)
        # Measure from entry_ts for now (whether stall_sec passed since the first peak)
        elapsed = now - (scalp.entry_ts or now)
        if elapsed < stall_sec:
            return (False, "")
        # AUTO mode: judge ranging by ATR
        if mode == "AUTO":
            coin_atr_pct = (scalp.atr_used / scalp.entry_price * 100) if scalp.entry_price > 0 else 0
            if coin_atr_pct >= vol_thr_pct:
                return (False, "")  # high volatility — follow the trend
        return (True, f"pre_be_stall_{int(elapsed)}s_peak+{peak_pct:.2f}%")

    def _update_scalp_peak_price(self, scalp: ScalpPosition, price: float, now: float = None) -> bool:
        """Update the scalp's peak_profit_price + record timestamp.
        Returns: True if peak was updated."""
        if now is None:
            now = time.time()
        updated = False
        if scalp.direction == "LONG":
            if price > scalp.peak_profit_price:
                scalp.peak_profit_price = price
                scalp.last_peak_update_ts = now
                updated = True
        else:
            if scalp.peak_profit_price <= 0 or price < scalp.peak_profit_price:
                scalp.peak_profit_price = price
                scalp.last_peak_update_ts = now
                updated = True
        return updated

    def _check_be_stall_exit_for_scalp(self, scalp: ScalpPosition, price: float, now: float) -> tuple:
        """BE Stall Exit (Phase M.G generic): exit if no update for 30 sec after BE lock.
        Fee guard: fires only if pnl ≥ 0.15% OR peak ≥ 0.30%.
        Returns: (should_exit: bool, reason: str)"""
        cfg = self.config
        if not getattr(cfg, "be_stall_exit_enabled", True):
            return (False, "")
        if not scalp.breakeven_locked or scalp.be_locked_ts <= 0:
            return (False, "")
        stall_sec = float(getattr(cfg, "be_stall_exit_sec", 30.0))
        ref_ts = max(scalp.be_locked_ts, scalp.last_peak_update_ts or scalp.be_locked_ts)
        elapsed = now - ref_ts
        if elapsed < stall_sec:
            return (False, "")
        # Fee guard
        if scalp.direction == "LONG":
            pnl_pct = (price / scalp.entry_price - 1) * 100 if scalp.entry_price > 0 else 0
            peak_pct = (scalp.peak_profit_price / scalp.entry_price - 1) * 100 if scalp.peak_profit_price > 0 and scalp.entry_price > 0 else 0
        else:
            pnl_pct = (1 - price / scalp.entry_price) * 100 if scalp.entry_price > 0 else 0
            peak_pct = (1 - scalp.peak_profit_price / scalp.entry_price) * 100 if scalp.peak_profit_price > 0 and scalp.entry_price > 0 else 0
        if pnl_pct < 0.15 and peak_pct < 0.30:
            return (False, "")  # below fee guard
        return (True, f"be_stall_{int(elapsed)}s_peak+{peak_pct:.2f}%")

    def _check_timeout_for_scalp(self, scalp: ScalpPosition, now: float) -> tuple:
        """5 min Timeout check (Phase M.G generic).
        Returns: (should_exit: bool, reason: str)"""
        TIMEOUT_SEC = 300.0  # 5 min
        if not scalp.entry_ts or scalp.entry_ts <= 0:
            return (False, "")
        hold = now - scalp.entry_ts
        if hold > TIMEOUT_SEC:
            return (True, f"timeout_{int(hold)}s")
        return (False, "")

    def _close_secondary_scalp(self, scalp: ScalpPosition, reason: str, price: float) -> bool:
        """Market-close a secondary scalp (simplified version of Primary's _execute_scalp_exit).

        - Bybit market order close
        - remove from active_scalps
        - record Journal + recent_scalps
        - update counters
        """
        if not scalp or not scalp.market:
            return False
        market = scalp.market
        now = time.time()
        try:
            client = self._get_client()
            # Market close order (opposite of direction)
            close_side = "Sell" if scalp.direction == "LONG" else "Buy"
            _params = {
                "category": "linear", "symbol": market, "side": close_side,
                "orderType": "Market", "qty": str(scalp.qty),
                "reduceOnly": True,
            }
            client._request("POST", "/v5/order/create", params=None, body=_params)
        except Exception as exc:
            logger.warning("[HARPOON M.G] secondary close order failed %s: %s", market, exc)
            return False

        # Compute PnL
        if scalp.direction == "LONG":
            pnl = (price - scalp.entry_price) * scalp.qty
        else:
            pnl = (scalp.entry_price - price) * scalp.qty
        duration = now - scalp.entry_ts if scalp.entry_ts > 0 else 0.0

        # Journal
        try:
            from app.manager.trade_journal import journal as _journal
            _journal.append(
                strategy="HARPOON", event="EXIT", market=market,
                direction=scalp.direction, price=price, qty=scalp.qty,
                pnl_net=round(pnl, 4), exit_reason=reason, hold_sec=round(duration, 1),
            )
        except Exception as exc:
            logger.debug("[HARPOON M.G] journal append failed: %s", exc)

        # recent_scalps
        record = ScalpRecord(
            scalp_id=scalp.scalp_id, market=market, direction=scalp.direction,
            entry_price=scalp.entry_price, exit_price=price, qty=scalp.qty,
            pnl_usdt=round(pnl, 4), result=reason, duration_sec=round(duration, 1),
            ts=now,
        )
        self.recent_scalps.append(asdict(record))
        if len(self.recent_scalps) > 50:
            self.recent_scalps = self.recent_scalps[-50:]

        # Cooldown + remove from active_scalps + counters
        self._record_scalp_exit(market, scalp.direction, reason)
        self.scalps_today += 1
        self.daily_pnl += pnl
        self.total_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        logger.info("[HARPOON M.G] SECONDARY close: %s %s @ $%.4f — %s pnl=$%.4f",
                    market, scalp.direction, price, reason, pnl)
        return True

    def _try_extra_entries(self, now: float) -> int:
        """Try extra multi-market entries.
        Returns: number of scalps newly entered this tick.

        Behavior:
          1. Fetch the candidate list (Phase M.A scanner)
          2. For each candidate, run _can_open_new_scalp (Multi-position + Shared guards)
          3. Passing candidates call _execute_scalp_entry (reuse the existing function)
          4. Auto-register in active_scalps (already inside _execute_scalp_entry)

        Note:
          - primary (current_scalp) entry is handled by the existing state machine
          - this function handles "extra" entries only (secondary)
          - only meaningful when scan_universe != "all" or max_concurrent>1
        """
        cfg = self.config
        max_concurrent = int(getattr(cfg, "max_concurrent_scalps", 3))
        if max_concurrent <= 1:
            return 0  # single-position mode — no extra entries

        # Skip if already at max held
        if self._count_active_scalps() >= max_concurrent:
            return 0

        # Fetch candidate list
        candidates = self._get_harpoon_candidates()
        if not candidates:
            return 0

        # Skip in paper_mode (would need to reuse _handle_stalking's paper logic; skip here)
        if getattr(cfg, "paper_mode", False):
            # Paper: log only
            for c in candidates[:max_concurrent - self._count_active_scalps()]:
                self._log_paper_entry(c["market"], c["direction"], c.get("price", 0), c.get("atr", 0))
            return 0

        entered = 0
        for c in candidates:
            if self._count_active_scalps() >= max_concurrent:
                break
            market = c.get("market", "")
            direction = c.get("direction", "")
            price = float(c.get("price", 0) or 0)
            atr = float(c.get("atr", 0) or 0)
            if not (market and direction and price > 0 and atr > 0):
                continue
            # Re-check _can_open_new_scalp (already checked in the candidate filter, but prevent race condition)
            can_open, reason = self._can_open_new_scalp(market, direction)
            if not can_open:
                logger.debug("[HARPOON M.F] extra entry skip: %s %s — %s", market, direction, reason)
                continue
            try:
                result = self._execute_scalp_entry(market, direction, price, atr)
                if result and result.get("success"):
                    entered += 1
                    logger.info("[HARPOON M.F] EXTRA entry: %s %s @ $%.4f (active=%d/%d)",
                                market, direction, price, self._count_active_scalps(), max_concurrent)
                else:
                    logger.debug("[HARPOON M.F] extra entry failed: %s %s — %s",
                                 market, direction, result.get("reason", "?") if result else "?")
            except Exception as exc:
                logger.warning("[HARPOON M.F] extra entry exception: %s %s — %s", market, direction, exc)
                continue
        return entered

    def _monitor_additional_scalps(self, now: float) -> int:
        """Check Bybit server state for additional scalps (active_scalps other than current_scalp).
        Returns: number of scalps cleaned up this tick.

        Behavior:
          - current_scalp is handled in _handle_fired (skip)
          - check Bybit positions for the remaining active_scalps
          - if no position on Bybit → closed by server-side TP/SL → remove from active_scalps + journal
          - based on server-side TP/SL, so peak_profit / trailing etc. aren't possible (secondary constraint)
        """
        if not self.active_scalps:
            return 0

        current_id = getattr(self.current_scalp, "scalp_id", 0) if self.current_scalp else 0
        additional = [s for s in self.active_scalps if s.scalp_id != current_id]
        if not additional:
            return 0

        cleared = 0
        try:
            from app.core.constants import BYBIT_POSITION_LIST
            client = self._get_client()
        except Exception as exc:
            logger.debug("[HARPOON M.F] monitor additional: client init failed: %s", exc)
            return 0

        for s in additional:
            market = s.market
            if not market:
                continue
            try:
                resp = client._request("GET", BYBIT_POSITION_LIST,
                                       params={"category": "linear", "symbol": market})
                pos_list = resp.get("result", {}).get("list", [])
                has_position = any(float(p.get("size", 0)) > 0 for p in pos_list)
                if has_position:
                    # ★ Phase M.G: full Secondary guards (all 6 same as Primary)
                    current_price = self._get_current_price(market)
                    if current_price:
                        # 1) update peak (auto-records last_peak_update_ts)
                        self._update_scalp_peak_price(s, current_price, now)
                        # 2) 5 min Timeout
                        to_exit, to_reason = self._check_timeout_for_scalp(s, now)
                        if to_exit:
                            self._close_secondary_scalp(s, to_reason, current_price)
                            cleared += 1
                            continue
                        # 3) Fast-Reject v2 (60s peak 0% + loss)
                        fr_exit, fr_reason = self._check_fast_reject_v2_for_scalp(s, current_price, now)
                        if fr_exit:
                            self._close_secondary_scalp(s, fr_reason, current_price)
                            cleared += 1
                            continue
                        # 4) Pre-BE Stall Exit (before BE, peak +0.10% + no update for 60s)
                        pb_exit, pb_reason = self._check_pre_be_stall_exit_for_scalp(s, current_price, now)
                        if pb_exit:
                            self._close_secondary_scalp(s, pb_reason, current_price)
                            cleared += 1
                            continue
                        # 5) BE Stall Exit (no update for 30s after BE lock + fee guard)
                        be_exit, be_reason = self._check_be_stall_exit_for_scalp(s, current_price, now)
                        if be_exit:
                            self._close_secondary_scalp(s, be_reason, current_price)
                            cleared += 1
                            continue
                        # 6) Dynamic Trailing SL (update server SL, auto-handle BE lock)
                        if getattr(self.config, "dynamic_trailing", False):
                            try:
                                self._apply_scalp_trailing_sl(s, current_price)
                            except Exception as exc:
                                logger.debug("[HARPOON M.G] trailing secondary %s failed: %s", market, exc)
                    continue  # position still held — wait for server-side TP/SL

                # No position → closed by server-side TP/SL → clean up
                price = self._get_current_price(market) or s.entry_price
                if s.direction == "LONG":
                    tp_hit = price >= s.tp if s.tp else False
                else:
                    tp_hit = price <= s.tp if s.tp else False
                exit_price = (s.tp if tp_hit else s.sl) if s.sl else price
                exit_reason = "SERVER_TP_MULTI" if tp_hit else "SERVER_SL_MULTI"

                # Compute PnL
                if s.direction == "LONG":
                    pnl = (exit_price - s.entry_price) * s.qty
                else:
                    pnl = (s.entry_price - exit_price) * s.qty
                duration = now - s.entry_ts if s.entry_ts > 0 else 0.0

                # Journal + record_scalp_exit
                try:
                    from app.manager.trade_journal import journal as _journal
                    _journal.append(
                        strategy="HARPOON",
                        event="EXIT",
                        market=market,
                        direction=s.direction,
                        price=exit_price,
                        qty=s.qty,
                        pnl_net=round(pnl, 4),
                        exit_reason=exit_reason,
                        hold_sec=round(duration, 1),
                    )
                except Exception as exc:
                    logger.debug("[HARPOON M.F] journal append failed: %s", exc)

                # Record recent_scalps
                record = ScalpRecord(
                    scalp_id=s.scalp_id, market=market, direction=s.direction,
                    entry_price=s.entry_price, exit_price=exit_price, qty=s.qty,
                    pnl_usdt=round(pnl, 4), result=exit_reason, duration_sec=round(duration, 1),
                    ts=now,
                )
                self.recent_scalps.append(asdict(record))
                if len(self.recent_scalps) > 50:
                    self.recent_scalps = self.recent_scalps[-50:]

                # Record cooldown
                self._record_scalp_exit(market, s.direction, exit_reason)

                # Update counters
                self.scalps_today += 1
                self.daily_pnl += pnl
                self.total_pnl += pnl
                if pnl < 0:
                    self.consecutive_losses += 1
                else:
                    self.consecutive_losses = 0

                logger.info("[HARPOON M.F] SECONDARY exit: %s %s @ $%.4f — %s pnl=$%.4f dur=%.0fs",
                            market, s.direction, exit_price, exit_reason, pnl, duration)
                cleared += 1

            except Exception as exc:
                logger.debug("[HARPOON M.F] monitor %s exception: %s", market, exc)
                continue
        return cleared

    def _clear_current_scalp_from_active(self):
        """Remove current_scalp from active_scalps (by id or market).
        Phase M.B: called from the 4 exit paths — keeps active_scalps consistent."""
        if not self.current_scalp:
            return
        sc_id = getattr(self.current_scalp, "scalp_id", 0)
        sc_mkt = (self.current_scalp.market or "").upper()
        self.active_scalps = [
            s for s in self.active_scalps
            if (sc_id and s.scalp_id != sc_id) or (not sc_id and (s.market or "").upper() != sc_mkt)
        ]

    def _filter_candidates_by_focus_coordination(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return only candidates from the HARPOON list that pass FOCUS coordination.

        Filter order:
          1. when focus_entry_freeze_sec active: block all candidates (FOCUS just entered)
          2. respect_focus_coin_lock: skip coins FOCUS holds
          3. respect_focus_direction_lock: skip FOCUS-opposite directions
          4. coin_exclusive_priority: handle HARPOON↔HARPOON conflicts later in Phase 3+
        """
        if not candidates:
            return candidates
        cfg = self.config

        # 1) Focus entry freeze (right after a recent FOCUS entry)
        if self._is_focus_entry_freeze_active():
            logger.debug("[HARPOON] focus_entry_freeze active — all candidates skipped")
            return []

        filtered = []
        skip_log = []
        for c in candidates:
            mkt = c.get("market", "")
            direction = c.get("direction", "")

            # 2) Coin lock — FOCUS holds the same coin
            if self._is_focus_coin_locked(mkt):
                skip_log.append(f"{mkt}:focus_holds")
                continue

            # 3) Direction conflict — FOCUS holds the opposite direction on the same coin (redundant but explicit)
            if self._is_focus_direction_conflict(mkt, direction):
                skip_log.append(f"{mkt}:focus_dir_conflict")
                continue

            filtered.append(c)

        if skip_log:
            logger.debug("[HARPOON] FOCUS coordination filter: %s", " | ".join(skip_log))
        return filtered

    def _get_focus_atr(self) -> float:
        """Get FOCUS's H4 ATR value. Try all sources."""
        # 1) FOCUS primary_sig [2026-05-15 h4_sig→primary_sig, old-attr fallback]
        if self.focus:
            sig = getattr(self.focus, 'primary_sig', None) or getattr(self.focus, 'h4_sig', None)
            if sig and isinstance(sig, dict):
                atr = sig.get("atr", 0) or 0
                if atr > 0:
                    return float(atr)
            # 2) FOCUS positions
            positions = getattr(self.focus, 'positions', []) or []
            for p in positions:
                atr = getattr(p, 'atr_used', 0) or 0
                if atr > 0:
                    return float(atr)
        # 3) ★ Fallback: compute ATR directly from kline (works independently even without FOCUS h4_sig)
        try:
            market = self._get_focus_market()
            if market and self.focus:
                client = getattr(self.focus, '_client', None)
                if client:
                    raw = client.get_kline(market, interval="240", limit=20)
                    closes = [float(r[4]) for r in raw if len(r) >= 5]
                    if len(closes) >= 14:
                        from app.strategy.greenpen import _simple_atr
                        from app.strategy.greenpen.pa_detector import OHLCV
                        candles = [OHLCV(float(r[1]),float(r[2]),float(r[3]),float(r[4])) for r in raw if len(r)>=5]
                        atr = _simple_atr(candles)
                        if atr > 0:
                            logger.debug("[HARPOON] ATR fallback from kline: %.4f", atr)
                            return float(atr)
        except Exception as exc:
            logger.debug("[HARPOON] ATR fallback calc failed: %s", exc)
        return 0.0

    def _find_nearest_zone(
        self, price: float, direction: str, zones: List[Dict], proximity: float,
    ) -> Optional[Dict]:
        """Find the nearest zone — the scalper decides direction by zone type.

        SUPPORT zone → LONG, RESISTANCE zone → SHORT.
        The direction arg is for legacy compatibility (empty string searches both directions).
        """
        best = None
        best_dist = float("inf")

        for z in zones:
            z_type = z.get("type", "")
            z_low = z.get("price_low", 0)
            z_high = z.get("price_high", 0)
            z_mid = (z_low + z_high) / 2 if z_low and z_high else 0

            if z_mid <= 0:
                continue

            # Skip if there's no zone type
            if "SUPPORT" not in z_type.upper() and "RESISTANCE" not in z_type.upper():
                continue

            dist = abs(price - z_mid)
            if dist <= proximity and dist < best_dist:
                best = z
                best_dist = dist

        return best

    # ------------------------------------------------------------------
    # M1 PA detection
    # ------------------------------------------------------------------

    def _check_m1_pa(self, market: str, direction: str) -> Optional[Dict]:
        """Detect PA patterns on the M1 timeframe."""
        try:
            client = self._get_client()
            klines_raw = client.get_kline(market, interval=self.config.entry_tf, limit=50)
            if not klines_raw or len(klines_raw) < 10:
                return None

            from app.strategy.greenpen.pa_detector import OHLCV, detect_pa_patterns
            from app.strategy.greenpen.market_structure import analyze_structure

            candles = []
            for k in klines_raw:
                try:
                    candles.append(OHLCV(
                        open=float(k[1]),
                        high=float(k[2]),
                        low=float(k[3]),
                        close=float(k[4]),
                        volume=float(k[5]) if len(k) > 5 else 0.0,
                    ))
                except (IndexError, ValueError, TypeError):
                    continue

            if len(candles) < 10:
                return None

            # ── M1 trend+momentum filter (block counter-trend PA) ──
            m1_trend_opposed = False
            m1_momentum_opposed = False
            try:
                m1_struct = analyze_structure(candles, lookback=3)
                m1_trend = m1_struct.trend.value if hasattr(m1_struct.trend, 'value') else str(m1_struct.trend)
                if direction == "LONG" and m1_trend == "DOWNTREND":
                    m1_trend_opposed = True
                elif direction == "SHORT" and m1_trend == "UPTREND":
                    m1_trend_opposed = True
            except Exception:
                pass

            # M1 momentum: if 4+ of the last 5 bars are in the opposite direction, momentum is against us
            recent5 = candles[-5:]
            bearish_count = sum(1 for c in recent5 if c.close < c.open)
            if direction == "LONG" and bearish_count >= 4:
                m1_momentum_opposed = True
            elif direction == "SHORT" and (5 - bearish_count) >= 4:
                m1_momentum_opposed = True

            # If both trend and momentum oppose, ignore the PA signal
            if m1_trend_opposed and m1_momentum_opposed:
                logger.debug("[HARPOON] M1 PA blocked: trend+momentum opposed to %s", direction)
                return None

            # ★ zone_prices=None — Harpoon already verifies zone proximity separately,
            # so skip location filtering in the PA patterns (passing zone_tup=(low,high)
            # makes detect_pa_patterns misread it as (support,resistance) and block valid signals)
            signals = detect_pa_patterns(candles, zone_prices=None)

            # Find the latest signal matching the direction
            for sig in reversed(signals):
                sig_dir = getattr(sig, 'direction', '') or ''
                if isinstance(sig, dict):
                    sig_dir = sig.get('direction', '')

                if sig_dir.upper() == direction.upper():
                    conf = getattr(sig, 'confidence', 0) if not isinstance(sig, dict) else sig.get('confidence', 0)
                    pattern = getattr(sig, 'pattern', '') if not isinstance(sig, dict) else sig.get('pattern', '')
                    if conf >= 0.4:  # scalps use a lower confidence threshold
                        return {
                            "pattern": str(pattern),
                            "direction": direction,
                            "confidence": float(conf),
                        }

            return None
        except Exception as exc:
            logger.debug("[HARPOON] M1 PA check failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # M5 trend filter (Harpoon's higher-TF filter — plays FOCUS's H1 role)
    # ------------------------------------------------------------------

    def _check_m5_trend(self, market: str, direction: str) -> Optional[str]:
        """Check M5 trend — Harpoon's higher-TF filter.

        Just as FOCUS confirms trend with H1, Harpoon confirms with M5.
        M5 DOWNTREND while attempting LONG → block, M5 UPTREND while attempting SHORT → block.

        Returns: "aligned" | "neutral" | "opposed" | None (API failure)
        """
        try:
            client = self._get_client()
            raw = client.get_kline(market, interval="5", limit=20)
            if not raw or len(raw) < 10:
                return None

            from app.strategy.greenpen.pa_detector import OHLCV
            from app.strategy.greenpen.market_structure import analyze_structure

            candles = []
            for k in raw:
                try:
                    candles.append(OHLCV(
                        open=float(k[1]), high=float(k[2]),
                        low=float(k[3]), close=float(k[4]),
                        volume=float(k[5]) if len(k) > 5 else 0.0,
                    ))
                except (IndexError, ValueError, TypeError):
                    continue

            if len(candles) < 10:
                return None

            struct = analyze_structure(candles, lookback=3)
            trend = struct.trend.value if hasattr(struct.trend, 'value') else str(struct.trend)

            if direction == "LONG":
                if trend == "UPTREND":
                    return "aligned"
                elif trend == "DOWNTREND":
                    return "opposed"
            else:  # SHORT
                if trend == "DOWNTREND":
                    return "aligned"
                elif trend == "UPTREND":
                    return "opposed"

            return "neutral"
        except Exception as exc:
            logger.debug("[HARPOON] M5 trend check failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Entry execution
    # ------------------------------------------------------------------

    def _execute_scalp_entry(
        self, market: str, direction: str, price: float, atr: float,
    ) -> Dict[str, Any]:
        """Execute a scalp entry."""
        # ★ Re-check FOCUS conflict right before entry (FOCUS may have taken a position during STALKING)
        if self._has_focus_conflict():
            return {"success": False, "reason": "focus_conflict_at_entry"}

        from app.strategy.greenpen.harpoon_tp import compute_scalp_targets, compute_scalp_size

        # Compute TP/SL
        targets = compute_scalp_targets(
            entry_price=price,
            direction=direction,
            atr=atr,
            tp_atr_mult=self.config.tp_atr_mult,
            sl_atr_mult=self.config.sl_atr_mult,
        )

        if targets.tp <= 0 or targets.sl <= 0:
            return {"success": False, "reason": "invalid_targets"}

        # Position sizing
        budget = self.effective_budget
        sizing = compute_scalp_size(
            budget_usdt=budget,
            risk_pct=self.config.risk_pct,
            sl_distance=targets.sl_dist,
            current_price=price,
            leverage=float(self.config.leverage),
        )

        if sizing.qty <= 0:
            return {"success": False, "reason": "zero_qty"}

        # ★ Bybit qty step/min adjustment (prevent Qty invalid)
        # ★ category="linear" — Harpoon uses USDT Perpetual, differs from spot qty_step!
        try:
            from app.integrations.bybit_instrument_cache import BybitInstrumentCache
            _cat = "linear"
            BybitInstrumentCache.load(category=_cat)  # ensure linear cache
            raw_qty = sizing.qty
            sizing.qty = BybitInstrumentCache.adjust_qty(market, sizing.qty, category=_cat)
            min_q = BybitInstrumentCache.get_min_qty(market, category=_cat)
            max_q = float((BybitInstrumentCache.get(market, category=_cat) or {}).get("max_qty", 0) or 0)
            if sizing.qty <= 0 or sizing.qty < min_q:
                logger.warning("[HARPOON] qty %.6f (raw %.6f) < min %.6f for %s — skip",
                               sizing.qty, raw_qty, min_q, market)
                return {"success": False, "reason": f"qty_below_min_{sizing.qty}"}
            if max_q > 0 and sizing.qty > max_q:
                logger.warning("[HARPOON] qty %.6f > maxOrderQty %.6f for %s — clamping",
                               sizing.qty, max_q, market)
                sizing.qty = max_q
            sizing.notional = sizing.qty * price
            logger.debug("[HARPOON] sizing: raw=%.6f adj=%.6f min=%.6f step=%s market=%s",
                         raw_qty, sizing.qty, min_q,
                         BybitInstrumentCache.get_qty_step(market, category=_cat), market)
        except Exception as exc:
            logger.warning("[HARPOON] instrument cache adjust FAILED for %s: %s — aborting entry",
                           market, exc)
            return {"success": False, "reason": f"cache_adjust_failed_{market}"}

        # Min order check
        min_notional = 5.0  # USDT
        if sizing.notional < min_notional:
            return {"success": False, "reason": f"notional_too_small_{sizing.notional}"}

        # ★ Margin cap: skip if notional/leverage exceeds available balance
        margin_needed = sizing.notional / max(1, float(self.config.leverage))
        try:
            equity = float(getattr(self.system, '_last_equity_usdt', 0) or 0)
            if equity > 0 and margin_needed > equity * 0.30:
                logger.warning(
                    "[HARPOON] MARGIN CAP: margin=$%.0f > equity $%.0f × 30%% — skip %s",
                    margin_needed, equity, market)
                return {"success": False, "reason": f"margin_cap_{market}"}
        except Exception:
            pass

        try:
            client = self._get_client()

            # Set leverage
            try:
                client.set_leverage(market, self.config.leverage)
            except Exception as exc:
                # Ignore if already set (110043 error)
                if "110043" not in str(exc):
                    logger.warning("[HARPOON] set_leverage failed: %s", exc)

            # Market order
            side = "Buy" if direction.upper() == "LONG" else "Sell"
            order = client.place_order(
                market=market,
                side=side,
                ord_type="Market",
                volume=sizing.qty,
            )

            if not order:
                return {"success": False, "reason": "order_failed"}

            # Set server-side TP/SL
            if self.config.server_side_tpsl:
                try:
                    client.set_trading_stop(
                        market,
                        take_profit=targets.tp,
                        stop_loss=targets.sl,
                    )
                except Exception as exc:
                    logger.warning("[HARPOON] set_trading_stop failed: %s", exc)

            # Record position
            self.current_scalp = ScalpPosition(
                market=market,
                direction=direction,
                entry_price=price,
                qty=sizing.qty,
                tp=targets.tp,
                sl=targets.sl,
                atr_used=atr,
                entry_ts=time.time(),
                scalp_id=self._next_scalp_id,
            )
            self._next_scalp_id += 1
            # ★ Phase M.B: sync active_scalps (prevent duplicates)
            if not any(s.scalp_id == self.current_scalp.scalp_id for s in self.active_scalps):
                self.active_scalps.append(self.current_scalp)
            # ★ Phase M.G: init last_peak_update_ts (reference time for Pre-BE/BE Stall Exit)
            self.current_scalp.last_peak_update_ts = time.time()

            logger.info(
                "[HARPOON] Entry: %s %s qty=%.6f @ $%.4f | TP=$%.4f SL=$%.4f | budget=$%.2f lev=%dx",
                market, direction, sizing.qty, price, targets.tp, targets.sl,
                budget, self.config.leverage,
            )
            try:
                if self.system and hasattr(self.system, 'ledger'):
                    self.system.ledger.append(
                        "HARPOON_ENTRY", market=market,
                        direction=direction, qty=sizing.qty, entry_price=price,
                        tp=targets.tp, sl=targets.sl,
                        leverage=self.config.leverage, budget_usdt=budget,
                    )
            except Exception:
                pass
            # 🔱 Telegram entry alert (owner "connect Harpoon" · disable with OMA_HARPOON_ALERTS=0)
            try:
                if self.system and hasattr(self.system, '_send_telegram_safe') and os.getenv("OMA_HARPOON_ALERTS", "1").strip().lower() not in ("0", "false", "no", "off"):
                    self.system._send_telegram_safe(f"🔱 [HARPOON] ENTRY {direction} {market}\n@ ${price:.4f} qty={sizing.qty:.4f}\nTP=${targets.tp:.4f} SL=${targets.sl:.4f} | {self.config.leverage}x ${budget:.0f}")
            except Exception:
                pass
            # ── Bookkeeping ──
            try:
                from app.manager.trade_journal import journal
                journal.record_entry(
                    strategy="HARPOON", market=market, direction=direction,
                    price=price, qty=sizing.qty, leverage=self.config.leverage,
                )
            except Exception:
                pass

            return {
                "success": True,
                "qty": sizing.qty,
                "tp": targets.tp,
                "sl": targets.sl,
                "rr": targets.rr_ratio,
                "notional": sizing.notional,
            }

        except Exception as exc:
            logger.error("[HARPOON] entry order error: %s", exc, exc_info=True)
            return {"success": False, "reason": str(exc)}

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------

    def _execute_scalp_exit(self, reason: str, price: float) -> Dict[str, Any]:
        """Execute a scalp exit."""
        if not self.current_scalp:
            self.state = HarpoonState.COOLDOWN
            self.cooldown_start_ts = time.time()
            return {"success": False, "reason": "no_position"}

        scalp = self.current_scalp
        try:
            client = self._get_client()
            close_side = "Sell" if scalp.direction == "LONG" else "Buy"

            # ★ C5: pass both reduceOnly + reduce_only (ensure API compatibility)
            order = client.place_order(
                market=scalp.market,
                side=close_side,
                ord_type="Market",
                volume=scalp.qty,
                reduceOnly=True,
                reduce_only=True,
            )

            # Compute PnL
            if scalp.direction == "LONG":
                pnl = (price - scalp.entry_price) * scalp.qty
            else:
                pnl = (scalp.entry_price - price) * scalp.qty

            # Fee deduction (approximate)
            fee = price * scalp.qty * 0.00055 * 2  # taker fee round trip
            pnl -= fee

            duration = time.time() - scalp.entry_ts
            is_loss = pnl < 0

            # Record
            record = ScalpRecord(
                scalp_id=scalp.scalp_id,
                market=scalp.market,
                direction=scalp.direction,
                entry_price=scalp.entry_price,
                exit_price=price,
                qty=scalp.qty,
                pnl_usdt=round(pnl, 4),
                result=reason,
                duration_sec=round(duration, 1),
                ts=time.time(),
            )
            self.recent_scalps.append(asdict(record))
            if len(self.recent_scalps) > 50:
                self.recent_scalps = self.recent_scalps[-50:]

            # Update counters
            self.scalps_today += 1
            self.scalps_this_hour += 1
            self.daily_pnl += pnl
            self.total_pnl += pnl

            if is_loss:
                self.consecutive_losses += 1
                # ★ Stage 0-7 hook (2026-04-22 fix for this agent's letter FAIL 4-3):
                # On SL/Fast-Reject, record a 30 min cooldown for (market, direction)
                try:
                    _mkt = scalp.market if hasattr(scalp, 'market') else market
                    _dir = scalp.direction if hasattr(scalp, 'direction') else direction
                    self._record_post_sl(_mkt, _dir)
                except Exception as exc:
                    logger.debug("[HARPOON] _record_post_sl failed: %s", exc)
                if self.consecutive_losses >= self.config.max_consecutive_loss:
                    self.loss_pause_until = time.time() + 3600  # pause 1 hour
                    logger.warning(
                        "[HARPOON] %d consecutive losses → paused 1 hour",
                        self.consecutive_losses,
                    )
            else:
                self.consecutive_losses = 0

            logger.info(
                "[HARPOON] Exit (%s): %s %s @ $%.4f → $%.4f | PnL=$%.4f | %.1fs | today=%d",
                reason, scalp.market, scalp.direction,
                scalp.entry_price, price, pnl, duration, self.scalps_today,
            )
            # ── Bookkeeping ──
            try:
                from app.manager.trade_journal import journal
                _peak = 0.0
                if scalp.peak_profit_price > 0 and scalp.entry_price > 0:
                    if scalp.direction == "LONG":
                        _peak = (scalp.peak_profit_price / scalp.entry_price - 1) * 100
                    else:
                        _peak = (1 - scalp.peak_profit_price / scalp.entry_price) * 100
                journal.record_exit(
                    strategy="HARPOON", market=scalp.market, direction=scalp.direction,
                    entry_price=scalp.entry_price, exit_price=price, qty=scalp.qty,
                    reason=reason, leverage=self.config.leverage, hold_sec=duration,
                    dynamic_trailing=self.config.dynamic_trailing,
                    breakeven_locked=scalp.breakeven_locked,
                    peak_profit_pct=_peak,
                )
            except Exception:
                pass
            try:
                if self.system and hasattr(self.system, 'ledger'):
                    self.system.ledger.append(
                        "HARPOON_EXIT", market=scalp.market,
                        direction=scalp.direction, qty=scalp.qty,
                        entry_price=scalp.entry_price, exit_price=price,
                        pnl_usdt=round(pnl, 4), reason=reason,
                        duration_sec=round(duration, 1),
                    )
            except Exception:
                pass
            # 🔱 Telegram exit alert (PnL+reason · disable with OMA_HARPOON_ALERTS=0)
            try:
                if self.system and hasattr(self.system, '_send_telegram_safe') and os.getenv("OMA_HARPOON_ALERTS", "1").strip().lower() not in ("0", "false", "no", "off"):
                    self.system._send_telegram_safe(f"🔱 [HARPOON] EXIT {scalp.direction} {scalp.market}\nentry ${scalp.entry_price:.4f} → exit ${price:.4f}\nPnL ${pnl:+.2f} | {reason} | {duration:.0f}s")
            except Exception:
                pass

            # Cleanup
            self._clear_current_scalp_from_active()  # Phase M.B sync
            self.current_scalp = None
            self.state = HarpoonState.COOLDOWN
            self.cooldown_start_ts = time.time()
            self._exit_retry_count = 0  # ★ H12: reset on success
            self._save_config()

            return {"success": True, "pnl": round(pnl, 4), "duration": round(duration, 1)}

        except Exception as exc:
            logger.error("[HARPOON] exit order error: %s", exc, exc_info=True)
            # ★ H12 FIX: retry cap — abandon the position after 5 attempts
            self._exit_retry_count = getattr(self, '_exit_retry_count', 0) + 1
            if self._exit_retry_count >= 5:
                logger.critical("[HARPOON] Exit failed 5x — abandoning scalp, forcing COOLDOWN")
                self._clear_current_scalp_from_active()  # Phase M.B sync
                self.current_scalp = None
                self.state = HarpoonState.COOLDOWN
                self.cooldown_start_ts = time.time()
                self._exit_retry_count = 0
                self._save_config()
            return {"success": False, "reason": str(exc)}

    # ------------------------------------------------------------------
    # Emergency
    # ------------------------------------------------------------------

    def _emergency_close(self):
        """Emergency close."""
        if self.current_scalp:
            price = self._get_current_price(self.current_scalp.market)
            # ★ C3 FIX: if price=0, fall back to entry_price (prevent PnL contamination)
            if not price or price <= 0:
                price = self.current_scalp.entry_price
            self._execute_scalp_exit("EMERGENCY", price)

    # ------------------------------------------------------------------
    # Counter resets
    # ------------------------------------------------------------------

    def _maybe_reset_counters(self, now: float):
        """Reset hourly/daily counters."""
        # Hourly reset
        if now - self.hour_reset_ts >= 3600:
            self.scalps_this_hour = 0
            self.hour_reset_ts = now

        # Daily reset (07:00 KST = 22:00 UTC)
        # ★ H11 FIX: utcnow() deprecated → use now(UTC)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        reset_hour_utc = 22
        today_reset = now_utc.replace(hour=reset_hour_utc, minute=0, second=0, microsecond=0)
        if now_utc.hour < reset_hour_utc:
            today_reset -= datetime.timedelta(days=1)
        reset_ts = today_reset.timestamp()
        if self.daily_reset_ts < reset_ts:
            self.scalps_today = 0
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self.daily_reset_ts = reset_ts
            logger.info("[HARPOON] Daily counters reset")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_config(self):
        """Save config + state."""
        data = {
            "config": asdict(self.config),
            "state": {
                "harpoon_state": self.state.value,
                "scalps_today": self.scalps_today,
                "scalps_this_hour": self.scalps_this_hour,
                "consecutive_losses": self.consecutive_losses,
                "daily_pnl": self.daily_pnl,
                "total_pnl": self.total_pnl,
                "next_scalp_id": self._next_scalp_id,
                "daily_reset_ts": self.daily_reset_ts,
                "hour_reset_ts": self.hour_reset_ts,
                "loss_pause_until": self.loss_pause_until,
                "current_scalp": asdict(self.current_scalp) if self.current_scalp else None,
                "target_zone": self.target_zone,
                "target_direction": self.target_direction,
                "recent_scalps": self.recent_scalps[-20:],  # save only the last 20
            },
        }
        try:
            from app.core.io_utils import safe_write_json
            safe_write_json(CONFIG_PATH, data)
        except (OSError, ImportError) as exc:
            logger.warning("[HARPOON] save config failed: %s", exc)

    def _load_config(self):
        """Restore config + state."""
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Config — ★ M19 FIX: special handling for bool fields (prevent bool("false")==True)
            cfg = data.get("config", {})
            for k, v in cfg.items():
                if hasattr(self.config, k):
                    try:
                        cur = getattr(self.config, k)
                        if isinstance(cur, bool):
                            setattr(self.config, k, v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes"))
                        else:
                            setattr(self.config, k, type(cur)(v))
                    except (TypeError, ValueError):
                        pass

            # State
            st = data.get("state", {})
            self.state = HarpoonState(st.get("harpoon_state", "STANDBY"))
            self.scalps_today = int(st.get("scalps_today", 0))
            self.scalps_this_hour = int(st.get("scalps_this_hour", 0))
            self.consecutive_losses = int(st.get("consecutive_losses", 0))
            self.daily_pnl = float(st.get("daily_pnl", 0))
            self.total_pnl = float(st.get("total_pnl", 0))
            self._next_scalp_id = int(st.get("next_scalp_id", 1))
            self.daily_reset_ts = float(st.get("daily_reset_ts", 0))
            self.hour_reset_ts = float(st.get("hour_reset_ts", 0))
            self.loss_pause_until = float(st.get("loss_pause_until", 0))
            self.target_zone = st.get("target_zone")
            self.target_direction = st.get("target_direction", "")
            self.recent_scalps = st.get("recent_scalps", [])

            # Restore current scalp
            sp = st.get("current_scalp")
            if sp and isinstance(sp, dict) and sp.get("market"):
                self.current_scalp = ScalpPosition(**{
                    k: v for k, v in sp.items()
                    if k in ScalpPosition.__dataclass_fields__
                })
            else:
                self._clear_current_scalp_from_active()  # Phase M.B sync
                self.current_scalp = None

            # ★ Restore TP/SL on boot — prevent Bybit server-side TP/SL from vanishing on restart
            if self.current_scalp and self.current_scalp.market:
                try:
                    from app.integrations.bybit_trade import BybitTradeClient
                    _hc = BybitTradeClient(category="linear")
                    _tp = self.current_scalp.tp
                    _sl = self.current_scalp.sl
                    if _tp > 0 and _sl > 0:
                        _hc.set_trading_stop(self.current_scalp.market, take_profit=_tp, stop_loss=_sl)
                        logger.info("[HARPOON] BOOT: TP/SL restored — %s TP=$%.4f SL=$%.4f",
                                    self.current_scalp.market, _tp, _sl)
                except Exception as _boot_err:
                    logger.warning("[HARPOON] BOOT: TP/SL restore failed — %s (will retry in tick)",
                                   _boot_err)

            logger.info(
                "[HARPOON] Config loaded: state=%s enabled=%s scalps_today=%d pnl=$%.2f",
                self.state.value, self.config.enabled, self.scalps_today, self.daily_pnl,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("[HARPOON] load config failed: %s", exc)

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Return status for the API/dashboard."""
        with self._lock:
            focus_state = ""
            focus_market = ""
            focus_adx = 0.0
            adx_ok = True
            adx_threshold = 0
            adx_source = "inherit"
            if self.focus:
                fs = getattr(self.focus, 'state', None)
                focus_state = fs.value if hasattr(fs, 'value') else str(fs or "")
                focus_market = getattr(self.focus, 'selected_market', "") or ""
                focus_adx = round(getattr(self.focus, '_last_adx_value', 0), 1)
                fc = getattr(self.focus, 'config', None)
                if fc and getattr(fc, 'adx_filter_enabled', False):
                    focus_thr = getattr(fc, 'dormant_adx_threshold', 15)
                    harpoon_min = self.config.min_adx
                    if harpoon_min > 0:
                        adx_threshold = harpoon_min
                        adx_source = "harpoon"
                    else:
                        adx_threshold = focus_thr
                        adx_source = "focus"
                    if focus_adx > 0 and focus_adx < adx_threshold:
                        adx_ok = False

            return {
                "enabled": self.config.enabled,
                "state": self.state.value,
                "focus_state": focus_state,
                "focus_market": focus_market,
                "focus_adx": focus_adx,
                "adx_ok": adx_ok,
                "adx_threshold": adx_threshold,
                "adx_source": adx_source,
                "current_scalp": asdict(self.current_scalp) if self.current_scalp else None,
                "target_zone": self.target_zone,
                "target_direction": self.target_direction,
                "scalps_today": self.scalps_today,
                "scalps_this_hour": self.scalps_this_hour,
                "max_daily_scalps": self.config.max_daily_scalps,
                "consecutive_losses": self.consecutive_losses,
                "daily_pnl": round(self.daily_pnl, 4),
                "total_pnl": round(self.total_pnl, 4),
                "budget": round(self.effective_budget, 2),
                "recent_scalps": self.recent_scalps[-10:],
                "config": asdict(self.config),
            }

    def update_config(self, patch: Dict) -> Dict:
        """Update config via the API."""
        with self._lock:
            for k, v in patch.items():
                if hasattr(self.config, k):
                    try:
                        cur = getattr(self.config, k)
                        if isinstance(cur, bool):
                            setattr(self.config, k, v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes"))
                        else:
                            setattr(self.config, k, type(cur)(v))
                    except (TypeError, ValueError) as exc:
                        logger.warning("[HARPOON] config update failed for %s=%s: %s", k, v, exc)
            self._save_config()
            return asdict(self.config)
