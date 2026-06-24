# ============================================================
# Upbit FOCUS guard_score — entry conviction score (G1: ADX + trend confidence)
# ------------------------------------------------------------
# Upbit-native port of Bybit guard_score (focus_manager._compute_guard_score_modifiers).
# ★ "score must match the chart" (feedback_score_must_match_chart) — the better the setup, the higher the +.
# ★ 65-80 sweet / 80+ lagging asymmetry (journal analysis) → no blind ADX floor, balanced score + total_cap(G4).
#
# Stages (DESIGN_spot_guard_score_port_20260617.md):
#   G1(here) — ADX strong/weak + trend conf high/low. Display/observation only (threshold=0 means no gate).
#   G2~ — PA completion/Frame/Anchor/BTC alignment/Vol/RSI. G4 — total_cap + threshold gate.
#
# Pure function — no I/O or state. Returns (total, breakdown list).
# ============================================================
from __future__ import annotations

from typing import List, Tuple

# ── G1 component constants (Bybit guard_score defaults — focus_manager.py:1327~) ──
_ADX_STRONG_THR = 30.0    # ADX ≥ 30 = strong trend
_ADX_WEAK_THR = 20.0      # ADX < 20 = weak trend (ranging)
_ADX_STRONG_PTS = 10.0
_ADX_WEAK_PTS = -5.0
_TREND_HIGH_CONF = 0.75   # trend confidence ≥ 0.75 = clear
_TREND_LOW_CONF = 0.50    # < 0.50 = ambiguous
_TREND_HIGH_PTS = 10.0
_TREND_LOW_PTS = -5.0
# ── G2 components (PA completion ⭐ + Frame alignment, same weights as Bybit guard_score) ──
_PA_OK_PTS = 30.0         # aligned PA pattern completion (⭐ owner's key point — good setup)
_PA_NONE_PTS = -10.0      # no PA or opposite direction
_PA_MIN_CONF = 0.5
_FRAME_ALIGNED_PTS = 15.0  # trend direction matches (UPTREND + LONG)
_FRAME_NEUTRAL_PTS = 5.0   # SIDEWAYS (neutral setup)
_FRAME_OPPOSITE_PTS = -20.0  # DOWNTREND (counter-trend — usually blocked upstream, defensive)
# ── G3 components (BTC alignment + Anchor pullback proximity) ──
_BTC_ALIGNED_PTS = 15.0    # BTC UP + LONG = tailwind
_BTC_OPPOSITE_PTS = -15.0  # BTC DOWN = headwind
_ANCHOR_PTS = 20.0         # pullback — close to nearest SUPPORT (cycle start, good entry)
_ANCHOR_PROX_ATR = 1.0     # distance to SUPPORT ≤ 1×ATR = close
# ── G5 components (Vol confirmation + RSI oversold bounce — minor bonus) ──
_VOL_BIG_PTS = 10.0        # volume large vs average + up bar = buying confirmation
_VOL_MULT = 2.0
_RSI_PTS = 10.0            # 5/primary RSI oversold (<30) + upturn = bounce start (LONG)
_RSI_OVERSOLD = 30.0

# ── 672 canonical guard_score weights (same names/defaults as futures FocusConfig) ──
#   Ch2 (2026-06-18): old hardcoded constants config-driven via SpotGazuaConfig 672 fields.
#   Defaults = same as constants above (=futures 672 defaults) → no behavior change if cfg not injected/changed. Reflected instantly on UI adjustment.
_DEFAULT_GS_WEIGHTS = {
    "guard_score_adx_strong": _ADX_STRONG_PTS,
    "guard_score_adx_weak": _ADX_WEAK_PTS,
    "guard_score_trend_high_conf": _TREND_HIGH_PTS,
    "guard_score_trend_low_conf": _TREND_LOW_PTS,
    "guard_score_pa_completion_ok": _PA_OK_PTS,
    "guard_score_pa_completion_none": _PA_NONE_PTS,
    "guard_score_frame_aligned": _FRAME_ALIGNED_PTS,
    "guard_score_frame_neutral": _FRAME_NEUTRAL_PTS,
    "guard_score_frame_opposite": _FRAME_OPPOSITE_PTS,
    "guard_score_btc_aligned": _BTC_ALIGNED_PTS,
    "guard_score_btc_opposite": _BTC_OPPOSITE_PTS,
    "guard_score_anchor_close": _ANCHOR_PTS,
    "guard_score_vol_big_align": _VOL_BIG_PTS,
    "guard_score_rsi_extreme": _RSI_PTS,
}


