"""reserved_selector_budget.py – Budget allocation helpers.

Extracted from reserved_selector.py (L2630-2984) without any logic changes.
Functions: _suggest_budget, _suggest_budget_dynamic
"""
from __future__ import annotations

import math
from typing import Dict, Optional

from app.manager.reserved_selector_utils import _clamp, _finalize_usdt_notional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _suggest_budget(
    *,
    strategy: str,
    base_usdt: float,
    vol24_usdt: float,
    vol_median_usdt: float,
    min_order_usdt: float,
    max_budget_usdt: float,
    price: float,
    entry_qty_guard_on: bool,
    entry_max_qty: float,
    depth_factor: float,
    depth_ask_usdt: float,
    depth_bid_usdt: float,
    # Dynamic budget allocation parameters (optional)
    total_capital_usdt: float = 0.0,
    existing_markets_count: int = 0,
    spread_bps: float = 0.0,
    range_ratio_24h: float = 0.0,
    trend_score: float = 0.0,  # [2026-02-03] trend score (-10 ~ +10)
    ai_features: Optional[Dict[str, float]] = None,  # [2026-02-03] volatility for LIGHTNING
) -> Optional[float]:
    """Compute a recommended per-market budget that respects qty/depth guards.

    Dynamic budget calculation:
    - if total_capital_usdt > 0, use total-capital-based slot allocation
    - reflect liquidity (vol24), spread, and strategy characteristics
    - balance allocation considering the number of existing markets
    - differentiate allocation based on volatility (range_ratio_24h) and price
    """
    # Dynamic budget allocation mode
    if total_capital_usdt > 0 and existing_markets_count >= 0:
        return _suggest_budget_dynamic(
            strategy=strategy,
            total_capital_usdt=total_capital_usdt,
            existing_markets_count=existing_markets_count,
            vol24_usdt=vol24_usdt,
            vol_median_usdt=vol_median_usdt,
            spread_bps=spread_bps,
            price=price,
            min_order_usdt=min_order_usdt,
            max_budget_usdt=max_budget_usdt,
            entry_qty_guard_on=entry_qty_guard_on,
            entry_max_qty=entry_max_qty,
            depth_factor=depth_factor,
            depth_ask_usdt=depth_ask_usdt,
            depth_bid_usdt=depth_bid_usdt,
            range_ratio_24h=range_ratio_24h,
            trend_score=trend_score,  # [2026-02-03] pass trend
            ai_features=ai_features,  # [2026-02-03] pass AI features
        )

    # Legacy logic (base_usdt based)
    base = float(base_usdt)
    if base <= 0:
        return None

    # scale by liquidity (gentle)
    med = float(vol_median_usdt) if vol_median_usdt > 0 else float(vol24_usdt)
    mult = 1.0
    if med > 0 and vol24_usdt > 0:
        mult = math.sqrt(float(vol24_usdt) / float(med))

    # pingpong: tighter scaling to keep order sizes consistent
    if str(strategy).upper() == "PINGPONG":
        mult = _clamp(mult, 0.75, 1.45)
    else:
        mult = _clamp(mult, 0.70, 1.70)

    budget = float(base) * float(mult)
    budget = _clamp(budget, float(min_order_usdt), float(max_budget_usdt) if max_budget_usdt > 0 else budget)

    # qty guard: budget <= price * max_qty
    if entry_qty_guard_on and entry_max_qty > 0 and price > 0:
        max_by_qty = float(price) * float(entry_max_qty)
        budget = min(budget, max_by_qty)

    # depth guard: required_notional = budget * depth_factor <= min(depth)
    if depth_factor > 0 and depth_ask_usdt > 0 and depth_bid_usdt > 0:
        max_by_depth = min(depth_ask_usdt, depth_bid_usdt) / float(depth_factor)
        budget = min(budget, max_by_depth)

    return _finalize_usdt_notional(budget, float(min_order_usdt))


