# ============================================================
# FOCUS Coin Selector — Triple-Confirmation
# ------------------------------------------------------------
# Source 1: Volume + Momentum ranking (topn_selector reuse)
# Source 2: Multi-TF AI consensus (H1/H4/D1)
# Source 3: Market Structure + BTC correlation + Volume spike
#
# Only coins passing ALL 3 sources are selected.
# 0 candidates = "not entering is also a strategy"
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def select_focus_coin(
    system: Any,
    client: Any,
    *,
    direction_mode: str = "both",
    primary_tf: str = "240",
    top_n: int = 10,
) -> Optional[Dict]:
    """Run triple-confirmation scan and return best coin.

    Returns:
        {"market": "BTCUSDT", "direction": "LONG", "zones": [...], "primary_sig": {...}}
        or None if no candidate passes.
    """
    # ── Source 1: Volume + Momentum top-N ───────────────────
    candidates = _source1_volume_momentum(client, top_n=top_n)
    if not candidates:
        logger.info("[FOCUS_SELECT] Source 1: no candidates from volume scan")
        return None

    # ── Source 2: Multi-TF GreenPen analysis ────────────────
    confirmed = []
    for market in candidates:
        try:
            analysis = _source2_greenpen_analysis(client, market, primary_tf, direction_mode)
            if analysis:
                confirmed.append(analysis)
        except Exception as exc:
            logger.warning("[FOCUS_SELECT] Source 2 failed for %s: %s", market, exc)

    if not confirmed:
        logger.info("[FOCUS_SELECT] Source 2: no candidates passed GreenPen analysis")
        return None

    # ── Source 3: Structure + External signals ──────────────
    final = []
    for c in confirmed:
        try:
            if _source3_structural_filter(client, c, system):
                final.append(c)
        except Exception as exc:
            logger.warning("[FOCUS_SELECT] Source 3 failed for %s: %s", c.get("market"), exc)

    if not final:
        logger.info("[FOCUS_SELECT] Source 3: no candidates passed structural filter")
        return None

    # Return highest confidence candidate
    final.sort(key=lambda x: -x.get("confidence", 0))
    best = final[0]
    logger.info("[FOCUS_SELECT] Selected: %s %s (conf=%.2f)",
                best.get("market"), best.get("direction"), best.get("confidence", 0))
    return best


def _source1_volume_momentum(client: Any, top_n: int = 10) -> List[str]:
    """Fetch top-N linear perp symbols by 24h turnover."""
    try:
        # ★ [2026-06-23] Exchange abstraction — go through client instead of direct bybit_get (respect client param).
        tickers = client.get_market_tickers()

        scored = []
        for t in tickers:
            if not isinstance(t, dict):
                continue
            symbol = str(t.get("symbol", ""))
            if not symbol.endswith("USDT"):
                continue
            turnover = float(t.get("turnover24h", 0) or 0)
            change_pct = float(t.get("price24hPcnt", 0) or 0)
            last_price = float(t.get("lastPrice", 0) or 0)
            # Minimum price: >= $1.0 (avoid qty explosion on cheap coins — excludes ADA/XPL/PEPE etc.)
            if last_price < 1.0:
                continue
            # Liquidity: 24h turnover >= $10M (avoid slippage)
            if turnover < 10_000_000:
                continue
            # Exclude extreme moves: 24h > 20%
            if abs(change_pct) > 0.20:
                continue
            # Minimum range: 24h > 0.5% (too quiet means TP won't get hit)
            if abs(change_pct) < 0.005:
                continue
            scored.append((symbol, turnover, abs(change_pct)))

        scored.sort(key=lambda x: (-x[1], -x[2]))
        return [s[0] for s in scored[:top_n]]
    except Exception as exc:
        logger.warning("[FOCUS_SELECT] Source 1 volume scan error: %s", exc)
        return []