def gs_weights_from_config(cfg) -> dict:
    """Extract guard_score_* weights from SpotGazuaConfig(672). cfg None/field missing → default constants (=futures 672 defaults).
    If values equal the futures 672 defaults, no behavior change; reflected instantly when the owner adjusts via UI."""
    if cfg is None:
        return dict(_DEFAULT_GS_WEIGHTS)
    return {k: float(getattr(cfg, k, v)) for k, v in _DEFAULT_GS_WEIGHTS.items()}


def compute_guard_score(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    trend_conf: float,
    *,
    trend: str = "",
    pa_direction: str = "",
    pa_confidence: float = 0.0,
    btc_direction: str = "",
    price: float = 0.0,
    zones: List[dict] = None,
    atr: float = 0.0,
    volumes: List[float] = None,
    total_cap: float = 0.0,
    weights: dict = None,
) -> Tuple[float, List[str]]:
    """guard_score = G1(ADX+trend conf) + G2(PA completion ⭐ + Frame) + G3(BTC alignment + Anchor) + G5(Vol + RSI). (total, breakdown).

    ★ Not a floor — additive score (good-setup conviction). If total_cap>0, clamp to ±cap (suppress 80+, prepares G4).
    On insufficient data / calculation failure, skip only that component (0 points, fail-open).

    ★ SIDEWAYS-ADX correction (2026-06-17 owner): if structure is SIDEWAYS, *waive* the ADX strong-trend bonus.
      Corrects the case where ADX reads ≥30 from swing residue in a ranging consensus zone, making the score diverge from the chart (ranging).
      (Directionless ADX is not a real trend. Keep the weak-trend penalty — ranging should be docked.)

    Args (G2): pa_direction/pa_confidence = GreenPen top-priority PA signal (spot long_only → only LONG alignment gets +).
    """
    # ── 672 config-drive: when weights injected, override those weights (none injected = default constants). Local shadowing → body unchanged ──
    _W = _DEFAULT_GS_WEIGHTS if not weights else {**_DEFAULT_GS_WEIGHTS, **weights}
    _ADX_STRONG_PTS = _W["guard_score_adx_strong"]
    _ADX_WEAK_PTS = _W["guard_score_adx_weak"]
    _TREND_HIGH_PTS = _W["guard_score_trend_high_conf"]
    _TREND_LOW_PTS = _W["guard_score_trend_low_conf"]
    _PA_OK_PTS = _W["guard_score_pa_completion_ok"]
    _PA_NONE_PTS = _W["guard_score_pa_completion_none"]
    _FRAME_ALIGNED_PTS = _W["guard_score_frame_aligned"]
    _FRAME_NEUTRAL_PTS = _W["guard_score_frame_neutral"]
    _FRAME_OPPOSITE_PTS = _W["guard_score_frame_opposite"]
    _BTC_ALIGNED_PTS = _W["guard_score_btc_aligned"]
    _BTC_OPPOSITE_PTS = _W["guard_score_btc_opposite"]
    _ANCHOR_PTS = _W["guard_score_anchor_close"]
    _VOL_BIG_PTS = _W["guard_score_vol_big_align"]
    _RSI_PTS = _W["guard_score_rsi_extreme"]
    total = 0.0
    bd: List[str] = []
    _is_sideways = str(trend or "").upper() == "SIDEWAYS"

    # ── ADX (strong trend +/weak trend -) ──
    try:
        from app.strategy import indicators
        a = indicators.adx(highs, lows, closes)
        if a:
            adx = float(a.get("adx", 0) or 0)
            if adx >= _ADX_STRONG_THR:
                if _is_sideways:
                    bd.append(f"ADX·0({adx:.0f}, 횡보=가점면제)")   # structure ranging → waive strong-trend bonus
                else:
                    total += _ADX_STRONG_PTS
                    bd.append(f"ADX+{_ADX_STRONG_PTS:.0f}({adx:.0f})")
            elif adx < _ADX_WEAK_THR:
                total += _ADX_WEAK_PTS
                bd.append(f"ADX{_ADX_WEAK_PTS:.0f}({adx:.0f})")
            else:
                bd.append(f"ADX·0({adx:.0f})")
    except Exception:
        pass

    # ── trend confidence (clear +/ambiguous -) ──
    tc = float(trend_conf or 0)
    if tc >= _TREND_HIGH_CONF:
        total += _TREND_HIGH_PTS
        bd.append(f"Trend+{_TREND_HIGH_PTS:.0f}({tc:.2f})")
    elif tc < _TREND_LOW_CONF:
        total += _TREND_LOW_PTS
        bd.append(f"Trend{_TREND_LOW_PTS:.0f}({tc:.2f})")
    else:
        bd.append(f"Trend·0({tc:.2f})")

    # ── PA completion (G2 ⭐) — aligned (LONG) PA pattern = good setup ──
    pad = str(pa_direction or "").upper()
    if pad == "LONG" and float(pa_confidence or 0) >= _PA_MIN_CONF:
        total += _PA_OK_PTS
        bd.append(f"PA+{_PA_OK_PTS:.0f}({float(pa_confidence):.2f})")
    elif pad in ("", "SHORT"):   # no PA or opposite direction (spot long_only)
        total += _PA_NONE_PTS
        bd.append(f"PA{_PA_NONE_PTS:.0f}")
    # (LONG but low conf → 0, neutral)

    # ── Frame alignment (G2) — trend direction matches ──
    tu = str(trend or "").upper()
    if tu == "UPTREND":
        total += _FRAME_ALIGNED_PTS
        bd.append(f"Frame+{_FRAME_ALIGNED_PTS:.0f}(aligned)")
    elif tu == "SIDEWAYS":
        total += _FRAME_NEUTRAL_PTS
        bd.append(f"Frame+{_FRAME_NEUTRAL_PTS:.0f}(neutral)")
    elif tu == "DOWNTREND":
        total += _FRAME_OPPOSITE_PTS
        bd.append(f"Frame{_FRAME_OPPOSITE_PTS:.0f}(counter)")

    # ── BTC alignment (G3) — BTC tailwind/headwind (spot long_only) ──
    bdir = str(btc_direction or "").upper()
    if bdir == "UP":
        total += _BTC_ALIGNED_PTS
        bd.append(f"BTC+{_BTC_ALIGNED_PTS:.0f}(tailwind)")
    elif bdir == "DOWN":
        total += _BTC_OPPOSITE_PTS
        bd.append(f"BTC{_BTC_OPPOSITE_PTS:.0f}(headwind)")

    # ── Anchor pullback (G3) — close to nearest SUPPORT = cycle start (good entry) ──
    if zones and price > 0 and atr > 0:
        try:
            supports_below = [
                float(z.get("price_high", 0) or 0)
                for z in zones
                if str(z.get("type", "")).upper() == "SUPPORT"
                and float(z.get("price_high", 0) or 0) <= price
            ]
            if supports_below:
                nearest = max(supports_below)         # nearest support just below current price
                prox_atr = (price - nearest) / atr
                if prox_atr <= _ANCHOR_PROX_ATR:
                    total += _ANCHOR_PTS
                    bd.append(f"Anchor+{_ANCHOR_PTS:.0f}(support {prox_atr:.1f}ATR)")
        except Exception:
            pass

    # ── Vol confirmation (G5) — volume 2x+ average & up bar = buying confirmation ──
    if volumes and len(volumes) >= 5:
        try:
            cur_v = float(volumes[-1] or 0)
            prev_v = [float(v or 0) for v in volumes[:-1]]
            avg_v = sum(prev_v) / max(len(prev_v), 1)
            up_bar = len(closes) >= 2 and closes[-1] >= closes[-2]
            if avg_v > 0 and cur_v >= avg_v * _VOL_MULT and up_bar:
                total += _VOL_BIG_PTS
                bd.append(f"Vol+{_VOL_BIG_PTS:.0f}({cur_v / avg_v:.1f}x↑)")
        except Exception:
            pass

    # ── RSI oversold bounce (G5) — RSI<30 + upturn = bounce start (LONG upturn) ──
    try:
        from app.strategy import indicators
        _r = indicators.rsi_with_prev(closes)
        if _r:
            cur_rsi, prev_rsi = _r
            if (cur_rsi is not None and cur_rsi < _RSI_OVERSOLD
                    and (prev_rsi is None or cur_rsi > prev_rsi)):
                total += _RSI_PTS
                bd.append(f"RSI+{_RSI_PTS:.0f}({cur_rsi:.0f}<30↑)")
    except Exception:
        pass

    if total_cap > 0:
        total = max(-total_cap, min(total_cap, total))
    return round(total, 1), bd