def _suggest_budget_dynamic(
    *,
    strategy: str,
    total_capital_usdt: float,
    existing_markets_count: int,
    vol24_usdt: float,
    vol_median_usdt: float,
    spread_bps: float,
    price: float,
    min_order_usdt: float,
    max_budget_usdt: float,
    entry_qty_guard_on: bool,
    entry_max_qty: float,
    depth_factor: float,
    depth_ask_usdt: float,
    depth_bid_usdt: float,
    range_ratio_24h: float = 0.0,
    trend_score: float = 0.0,  # [2026-02-03] trend score (-10 ~ +10)
    ai_features: Optional[Dict[str, float]] = None,  # [2026-02-03] volatility for LIGHTNING
) -> Optional[float]:
    """Dynamic budget calculation (total-capital based).

    [2026-02-03] Added trend reflection:
    - uptrend (trend > 3): budget +30%
    - downtrend (trend < -3): budget -30%
    - sideways (-3 ~ +3): no change

    Calculation steps:
    1. slot budget = total capital / (existing markets + 1 new)
    2. liquidity scaling (sqrt softened)
    3. per-strategy weight
    4. spread penalty
    5. volatility (range_ratio) penalty
    6. price-based differentiation
    7. trend-based adjustment (NEW!)
    8. large-capital safeguard
    """
    if total_capital_usdt <= 0:
        return None

    # ai_features default
    if ai_features is None:
        ai_features = {}

    # 1. Base slot budget
    slot_count = max(1, existing_markets_count + 1)
    reserve_ratio = 0.05  # 5% reserve
    available = total_capital_usdt * (1.0 - reserve_ratio)
    base_budget = available / slot_count

    # 2. Liquidity scaling
    liq_factor = 1.0
    med = float(vol_median_usdt) if vol_median_usdt > 0 else 10_000_000.0  # 10M USDT baseline
    if vol24_usdt > 0:
        liq_factor = min(1.5, max(0.6, math.sqrt(vol24_usdt / med)))

    # 3. Per-strategy weight
    strategy_weights = {
        "PINGPONG": 1.0,
        "AUTOLOOP": 1.1,
        "LADDER": 1.4,    # DCA needs more capital
        "LIGHTNING": 0.7,  # fast in/out → less capital
        "GAZUA": 1.2,      # long-term hold
    }
    strat_weight = strategy_weights.get(str(strategy).upper(), 1.0)

    # 4. Spread penalty
    spread_factor = 1.0
    if spread_bps > 15:
        spread_factor = max(0.7, 1.0 - (spread_bps - 15) / 80)

    # 5. Volatility penalty (range_ratio_24h based)
    #    - range_ratio <= 3%: 1.2x (stable-coin bonus)
    #    - range_ratio 3~8%: 1.0x (normal)
    #    - range_ratio >= 8%: 0.5~0.8x (high-volatility penalty)
    volatility_factor = 1.0
    if range_ratio_24h > 0:
        range_pct = range_ratio_24h * 100.0  # 0.08 → 8%
        if range_pct <= 3.0:
            volatility_factor = 1.2  # stable/low-volatility bonus
        elif range_pct <= 8.0:
            volatility_factor = 1.0  # normal
        else:
            # above 8%: linear decay (8% → 1.0, 20% → 0.5)
            volatility_factor = max(0.5, 1.0 - (range_pct - 8.0) / 24.0)

    # 6. Price-based differentiation (USDT price based)
    #    [FIX 2026-01-23] The old code's comments assumed USD, but the actual input is USDT,
    #    so log10(price) is almost always positive → saturated to ~1.0 by min(1.0, ...).
    #    → Set a USDT pivot so low prices < 1 and high prices > 1.
    #
    #    Examples (pivot=$10=log10(1)):
    #    - BTC $95K: log10≈4.98 → factor≈1.60 → clamped to 1.5
    #    - ETH $3.2K: log10≈3.51 → factor≈1.38
    #    - low $0.50: log10≈-0.30 → factor≈0.81
    price_factor = 1.0
    if price > 0:
        logp = math.log10(price + 0.0001)  # avoid 0
        pivot = 1.0  # log10(10) - $10 reference point
        price_factor = _clamp(1.0 + (logp - pivot) * 0.15, 0.4, 1.5)

    # 7. Final calculation (base) - trend excluded (applied later)
    budget = base_budget * liq_factor * strat_weight * spread_factor * volatility_factor * price_factor

    # =========================================================
    # 9. Large-capital safeguard (prevent slippage/orderbook impact)
    # =========================================================

    # 6-1. Cap relative to daily volume (within 0.5% of vol24)
    #      → low-price coin with vol24=50M USDT → max 25K USDT
    #      → most coins are far below this
    vol_ratio_limit = 0.005  # 0.5%
    if vol24_usdt > 0:
        max_by_vol = vol24_usdt * vol_ratio_limit
        budget = min(budget, max_by_vol)

    # 6-2. Orderbook-depth cap (within 20% of two-sided depth)
    #      → avoid consuming the whole orderbook
    if depth_ask_usdt > 0 and depth_bid_usdt > 0:
        depth_limit_ratio = 0.20  # 20% of orderbook depth
        min_depth = min(depth_ask_usdt, depth_bid_usdt)
        max_by_depth_safe = min_depth * depth_limit_ratio
        budget = min(budget, max_by_depth_safe)

    # 6-3. Low-price coin protection (limit max qty relative to price)
    #      → large orders on low-price coins → slippage risk
    #      → limit max holding qty to 1% of daily volume
    if price > 0 and vol24_usdt > 0:
        # estimate daily traded qty (notional / price)
        daily_vol_qty = vol24_usdt / price
        # max holding qty = 1% of daily volume
        max_qty_safe = daily_vol_qty * 0.01
        max_by_qty_safe = price * max_qty_safe
        budget = min(budget, max_by_qty_safe)

    # 6-4. Per-coin cap (total-capital ratio) - dynamic cap linked to price_factor
    #      [FIX 2026-01-23] The old fixed 10% cap converged every coin to the same budget.
    #      → Open the cap a bit more for high-price coins and tighten it for low-price coins.
    #      Examples (total capital $2,000):
    #      - BTC (price_factor=1.5): 10%*1.5=15% → $300 cap
    #      - ETH (price_factor=1.4): 10%*1.4=14% → $280 cap
    #      - low (price_factor=0.77): 10%*0.77=7.7% → $154 cap
    base_max_ratio = 0.10  # keep existing diversification policy
    max_per_coin_ratio = _clamp(base_max_ratio * price_factor, 0.06, 0.20)
    max_by_total = total_capital_usdt * max_per_coin_ratio
    budget = min(budget, max_by_total)

    # =========================================================
    # =========================================================
    # 7. Apply basic constraints
    # =========================================================
    min_b = max(0.0, float(min_order_usdt))
    max_b = max_budget_usdt if max_budget_usdt > 0 else 10000.0  # default cap ($10K)
    budget = _clamp(budget, min_b, max_b)

    # qty guard (existing)
    if entry_qty_guard_on and entry_max_qty > 0 and price > 0:
        max_by_qty = float(price) * float(entry_max_qty)
        budget = min(budget, max_by_qty)

    # depth guard (existing - more conservative)
    if depth_factor > 0 and depth_ask_usdt > 0 and depth_bid_usdt > 0:
        max_by_depth = min(depth_ask_usdt, depth_bid_usdt) / float(depth_factor)
        budget = min(budget, max_by_depth)

    # =========================================================
    # [2026-02-03] 8. Trend-based final adjustment
    # =========================================================
    # Apply trend after safeguards (adjust within the cap)
    trend_factor = 1.0

    # CONTRARIAN is a contrarian strategy, so the logic is reversed
    if strategy == "CONTRARIAN":
        # Buy counter-trend coins when the benchmark (BTC) drops
        # - mild drop (-1 ~ -3): high counter-trend confidence → normal budget
        # - strong drop (< -3): further-downside risk → reduce budget
        # - crash (< -5): extremely risky → minimize budget
        # - uptrend (> 1): not counter-trend → reduce budget
        if abs(trend_score) > 0.1:
            if trend_score < -5.0:  # crash (-15% or more)
                trend_factor = 0.5  # -50% budget
            elif trend_score < -3.0:  # strong drop (-10% or more)
                trend_factor = 0.7  # -30% budget
            elif trend_score < -1.0:  # mild drop (-3% or more)
                trend_factor = 1.0  # normal (optimal counter-trend environment)
            elif trend_score > 1.0:  # uptrend
                trend_factor = 0.3  # -70% budget (low counter-trend signal confidence)

    # [2026-02-03] LADDER is a DCA strategy → drop = opportunity
    elif strategy == "LADDER":
        # In a falling market, scale-in (DCA) opportunity
        # - crash (< -5): extreme DCA opportunity → increase budget
        # - strong drop (< -3): DCA opportunity → increase budget
        # - mild drop (< -1): normal
        # - uptrend (> 3): unsuitable for DCA → reduce budget
        if abs(trend_score) > 0.1:
            if trend_score < -5.0:  # crash (-15% or more)
                trend_factor = 1.3  # +30% budget (extreme DCA opportunity)
            elif trend_score < -3.0:  # strong drop (-10% or more)
                trend_factor = 1.2  # +20% budget
            elif trend_score < -1.0:  # mild drop (-3% or more)
                trend_factor = 1.1  # +10% budget
            elif trend_score > 3.0:  # strong uptrend
                trend_factor = 0.7  # -30% budget (unsuitable for DCA)

    # [2026-02-03] GAZUA is selective long-term holding → trend-agnostic even allocation
    elif strategy == "GAZUA":
        # Discover undervalued coins then hold long term
        # - keep a constant budget regardless of trend
        # - entry timing: AI conviction (0.75+) + undervaluation indicators
        # - holding philosophy: bury long term (TP 25%, Grace 24h)
        trend_factor = 1.0  # trend-agnostic even allocation

    # [2026-02-03] LIGHTNING is volatility-based (trend-agnostic)
    elif strategy == "LIGHTNING":
        # Spike potential = volatility
        # - extreme volatility (> 5): many spike opportunities → increase budget
        # - high volatility (> 3): normal
        # - low volatility (< 1.5): spikes impossible → reduce budget
        volatility = ai_features.get("volatility", 2.0)
        if volatility > 5.0:  # extreme volatility
            trend_factor = 1.3  # +30% budget
        elif volatility > 3.0:  # high volatility
            trend_factor = 1.2  # +20% budget
        elif volatility > 1.5:  # medium volatility
            trend_factor = 1.1  # +10% budget
        else:  # low volatility (<1.5)
            trend_factor = 0.7  # -30% budget (unsuitable)

    # [FIX 2026-03-05] SNIPER strategy: counter-trend (rebound-from-drop) - drop = opportunity, rise = unsuitable
    elif strategy == "SNIPER":
        if abs(trend_score) > 0.1:
            if trend_score < -3.0:    # strong drop = SNIPER entry opportunity
                trend_factor = 1.2   # +20% budget
            elif trend_score < -1.0:  # mild drop = slight preference
                trend_factor = 1.1   # +10% budget
            elif trend_score > 3.0:   # strong rise = unsuitable for SNIPER counter-trend
                trend_factor = 0.7   # -30% budget
            elif trend_score > 1.0:   # mild rise = slight reduction
                trend_factor = 0.85  # -15% budget
    # General strategy: favor uptrends
    elif abs(trend_score) > 0.1:
        if trend_score > 3.0:  # strong uptrend
            trend_factor = 1.3  # +30%
        elif trend_score > 1.0:  # mild uptrend
            trend_factor = 1.15  # +15%
        elif trend_score < -3.0:  # strong downtrend
            trend_factor = 0.7  # -30%
        elif trend_score < -1.0:  # mild downtrend
            trend_factor = 0.85  # -15%

    budget = budget * trend_factor

    # Re-check cap after applying trend
    budget = min(budget, max_b)
    budget = max(budget, min_b)

    return _finalize_usdt_notional(budget, float(min_order_usdt))
