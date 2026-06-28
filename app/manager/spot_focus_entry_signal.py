# ============================================================
# Upbit FOCUS Entry Signal — precise pre-entry confirmation (long_only)
# ------------------------------------------------------------
# 5-State simplification (guide §3.3/§9.1): ZONE_WAIT omitted.
#   conf >= threshold → enter immediately (fast entry)
#   conf <  threshold → enter only when M5 PA agrees with the direction
# Pure helper function — stateless.
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Tuple

logger = logging.getLogger(__name__)


def confirm_entry(
    client: Any,
    market: str,
    direction: str = "LONG",
    *,
    conf: float = 0.0,
    threshold: float = 0.85,
) -> Tuple[bool, str]:
    """Pre-entry confirmation. Returns (allow, reason)."""
    if conf >= threshold:
        return True, f"fast_entry(conf={conf:.2f}>={threshold:.2f})"

    try:
        from app.strategy.greenpen.pa_detector import OHLCV, detect_pa_patterns
        raw = client.get_kline(market, interval="5", limit=30)
        candles = [
            OHLCV(open=float(r[1]), high=float(r[2]), low=float(r[3]),
                  close=float(r[4]), volume=float(r[5]) if len(r) > 5 else 0)
            for r in raw if len(r) >= 5
        ]
        if len(candles) < 4:
            return False, f"m5_insufficient({len(candles)})"

        signals = detect_pa_patterns(candles)
        if signals and signals[0].direction.value == direction:
            best = signals[0]
            return True, f"m5_confirm({best.pattern.value} conf={best.confidence:.2f})"
        return False, "m5_no_aligned_pa"
    except Exception as exc:
        logger.debug("[UPBIT_ENTRY] confirm error %s: %s", market, exc)
        return False, f"error:{exc}"
