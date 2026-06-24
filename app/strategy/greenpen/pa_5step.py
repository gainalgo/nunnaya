# ============================================================
# PA 5 STEP Completeness Scorer
# ------------------------------------------------------------
# [Phase 5 — 2026-05-17] owner's vision: "PA = the trend body. Other
# trend indicators (MTF/rate-of-change/ADX) = indirect validation of PA."
#
# PDF source: XAUUSD 88 (ครูลูกแก้ว green-pen system) p84-91 EP.6
#  "M5 million break" — 5-step entry-location validation
#
# ★ TF policy (2026-05-17 corrected by owner):
#   candle input = config.entry_tf (default M5) — entry-candle pattern check.
#   Does NOT use primary_tf(H1) candles. The wrapper auto-fetches + converts entry_tf.
#   zones stay as primary_tf-based S/R (the larger flow).
#
# 5 steps + OVL bonus = max 12 points:
#   STEP 1  big candle + strong volume (body 1.5×, vol 1.5×)   +2
#   STEP 2  next small candle (body 0.6×, vol decreasing)      +2
#   STEP 3  ZONE REJECT (close to zone + wick)                 +2
#   STEP 4  color flip (previous ≠ entry color)                +2
#   STEP 5  RETEST (zone re-rejection after break)             +2
#   OVL     second break location (first is a trap, second real) +2
#
# owner's adage (PDF):
#   "Don't rush the first — the second always goes farther"
# ============================================================
from __future__ import annotations

from typing import List, Optional, Tuple


def _normalize_candles(candles: list) -> List[dict]:
    """OHLCV object / [o,h,l,c,v] tuple / dict → uniform dict list.

    focus_manager's candles_primary arrives in OHLCV-like (c[0]=open) form,
    identical to the _compute_pa_weight pattern.
    """
    out: List[dict] = []
    for c in candles:
        try:
            if hasattr(c, 'open') and hasattr(c, 'close'):
                out.append({
                    'o': float(c.open),
                    'h': float(c.high),
                    'l': float(c.low),
                    'c': float(c.close),
                    'v': float(getattr(c, 'volume', 0.0) or 0.0),
                })
            elif isinstance(c, dict):
                out.append({
                    'o': float(c.get('open', c.get('o', 0))),
                    'h': float(c.get('high', c.get('h', 0))),
                    'l': float(c.get('low', c.get('l', 0))),
                    'c': float(c.get('close', c.get('c', 0))),
                    'v': float(c.get('volume', c.get('v', 0)) or 0),
                })
            elif isinstance(c, (list, tuple)) and len(c) >= 4:
                v = float(c[4]) if len(c) >= 5 else 0.0
                out.append({
                    'o': float(c[0]),
                    'h': float(c[1]),
                    'l': float(c[2]),
                    'c': float(c[3]),
                    'v': v,
                })
        except (ValueError, TypeError):
            continue
    return out


def _body(k: dict) -> float:
    return abs(k['c'] - k['o'])


def _is_green(k: dict) -> bool:
    return k['c'] > k['o']


def _is_red(k: dict) -> bool:
    return k['c'] < k['o']


def _atr_est(candles: List[dict], n: int = 14) -> float:
    """ATR approximation — average of (H - L) over the last n candles."""
    if not candles:
        return 0.0
    sample = candles[-n:] if len(candles) >= n else candles
    hl = [c['h'] - c['l'] for c in sample if c['h'] > c['l']]
    if not hl:
        return float(candles[-1]['c']) * 0.01 if candles[-1]['c'] > 0 else 0.0
    return sum(hl) / len(hl)


# ── STEP 1: big candle + strong volume ──────────────────────

def step1_big_candle(candles: List[dict], body_mult: float = 1.5,
                      vol_mult: float = 1.5) -> Tuple[bool, int, float, float]:
    """Whether the biggest of the last 2~3 candles satisfies
    body 1.5× + volume 1.5× vs the baseline (previous 5 candles) average.

    Returns: (pass, big_idx, big_body, big_vol)
      big_idx: 0 if not passed, else the index (-3 or -2)
    """
    if len(candles) < 8:
        return False, 0, 0.0, 0.0

    baseline = candles[-8:-3]
    avg_body = sum(_body(c) for c in baseline) / len(baseline)
    avg_vol = sum(c['v'] for c in baseline) / len(baseline)

    if avg_body <= 0:
        return False, 0, 0.0, 0.0

    best_idx = 0
    best_body = 0.0
    best_vol = 0.0
    for idx in (-3, -2):
        c = candles[idx]
        b = _body(c)
        if b > best_body:
            best_body = b
            best_vol = c['v']
            best_idx = idx

    body_ok = best_body >= avg_body * body_mult
    if avg_vol <= 0:
        vol_ok = True  # data without volume = check exempted
    else:
        vol_ok = best_vol >= avg_vol * vol_mult

    if body_ok and vol_ok:
        return True, best_idx, best_body, best_vol
    return False, 0, best_body, best_vol