def _source2_greenpen_analysis(
    client: Any, market: str, primary_tf: str, direction_mode: str,
) -> Optional[Dict]:
    """Run GreenPen full_analysis and check if coin qualifies."""
    from app.strategy.greenpen import full_analysis
    from app.strategy.greenpen.pa_detector import OHLCV

    # Fetch primary_tf(H1) candles
    try:
        raw = client.get_kline(market, interval=primary_tf, limit=30)
    except Exception as exc:
        logger.warning("[FOCUS_SELECT] kline fetch failed for %s: %s", market, exc)
        return None

    if len(raw) < 15:
        return None

    candles = []
    for r in raw:
        try:
            candles.append(OHLCV(
                open=float(r[1]), high=float(r[2]),
                low=float(r[3]), close=float(r[4]),
                volume=float(r[5]) if len(r) > 5 else 0,
                ts=float(r[0]) / 1000 if r[0] else 0,
            ))
        except (IndexError, TypeError, ValueError):
            continue

    if len(candles) < 15:
        return None

    gp = full_analysis(candles)

    # Direction check
    trend = gp.structure.trend.value
    pa_signals = gp.pa_signals

    direction = None
    confidence = 0.0

    if pa_signals:
        best_pa = pa_signals[0]
        direction = best_pa.direction.value
        confidence = best_pa.confidence

    # Trend must support direction — fully block counter-trend entry
    if direction == "LONG" and trend == "DOWNTREND":
        logger.info("[FOCUS_SELECT] %s PA→LONG but PRIMARY DOWNTREND — BLOCKED (no counter-trend)", market)
        return None
    elif direction == "SHORT" and trend == "UPTREND":
        logger.info("[FOCUS_SELECT] %s PA→SHORT but PRIMARY UPTREND — BLOCKED (no counter-trend)", market)
        return None

    # ── 30M trend alignment — when primary(H1) lags, 30M acts as a safety net ──
    # [2026-05-15] H1 switch: as primary_tf became H1, the old "H4↔H1" comparison became a redundant H1↔H1 → step down one to primary↔30M.
    # primary↔30M conflict → confidence × 0.5 (could be a transition, so not a hard block)
    # direction↔30M conflict → confidence × 0.5
    # both overlapping → × 0.25 → naturally drops out under the confidence < 0.3 threshold
    try:
        from app.strategy.greenpen.market_structure import analyze_structure
        m30_raw = client.get_kline(market, interval="30", limit=20)
        m30_candles = [OHLCV(
            open=float(r[1]), high=float(r[2]),
            low=float(r[3]), close=float(r[4]),
        ) for r in m30_raw if len(r) >= 5]
        if len(m30_candles) >= 10:
            m30_struct = analyze_structure(m30_candles, lookback=3)
            m30_trend = m30_struct.trend.value if hasattr(m30_struct.trend, 'value') else str(m30_struct.trend)

            # primary↔30M conflict → halve confidence
            if (trend == "UPTREND" and m30_trend == "DOWNTREND") or \
               (trend == "DOWNTREND" and m30_trend == "UPTREND"):
                confidence *= 0.5
                logger.info("[FOCUS_SELECT] %s PRI=%s vs 30M=%s conflict — confidence *= 0.5 → %.2f",
                            market, trend, m30_trend, confidence)

            # direction↔30M conflict → halve confidence
            if (direction == "LONG" and m30_trend == "DOWNTREND") or \
               (direction == "SHORT" and m30_trend == "UPTREND"):
                confidence *= 0.5
                logger.info("[FOCUS_SELECT] %s %s vs 30M=%s — confidence *= 0.5 → %.2f",
                            market, direction, m30_trend, confidence)
    except Exception as exc:
        logger.debug("[FOCUS_SELECT] 30M check failed for %s: %s — proceed with PRIMARY only", market, exc)

    # Direction mode filter
    if direction_mode == "long_only" and direction == "SHORT":
        return None
    if direction_mode == "short_only" and direction == "LONG":
        return None

    if not direction or confidence < 0.3:
        return None

    # Serialize zones for storage (price_low/price_high — matches Zone dataclass & HARPOON)
    zones_serialized = [
        {"type": z.type.value, "price_low": z.price_low, "price_high": z.price_high, "strength": z.strength}
        for z in gp.zones
    ]

    return {
        "market": market,
        "direction": direction,
        "confidence": confidence,
        "trend": trend,
        "atr": gp.atr,
        "price": candles[-1].close if candles else 0,
        "zones": zones_serialized,
        "pa_pattern": pa_signals[0].pattern.value if pa_signals else None,
        "primary_sig": {
            "pattern": pa_signals[0].pattern.value if pa_signals else None,
            "direction": direction,
            "confidence": confidence,
            "atr": gp.atr,
        } if pa_signals else None,
    }


def _source3_structural_filter(client: Any, candidate: Dict, system: Any) -> bool:
    """Structural/external signal filter.

    - Volume spike (24h > 1.5x average) ← already filtered by Source 1
    - Trend structure clear (confidence > 0.4)
    - Not in sideways with no breakout
    """
    conf = candidate.get("confidence", 0)
    trend = candidate.get("trend", "SIDEWAYS")

    # Sideways without PA is weak
    if trend == "SIDEWAYS" and conf < 0.5:
        return False

    # Minimum confidence threshold
    if conf < 0.4:
        return False

    return True
