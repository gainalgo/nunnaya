# ============================================================
# Upbit FOCUS Entry Quality — entry-quality gates (long_only, isolated & pure)
# ------------------------------------------------------------
# Owner diagnosis (2026-06-16): on Upbit's first live day entries were reckless —
#   chasing tops/exhaustion. The disease is *entry* (chasing tops); exits are the
#   symptom. Don't add more cuts; look at entry room.
#   "headroom is a gate, not a penalty (no room -> block entry)" — feedback_bad_entry_not_fixed_by_cut.
#
# Pure functions only — no I/O or state. Each gate is independently ON/OFF via
#   config, default OFF = zero change.
#   (DESIGN_upbit_v3_ribbon §② entry-quality config expansion — live-return gate)
# ============================================================
from __future__ import annotations

from typing import Any, Dict, List, Tuple


def check_headroom(
    price: float,
    zones: List[Dict[str, Any]],
    *,
    min_headroom_pct: float,
) -> Tuple[bool, str]:
    """Headroom gate: room up to the nearest overhead resistance (RESISTANCE).

    Room < min_headroom_pct -> chasing the top = block entry (a gate, not a penalty).
    No overhead resistance (clear sky) -> pass. min_headroom_pct<=0 -> gate OFF (always pass).

    Args:
        price: current price (intended entry price).
        zones: GreenPen-serialized zone list [{type, price_low, price_high, strength}, ...].
        min_headroom_pct: required minimum room %. Gate disabled if <= 0.

    Returns:
        (ok, reason). ok=False means blocked.
    """
    if min_headroom_pct <= 0 or price <= 0:
        return True, "headroom:off"
    # overhead resistance = nearest RESISTANCE zone whose price_low is above the current price
    overhead = [
        float(z.get("price_low", 0) or 0)
        for z in (zones or [])
        if str(z.get("type", "")).upper() == "RESISTANCE"
        and float(z.get("price_low", 0) or 0) > price
    ]
    if not overhead:
        return True, "headroom:clear_sky"
    nearest = min(overhead)
    headroom_pct = (nearest - price) / price * 100.0
    if headroom_pct < min_headroom_pct:
        return False, f"headroom_block:{headroom_pct:.2f}%<{min_headroom_pct:.2f}% (resistance {nearest:.4f})"
    return True, f"headroom_ok:{headroom_pct:.2f}%"


def check_overextension(
    last: float,
    hi24: float,
    lo24: float,
    move_pct: float,
    *,
    range_pos_pct: float,
    min_move_pct: float,
) -> Tuple[bool, str]:
    """Exhaustion-chasing gate — block entry near the top of the 24H range after a
    big surge (avoids chasing an exhausted trend).

    Ported from Bybit `_check_overextension` (2026-06-07, owner). ★But **no ADX exemption** —
    owner diagnosis (feedback_bad_entry_not_fixed_by_cut): "the ADX>=30 exemption is the common
    leak for top entries". Pumps make ADX explode, so the exemption let every 80+ top entry
    through. -> removing the exemption is the core fix.

    LONG only (spot): pos = (last-lo)/(hi-lo) >= range_pos_pct  AND  |move| >= min_move_pct -> block.
    range_pos_pct<=0 -> gate OFF. If the move is small (<min_move) it's not exhaustion -> pass.

    Returns:
        (ok, reason). ok=False means blocked.
    """
    if range_pos_pct <= 0:
        return True, "overext:off"
    rng = hi24 - lo24
    if last <= 0 or rng <= 0:
        return True, "overext:no_data"
    if abs(move_pct) < min_move_pct:
        return True, f"overext:small_move({move_pct:.1f}%)"
    pos = (last - lo24) / rng   # 0 = 24H low, 1 = 24H high
    if pos >= range_pos_pct:
        return False, f"overext_block:pos{pos*100:.0f}%≥{range_pos_pct*100:.0f}% move{move_pct:.1f}%"
    return True, f"overext_ok:pos{pos*100:.0f}%"


def check_blowoff(
    move_pct: float,
    *,
    blowoff_move_pct: float,
    direction: str = "LONG",
) -> Tuple[bool, str]:
    """Blow-off exhaustion — block entry when the 24H |move| is extreme (>=threshold)
    and the direction is *chasing*.

    Ported from Bybit `_check_blowoff` (2026-06-13, owner #1). Complements the ADX-exemption
    leak in overext — pumps explode ADX and slipped past overext, but this catches them directly
    by the *24H move magnitude* (ADX-independent). No range-position (pos) requirement, so even
    after a surge pulls back (not at the range top) it still catches parabolic chasing risk.

    Spot long_only: LONG on a +surge (chg>0) = chasing -> block. LONG on a -drop (chg<0) =
    fade (buy the dip) -> exempt (handled by other gates). blowoff_move_pct<=0 -> OFF.

    Returns:
        (ok, reason). ok=False means blocked.
    """
    if blowoff_move_pct <= 0:
        return True, "blowoff:off"
    move = abs(move_pct)
    if move < blowoff_move_pct:
        return True, f"blowoff:below({move:.0f}%<{blowoff_move_pct:.0f}%)"
    chasing = (direction or "").upper() == "LONG" and move_pct > 0
    if not chasing:
        return True, f"blowoff:fade({move_pct:+.0f}%)"
    return False, f"blowoff_block:24h{move_pct:+.0f}%≥{blowoff_move_pct:.0f}% chasing"


def atr_floored_sl_distance(
    entry_price: float,
    pct_sl_distance: float,
    atr: float,
    *,
    atr_sl_floor_mult: float,
) -> float:
    """Put an ATR floor under the fixed-% SL distance — prevents instant death from
    1-minute noise on small/illiquid coins.

    Owner diagnosis: "a fixed 1% SL is narrower than the 1-minute noise of small coins,
    causing back-to-back 0-2 minute SL hits". SL distance = max(fixed % distance,
    atr_sl_floor_mult × ATR). Only ever *widens* the SL (never tightens it).
    atr_sl_floor_mult<=0 -> disabled (fixed % distance as-is).

    Returns:
        Adjusted SL distance (in price units). Caller computes SL price as entry - dist.
    """
    if atr_sl_floor_mult <= 0 or atr <= 0:
        return pct_sl_distance
    atr_floor = atr_sl_floor_mult * atr
    return max(pct_sl_distance, atr_floor)