# ── STEP 2: next small candle (weak) ────────────────────────

def step2_small_next(candles: List[dict], big_idx: int, big_body: float,
                      big_vol: float, body_ratio: float = 0.6) -> bool:
    """The candle *after* the STEP 1 big candle has body 0.6× or less + decreasing volume.
    Always False if big_idx == 0 (STEP 1 fail).
    """
    if big_idx == 0 or big_body <= 0:
        return False
    next_idx = big_idx + 1
    if next_idx > -1 or abs(next_idx) > len(candles):
        return False
    c = candles[next_idx]
    next_body = _body(c)
    body_ok = next_body < big_body * body_ratio
    if big_vol <= 0:
        vol_ok = True
    else:
        vol_ok = c['v'] < big_vol
    return body_ok and vol_ok


# ── STEP 3: ZONE REJECT ──────────────────────────────────────

def step3_zone_reject(candles: List[dict], direction: str,
                       zones: Optional[Tuple[float, float]],
                       atr_proximity: float = 0.5) -> bool:
    """Recent candle [-2] or [-1] rejects with a wick within 0.5×ATR of the zone (support/resistance).

    LONG : low near support + lower_wick > body (rejection from below)
    SHORT: high near resistance + upper_wick > body (rejection from above)
    """
    if not zones or len(zones) < 2 or len(candles) < 4:
        return False
    support, resistance = float(zones[0]), float(zones[1])
    if support <= 0 or resistance <= 0:
        return False

    atr = _atr_est(candles)
    if atr <= 0:
        return False
    dir_up = direction.upper()

    for idx in (-2, -1):
        if abs(idx) > len(candles):
            continue
        c = candles[idx]
        body = _body(c)
        if dir_up == "LONG":
            dist = abs(c['l'] - support)
            if dist <= atr * atr_proximity:
                lower_wick = min(c['o'], c['c']) - c['l']
                if lower_wick > body and lower_wick > 0:
                    return True
        elif dir_up == "SHORT":
            dist = abs(resistance - c['h'])
            if dist <= atr * atr_proximity:
                upper_wick = c['h'] - max(c['o'], c['c'])
                if upper_wick > body and upper_wick > 0:
                    return True
    return False


# ── STEP 4: color flip ───────────────────────────────────────

def step4_color_flip(candles: List[dict], direction: str) -> bool:
    """Average color of the last 2~3 candles ≠ the latest candle's entry color.

    LONG  entry color = GREEN — previous mostly RED → latest GREEN
    SHORT entry color = RED   — previous mostly GREEN → latest RED
    """
    if len(candles) < 4:
        return False
    dir_up = direction.upper()
    last = candles[-1]

    if dir_up == "LONG":
        if not _is_green(last):
            return False
        prev_window = candles[-4:-1]
        red_count = sum(1 for c in prev_window if _is_red(c))
        return red_count >= 2
    elif dir_up == "SHORT":
        if not _is_red(last):
            return False
        prev_window = candles[-4:-1]
        green_count = sum(1 for c in prev_window if _is_green(c))
        return green_count >= 2
    return False


# ── STEP 5: RETEST (zone re-rejection after break) ──────────

