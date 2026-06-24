# ============================================================
# GreenPen Zone Engine
# ------------------------------------------------------------
# Implements EP.8 from the Green Pen System:
#   - "gap·wick·pair" (Zok·Sai·Koo) based S/R zone detection
#   - Daily range calculation (ATR-based)
#   - Zone proximity checking
#
# Pure functions — no state, no HyperSystem dependency.
# ============================================================
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from .pa_detector import OHLCV


# ── Data Types ──────────────────────────────────────────────

class ZoneType(str, Enum):
    SUPPORT = "SUPPORT"
    RESISTANCE = "RESISTANCE"


@dataclass
class Zone:
    type: ZoneType
    price_low: float   # zone bottom
    price_high: float  # zone top
    strength: float    # 0.0~1.0 (how many elements overlap here)
    source: str = ""   # "zok", "sai", "koo", "cluster"


@dataclass
class DailyRange:
    open_price: float
    upper: float       # open + ATR/2
    lower: float       # open - ATR/2
    atr: float


# ── Zone Computation ────────────────────────────────────────

def compute_zones(
    candles: List[OHLCV],
    atr: float,
    *,
    max_zones: int = 4,
    zone_width_atr_mult: float = 0.2,
) -> List[Zone]:
    """Compute support/resistance zones using the gap·wick·pair method.

    Args:
        candles: H4 OHLCV list (oldest first). 10+ candles recommended.
        atr: ATR(14) of the same timeframe.
        max_zones: maximum number of zones to return.
        zone_width_atr_mult: zone width as fraction of ATR.

    Returns:
        List of Zone sorted by strength (strongest first).
    """
    if len(candles) < 3 or atr <= 0:
        return []

    zone_width = atr * zone_width_atr_mult
    price_points: List[Tuple[float, str]] = []

    # 1. gap (Zok) — gap between same-color adjacent candle bodies
    for i in range(len(candles) - 1):
        c1, c2 = candles[i], candles[i + 1]
        if c1.is_bullish == c2.is_bullish:  # same color
            gap_low = min(c1.close, c2.open)
            gap_high = max(c1.close, c2.open)
            mid = (gap_low + gap_high) / 2
            price_points.append((mid, "zok"))

    # 2. wick (Sai) — candle wick tips
    for c in candles:
        if c.upper_wick > c.body_len * 0.3:
            price_points.append((c.high, "sai"))
        if c.lower_wick > c.body_len * 0.3:
            price_points.append((c.low, "sai"))

    # 3. pair (Koo) — body overlap between different-color adjacent candles
    for i in range(len(candles) - 1):
        c1, c2 = candles[i], candles[i + 1]
        if c1.is_bullish != c2.is_bullish:  # different color
            overlap_low = max(c1.body_bottom, c2.body_bottom)
            overlap_high = min(c1.body_top, c2.body_top)
            if overlap_high > overlap_low:
                mid = (overlap_low + overlap_high) / 2
                price_points.append((mid, "koo"))

    if not price_points:
        return []

    # 4. Cluster price points into zones
    zones = _cluster_into_zones(price_points, zone_width, candles[-1].close)

    # 5. Balanced selection: guarantee both SUPPORT & RESISTANCE represented
    supports = sorted([z for z in zones if z.type == ZoneType.SUPPORT], key=lambda z: -z.strength)
    resistances = sorted([z for z in zones if z.type == ZoneType.RESISTANCE], key=lambda z: -z.strength)

    if supports and resistances:
        # guarantee at least one of each → fill the rest by strength
        result: List[Zone] = [supports[0], resistances[0]]
        leftovers = supports[1:] + resistances[1:]
        leftovers.sort(key=lambda z: -z.strength)
        result.extend(leftovers[: max_zones - 2])
    else:
        # only one side present → keep as is
        result = (supports + resistances)[:max_zones]

    result.sort(key=lambda z: -z.strength)
    return result


def _cluster_into_zones(
    points: List[Tuple[float, str]],
    zone_width: float,
    current_price: float,
) -> List[Zone]:
    """Cluster nearby price points into zones."""
    if not points:
        return []

    # Sort by price
    sorted_pts = sorted(points, key=lambda x: x[0])

    clusters: List[List[Tuple[float, str]]] = []
    current_cluster: List[Tuple[float, str]] = [sorted_pts[0]]

    for i in range(1, len(sorted_pts)):
        price, src = sorted_pts[i]
        cluster_mid = sum(p for p, _ in current_cluster) / len(current_cluster)
        if abs(price - cluster_mid) <= zone_width:
            current_cluster.append((price, src))
        else:
            clusters.append(current_cluster)
            current_cluster = [(price, src)]
    clusters.append(current_cluster)

    # Convert clusters to Zones
    zones: List[Zone] = []
    for cluster in clusters:
        if not cluster:
            continue
        prices = [p for p, _ in cluster]
        sources = [s for _, s in cluster]
        mid = sum(prices) / len(prices)
        hw = max(zone_width / 2, (max(prices) - min(prices)) / 2)

        # Strength: more points + diverse sources = stronger
        unique_sources = len(set(sources))
        strength = min(1.0, (len(cluster) / 5.0) * (unique_sources / 3.0))

        zone_type = ZoneType.SUPPORT if mid < current_price else ZoneType.RESISTANCE
        zones.append(Zone(
            type=zone_type,
            price_low=mid - hw,
            price_high=mid + hw,
            strength=strength,
            source="+".join(sorted(set(sources))),
        ))

    return zones


# ── Daily Range ─────────────────────────────────────────────

def compute_daily_range(
    candles_d1: List[OHLCV],
    atr_d1: float,
) -> Optional[DailyRange]:
    """Daily range = today's open ± ATR(14,D1)/2.

    Replaces the Green Pen's fixed 1000-point daily range for gold.
    """
    if not candles_d1 or atr_d1 <= 0:
        return None

    today_open = candles_d1[-1].open
    half_atr = atr_d1 / 2.0

    return DailyRange(
        open_price=today_open,
        upper=today_open + half_atr,
        lower=today_open - half_atr,
        atr=atr_d1,
    )


# ── Zone Queries ────────────────────────────────────────────

def is_price_in_zone(
    price: float,
    zones: List[Zone],
    zone_type: Optional[str] = None,
) -> Optional[Zone]:
    """Check if price falls within any zone.

    Args:
        price: current price.
        zones: list of Zone objects.
        zone_type: "SUPPORT", "RESISTANCE", or None for any.

    Returns:
        The matching Zone, or None.
    """
    for z in zones:
        if zone_type and z.type.value != zone_type:
            continue
        if z.price_low <= price <= z.price_high:
            return z
    return None


def nearest_zone(
    price: float,
    zones: List[Zone],
    zone_type: Optional[str] = None,
) -> Optional[Tuple[Zone, float]]:
    """Find nearest zone and distance to it.

    Returns:
        (Zone, distance) where distance is negative if price is below zone,
        positive if above. Returns None if no zones.
    """
    best: Optional[Tuple[Zone, float]] = None
    for z in zones:
        if zone_type and z.type.value != zone_type:
            continue
        mid = (z.price_low + z.price_high) / 2
        dist = price - mid
        if best is None or abs(dist) < abs(best[1]):
            best = (z, dist)
    return best


def is_price_in_daily_range(price: float, daily: DailyRange) -> bool:
    """Check if price is within today's expected daily range."""
    return daily.lower <= price <= daily.upper
