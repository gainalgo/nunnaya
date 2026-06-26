# ============================================================
# Spot Base Conviction — *faithful* port of the futures FOCUS _compute_conviction_score chart core
# ------------------------------------------------------------
# Owner directive / design: docs/plan_spot_base_conviction_2026-06-19.md
#   "Futures has a live base conviction + guard modifier system, but spot's base is unfinished."
#   -> Port the futures-style base conviction (0~100, sum of chart components) to a client-agnostic spot version.
#
# Source: app/manager/focus_manager.py:9124-9543 (_compute_conviction_score, ★live futures treasure = READ-ONLY)
#   + helpers _coin_baseline_stats / _fetch_primary_adx / _compute_rsi_score / _compute_pa_weight /
#     _compute_pa_5step_score / _compute_mtf_alignment_matrix / _compute_tf_change_rate /
#     _compute_sr_position_v2 / _compute_volume_pattern_score / _compute_phase3_context_bonus.
#
# Porting policy (per analysis spec):
#   port  = pure kline chart components — formulas verbatim. (ADX/MACD/RSI/BB/PA/5STEP/MTF/change-rate/SR/H4 trend/reversal/Vol/time-coin bonus)
#   skip(0 points, excluded) = futures-only infra (market_breadth top10, news, macro_compass, decouple, CFID, regime, context_engine, reentry, momentum_reversal).
#
# ★ Spot long_only — direction defaults to "LONG". All SHORT branches are preserved as mirrors (harmless) but callers use LONG.
# ★ closed-candle: after every candle fetch, *exclude the last (forming) candle* before computing (avoids forming-candle flicker).
# ★ Weights/thresholds all come from SpotGazuaConfig(672) via getattr — fall back to futures defaults when not injected.
# ★ Pure/module functions: zero self state. imports = only app.strategy.indicators / app.strategy.greenpen / client.get_kline.
#     Never call futures-only methods (_linear_last_price, day_direction, _compute_market_breadth, peer, etc.).
# ★ No wiring — this module is Phase 1 (authoring) only. Do not add calls anywhere (Phase 2 is separate).
# ============================================================
from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# piecewise linear interpolation (verbatim from the futures _compute_conviction_score embedded _pwl)
# ──────────────────────────────────────────────────────────────
def _pwl(v: float, points: list) -> float:
    if v <= points[0][0]:
        return float(points[0][1])
    if v >= points[-1][0]:
        return float(points[-1][1])
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        if x0 <= v <= x1:
            r = (v - x0) / (x1 - x0) if x1 != x0 else 0
            return float(y0 + (y1 - y0) * r)
    return float(points[-1][1])


# ──────────────────────────────────────────────────────────────
# Candle utils (get_kline = oldest-first [ts,open,high,low,close,vol,turnover])
#   * Both futures and spot get_kline are oldest-first -> raw[-1]=forming (in-progress candle).
#   ★ closed-candle policy: _closed() trims the last forming candle so only closed candles are used.
#     Reuses the spot_guard_chain._kl / _ohlcv pattern.
# ──────────────────────────────────────────────────────────────
def _kl(client: Any, market: str, interval: str, limit: int) -> List[list]:
    try:
        raw = client.get_kline(market, interval=interval, limit=limit)
        return raw or []
    except Exception as exc:
        logger.debug("[SPOT_CONV] get_kline fail %s %s: %s", market, interval, exc)
        return []


def _closed(raw: List[list]) -> List[list]:
    """Exclude the in-progress (last) candle — closed candles only. Avoids forming-candle flicker."""
    if raw and len(raw) >= 2:
        return raw[:-1]
    return raw or []


def _ohlcv(raw: List[list]):
    """raw kline -> greenpen OHLCV list (oldest-first, unchanged)."""
    from app.strategy.greenpen.pa_detector import OHLCV
    out = []
    for r in raw:
        if len(r) >= 5:
            out.append(OHLCV(open=float(r[1]), high=float(r[2]),
                             low=float(r[3]), close=float(r[4]),
                             volume=float(r[5]) if len(r) > 5 else 0.0))
    return out


def _closes_of(raw: List[list]) -> List[float]:
    return [float(r[4]) for r in raw if len(r) >= 5]


# ──────────────────────────────────────────────────────────────
# MTF kline — client-agnostic replacement for the futures _get_mtf_kline (no TTL cache = fetch every time).
#   Returns with closed-candle applied. Does not use futures infra (_mtf_kline_cache).
# ──────────────────────────────────────────────────────────────
def _mtf_kline(client: Any, market: str, interval: str, limit: int = 40) -> List[list]:
    return _closed(_kl(client, market, interval, max(limit + 1, 11)))