def step5_retest(candles: List[dict], direction: str,
                  zones: Optional[Tuple[float, float]],
                  lookback: int = 10, atr_proximity: float = 0.5) -> bool:
    """Recognize a break + retest sequence within the last lookback candles.

    LONG : close above support → downward retest (low near support) → rejection (close above)
    SHORT: close below resistance → upward retest (high near resistance) → rejection (close below)
    """
    if not zones or len(zones) < 2 or len(candles) < 5:
        return False
    support, resistance = float(zones[0]), float(zones[1])
    if support <= 0 or resistance <= 0:
        return False
    atr = _atr_est(candles)
    if atr <= 0:
        return False
    dir_up = direction.upper()

    window = candles[-lookback:] if len(candles) >= lookback else candles
    if len(window) < 4:
        return False

    if dir_up == "LONG":
        for i in range(len(window) - 2):
            if window[i]['c'] <= support:
                continue
            for j in range(i + 1, len(window)):
                cj = window[j]
                if abs(cj['l'] - support) <= atr * atr_proximity:
                    # rejection: lower_wick > body OR (close>=open AND lower_wick > 0)
                    body = abs(cj['c'] - cj['o'])
                    lower_wick = min(cj['o'], cj['c']) - cj['l']
                    if cj['c'] > support and (lower_wick > body or (cj['c'] >= cj['o'] and lower_wick > 0)):
                        return True
        return False
    elif dir_up == "SHORT":
        for i in range(len(window) - 2):
            if window[i]['c'] >= resistance:
                continue
            for j in range(i + 1, len(window)):
                cj = window[j]
                if abs(resistance - cj['h']) <= atr * atr_proximity:
                    body = abs(cj['c'] - cj['o'])
                    upper_wick = cj['h'] - max(cj['o'], cj['c'])
                    if cj['c'] < resistance and (upper_wick > body or (cj['c'] <= cj['o'] and upper_wick > 0)):
                        return True
        return False
    return False


# ── OVL bonus: second break location ─────────────────────────

def ovl_bonus(candles: List[dict], direction: str,
              zones: Optional[Tuple[float, float]],
              lookback: int = 12) -> bool:
    """First break → retest → second break (farther). owner's adage:
    "Don't rush the first — the second always goes farther"

    LONG : close above support twice + second higher than first
    SHORT: close below resistance twice + second lower than first
    """
    if not zones or len(zones) < 2 or len(candles) < 6:
        return False
    support, resistance = float(zones[0]), float(zones[1])
    if support <= 0 or resistance <= 0:
        return False
    dir_up = direction.upper()
    window = candles[-lookback:] if len(candles) >= lookback else candles

    if dir_up == "LONG":
        # break = actual breakout candle (close > support AND open/low below zone)
        breaks = [c['c'] for c in window
                  if c['c'] > support and (c['o'] <= support or c['l'] <= support)]
        if len(breaks) < 2:
            return False
        first_break = breaks[0]
        last_break = breaks[-1]
        return last_break > first_break * 1.001
    elif dir_up == "SHORT":
        breaks = [c['c'] for c in window
                  if c['c'] < resistance and (c['o'] >= resistance or c['h'] >= resistance)]
        if len(breaks) < 2:
            return False
        first_break = breaks[0]
        last_break = breaks[-1]
        return last_break < first_break * 0.999
    return False


# ── Main wrapper ─────────────────────────────────────────────

def _pa_5step_score_impl(direction: str, candles_primary: list,
                          zones: Optional[Tuple[float, float]] = None) -> Tuple[int, str]:
    """5-STEP completeness score + label — the pure implementation behind focus_manager's self._compute_pa_5step_score.

    Returns: (score 0~12, label)
      score: int 0~12
      label: "S1+/S2+/S3-/S4+/S5+/OVL- → 8"  (- is display-only, scores 0)
    """
    dir_up = (direction or "").upper()
    if dir_up not in ("LONG", "SHORT"):
        return (0, "no-dir")

    candles = _normalize_candles(candles_primary)
    if len(candles) < 8:
        return (0, f"short({len(candles)})")

    s1_pass, big_idx, big_body, big_vol = step1_big_candle(candles)
    s1_score = 2 if s1_pass else 0

    s2_pass = step2_small_next(candles, big_idx, big_body, big_vol) if s1_pass else False
    s2_score = 2 if s2_pass else 0

    s3_pass = step3_zone_reject(candles, dir_up, zones)
    s3_score = 2 if s3_pass else 0

    s4_pass = step4_color_flip(candles, dir_up)
    s4_score = 2 if s4_pass else 0

    s5_pass = step5_retest(candles, dir_up, zones)
    s5_score = 2 if s5_pass else 0

    # OVL bonus — added only when both STEP 1+5 pass (prevents simple break accumulation)
    ovl_pass = ovl_bonus(candles, dir_up, zones) if (s1_pass and s5_pass) else False
    ovl_score = 2 if ovl_pass else 0

    total = s1_score + s2_score + s3_score + s4_score + s5_score + ovl_score
    total = max(0, min(12, total))

    parts = [
        f"S1{'+' if s1_pass else '-'}",
        f"S2{'+' if s2_pass else '-'}",
        f"S3{'+' if s3_pass else '-'}",
        f"S4{'+' if s4_pass else '-'}",
        f"S5{'+' if s5_pass else '-'}",
        f"OVL{'+' if ovl_pass else '-'}",
    ]
    label = "/".join(parts) + f" → {total}"
    return (total, label)
