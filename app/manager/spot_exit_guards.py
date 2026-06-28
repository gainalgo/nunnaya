# ============================================================
# Upbit FOCUS Exit Guards — exit guard helpers (pure functions)
# ------------------------------------------------------------
# Momentum scoring for be_stall intelligent, etc. No I/O, no state → 100% unit-testable.
#   (DESIGN_upbit_be_stall_intelligent_20260617.md §2.2)
# ============================================================
from __future__ import annotations

from typing import List, Tuple


def score_momentum_long(
    closes5: List[float],
    *,
    rsi_strong: float = 55.0,
    rsi_weak: float = 45.0,
) -> Tuple[int, int, str]:
    """LONG 5m momentum → (for_score, against_score, detail). Each 0~3 (MACD/RSI/BB).

    Same formula as Bybit be_stall_intelligent (focus_manager.py:4693~4708), LONG branch.
    for  = MACD hist>0 & rising / RSI≥strong / close≥BB mid
    against = MACD hist<0 / RSI≤weak / close<BB mid
    Insufficient data / calc failure → (0,0,"insufficient") (caller treats as neutral).
    """
    if not closes5 or len(closes5) < 26:
        return 0, 0, "insufficient"
    try:
        from app.strategy import indicators
        rsi_v = indicators.rsi(closes5, length=14)
        hist_now, hist_prev = indicators.macd_hist_pair(closes5)
        bb = indicators.bollinger_bands(closes5, 20, 2.0)
        bb_mid = float((bb or {}).get("mid", 0.0) or 0.0)
        px = float(closes5[-1])

        macd_for = (hist_now is not None and hist_now > 0 and hist_now >= hist_prev)
        macd_against = (hist_now is not None and hist_now < 0)
        rsi_for = (rsi_v is not None and rsi_v >= rsi_strong)
        rsi_against = (rsi_v is not None and rsi_v <= rsi_weak)
        bb_for = (bb_mid > 0 and px >= bb_mid)
        bb_against = (bb_mid > 0 and px < bb_mid)

        for_s = int(macd_for) + int(rsi_for) + int(bb_for)
        against_s = int(macd_against) + int(rsi_against) + int(bb_against)
        _h = f"{hist_now:.4f}" if hist_now is not None else "na"
        _r = f"{rsi_v:.0f}" if rsi_v is not None else "na"
        return for_s, against_s, f"macd={_h},rsi={_r},bb={'≥mid' if bb_for else '<mid'}"
    except Exception:
        return 0, 0, "error"
