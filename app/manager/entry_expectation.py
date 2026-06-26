# ============================================================
# Entry Expectation Calculator — entry expectation mechanism (Phase 1)
# ------------------------------------------------------------
# At entry time, quantify "how far can price go from here" via market structure.
#   Reward distance = entry price ~ next natural target (primary_tf(H1) S/R zone)
#   Risk distance   = entry price ~ invalidation price (prior primary_tf(H1) swing low/high)
#   RR ratio        = Reward / Risk
#
# Pure functions — no state, no HyperSystem dependency. cycle_tp.py style.
# Input candles are OHLCV (oldest-first). dict↔OHLCV conversion is the caller's job.
# ATR is taken as an argument, but a helper atr_from_ohlcv() is provided for the
# OHLCV path (technical_indicators.calc_atr_from_candles is Bybit-dict-only, unusable here).
# ============================================================
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.strategy.greenpen.pa_detector import OHLCV
from app.strategy.greenpen.zone_engine import compute_zones, Zone, ZoneType
from app.strategy.greenpen.market_structure import analyze_structure


# ── ATR Helper (for OHLCV input) ────────────────────────────

def atr_from_ohlcv(candles: List[OHLCV], period: int = 14) -> float:
    """Compute ATR (absolute price units) from an OHLCV list (oldest-first).

    Standard True Range average. The OHLCV-input version of
    technical_indicators.calc_atr_from_candles — that one is Bybit-dict-only
    ("high_price" etc.), so it can't be used on the OHLCV path
    (same input as zone_engine / market_structure).

    Args:
        candles: OHLCV list, oldest-first (candles[-1] is the latest).
        period: ATR period (default 14). If candles are short, use as many as available.

    Returns:
        ATR value (price units). 0.0 if fewer than 2 candles or no valid TR.
    """
    if len(candles) < 2:
        return 0.0
    n = min(period, len(candles) - 1)
    recent = candles[-(n + 1):]
    true_ranges: List[float] = []
    for i in range(1, len(recent)):
        h = recent[i].high
        lo = recent[i].low
        pc = recent[i - 1].close
        if h <= 0 or lo <= 0 or pc <= 0:
            continue
        true_ranges.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    if not true_ranges:
        return 0.0
    return sum(true_ranges) / len(true_ranges)


# ── Data Types ──────────────────────────────────────────────

@dataclass
class EntryExpectation:
    """Result of the entry expectation computation.

    reward_pct / risk_pct are price-move distances (%) relative to the entry
    price — leverage-independent. rr_ratio is reward_pct / risk_pct.
    """
    reward_pct: float            # entry → reward_target distance (%, always ≥ 0)
    risk_pct: float              # entry → risk_invalidation distance (%, always ≥ 0)
    rr_ratio: float              # reward_pct / risk_pct (0.0 if risk is 0)
    reward_target: float         # target price (absolute, 0 = not computable)
    risk_invalidation: float     # invalidation price (absolute, 0 = not computable)
    reward_source: str           # "primary_zone" / "m15_obstacle" / "atr_fallback" / "none"
    risk_source: str             # "primary_swing" / "atr_fallback" / "none"
    note: str                    # one-line diagnostic description

    @property
    def is_valid(self) -> bool:
        """Whether both reward and risk were computed — prerequisite for the Gate (Phase 2)."""
        return self.reward_pct > 0.0 and self.risk_pct > 0.0


# ── Public API ──────────────────────────────────────────────