# ──────────────────────────────────────────────────────────────
# zones_from_candles — pure replacement for the futures _zones_for_pa / _compute_zones_for_symbol.
#   candles(closed primary) -> greenpen.full_analysis -> (support, resistance) tuple. Does not use futures self.zones.
# ──────────────────────────────────────────────────────────────
def _zones_from_candles(candles) -> Optional[Tuple[float, float]]:
    try:
        if not candles or len(candles) < 20:
            return None
        from app.strategy.greenpen import full_analysis
        analysis = full_analysis(candles)
        zones_raw = getattr(analysis, "zones", []) or []
        sup_prices, res_prices = [], []
        for z in zones_raw:
            ztype = str(getattr(z, "type", "")).upper()
            if hasattr(getattr(z, "type", None), "value"):
                ztype = str(z.type.value).upper()
            pl = float(getattr(z, "price_low", 0) or 0)
            ph = float(getattr(z, "price_high", 0) or 0)
            mid = (pl + ph) / 2 if (pl > 0 and ph > 0) else (pl or ph)
            if mid <= 0:
                continue
            if "SUPPORT" in ztype or "DEMAND" in ztype:
                sup_prices.append(mid)
            elif "RESIST" in ztype or "SUPPLY" in ztype:
                res_prices.append(mid)
        if not sup_prices or not res_prices:
            return None
        return (max(sup_prices), min(res_prices))
    except Exception as exc:
        logger.debug("[SPOT_CONV] zones_from_candles error: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────
# _coin_baseline_stats — pure port of futures focus_manager.py:8185-8283.
#   Coin's typical volatility (H4 100 candles) bb/macd/atr/adx averages -> normalization denominators.
#   ★required — normalization denominators for the ADX/MACD/BB components. When baseline is None, each component falls back to its absolute value.
#   Fetches the ADX baseline separately via client.get_kline(primary_tf). Cache is a simple dict.
# ──────────────────────────────────────────────────────────────
_BASELINE_CACHE: dict = {}  # {market: (ts, stats)}


def _coin_baseline_stats(client: Any, market: str, candles_primary, cfg: Any) -> Optional[dict]:
    import time as _time
    now = _time.time()
    cached = _BASELINE_CACHE.get(market)
    if cached and (now - cached[0]) < 3600:
        return cached[1]

    if not candles_primary or len(candles_primary) < 50:
        return None

    try:
        from app.strategy.indicators import bollinger_bands, macd_hist_pair, atr_simplified

        closes = []
        for c in candles_primary:
            if hasattr(c, 'close'):
                closes.append(float(c.close))
            elif isinstance(c, (list, tuple)) and len(c) >= 4:
                closes.append(float(c[3]))

        if len(closes) < 50:
            return None

        N = min(100, len(closes))
        recent = closes[-N:]

        # BB bandwidth% average (sliding window)
        bb_widths = []
        for i in range(20, N):
            bb = bollinger_bands(recent[max(0, i - 19):i + 1], 20, 2.0)
            if bb and bb.get("bandwidth"):
                bb_widths.append(bb["bandwidth"] * 100)
        bb_avg = sum(bb_widths) / len(bb_widths) if bb_widths else 1.0

        # MACD hist% average (every other bar = half the load)
        macd_hists = []
        for i in range(35, N, 2):
            h, _ = macd_hist_pair(recent[:i + 1])
            if recent[i] > 0:
                macd_hists.append(abs(h) / recent[i] * 100)
        macd_avg = sum(macd_hists) / len(macd_hists) if macd_hists else 0.1

        # ATR% average (atr_simplified — closes-based approximation)
        atr_pcts = []
        for i in range(14, N, 2):
            a = atr_simplified(recent[:i + 1], length=14)
            if a and recent[i] > 0:
                atr_pcts.append(a / recent[i] * 100)
        atr_avg_pct = sum(atr_pcts) / len(atr_pcts) if atr_pcts else 1.0

        # ADX average — mean of the last 50 of adx_series over primary_tf 100 candles
        adx_baseline = 25.0  # fallback (average for a stable coin)
        try:
            from app.strategy.indicators import adx_series
            raw_p = _kl(client, market, str(getattr(cfg, "primary_tf", "240")), 101)
            raw_p = _closed(raw_p)  # ★ closed-candle
            if raw_p and len(raw_p) >= 30:
                highs_p = [float(r[2]) for r in raw_p if len(r) >= 5]
                lows_p = [float(r[3]) for r in raw_p if len(r) >= 5]
                closes_p = [float(r[4]) for r in raw_p if len(r) >= 5]
                adx_vals = adx_series(highs_p, lows_p, closes_p, period=14)
                if adx_vals:
                    adx_only = [d["adx"] for d in adx_vals if d and "adx" in d]
                    if adx_only:
                        recent_adx = adx_only[-50:] if len(adx_only) >= 50 else adx_only
                        adx_baseline = sum(recent_adx) / len(recent_adx)
        except Exception as exc:
            logger.debug("[SPOT_CONV] adx baseline calc error %s: %s", market, exc)

        stats = {
            'bb_avg': bb_avg,
            'macd_avg': macd_avg,
            'adx_avg': adx_baseline,
            'atr_avg_pct': atr_avg_pct,
            'n': N,
        }
        _BASELINE_CACHE[market] = (now, stats)
        return stats
    except Exception as exc:
        logger.debug("[SPOT_CONV] coin baseline stats error %s: %s", market, exc)
        return None


# ──────────────────────────────────────────────────────────────
# _fetch_primary_adx — pure port of futures focus_manager.py:7417-7453 (cache and legacy-attr side effects removed).
#   Queries primary_tf ADX. closed-candle applied.
# ──────────────────────────────────────────────────────────────
def _fetch_primary_adx(client: Any, market: str, cfg: Any) -> Optional[dict]:
    try:
        from app.strategy.indicators import adx as _adx_fn
        raw = _kl(client, market, str(getattr(cfg, "primary_tf", "240")), 61)
        raw = _closed(raw)  # ★ closed-candle
        if not raw or len(raw) < 30:
            return None
        highs = [float(r[2]) for r in raw if len(r) >= 5]
        lows = [float(r[3]) for r in raw if len(r) >= 5]
        closes = [float(r[4]) for r in raw if len(r) >= 5]
        return _adx_fn(highs, lows, closes, period=14)
    except Exception as exc:
        logger.debug("[SPOT_CONV] ADX fetch failed for %s: %s", market, exc)
        return None


# ──────────────────────────────────────────────────────────────
# _compute_rsi_score — verbatim port of futures focus_manager.py:9013-9051.
#   LONG: RSI 30~50=+4 / 50~70=+2 / <30=+2 / ≥70=0. SHORT symmetric (mirror).
# ──────────────────────────────────────────────────────────────
def _compute_rsi_score(market: str, direction: str, candles_primary, cfg: Any) -> Tuple[int, str]:
    if not getattr(cfg, "phase4_rsi_enabled", True):
        return 0, "OFF"
    dir_up = (direction or "").upper()
    if dir_up not in ("LONG", "SHORT"):
        return 0, "no-dir"
    try:
        from app.strategy.indicators import rsi as _rsi_fn
        if not candles_primary or len(candles_primary) < 15:
            return 0, "no-candles"
        closes = []
        for c in candles_primary:
            if hasattr(c, 'close'):
                closes.append(float(c.close))
            elif isinstance(c, (list, tuple)) and len(c) >= 4:
                closes.append(float(c[3]))
        if len(closes) < 15:
            return 0, "short"
        rsi_val = _rsi_fn(closes, length=14)
        if rsi_val is None:
            return 0, "no-rsi"

        if dir_up == "LONG":
            if 30 <= rsi_val < 50:
                score = 4
            elif 50 <= rsi_val < 70:
                score = 2
            elif rsi_val < 30:
                score = 2
            else:
                score = 0  # ≥70 overbought
        else:  # SHORT (mirror, unused in LONG-only)
            if 50 <= rsi_val < 70:
                score = 4
            elif 30 <= rsi_val < 50:
                score = 2
            elif rsi_val >= 70:
                score = 2
            else:
                score = 0
        return score, f"RSI={rsi_val:.1f}+{score}"
    except Exception as exc:
        logger.debug("[SPOT_CONV] rsi score error for %s: %s", market, exc)
        return 0, "ERROR"


# ──────────────────────────────────────────────────────────────
# _compute_pa_weight — port of futures focus_manager.py:7626-7757 (caller="focus" branch only).
#   getattr of 672 pa_weight_*. candles=closed primary OHLCV. zones=_zones_from_candles.
# ──────────────────────────────────────────────────────────────
def _compute_pa_weight(candles_primary, direction: str, zones, cfg: Any) -> Tuple[int, str]:
    if not getattr(cfg, "pa_weight_enabled", False):
        return (0, "pa_off")
    if not candles_primary or len(candles_primary) < 4:
        return (0, "pa_no_candles")
    if not direction:
        return (0, "pa_no_direction")

    weights = {
        "PIN_BAR": int(getattr(cfg, "pa_weight_pin_bar", 1)),
        "ENGULFING": int(getattr(cfg, "pa_weight_engulfing", 2)),
        "STAR_V1": int(getattr(cfg, "pa_weight_star_v1", 3)),
        "STAR_V2": int(getattr(cfg, "pa_weight_star_v2", 3)),
        "SQUEEZE_BREAK": int(getattr(cfg, "pa_weight_squeeze_break", 2)),
        "BOS_BULLISH": int(getattr(cfg, "pa_weight_bos", 2)),
        "BOS_BEARISH": int(getattr(cfg, "pa_weight_bos", 2)),
    }
    zone_bonus = int(getattr(cfg, "pa_weight_zone_bonus", 1))
    proximity_atr = float(getattr(cfg, "pa_zone_proximity_atr", 0.5))
    penalty_mult = float(getattr(cfg, "pa_location_penalty_far", 0.5))

    try:
        from app.strategy.greenpen.pa_detector import (
            detect_pa_patterns, OHLCV as _PAO, Direction as PADir,
        )
        _pa_candles = []
        for c in candles_primary[-50:]:
            if hasattr(c, "open") and hasattr(c, "close"):
                _pa_candles.append(c)
            elif isinstance(c, (list, tuple)) and len(c) >= 4:
                _pa_candles.append(_PAO(float(c[0]), float(c[1]), float(c[2]), float(c[3])))
        if len(_pa_candles) < 4:
            return (0, "pa_too_short")

        signals = detect_pa_patterns(_pa_candles, zone_prices=zones)
        if not signals:
            return (0, "pa_no_signal")

        target_dir = PADir.LONG if direction.upper() == "LONG" else PADir.SHORT
        last_idx = len(_pa_candles) - 1
        recent = [s for s in signals
                  if s.direction == target_dir
                  and (s.candle_idx == -1 or s.candle_idx == last_idx or s.candle_idx >= last_idx - 1)]
        if not recent:
            return (0, "pa_dir_mismatch")

        best = 0
        best_label = "pa_none"
        for s in recent:
            pname = s.pattern.value
            w = weights.get(pname, 0)
            if w > best:
                best, best_label = w, f"{pname}({w})"

        if best <= 0:
            return (0, "pa_unknown")

        if zones is not None and len(zones) >= 2:
            support, resistance = float(zones[0]), float(zones[1])
            last_price = float(_pa_candles[-1].close)
            _hl = [c.high - c.low for c in _pa_candles[-14:]]
            atr_est = sum(_hl) / max(1, len(_hl)) if _hl else (last_price * 0.01)
            if atr_est <= 0:
                atr_est = last_price * 0.01

            if direction.upper() == "LONG":
                dist = abs(last_price - support) / atr_est
            elif direction.upper() == "SHORT":
                dist = abs(resistance - last_price) / atr_est
            else:
                dist = 999.0

            if dist <= proximity_atr:
                best += zone_bonus
                best_label += f"+zone({zone_bonus})"
            else:
                new_best = int(best * penalty_mult)
                best_label += f"x{penalty_mult:.1f}far({best}->{new_best})"
                best = new_best

        best = max(0, min(best, 10))
        return (best, best_label)
    except Exception as exc:
        logger.debug("[SPOT_CONV] _compute_pa_weight error: %s", exc)
        return (0, f"pa_err:{exc.__class__.__name__}")


# ──────────────────────────────────────────────────────────────
# _compute_pa_5step_score — port of futures focus_manager.py:9060-9095.
#   entry_tf(M5) fetch -> _pa_5step_score_impl. closed-candle.
# ──────────────────────────────────────────────────────────────
def _compute_pa_5step_score(client: Any, market: str, direction: str, zones, cfg: Any) -> Tuple[int, str]:
    try:
        from app.strategy.greenpen.pa_5step import _pa_5step_score_impl
        entry_tf = str(getattr(cfg, "entry_tf", "5"))
        raw = _mtf_kline(client, market, entry_tf, limit=30)  # ★ closed-candle applied
        if not raw or len(raw) < 8:
            return (0, f"entry_tf({entry_tf})_short:{len(raw) if raw else 0}")
        candles = []
        for k in raw:
            if isinstance(k, (list, tuple)) and len(k) >= 6:
                candles.append({
                    'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]),
                    'c': float(k[4]), 'v': float(k[5]),
                })
        if len(candles) < 8:
            return (0, f"entry_tf({entry_tf})_parse:{len(candles)}")
        return _pa_5step_score_impl(direction, candles, zones)
    except Exception as exc:
        logger.debug("[SPOT_CONV] 5step calc error for %s: %s", market, exc)
        return (0, "ERROR")


# ──────────────────────────────────────────────────────────────
# _compute_mtf_alignment_matrix — port of futures focus_manager.py:8718-8800.
#   6 TF (D1/H4/H1/M30/M15/M5) analyze_structure -> net×4. closed-candle.
# ──────────────────────────────────────────────────────────────
def _compute_mtf_alignment_matrix(client: Any, market: str, direction: str,
                                  candles_primary, cfg: Any) -> Tuple[int, str]:
    if not getattr(cfg, "phase4_mtf_matrix_enabled", True):
        return 0, "OFF"
    dir_up = (direction or "").upper()
    if dir_up not in ("LONG", "SHORT"):
        return 0, "no-dir"
    try:
        from app.strategy.greenpen.pa_detector import OHLCV as _O
        from app.strategy.greenpen.market_structure import analyze_structure as _as

        def _trend_of(raw, is_raw=True, reality_drop=0.0):
            try:
                if not raw or len(raw) < 10:
                    return "?"
                _c = []
                if is_raw:
                    for r in raw:
                        if len(r) >= 5:
                            _c.append(_O(float(r[1]), float(r[2]), float(r[3]), float(r[4])))
                else:
                    for c in raw:
                        if hasattr(c, 'open'):
                            _c.append(c)
                        elif isinstance(c, (list, tuple)) and len(c) >= 4:
                            _c.append(_O(float(c[0]), float(c[1]), float(c[2]), float(c[3])))
                if len(_c) < 10:
                    return "?"
                st = _as(_c, recent_reality_drop_pct=reality_drop)
                return st.trend.value if hasattr(st.trend, 'value') else str(st.trend)
            except Exception:
                return "?"

        _primary_interval = str(getattr(cfg, "primary_tf", "240"))
        _rd_on = getattr(cfg, "d1_reality_demote_enabled", False)
        _rd_pct = abs(float(getattr(cfg, "d1_reality_demote_drop_pct", 1.0) or 1.0)) if _rd_on else 0.0

        def _trend_for(interval: str) -> str:
            _rd = _rd_pct if interval == "D" else 0.0
            if interval == _primary_interval and candles_primary:
                return _trend_of(candles_primary, is_raw=False, reality_drop=_rd)
            return _trend_of(_mtf_kline(client, market, interval), reality_drop=_rd)

        d1_trend = _trend_for("D")
        h4_trend = _trend_for("240")
        h1_trend = _trend_for("60")
        m30_trend = _trend_for("30")
        m15_trend = _trend_for("15")
        m5_trend = _trend_for("5")

        def _is_align(t):
            return ((dir_up == "LONG" and t == "UPTREND") or (dir_up == "SHORT" and t == "DOWNTREND"))

        def _is_opposite(t):
            return ((dir_up == "LONG" and t == "DOWNTREND") or (dir_up == "SHORT" and t == "UPTREND"))

        trends = [d1_trend, h4_trend, h1_trend, m30_trend, m15_trend, m5_trend]
        align_count = sum(1 for t in trends if _is_align(t))
        opp_count = sum(1 for t in trends if _is_opposite(t))
        net = align_count - opp_count  # -6 ~ +6
        score = net * 4  # -24 ~ +24

        label = (f"D1:{d1_trend[:4]}/H4:{h4_trend[:4]}/H1:{h1_trend[:4]}/30M:{m30_trend[:4]}"
                 f"/15M:{m15_trend[:4]}/5m:{m5_trend[:4]} align={align_count}-{opp_count}={net:+d}")
        return score, label
    except Exception as exc:
        logger.debug("[SPOT_CONV] mtf alignment matrix error for %s: %s", market, exc)
        return 0, "ERROR"


# ──────────────────────────────────────────────────────────────
# _compute_tf_change_rate — port of futures focus_manager.py:8802-8914.
#   Per-TF change rate (speed + consistency). Three BEAT guards via getattr (default OFF). × tf_weight. closed-candle.
# ──────────────────────────────────────────────────────────────
def _compute_tf_change_rate(client: Any, market: str, tf_str: str, direction: str,
                            candles_primary, cfg: Any, baseline: Optional[dict] = None) -> Tuple[int, str]:
    if not getattr(cfg, "phase4_change_rate_enabled", True):
        return 0, "OFF"
    dir_up = (direction or "").upper()
    if dir_up not in ("LONG", "SHORT"):
        return 0, "no-dir"
    try:
        _tf_interval = {"D1": "D", "H4": "240", "H1": "60"}.get(tf_str, tf_str)
        if _tf_interval == str(getattr(cfg, "primary_tf", "240")) and candles_primary:
            closes = []
            for c in candles_primary:
                if hasattr(c, 'close'):
                    closes.append(float(c.close))
                elif isinstance(c, (list, tuple)) and len(c) >= 4:
                    closes.append(float(c[3]))
        else:
            raw = _mtf_kline(client, market, _tf_interval)
            if not raw or len(raw) < 10:
                return 0, f"{tf_str}:no-data"
            closes = [float(r[4]) for r in raw if len(r) >= 5]

        if len(closes) < 6:
            return 0, f"{tf_str}:short"

        N = 5
        speed_pct = (closes[-1] - closes[-N - 1]) / closes[-N - 1] * 100 if closes[-N - 1] > 0 else 0
        up_count = sum(1 for i in range(-N, 0) if closes[i] > closes[i - 1])
        down_count = sum(1 for i in range(-N, 0) if closes[i] < closes[i - 1])
        dominant_count = max(up_count, down_count)
        consistency = dominant_count / N
        dominant_dir = "UP" if up_count > down_count else ("DOWN" if down_count > up_count else "FLAT")

        # BEAT Fix A — correct divergence between direction count and actual speed sign (default OFF)
        if getattr(cfg, "cr_speed_sign_guard_enabled", False) and dominant_dir != "FLAT":
            _speed_dir = "UP" if speed_pct > 0 else ("DOWN" if speed_pct < 0 else "FLAT")
            if _speed_dir != dominant_dir:
                dominant_dir = "FLAT"

        # BEAT Fix C — larger-trend agreement guard (default OFF)
        if getattr(cfg, "cr_trend_agree_guard_enabled", False) and dominant_dir != "FLAT":
            _long_n = int(getattr(cfg, "cr_trend_agree_lookback", 20))
            if len(closes) > _long_n and closes[-_long_n - 1] > 0:
                _long_chg = (closes[-1] - closes[-_long_n - 1]) / closes[-_long_n - 1] * 100
                _long_dir = "UP" if _long_chg > 0 else ("DOWN" if _long_chg < 0 else "FLAT")
                if _long_dir != "FLAT" and _long_dir != dominant_dir:
                    dominant_dir = "FLAT"

        entry_aligned = (dir_up == "LONG" and dominant_dir == "UP") or (dir_up == "SHORT" and dominant_dir == "DOWN")
        entry_opposite = (dir_up == "LONG" and dominant_dir == "DOWN") or (dir_up == "SHORT" and dominant_dir == "UP")

        atr_ref = baseline['atr_avg_pct'] if baseline else 0.5
        speed_ratio = abs(speed_pct) / atr_ref if atr_ref > 0 else 1.0

        # BEAT Fix B — extreme blow-off spike/crash tail-end -> neutral (default OFF)
        if getattr(cfg, "cr_blowoff_extreme_guard_enabled", False) and dominant_dir != "FLAT":
            _ext_ratio = float(getattr(cfg, "cr_blowoff_extreme_ratio", 4.0))
            if speed_ratio >= _ext_ratio:
                dominant_dir = "FLAT"
                entry_aligned = False
                entry_opposite = False
        strong_accel = consistency >= 0.8 and speed_ratio >= 1.0
        weak_change = consistency < 0.6 or speed_ratio < 0.4

        if entry_aligned:
            if strong_accel:
                score = 6
            elif weak_change:
                score = 3
            else:
                score = 4
        elif entry_opposite:
            if strong_accel:
                score = -6
            elif weak_change:
                score = 0
            else:
                score = -3
        else:
            score = 0

        # Reversal signal — opposite is dominant but the last 1~2 candles flipped sharply toward the entry direction
        if entry_opposite and len(closes) >= 3:
            recent_up = closes[-1] > closes[-2] and closes[-2] > closes[-3]
            recent_down = closes[-1] < closes[-2] and closes[-2] < closes[-3]
            if (dir_up == "LONG" and recent_up) or (dir_up == "SHORT" and recent_down):
                score = 4

        _tf_w_key = {"D1": "d1_trend_weight", "H4": "h4_trend_weight", "H1": "h1_trend_weight",
                     "30": "m30_trend_weight", "15": "m15_trend_weight", "5": "m5_trend_weight"}.get(tf_str)
        _tf_w = float(getattr(cfg, _tf_w_key, 1.0)) if _tf_w_key else 1.0
        score = int(round(score * _tf_w))
        label = f"{tf_str}:{dominant_dir}({consistency:.0%},{speed_pct:+.2f}%){score:+d}"
        return score, label
    except Exception as exc:
        logger.debug("[SPOT_CONV] tf change rate error %s %s: %s", market, tf_str, exc)
        return 0, "ERROR"


# ──────────────────────────────────────────────────────────────
# _compute_sr_position_v2 — port of futures focus_manager.py:8916-8960.
#   Price↔S/R distance position score (0~8, no penalty). zones=_zones_from_candles.
# ──────────────────────────────────────────────────────────────
def _compute_sr_position_v2(market: str, direction: str, candles_primary, zones_tuple, cfg: Any) -> Tuple[int, str]:
    if not getattr(cfg, "phase4_sr_position_enabled", True):
        return 0, "OFF"
    dir_up = (direction or "").upper()
    if dir_up not in ("LONG", "SHORT"):
        return 0, "no-dir"
    try:
        if not candles_primary or len(candles_primary) == 0:
            return 0, "no-candles"
        _last = candles_primary[-1]
        if hasattr(_last, 'close'):
            current_price = float(_last.close)
        elif isinstance(_last, (list, tuple)) and len(_last) >= 4:
            current_price = float(_last[3])
        else:
            return 0, "no-price"

        if not zones_tuple:
            return 0, "no-zones"
        support_price, resistance_price = zones_tuple
        if support_price <= 0 or resistance_price <= 0 or current_price <= 0:
            return 0, "invalid"

        dist_to_support_pct = abs(current_price - support_price) / current_price * 100
        dist_to_resistance_pct = abs(resistance_price - current_price) / current_price * 100

        if dir_up == "LONG":
            if dist_to_support_pct <= 2.0:
                return 8, f"near_S({dist_to_support_pct:.1f}%)+8"
            if dist_to_support_pct <= 5.0:
                return 5, f"mid_S({dist_to_support_pct:.1f}%)+5"
            if dist_to_resistance_pct <= 5.0:
                return 0, f"near_R({dist_to_resistance_pct:.1f}%)+0"
            return 3, f"between(S:{dist_to_support_pct:.1f}/R:{dist_to_resistance_pct:.1f}%)+3"
        else:  # SHORT (mirror)
            if dist_to_resistance_pct <= 2.0:
                return 8, f"near_R({dist_to_resistance_pct:.1f}%)+8"
            if dist_to_resistance_pct <= 5.0:
                return 5, f"mid_R({dist_to_resistance_pct:.1f}%)+5"
            if dist_to_support_pct <= 5.0:
                return 0, f"near_S({dist_to_support_pct:.1f}%)+0"
            return 3, f"between(S:{dist_to_support_pct:.1f}/R:{dist_to_resistance_pct:.1f}%)+3"
    except Exception as exc:
        logger.debug("[SPOT_CONV] sr position v2 error for %s: %s", market, exc)
        return 0, "ERROR"


# ──────────────────────────────────────────────────────────────
# _compute_volume_pattern_score — port of futures focus_manager.py:8962-9011.
#   M5 last-closed candle vol/avg. Big candle & entry direction = +5. closed-candle (vols[-2]=last closed candle, last is forming).
#   ★ _mtf_kline already excludes forming -> vols[-1]=last closed candle, vols[-2]=the one before.
#     The futures original grabbed [-2] from forming-inclusive raw; here, [-1] of the closed list (the last closed candle) is equivalent.
# ──────────────────────────────────────────────────────────────
def _compute_volume_pattern_score(client: Any, market: str, direction: str, cfg: Any) -> Tuple[int, str]:
    if not getattr(cfg, "phase4_volume_pattern_enabled", True):
        return 0, "OFF"
    dir_up = (direction or "").upper()
    if dir_up not in ("LONG", "SHORT"):
        return 0, "no-dir"
    try:
        raw = _mtf_kline(client, market, "5")  # list of closed candles (forming excluded)
        if not raw or len(raw) < 10:
            return 0, "no-vol-data"
        vols = []
        opens_closes = []
        for r in raw:
            if len(r) >= 6:
                vols.append(float(r[5]))
                opens_closes.append((float(r[1]), float(r[4])))
        if len(vols) < 6:
            return 0, "short-vol"
        # closed list -> last = last closed candle (equivalent to [-2] of the futures forming-inclusive raw)
        avg_vol = sum(vols[-6:-1]) / 5  # average of the 5 closed candles before it
        current_vol = vols[-1]           # last closed candle
        if avg_vol <= 0:
            return 0, "zero-avg"
        ratio = current_vol / avg_vol

        cur_open, cur_close = opens_closes[-1]
        candle_dir = "UP" if cur_close > cur_open else ("DOWN" if cur_close < cur_open else "FLAT")
        entry_aligned = (dir_up == "LONG" and candle_dir == "UP") or (dir_up == "SHORT" and candle_dir == "DOWN")

        if ratio >= 2.0:
            if entry_aligned:
                return 5, f"big({ratio:.1f}x){candle_dir}+5"
            else:
                return 0, f"big({ratio:.1f}x){candle_dir}+0"
        if ratio >= 0.7:
            return 2, f"avg({ratio:.1f}x)+2"
        return 1, f"small({ratio:.1f}x)+1"
    except Exception as exc:
        logger.debug("[SPOT_CONV] volume pattern error for %s: %s", market, exc)
        return 0, "ERROR"


# ──────────────────────────────────────────────────────────────
# _compute_phase3_context_bonus — port of futures focus_manager.py:9097-9122.
#   KST hour-of-day EV ±4 + coin bonus +2. (Analysis 'adapt (optional)' — preserved as mirror, ON/OFF via config gate.)
# ──────────────────────────────────────────────────────────────
def _compute_phase3_context_bonus(market: str, ts: Optional[float] = None) -> int:
    import time as _time
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    if ts is None:
        ts = _time.time()

    hr = _dt.fromtimestamp(ts, tz=_tz(_td(hours=9))).hour

    bonus = 0
    if hr in {21, 22, 4, 12, 19, 1, 5, 6, 3, 18}:
        bonus += 4
    elif hr in {16, 20, 2, 13, 15}:
        bonus -= 4

    if market in {"ZBTUSDT", "HYPEUSDT", "INJUSDT", "TAOUSDT", "BTCUSDT", "ZECUSDT"}:
        bonus += 2

    return bonus


# ══════════════════════════════════════════════════════════════
# PUBLIC — compute_base_conviction
#   Sum of the futures _compute_conviction_score chart core. Ported components only (skip=excluded=0).
#   After summing, clamp(0,100). breakdown holds each component's contribution.
# ══════════════════════════════════════════════════════════════
def compute_base_conviction(client: Any, market: str, cfg: Any,
                            direction: str = "LONG",
                            btc_dir: str = "NEUTRAL") -> Tuple[float, List[str]]:
    """Spot base conviction (0~100) — mirror of the futures FOCUS chart core.

    Args:
        client: exchange client with get_kline(market, interval, limit) (futures/spot agnostic, oldest-first).
        market: symbol (e.g. "BTCUSDT" / "KRW-BTC").
        cfg:    SpotGazuaConfig (672) — weights/thresholds via getattr.
        direction: "LONG" (default) / "SHORT" (mirror, unused on the LONG-only operating line).
        btc_dir:   BTC macro direction hint ("UP"/"DOWN"/"NEUTRAL"). Currently unused by base (placeholder for the skipped breadth — interface preserved).

    Returns:
        (base[0~100] float, breakdown[list[str]]).

    skip(excluded): market_breadth(top10), news, macro_compass, decouple, regime, macro_short, CFID,
                  context_engine, reentry_penalty, momentum_reversal — futures-only infra.
    """
    score = 0.0
    bd: List[str] = []
    _dir = (direction or "LONG").upper()
    _ = btc_dir  # interface preserved (Phase2 breadth-adapt hook)

    # ── primary_tf candles (closed) — shared by many components ──
    raw_primary = _closed(_kl(client, market, str(getattr(cfg, "primary_tf", "240")), 121))
    candles_primary = _ohlcv(raw_primary)
    closes = _closes_of(raw_primary)

    # ── baseline (ADX/MACD/BB normalization denominators) ──
    baseline = _coin_baseline_stats(client, market, candles_primary, cfg)

    # ── 6) ADX strength (0~10) — normalized by coin baseline ──
    try:
        adx_data = _fetch_primary_adx(client, market, cfg)
        if adx_data:
            adx_val = adx_data["adx"]
            if baseline and baseline.get('adx_avg', 0) > 0:
                ratio = adx_val / baseline['adx_avg']
                adx_s = _pwl(ratio, [(0, 0), (0.5, 0), (0.8, 2), (1.0, 4), (1.5, 8), (2.0, 10)])
                bd.append(f"ADX {adx_val:.1f}/{baseline['adx_avg']:.1f}=r{ratio:.2f}->{adx_s:+.1f}")
            else:
                adx_s = _pwl(adx_val, [(0, 0), (15, 1), (25, 4), (40, 8), (55, 9.5), (100, 10)])
                bd.append(f"ADX {adx_val:.1f}(abs)->{adx_s:+.1f}")
            score += adx_s
    except Exception as exc:
        logger.debug("[SPOT_CONV] adx component error %s: %s", market, exc)

    # ── 9) BB bandwidth% (0~4) — normalized by coin baseline ──
    try:
        from app.strategy.indicators import bollinger_bands
        if len(closes) >= 20:
            bb = bollinger_bands(closes, 20, 2.0)
            if bb and bb.get("bandwidth"):
                bw_pct = bb["bandwidth"] * 100
                if baseline and baseline['bb_avg'] > 0:
                    ratio = bw_pct / baseline['bb_avg']
                    bb_s = _pwl(ratio, [(0, 0), (0.5, 0), (0.8, 1.5), (1.0, 2.5), (1.5, 3.5), (2.0, 4)])
                    bd.append(f"BB {bw_pct:.1f}%/{baseline['bb_avg']:.1f}%=r{ratio:.2f}->{bb_s:+.1f}")
                else:
                    bb_s = _pwl(bw_pct, [(0, 0), (2, 1), (4, 3), (6, 3.5), (10, 4)])
                    bd.append(f"BB {bw_pct:.1f}%(abs)->{bb_s:+.1f}")
                score += bb_s
    except Exception as exc:
        logger.debug("[SPOT_CONV] bb component error %s: %s", market, exc)

    # ── 7) MACD hist% (0~8) — direction-aligned, normalized by coin baseline ──
    try:
        from app.strategy.indicators import macd_hist_pair
        if len(closes) >= 35:
            hist_now, _ = macd_hist_pair(closes)
            _macd_aligned = (
                (_dir == "LONG" and hist_now > 0)
                or (_dir == "SHORT" and hist_now < 0)
                or _dir not in ("LONG", "SHORT")
            )
            if closes[-1] > 0 and _macd_aligned:
                hist_pct = abs(hist_now) / closes[-1] * 100
                if baseline and baseline['macd_avg'] > 0:
                    ratio = hist_pct / baseline['macd_avg']
                    macd_s = _pwl(ratio, [(0, 0), (0.5, 0), (0.8, 3), (1.0, 5), (2.0, 7), (3.0, 8)])
                    bd.append(f"MACD {hist_pct:.3f}%/{baseline['macd_avg']:.3f}%=r{ratio:.2f}->{macd_s:+.1f}")
                else:
                    macd_s = _pwl(hist_pct, [(0, 0), (0.05, 2), (0.2, 6), (0.4, 7), (1.0, 8)])
                    bd.append(f"MACD {hist_pct:.3f}%(abs)->{macd_s:+.1f}")
                score += macd_s
            elif closes[-1] > 0:
                bd.append(f"MACD {hist_now:+.4f}({_dir}<>)+0")
    except Exception as exc:
        logger.debug("[SPOT_CONV] macd component error %s: %s", market, exc)

    # ── H4 trend score + H4 reversal (M/W/H&S) — primary struct shared once ──
    try:
        if candles_primary and len(candles_primary) >= 10:
            from app.strategy.greenpen.market_structure import analyze_structure as _as
            struct = _as(candles_primary)
            trend_val = struct.trend.value if hasattr(struct.trend, 'value') else str(struct.trend)
            conf = struct.confidence if hasattr(struct, 'confidence') else 0
            # H4 trend score
            _h4_dir = 1 if trend_val == "UPTREND" else -1 if trend_val == "DOWNTREND" else 0
            _h4_sign = 1 if _dir == "LONG" else -1
            _h4_s = _h4_dir * _h4_sign * float(conf) * float(getattr(cfg, "h4_trend_weight", 6.0))
            score += _h4_s
            bd.append(f"H4trend {trend_val}xconf{conf:.2f}={_h4_s:+.1f}")
            # H4 reversal
            try:
                from app.strategy.greenpen.market_structure import detect_reversal as _drev
                _rev = _drev(struct)
                if _rev.pattern != "NONE":
                    _rev_sign = 1 if _rev.direction == "BULLISH" else -1
                    _dir_s = 1 if _dir == "LONG" else -1
                    _rev_base = float(getattr(cfg, "reversal_score", 10.0))
                    if not _rev.confirmed:
                        _rev_base *= 0.5
                    _rev_macro = _rev_base if (_rev_sign == _dir_s) else -_rev_base
                    score += _rev_macro
                    bd.append(f"Reversal {_rev.pattern}/{_rev.direction}{'v' if _rev.confirmed else '~'}={_rev_macro:+.0f}")
            except Exception as _rev_exc:
                logger.debug("[SPOT_CONV] detect_reversal error %s: %s", market, _rev_exc)
    except Exception as exc:
        logger.debug("[SPOT_CONV] h4/reversal component error %s: %s", market, exc)

    # ── 10) PA Pattern (0~10) ──
    try:
        _zones = _zones_from_candles(candles_primary)
        _pa_score, _pa_label = _compute_pa_weight(candles_primary, _dir, _zones, cfg)
        _pa_v2 = min(10.0, float(_pa_score)) if _pa_score > 0 else 0.0
        if _pa_v2 == 0.0 and any(p in _pa_label for p in ("PIN_BAR", "ENGULFING", "BOS_BULLISH", "BOS_BEARISH")):
            _pa_v2 = 2.0
        score += _pa_v2
        bd.append(f"PA {_pa_label}+{_pa_v2:.1f}")
    except Exception as exc:
        _zones = None
        logger.debug("[SPOT_CONV] pa weight error %s: %s", market, exc)

    # ── 10.5) 5 STEP completeness (0~12) ──
    try:
        _5s_score, _5s_label = _compute_pa_5step_score(client, market, _dir, _zones, cfg)
        if _5s_score > 0:
            score += float(_5s_score)
            bd.append(f"5STEP {_5s_label}")
    except Exception as exc:
        logger.debug("[SPOT_CONV] 5step error %s: %s", market, exc)

    # ── 1) Multi-TF alignment matrix (±18 = net6×4 ×0.75) ──
    try:
        mtf_s, mtf_label = _compute_mtf_alignment_matrix(client, market, _dir, candles_primary, cfg)
        mtf_v5 = mtf_s * 0.75
        score += mtf_v5
        bd.append(f"MTF {mtf_label}{mtf_s:+d}x.75={mtf_v5:+.1f}")
    except Exception as exc:
        logger.debug("[SPOT_CONV] mtf alignment error %s: %s", market, exc)

    # ── 2~5) Per-TF change rate (±, 6TF ×tf_weight ×0.75) ──
    for tf in ("D1", "H4", "H1", "30", "15", "5"):
        try:
            cr_s, cr_label = _compute_tf_change_rate(client, market, tf, _dir, candles_primary, cfg, baseline)
            cr_v5 = cr_s * 0.75
            score += cr_v5
            bd.append(f"CR_{tf} {cr_label}x.75={cr_v5:+.1f}")
        except Exception as exc:
            logger.debug("[SPOT_CONV] change rate %s error %s: %s", tf, market, exc)

    # ── 8) RSI (0~4) ──
    try:
        rsi_s, rsi_label = _compute_rsi_score(market, _dir, candles_primary, cfg)
        score += rsi_s
        bd.append(f"RSI {rsi_label}")
    except Exception as exc:
        logger.debug("[SPOT_CONV] rsi error %s: %s", market, exc)

    # ── 11) S/R price position v2 (0~8) ──
    try:
        sr_s, sr_label = _compute_sr_position_v2(market, _dir, candles_primary, _zones, cfg)
        score += sr_s
        bd.append(f"SR {sr_label}")
    except Exception as exc:
        logger.debug("[SPOT_CONV] sr v2 error %s: %s", market, exc)

    # ── 12) Volume pattern (0~5) ──
    try:
        vol_s, vol_label = _compute_volume_pattern_score(client, market, _dir, cfg)
        score += vol_s
        bd.append(f"VOL {vol_label}")
    except Exception as exc:
        logger.debug("[SPOT_CONV] vol pattern error %s: %s", market, exc)

    # ── 14+15) Phase 3 Play C — time ±4 + coin +2 (config gate) ──
    if getattr(cfg, "phase3_context_bonus_enabled", True):
        try:
            _ctx_bonus = _compute_phase3_context_bonus(market)
            if _ctx_bonus != 0:
                # LONG-favorable time/coin = bonus, SHORT is symmetric (mirror)
                _cb = _ctx_bonus if _dir == "LONG" else -_ctx_bonus
                score += _cb
                bd.append(f"Phase3 {_cb:+d}")
        except Exception as exc:
            logger.debug("[SPOT_CONV] phase3 ctx bonus error %s: %s", market, exc)

    # ── Normalization: clamp(0,100) after summing (futures mirror — gates/cap are a separate stage) ──
    base = max(0.0, min(100.0, float(score)))
    bd.insert(0, f"BASE={base:.1f} (raw_sum={score:.1f}, dir={_dir})")
    return base, bd
