# ============================================================
# SL Safety — protection decision for naked positions (server-side SL unconfirmed) (pure function)
# ------------------------------------------------------------
# Prevents "price slips past a low SL unnoticed -> liquidation". Last-resort safety net for the server SL write-only gap.
#   (DIAGNOSIS_bybit_naked_sl_liquidation_20260617.md)
# Pure — no I/O, no state. 100% unit-tested.
# ============================================================
from __future__ import annotations


def naked_sl_should_cut(
    direction: str,
    price: float,
    sl: float,
    *,
    buffer_pct: float,
    in_grace: bool,
) -> bool:
    """Whether to immediately market-close a naked (server SL unconfirmed) position right now.

    - breach (true SL crossed): *always* cut regardless of grace (prevents slip-past -> liquidation, top priority).
    - near (within buffer): suppressed during the grace window right after entry (slippage noise), pre-emptive cut after grace.
    The caller only invokes this function when _tp_sl_confirmed=False (if a server SL exists, the exchange handles it).
    """
    if sl <= 0 or price <= 0:
        return False
    buf = max(0.0, buffer_pct) / 100.0
    d = (direction or "").upper()
    if d == "LONG":
        breach = price <= sl
        near = price <= sl * (1 + buf)
    else:  # SHORT
        breach = price >= sl
        near = price >= sl * (1 - buf)
    return breach or (near and not in_grace)