def compute_entry_expectation(
    direction: str,
    entry_price: float,
    primary_candles: List[OHLCV],
    atr_primary: float,
    *,
    m15_candles: Optional[List[OHLCV]] = None,
    m15_atr: Optional[float] = None,
    swing_lookback: int = 5,
    max_risk_pct: float = 0.15,
) -> EntryExpectation:
    """Compute entry expectation (Reward / Risk / RR) from market structure.

    Args:
        direction: "LONG" or "SHORT".
        entry_price: (planned) entry price.
        primary_candles: primary_tf(H1) OHLCV list (oldest-first). For reward zone + risk swing.
        atr_primary: primary_tf(H1) ATR(14). Zone width calc + fallback distance when no S/R found.
        m15_candles: 15m OHLCV list (oldest-first). For detecting obstacles along the reward path.
        m15_atr: 15m ATR(14). Must be present together with m15_candles for obstacle detection to work.
        swing_lookback: number of left/right candles used to confirm a swing point.
        max_risk_pct: if risk_invalidation (the invalidation line) is farther than this
            ratio relative to the entry price, distrust the swing and use ATR fallback
            (default 0.15 = 15%).

    Returns:
        EntryExpectation. If reward or risk is not computable, that _pct=0.0, source="none".
        If input is broken (entry_price<=0, primary_candles<3), all 0 + is_valid=False.
    """
    d = direction.upper()
    is_long = d == "LONG"

    # Broken input → empty result — blocked at the Gate (Phase 2) via is_valid=False
    if entry_price <= 0 or len(primary_candles) < 3:
        return EntryExpectation(
            reward_pct=0.0, risk_pct=0.0, rr_ratio=0.0,
            reward_target=0.0, risk_invalidation=0.0,
            reward_source="none", risk_source="none",
            note="invalid input (entry_price<=0 or primary_candles<3)",
        )

    reward_target, reward_source = _compute_reward_target(
        is_long, entry_price, primary_candles, atr_primary, m15_candles, m15_atr,
    )
    risk_invalidation, risk_source = _compute_risk_invalidation(
        is_long, entry_price, primary_candles, atr_primary, swing_lookback, max_risk_pct,
    )

    reward_pct = (
        abs(reward_target - entry_price) / entry_price * 100.0
        if reward_target > 0 else 0.0
    )
    risk_pct = (
        abs(entry_price - risk_invalidation) / entry_price * 100.0
        if risk_invalidation > 0 else 0.0
    )
    rr_ratio = reward_pct / risk_pct if risk_pct > 0 else 0.0

    note = (
        f"{d} @ {entry_price:.6g} | "
        f"reward {reward_pct:.2f}% (~{reward_target:.6g}, {reward_source}) | "
        f"risk {risk_pct:.2f}% (~{risk_invalidation:.6g}, {risk_source}) | "
        f"RR {rr_ratio:.2f}"
    )

    return EntryExpectation(
        reward_pct=round(reward_pct, 4),
        risk_pct=round(risk_pct, 4),
        rr_ratio=round(rr_ratio, 4),
        reward_target=round(reward_target, 8),
        risk_invalidation=round(risk_invalidation, 8),
        reward_source=reward_source,
        risk_source=risk_source,
        note=note,
    )


# ── Reward Target ───────────────────────────────────────────

def _compute_reward_target(
    is_long: bool,
    entry_price: float,
    primary_candles: List[OHLCV],
    atr_primary: float,
    m15_candles: Optional[List[OHLCV]],
    m15_atr: Optional[float],
) -> Tuple[float, str]:
    """Compute next natural target: primary_tf(H1) S/R zone → shorten by m15 path obstacle → ATR fallback."""
    target = 0.0
    source = "none"

    # 1. primary_tf(H1) zone-based primary target (compute_zones returns [] if atr<=0)
    if atr_primary > 0:
        zones = compute_zones(primary_candles, atr_primary)
        zone = _nearest_directional_zone(zones, entry_price, is_long)
        if zone is not None:
            # The zone boundary hit first — lower for LONG, upper for SHORT (conservative)
            target = zone.price_low if is_long else zone.price_high
            source = "primary_zone"

    # 2. m15 path obstacle — if an opposing structure is hit before the primary target, shorten it
    if target > 0 and m15_candles and m15_atr and m15_atr > 0 and len(m15_candles) >= 3:
        m15_zones = compute_zones(m15_candles, m15_atr)
        obstacle = _path_obstacle(m15_zones, entry_price, target, is_long)
        if obstacle is not None:
            target = obstacle
            source = "m15_obstacle"

    # 3. No S/R found → ATR fallback (a spot where structure can't be read = conservative 1×ATR)
    if target <= 0 and atr_primary > 0:
        target = entry_price + atr_primary if is_long else entry_price - atr_primary
        source = "atr_fallback"

    # Guard against the extreme case where ATR fallback goes negative (SHORT, atr > entry_price)
    if target <= 0:
        return 0.0, "none"

    return target, source


def _nearest_directional_zone(
    zones: List[Zone],
    entry_price: float,
    is_long: bool,
) -> Optional[Zone]:
    """Nearest target zone beyond the entry price.

    LONG → RESISTANCE above the entry price, SHORT → SUPPORT below the entry price.
    """
    best: Optional[Zone] = None
    best_dist = float("inf")
    for z in zones:
        mid = (z.price_low + z.price_high) / 2.0
        if is_long:
            if z.type != ZoneType.RESISTANCE or mid <= entry_price:
                continue
            dist = mid - entry_price
        else:
            if z.type != ZoneType.SUPPORT or mid >= entry_price:
                continue
            dist = entry_price - mid
        if dist < best_dist:
            best_dist = dist
            best = z
    return best


def _path_obstacle(
    zones: List[Zone],
    entry_price: float,
    reward_target: float,
    is_long: bool,
) -> Optional[float]:
    """Price of the opposing structure (obstacle) hit first along the entry ~ reward_target path.

    LONG → RESISTANCE along the path, SHORT → SUPPORT along the path. None if absent.
    """
    best: Optional[float] = None
    best_dist = float("inf")
    for z in zones:
        if is_long:
            if z.type != ZoneType.RESISTANCE:
                continue
            level = z.price_low  # boundary hit first
            if not (entry_price < level < reward_target):
                continue
            dist = level - entry_price
        else:
            if z.type != ZoneType.SUPPORT:
                continue
            level = z.price_high
            if not (reward_target < level < entry_price):
                continue
            dist = entry_price - level
        if dist < best_dist:
            best_dist = dist
            best = level
    return best


# ── Risk Invalidation ───────────────────────────────────────

def _compute_risk_invalidation(
    is_long: bool,
    entry_price: float,
    primary_candles: List[OHLCV],
    atr_primary: float,
    swing_lookback: int,
    max_risk_pct: float = 0.15,
) -> Tuple[float, str]:
    """Compute invalidation price: prior primary_tf(H1) swing low/high → ATR fallback.

    If a swing is farther than max_risk_pct (default 15%) from the entry price, distrust it.
    Sharply moved coins have no swing near the entry price and pick up a very old, far
    extreme (e.g. SHORT but the swing high is +117% from entry), which turns the SL into a
    cliff and wrecks RR. In that case, discard the swing and downgrade to ATR fallback.
    """
    structure = analyze_structure(primary_candles, lookback=swing_lookback)
    invalidation = 0.0
    source = "none"

    if is_long:
        # nearest (= highest) swing low below the entry price
        candidates = [
            s.price for s in structure.swings
            if not s.is_high and 0 < s.price < entry_price
        ]
        if candidates:
            invalidation = max(candidates)
            source = "primary_swing"
    else:
        # nearest (= lowest) swing high above the entry price
        candidates = [
            s.price for s in structure.swings
            if s.is_high and s.price > entry_price
        ]
        if candidates:
            invalidation = min(candidates)
            source = "primary_swing"

    # ★ If a swing is abnormally far (exceeds max_risk_pct vs entry), untrustworthy → discard
    if invalidation > 0 and abs(entry_price - invalidation) / entry_price > max_risk_pct:
        invalidation = 0.0
        source = "none"

    # Swing not found, or discarded for being too far → ATR fallback
    if invalidation <= 0 and atr_primary > 0:
        invalidation = entry_price - atr_primary if is_long else entry_price + atr_primary
        source = "atr_fallback"

    # Guard against the extreme case where ATR fallback goes negative (LONG, atr > entry_price)
    if invalidation <= 0:
        return 0.0, "none"

    return invalidation, source
