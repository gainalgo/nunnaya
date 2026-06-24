# ============================================================
# Spot Guard Chain — faithful *wholesale* port of futures FOCUS _compute_guard_score_modifiers (superset)
# ------------------------------------------------------------
# Owner directive: "Futures entry selection is a fully validated machine — don't validate piece
#            by piece, copy it wholesale and plug it into spot." Spot was losing money via
#            SIDEWAYS junk entries.
#
# Source: app/manager/focus_manager.py:2306-2688 (_compute_guard_score_modifiers,
#       ★live futures treasure = READ-ONLY) + 13266~/7417~/14354~ ADX entry gate.
#   ─ This module is the futures 24-factor *faithful version (superset)* — OK to overlap with the 8 factors in spot_guard_score.py.
#   ─ Pure/module functions: zero self state. imports = app.strategy.greenpen / app.strategy.indicators /
#     client.get_kline only. Never call futures-only methods (_linear_last_price, day_direction, peer_brief, etc.).
#
# Porting policy (per analysis spec spot_gap / guard_score):
#   portable  = pure kline → spot client.get_kline as-is.
#   adapt     = _linear_last_price → last close / day_direction → btc_dir argument / 24h ticker → daily kline.
#   skip(0pts) = things with no spot infrastructure (Flow Reversal, Alt-BTC, 5 Peer factors, Day Box state machine) = safe 0pts.
#
# ★ spot is long_only — direction defaults to "LONG". All SHORT branches are preserved (harmless) but calls use LONG.
# ★ All weights read SpotGazuaConfig's guard_score_* (672) fields via getattr (falls back to futures defaults if not injected).
# ============================================================
from __future__ import annotations

import datetime
import logging
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── candle utils (get_kline = oldest-first [ts,open,high,low,close,vol,turnover]) ──
#   ※ both futures and spot get_kline are oldest-first (bybit_trade.py:554 · upbit_trade.py:328 both reverse-sort,
#     focus_manager.py:2778 owner-verified comment 'oldest first → raw[-1]=latest bar'). So raw[-1]=forming (in-progress bar).
#   ★ However, futures _check_pa_completion(focus_manager.py:3088)'s `reversed(raw) # oldest first` flips the already
#     oldest-first list once more into newest-first, dropping the *oldest bar* instead of forming — a latent bug (comment contradicts reality).
#     Spot follows the docstring intent (huikkang=most recent closed bar=closed[-1], forming=ordered[-1] excluded) by skipping reverse = correct.
def _kl(client: Any, market: str, interval: str, limit: int) -> List[list]:
    try:
        raw = client.get_kline(market, interval=interval, limit=limit)
        return raw or []
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] get_kline fail %s %s: %s", market, interval, exc)
        return []


def _ohlcv(raw: List[list]):
    """raw kline → greenpen OHLCV list (oldest-first as-is)."""
    from app.strategy.greenpen.pa_detector import OHLCV
    out = []
    for r in raw:
        if len(r) >= 5:
            out.append(OHLCV(open=float(r[1]), high=float(r[2]),
                             low=float(r[3]), close=float(r[4]),
                             volume=float(r[5]) if len(r) > 5 else 0.0))
    return out


def _last_price(client: Any, market: str, raw_primary: Optional[List[list]] = None) -> float:
    """adapt: replaces futures _linear_last_price — use client.last_price if present, else last close.
    Pure — does not call any futures-only method."""
    try:
        lp = getattr(client, "last_price", None)
        if callable(lp):
            v = lp(market)
            if v and float(v) > 0:
                return float(v)
    except Exception:
        pass
    try:
        if raw_primary and len(raw_primary[-1]) >= 5:
            return float(raw_primary[-1][4])
        raw = _kl(client, market, "5", 2)
        if raw and len(raw[-1]) >= 5:
            return float(raw[-1][4])
    except Exception:
        pass
    return 0.0


def _atr_from(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """inline ATR (true range) — computed directly instead of the futures original's broken indicators.atr import."""
    trs, pc = [], None
    for h, l, c in zip(highs, lows, closes):
        tr = (h - l) if pc is None else max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        pc = c
    if not trs:
        return 0.0
    return sum(trs[-period:]) / min(len(trs), period)


# ============================================================
# PA Completion — pure port of futures _check_pa_completion (focus_manager.py:3069-3134)
#   shared by ① PA(H4 OR H1) / ①-2 D1 / ③ H1 PA pulse. spot=oldest-first → no reverse.
# ============================================================
def _pa_completion(client: Any, market: str, direction: str, tf: str, cfg: Any) -> bool:
    """Sig + follow-through bar (tail) both closed = PA complete. Source focus_manager.py:3069-3134.
    On error/insufficient data returns False (fail-closed, safe)."""
    try:
        min_ratio = float(getattr(cfg, "pa_completion_huikkang_min_ratio", 1.5))
        lookback = max(2, int(getattr(cfg, "pa_completion_lookback_bars", 3)))
        sig_max = float(getattr(cfg, "pa_completion_sig_max_ratio", 1.0))
        need = lookback + 3
        raw = _kl(client, market, tf, max(need + 2, 8))
        if not raw or len(raw) < need:
            return False
        ordered = list(raw)                # ★ spot oldest-first as-is (no reverse, unlike futures)
        closed = ordered[:-1]              # exclude forming (last) → closed candles only
        if len(closed) < lookback + 2:
            return False
        huikkang = closed[-1]
        h_open, h_close = float(huikkang[1]), float(huikkang[4])
        h_body = abs(h_close - h_open)
        h_dir = "UP" if h_close > h_open else "DOWN"
        want_dir = "UP" if (direction or "").upper() == "LONG" else "DOWN"
        if h_dir != want_dir:
            return False
        prev_bodies = []
        for c in closed[-(lookback + 1):-1]:
            try:
                pb = abs(float(c[4]) - float(c[1]))
                if pb > 0:
                    prev_bodies.append(pb)
            except Exception:
                continue
        if not prev_bodies:
            return False
        avg_prev = sum(prev_bodies) / len(prev_bodies)
        if h_body < avg_prev * min_ratio:
            return False
        sig = closed[-2]
        s_open, s_close = float(sig[1]), float(sig[4])
        s_body = abs(s_close - s_open)
        s_dir = "UP" if s_close > s_open else "DOWN"
        if s_dir == h_dir:
            return False
        if s_body > h_body * sig_max:
            return False
        return True
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _pa_completion %s %s tf=%s: %s", market, direction, tf, exc)
        return False


def _in_h4_pulse_window(cfg: Any) -> bool:
    """② futures _in_h4_pulse_window (focus_manager.py:3170-3191) — clock only (data-independent).
    Within window_min minutes after KST 01/05/09/13/17/21h close. On error returns True (safe pass)."""
    try:
        w = max(1, int(getattr(cfg, "h4_pulse_window_min", 30)))
        dt = datetime.datetime.now()       # assume local KST
        if dt.hour not in (1, 5, 9, 13, 17, 21):
            return False
        if dt.minute >= w:
            return False
        return True
    except Exception:
        return True


def _structure_trend(client: Any, market: str, tf: str = "240", limit: int = 30) -> str:
    """⑬/④ analyze_structure trend.value (UPTREND/DOWNTREND/SIDEWAYS). '' on failure."""
    try:
        from app.strategy.greenpen.market_structure import analyze_structure
        candles = _ohlcv(_kl(client, market, tf, limit))
        if len(candles) < 20:
            return ""
        st = analyze_structure(candles)
        return (st.trend.value if hasattr(st.trend, "value") else str(st.trend)).upper()
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _structure_trend %s: %s", market, exc)
        return ""


def _fetch_24h_metrics(client: Any, market: str) -> Optional[dict]:
    """㉑㉔ adapt: replaces futures _get_24h_ticker (Bybit linear) — reconstructed from daily ('D') hi/lo/chg.
    {last, high, low, move_pct, range_pos_pct}. None on failure."""
    try:
        raw = _kl(client, market, "D", 2)
        if not raw or len(raw[-1]) < 5:
            return None
        day = raw[-1]
        o, h, l, c = float(day[1]), float(day[2]), float(day[3]), float(day[4])
        if c <= 0 or h <= l:
            return None
        move_pct = (c - o) / o * 100.0 if o > 0 else 0.0
        range_pos = (c - l) / (h - l) * 100.0
        return {"last": c, "high": h, "low": l,
                "move_pct": move_pct, "range_pos_pct": range_pos}
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _fetch_24h_metrics %s: %s", market, exc)
        return None


# ============================================================
# compute_entry_guard_score — faithful port of futures _compute_guard_score_modifiers
# ============================================================
def compute_entry_guard_score(
    client: Any,
    market: str,
    cfg: Any,
    direction: str = "LONG",
    btc_dir: str = "NEUTRAL",
) -> Tuple[float, List[str]]:
    """guard_score: the futures guard's 24 factors ported wholesale for spot (base_conv not included, sum of bonuses/penalties).

    Source app/manager/focus_manager.py:2306-2688 (_compute_guard_score_modifiers).
    Returns (total_modifier: float, breakdown: list[str]).
      ─ portable components = futures as-is (pure kline).
      ─ adapt = _linear_last_price → last close / day_direction → btc_dir argument / 24h ticker → daily.
      ─ skip(0pts) = Flow Reversal⑮ · Alt-BTC⑯ · 5 Peer factors⑰⑱⑲⑳㉕ · Day Box⑥ (no spot infrastructure, safe 0).
    Each component is independent via try/except (one component failing does not block the whole = same as futures).

    ★ unwired (dead code) — not called anywhere yet. ✅ 4 adversarial-audit items *fixed* (verbatim from futures):
        · ㉑ Overext — added ADX-exempt (breakout exemption) + range_pos 0~1 scale fix
        · ㉔ Blowoff — multiplication → linear interpolation (same as futures _check_blowoff)
        · ㉒ Inflection — slope%·EMA-MACD lean·direction gating verbatim
        · ㉓ Retest — _pivots·post extremum·q soft·FAIL breakaway verbatim
      ⚠️ When wired (used as scanner score): compute_entry_guard_score is a conviction *modifier* (±, base not included),
      so it can't plug directly into the scanner's standalone threshold (50) — must be summed with the base conviction model (separate design).
    """
    d = (direction or "LONG").upper()
    bdir = (btc_dir or "NEUTRAL").upper()
    total = 0.0
    bd: List[str] = []
    # capture for trend-alignment multicollinearity cap (futures 2317-2320)
    _ra_frame = 0.0
    _ra_trend = 0.0
    _ra_altbtc = 0.0       # spot skip → stays 0 (automatically harmless)
    _ra_btcalign = 0.0

    # ── ① PA Completion (H4 OR H1) — owner's core (futures 2322-2336) ──
    try:
        _h4 = _pa_completion(client, market, d, "240", cfg)
        _h1 = _pa_completion(client, market, d, "60", cfg) if not _h4 else False
        if _h4 or _h1:
            s = float(getattr(cfg, "guard_score_pa_completion_ok", 30.0))
            total += s; bd.append(f"PA{s:+.0f}({'H4' if _h4 else 'H1'})")
        else:
            s = float(getattr(cfg, "guard_score_pa_completion_none", -10.0))
            total += s; bd.append(f"PA{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] PA eval: %s", exc)

    # ── ①-2 D1 (daily) PA — big-picture direction (futures 2337-2349) ──
    #   ★ 'D' interval support varies by exchange → if unsupported, _pa_completion returns False for lack of data (none penalty).
    try:
        _d1 = _pa_completion(client, market, d, "D", cfg)
        if _d1:
            s = float(getattr(cfg, "guard_score_d1_pa_ok", 25.0))
            total += s; bd.append(f"D1PA{s:+.0f}")
        else:
            s = float(getattr(cfg, "guard_score_d1_pa_none", -5.0))
            total += s; bd.append(f"D1PA{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] D1 PA eval: %s", exc)

    # ── ② H4 Pulse (60-min window) — clock-only portable (futures 2350-2358). PreClose is skipped for spot ──
    try:
        if _in_h4_pulse_window(cfg):
            s = float(getattr(cfg, "guard_score_h4_pulse_in", 20.0))
            total += s; bd.append(f"H4Pulse{s:+.0f}")
        else:
            s = float(getattr(cfg, "guard_score_h4_pulse_out", -3.0))
            total += s; bd.append(f"H4Pulse{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] H4 pulse: %s", exc)

    # ── ③ H1 PA Pulse (futures 2374-2384) — adapt: drop day_direction dependency (none on spot),
    #   recognize pure H1 PA completion only. h1_pa_pulse_require_day_dir is ignored (spec: portable when False). ──
    try:
        _h1pulse = _pa_completion(client, market, d, "60", cfg)
        if _h1pulse:
            s = float(getattr(cfg, "guard_score_h1_pa_in", 15.0))
            total += s; bd.append(f"H1PA{s:+.0f}")
        else:
            s = float(getattr(cfg, "guard_score_h1_pa_out", -2.0))
            total += s; bd.append(f"H1PA{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] H1 PA: %s", exc)

    # ── ④ Frame Guard (24h range alignment) — portable (futures 2385-2399) ──
    #   spot long_only: UPTREND=aligned(+) / SIDEWAYS=neutral / DOWNTREND=opposite(-). captures _ra_frame.
    try:
        ftrend = _structure_trend(client, market, "240", 30)
        if d == "LONG":
            if ftrend == "UPTREND":
                s = float(getattr(cfg, "guard_score_frame_aligned", 15.0))
                total += s; bd.append(f"Frame{s:+.0f}(aligned)"); _ra_frame = s
            elif ftrend == "DOWNTREND":
                s = float(getattr(cfg, "guard_score_frame_opposite", -20.0))
                total += s; bd.append(f"Frame{s:+.0f}(opposite)"); _ra_frame = s
            elif ftrend == "SIDEWAYS":
                s = float(getattr(cfg, "guard_score_frame_neutral", 5.0))
                total += s; bd.append(f"Frame{s:+.0f}(neutral)"); _ra_frame = s
        else:  # SHORT branch preserved (unused on spot, harmless)
            if ftrend == "DOWNTREND":
                s = float(getattr(cfg, "guard_score_frame_aligned", 15.0))
                total += s; bd.append(f"Frame{s:+.0f}(aligned)"); _ra_frame = s
            elif ftrend == "UPTREND":
                s = float(getattr(cfg, "guard_score_frame_opposite", -20.0))
                total += s; bd.append(f"Frame{s:+.0f}(opposite)"); _ra_frame = s
            elif ftrend == "SIDEWAYS":
                s = float(getattr(cfg, "guard_score_frame_neutral", 5.0))
                total += s; bd.append(f"Frame{s:+.0f}(neutral)"); _ra_frame = s
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Frame: %s", exc)

    # ── ⑤ Anchor Fast-Track (pullback proximity) — adapt: _linear_last_price → last close (futures 2400-2411) ──
    #   spot style: ATR proximity to nearest SUPPORT (LONG) = cycle start point. zones from greenpen.full_analysis.
    try:
        raw_p = _kl(client, market, str(getattr(cfg, "primary_tf", "240")), 30)
        candles = _ohlcv(raw_p)
        price = _last_price(client, market, raw_p)
        if len(candles) >= 15 and price > 0:
            from app.strategy.greenpen import full_analysis
            gp = full_analysis(candles)
            atr = float(getattr(gp, "atr", 0.0) or 0.0)
            zones = getattr(gp, "zones", None) or []
            max_prox = float(getattr(cfg, "anchor_fasttrack_max_proximity", 0.33))
            if atr > 0:
                if d == "LONG":
                    levels = [float(getattr(z, "price_high", 0) or 0) for z in zones
                              if str(getattr(getattr(z, "type", None), "value", getattr(z, "type", ""))).upper() == "SUPPORT"
                              and float(getattr(z, "price_high", 0) or 0) <= price]
                    nearest = max(levels) if levels else None
                else:
                    levels = [float(getattr(z, "price_low", 0) or 0) for z in zones
                              if str(getattr(getattr(z, "type", None), "value", getattr(z, "type", ""))).upper() == "RESISTANCE"
                              and float(getattr(z, "price_low", 0) or 0) >= price]
                    nearest = min(levels) if levels else None
                if nearest is not None:
                    prox = abs(price - nearest) / atr
                    if prox <= max_prox:
                        s = float(getattr(cfg, "guard_score_anchor_close", 20.0))
                        total += s; bd.append(f"FT{s:+.0f}(prox{prox:.2f})")
                    elif prox > 1.0:
                        s = float(getattr(cfg, "guard_score_anchor_far", -10.0))
                        total += s; bd.append(f"FT{s:+.0f}(prox{prox:.2f})")
                    # 0.33~1.0 = 0 pts
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Anchor: %s", exc)

    # ── ⑥ Day Box Guard — SKIP(0 pts) ──
    #   day_box_state (09h box formation·lock) state machine = impure·clock-dependent, absent in the spot manager.
    #   spec recommends skip. overext(㉑) covers top-of-24h-range chasing instead.

    # ── ⑦ microtiming 5M (RSI/MACD/BB inflection) — portable (futures 2426-2437) ──
    try:
        if getattr(cfg, "microtiming_5m_enabled", False):
            _mt_ok = _microtiming_5m_ok(client, market, d, cfg)
            if _mt_ok:
                s = float(getattr(cfg, "guard_score_microtiming_ok", 10.0))
                total += s; bd.append(f"MT{s:+.0f}")
            else:
                s = float(getattr(cfg, "guard_score_microtiming_no", -5.0))
                total += s; bd.append(f"MT{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] microtiming: %s", exc)

    # ── ⑧ raw_body_guard (3-bar net direction) — portable (futures 2438-2449) ──
    try:
        if getattr(cfg, "raw_body_guard_enabled", getattr(cfg, "raw_body_enabled", False)):
            _rb_against = _raw_body_against(client, market, d, cfg)
            if _rb_against:
                s = float(getattr(cfg, "guard_score_raw_body_against", -8.0))
                total += s; bd.append(f"RawBody{s:+.0f}")
            else:
                s = float(getattr(cfg, "guard_score_raw_body_align", 5.0))
                total += s; bd.append(f"RawBody{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] raw_body: %s", exc)

    # ── ⑨ momentum_deriv (RSI/MACD rate of change) — portable (futures 2450-2461) ──
    try:
        if getattr(cfg, "momentum_deriv_guard_enabled", getattr(cfg, "momentum_deriv_enabled", False)):
            _md_against = _momentum_deriv_against(client, market, d, cfg)
            if _md_against:
                s = float(getattr(cfg, "guard_score_momentum_deriv_against", -5.0))
                total += s; bd.append(f"MomDeriv{s:+.0f}")
            else:
                s = float(getattr(cfg, "guard_score_momentum_deriv_align", 5.0))
                total += s; bd.append(f"MomDeriv{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] momentum_deriv: %s", exc)

    # ── ⑩ BTC direction alignment — adapt: self.day_direction → btc_dir argument (futures 2462-2476) ──
    #   spot has no BTC breadth infrastructure → caller injects via selector._btc_direction (BTC H4 structure).
    try:
        if bdir in ("UP", "DOWN") and d in ("LONG", "SHORT"):
            want = "UP" if d == "LONG" else "DOWN"
            if bdir == want:
                s = float(getattr(cfg, "guard_score_btc_aligned", 15.0))
                total += s; bd.append(f"BTC{s:+.0f}(aligned)"); _ra_btcalign = s
            else:
                s = float(getattr(cfg, "guard_score_btc_opposite", -15.0))
                total += s; bd.append(f"BTC{s:+.0f}(opposite)"); _ra_btcalign = s
        # NEUTRAL = 0
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] BTC align: %s", exc)

    # ── ⑪ ADX strong/weak — portable (futures 2477-2504). same SIDEWAYS-ADX bonus-exemption fix ──
    try:
        from app.strategy import indicators
        raw_adx = _kl(client, market, str(getattr(cfg, "primary_tf", "240")), 60)
        highs = [float(r[2]) for r in raw_adx if len(r) >= 5]
        lows = [float(r[3]) for r in raw_adx if len(r) >= 5]
        closes = [float(r[4]) for r in raw_adx if len(r) >= 5]
        a = indicators.adx(highs, lows, closes)
        adx = float(a.get("adx", 0.0)) if a else 0.0
        if adx >= 30:
            _flat = False
            if getattr(cfg, "guard_score_adx_strong_requires_trend", False):
                if _structure_trend(client, market, "240", 30) == "SIDEWAYS":
                    _flat = True
            if _flat:
                bd.append(f"ADX·0({adx:.0f}≥30,sideways-exempt)")
            else:
                s = float(getattr(cfg, "guard_score_adx_strong", 10.0))
                total += s; bd.append(f"ADX{s:+.0f}({adx:.0f}≥30)")
        elif adx < 20:
            s = float(getattr(cfg, "guard_score_adx_weak", -5.0))
            total += s; bd.append(f"ADX{s:+.0f}({adx:.0f}<20)")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] ADX: %s", exc)

    # ── ⑫ volume big + direction match (5M) — portable (futures 2505-2522). vol index r[5] ──
    try:
        raw5 = _kl(client, market, "5", 20)
        if raw5 and len(raw5) >= 15:
            vols = [float(r[5]) if len(r) > 5 else 0.0 for r in raw5[-15:]]
            cur_vol = vols[-1] if vols else 0.0
            avg_vol = (sum(vols[:-1]) / max(len(vols) - 1, 1)) if len(vols) > 1 else 1.0
            last = raw5[-1]
            last_open = float(last[1]) if len(last) > 1 else 0.0
            last_close = float(last[4]) if len(last) > 4 else 0.0
            last_dir = "UP" if last_close > last_open else "DOWN"
            want_vol_dir = "UP" if d == "LONG" else "DOWN"
            if avg_vol > 0 and cur_vol >= avg_vol * 2.0 and last_dir == want_vol_dir:
                s = float(getattr(cfg, "guard_score_vol_big_align", 10.0))
                total += s; bd.append(f"Vol{s:+.0f}(big {cur_vol/avg_vol:.1f}x)")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Vol: %s", exc)

    # ── ⑬ H4 trend confidence (analyze_structure) — portable (futures 2523-2547). captures _ra_trend ──
    try:
        trend_str = _structure_trend(client, market, "240", 30)
        if trend_str in ("UPTREND", "DOWNTREND"):
            if (trend_str == "UPTREND" and d == "LONG") or (trend_str == "DOWNTREND" and d == "SHORT"):
                s = float(getattr(cfg, "guard_score_trend_high_conf", 10.0))
                total += s; bd.append(f"Trend{s:+.0f}(H4 aligned)"); _ra_trend = s
            else:
                s = float(getattr(cfg, "guard_score_trend_low_conf", -5.0))
                total += s; bd.append(f"Trend{s:+.0f}(H4 opposite)"); _ra_trend = s
        # SIDEWAYS = 0
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Trend: %s", exc)

    # ── ⑭ 5M RSI extreme inflection (reversal spot) — portable (futures 2548-2564) ──
    try:
        from app.strategy.indicators import rsi_with_prev
        raw5 = _kl(client, market, "5", 20)
        if raw5 and len(raw5) >= 15:
            closes5 = [float(r[4]) for r in raw5 if len(r) >= 5]
            rsi_now5, rsi_prev5 = rsi_with_prev(closes5, length=14)
            if rsi_now5 is not None and rsi_prev5 is not None:
                if d == "LONG" and rsi_prev5 < 30 and rsi_now5 > rsi_prev5:
                    s = float(getattr(cfg, "guard_score_rsi_extreme", 10.0))
                    total += s; bd.append(f"RSI5M{s:+.0f}({rsi_prev5:.0f}<30↑)")
                elif d == "SHORT" and rsi_prev5 > 70 and rsi_now5 < rsi_prev5:
                    s = float(getattr(cfg, "guard_score_rsi_extreme", 10.0))
                    total += s; bd.append(f"RSI5M{s:+.0f}({rsi_prev5:.0f}>70↓)")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] RSI extreme: %s", exc)

    # ── ⑮ Flow Reversal Signal — SKIP(0 pts) ──
    #   depends on _signal_history/_confidence_history time series + scanner entry dict (adx_past). No spot infrastructure.

    # ── ⑯ Alt-BTC Alignment — SKIP(0 pts) ──
    #   depends on _day_direction/confidence_history BTCUSDT macro. ⑩ (btc_dir) already covers BTC alignment. _ra_altbtc=0.

    # ── ⑰⑱⑲⑳㉕ 5 Peer factors (Win/SL/Struggle/Conflict/Crowding) — SKIP(0 pts) ──
    #   fully depend on app.core.peer_brief fleet infrastructure (multi-server position sharing). Absent in the spot manager. All 0.

    # ── ㉑ Overextension (chasing the tail end) — faithful to futures _check_overextension(3680-3725). 24h ticker → daily reconstruction ──
    #   ★ range_pos_pct is 0~100 → /100 to match futures pos(0~1)·top(0.85) scale. Includes strong-ADX-breakout exemption.
    try:
        if getattr(cfg, "overextension_enabled", False):
            m24 = _fetch_24h_metrics(client, market)
            if m24:
                min_move = float(getattr(cfg, "overextension_min_move_pct", 8.0))
                top = float(getattr(cfg, "overextension_range_pos_pct", 0.85))   # 0~1 (same as futures)
                pen = float(getattr(cfg, "overextension_penalty", 10.0))
                pos = m24["range_pos_pct"] / 100.0                                # 0~100 → 0~1
                ext = (d == "LONG" and abs(m24["move_pct"]) >= min_move and pos >= top) \
                    or (d == "SHORT" and abs(m24["move_pct"]) >= min_move and pos <= (1.0 - top))
                if ext:
                    # ★ strong-breakout exemption — if primary ADX ≥ adx_exempt it's not tail-end (riding the wall) (futures 3715-3724)
                    adx_exempt = float(getattr(cfg, "overextension_adx_exempt", 30.0))
                    exempt = False
                    if adx_exempt > 0:
                        try:
                            from app.strategy import indicators as _ind
                            _r = _kl(client, market, str(getattr(cfg, "primary_tf", "240")), 60)
                            _a = _ind.adx([float(x[2]) for x in _r if len(x) >= 5],
                                          [float(x[3]) for x in _r if len(x) >= 5],
                                          [float(x[4]) for x in _r if len(x) >= 5])
                            _adxv = float(_a.get("adx", 0.0)) if _a else 0.0
                            if _adxv >= adx_exempt:
                                exempt = True
                                bd.append(f"Overext·0(breakout-exempt ADX{_adxv:.0f}≥{adx_exempt:.0f})")
                        except Exception:
                            pass
                    if not exempt:
                        total -= pen
                        bd.append(f"Overext-{pen:.0f}({'top' if d == 'LONG' else 'bottom'}{pos * 100:.0f}%/{m24['move_pct']:+.0f}%)")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Overext: %s", exc)

    # ── ㉔ Blow-off (chasing a 24h spike) — faithful to futures _check_blowoff(3660-3675). multiplication → linear interpolation ──
    try:
        if getattr(cfg, "blowoff_filter_enabled", False):
            m24 = _fetch_24h_metrics(client, market)
            if m24:
                thr = float(getattr(cfg, "blowoff_move_pct", 30.0))
                chase_only = bool(getattr(cfg, "blowoff_chase_only", True))
                mv = m24["move_pct"]
                move = abs(mv)
                chasing = (d == "LONG" and mv > 0) or (d == "SHORT" and mv < 0)
                if move >= thr and (chasing or not chase_only):
                    base = float(getattr(cfg, "blowoff_penalty", 20.0))
                    ext = float(getattr(cfg, "blowoff_extreme_pct", 80.0))
                    maxp = float(getattr(cfg, "blowoff_max_penalty", 40.0))
                    if move >= ext:
                        eff = maxp
                    else:
                        span = max(1e-9, ext - thr)
                        eff = base + (move - thr) / span * (maxp - base)
                    eff = max(0.0, min(maxp, eff))
                    total -= eff; bd.append(f"Blowoff-{eff:.0f}({mv:+.0f}% chase)")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Blowoff: %s", exc)

    # ── ㉒ Inflection Setup (inflection score) — portable (futures 2640-2649) ──
    try:
        if getattr(cfg, "inflection_setup_enabled", False):
            _if_mod = _inflection_setup(client, market, d, cfg)
            if _if_mod != 0.0:
                total += _if_mod; bd.append(f"Inflect{_if_mod:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Inflection: %s", exc)

    # ── ㉓ Retest Setup (breakout → pullback → support) — portable (futures 2650-2658) ──
    try:
        if getattr(cfg, "retest_setup_enabled", False):
            _rt_mod = _retest_setup(client, market, d, cfg)
            if _rt_mod > 0.0:
                total += _rt_mod; bd.append(f"Retest+{_rt_mod:.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Retest: %s", exc)

    # ── DirDedupe (remove direction double-counting) — portable arithmetic (futures 2659-2670) ──
    #   when AltBTC/BTC-align are skipped _ra_altbtc/_ra_btcalign=0 → automatically harmless (only Frame/Trend subtracted).
    if getattr(cfg, "guard_dir_dedupe_enabled", False):
        try:
            _dd_sum = _ra_frame + _ra_trend + _ra_altbtc + _ra_btcalign
            if _dd_sum != 0.0:
                total -= _dd_sum
                bd.append(f"DirDedupe[F{_ra_frame:+.0f}/T{_ra_trend:+.0f}/AltB{_ra_altbtc:+.0f}/BTC{_ra_btcalign:+.0f}=-{_dd_sum:+.0f}]")
        except Exception as exc:
            logger.debug("[SPOT_CHAIN] DirDedupe: %s", exc)
    # ── RegimeAlignCap (cap on summed trend alignment) — portable arithmetic, only when dedupe OFF (futures 2671-2681) ──
    elif getattr(cfg, "regime_align_cap_enabled", False):
        try:
            _ra_raw = _ra_frame + _ra_trend + _ra_altbtc
            _ra_cap = float(getattr(cfg, "regime_align_cap", 15.0))
            _ra_clamped = max(-_ra_cap, min(_ra_cap, _ra_raw))
            if _ra_clamped != _ra_raw:
                total += (_ra_clamped - _ra_raw)
                bd.append(f"RegimeCap[{_ra_raw:+.0f}→{_ra_clamped:+.0f}]")
        except Exception as exc:
            logger.debug("[SPOT_CHAIN] RegimeCap: %s", exc)

    # ── TotalCap (cap on total bonus sum) — portable arithmetic (futures 2682-2688) ──
    if getattr(cfg, "guard_score_total_cap_enabled", False):
        _cap = abs(float(getattr(cfg, "guard_score_total_cap", 80.0)))
        if _cap > 0 and abs(total) > _cap:
            clamped = max(-_cap, min(_cap, total))
            bd.append(f"TotalCap[{total:+.0f}→{clamped:+.0f}]")
            total = clamped

    return round(total, 1), bd


# ============================================================
# ⑦⑧⑨ portable helpers — port only the pure parts of futures _check_microtiming_5m /
#   _check_raw_body_guard / _check_momentum_derivative_guard (BLOCK decision → bool for guard_score).
# ============================================================
def _microtiming_5m_ok(client: Any, market: str, direction: str, cfg: Any) -> bool:
    """⑦ 5M RSI/MACD/BB inflection: 3-factor score ≥ min → True. Source focus_manager.py:7911-8000+."""
    try:
        from app.strategy.indicators import rsi_with_prev, macd_hist_pair, bollinger_bands
        raw = _kl(client, market, "5", 30)
        if not raw or len(raw) < 27:
            return True   # insufficient fetch → invalid (safe side, ok)
        closes = [float(r[4]) for r in raw if len(r) >= 5]
        rsi_now, rsi_prev = rsi_with_prev(closes, length=14)
        hist_now, hist_prev = macd_hist_pair(closes)
        bb_now = bollinger_bands(closes, 20, 2.0)
        bb_prev = bollinger_bands(closes[:-1], 20, 2.0) if len(closes) >= 21 else None

        def _bb_pct(bb, price):
            if not bb or bb.get("upper", 0) <= bb.get("lower", 0):
                return None
            return (price - bb["lower"]) / (bb["upper"] - bb["lower"]) * 100.0
        bb_now_pct = _bb_pct(bb_now, closes[-1])
        bb_prev_pct = _bb_pct(bb_prev, closes[-2]) if bb_prev and len(closes) >= 2 else None
        rsi_thr = float(getattr(cfg, "microtiming_5m_rsi_long_threshold", 35.0))
        bb_low = float(getattr(cfg, "microtiming_5m_bb_low_pct", 20.0))
        bb_rec = float(getattr(cfg, "microtiming_5m_bb_recover_pct", 30.0))
        rsi_s = macd_s = bb_s = 0
        if rsi_prev is not None and rsi_now is not None and rsi_prev <= rsi_thr and rsi_now > rsi_prev:
            rsi_s = 1
        if hist_prev is not None and hist_now is not None and hist_prev < 0 and (hist_now > 0 or hist_now > hist_prev):
            macd_s = 1
        if bb_prev_pct is not None and bb_now_pct is not None and bb_prev_pct < bb_low and bb_now_pct >= bb_rec:
            bb_s = 1
        total = rsi_s + macd_s + bb_s
        min_score = int(getattr(cfg, "microtiming_5m_min_score", 2))
        return total >= min_score
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _microtiming_5m_ok: %s", exc)
        return True


def _raw_body_against(client: Any, market: str, direction: str, cfg: Any) -> bool:
    """⑧ if net open→close of the last N closed bars opposes the entry = blocked → True. Source focus_manager.py:11210-11251."""
    try:
        lookback = max(1, int(getattr(cfg, "raw_body_guard_lookback", getattr(cfg, "raw_body_lookback", 3))))
        min_net = float(getattr(cfg, "raw_body_guard_min_net_pct", getattr(cfg, "raw_body_min_net_pct", 0.05)))
        raw = _kl(client, market, "5", lookback + 1)
        if not raw or len(raw) < lookback + 1:
            return False
        recent = raw[-(lookback + 1):-1]
        if not recent:
            return False
        ref = float(recent[-1][4]) or 0.0
        if ref <= 0:
            return False
        net_pct = sum(float(b[4]) - float(b[1]) for b in recent) / ref * 100.0
        du = (direction or "").upper()
        if du == "LONG" and net_pct < -abs(min_net):
            return True
        if du == "SHORT" and net_pct > abs(min_net):
            return True
        return False
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _raw_body_against: %s", exc)
        return False


def _momentum_deriv_against(client: Any, market: str, direction: str, cfg: Any) -> bool:
    """⑨ if 5M RSI/MACD rate of change accelerates against the entry = blocked → True. Source focus_manager.py:11253+."""
    try:
        from app.strategy.indicators import rsi_series, macd_hist_series
        du = (direction or "").upper()
        if du not in ("LONG", "SHORT"):
            return False
        lookback = max(2, int(getattr(cfg, "momentum_deriv_guard_lookback", getattr(cfg, "momentum_deriv_lookback", 5))))
        rsi_thr = float(getattr(cfg, "momentum_deriv_guard_rsi_min_slope", getattr(cfg, "momentum_deriv_rsi_slope", 2.0)))
        macd_thr = float(getattr(cfg, "momentum_deriv_guard_macd_min_slope", getattr(cfg, "momentum_deriv_macd_slope", 0.0)))
        require_both = bool(getattr(cfg, "momentum_deriv_guard_require_both", getattr(cfg, "momentum_deriv_require_both", True)))
        tf = str(getattr(cfg, "momentum_deriv_guard_tf", "5"))
        need = 14 + lookback * 2 + 5
        raw = _kl(client, market, tf, need)
        if not raw or len(raw) < need:
            return False
        closes = [float(r[4]) for r in raw if len(r) >= 5]
        rsi_vals = rsi_series(closes, 14)
        macd_hist = macd_hist_series(closes, 12, 26, 9)
        if not rsi_vals or len(rsi_vals) < lookback * 2:
            return False
        if not macd_hist or len(macd_hist) < lookback * 2:
            return False
        rsi_delta = sum(rsi_vals[-lookback:]) / lookback - sum(rsi_vals[-2 * lookback:-lookback]) / lookback
        macd_delta = sum(macd_hist[-lookback:]) / lookback - sum(macd_hist[-2 * lookback:-lookback]) / lookback
        if du == "LONG":
            rsi_against = rsi_delta < -abs(rsi_thr)
            macd_against = macd_delta < -abs(macd_thr)
        else:
            rsi_against = rsi_delta > abs(rsi_thr)
            macd_against = macd_delta > abs(macd_thr)
        return (rsi_against and macd_against) if require_both else (rsi_against or macd_against)
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _momentum_deriv_against: %s", exc)
        return False


# ============================================================
# ㉒㉓ portable helpers — port the pure kline + inline formulas of futures
#   _compute_inflection_setup(3730-3792) / _compute_retest_setup(3794-3884).
# ============================================================
def _inflection_setup(client: Any, market: str, direction: str, cfg: Any) -> float:
    """㉒ 24h (5m 288 bars) position × 15-min momentum → ceiling-stall penalty / bottom-inflection bonus. ±cap.
    Verbatim port of futures _compute_inflection_setup(focus_manager.py:3744-3789)
    (slope15m %·EMA12/26-MACD hist Δ lean·at_high/at_low direction gating). Only _get_mtf_kline→_kl adapted."""
    try:
        import math
        raw = _kl(client, market, "5", 288)
        if not raw or len(raw) < 35:
            return 0.0
        cl = [float(r[4]) for r in raw if len(r) >= 5]
        his = [float(r[2]) for r in raw if len(r) >= 5]
        los = [float(r[3]) for r in raw if len(r) >= 5]
        if len(cl) < 35:
            return 0.0
        hi = max(his); lo = min(los); last = cl[-1]
        if hi <= lo or last <= 0:
            return 0.0
        pos = (last - lo) / (hi - lo)
        slope15m = (cl[-1] - cl[-4]) / cl[-4] * 100.0 if cl[-4] != 0 else 0.0

        def _ema(vals, n):
            k = 2.0 / (n + 1); e = vals[0]; out = [e]
            for v in vals[1:]:
                e = v * k + e * (1 - k); out.append(e)
            return out

        e12 = _ema(cl, 12); e26 = _ema(cl, 26)
        macd_line = [a - b for a, b in zip(e12, e26)]
        sig = _ema(macd_line, 9)
        histl = [a - b for a, b in zip(macd_line, sig)]
        hist_d = histl[-1] - histl[-2] if len(histl) >= 2 else 0.0
        sscale = float(getattr(cfg, "inflection_setup_slope_scale", 0.40)) or 0.40
        lean = 0.25 * (1 if hist_d > 0 else (-1 if hist_d < 0 else 0))
        up = max(-1.0, min(1.0, 0.75 * math.tanh(slope15m / sscale) + lean))
        at_high = max(0.0, pos - 0.5) * 2.0
        at_low = max(0.0, 0.5 - pos) * 2.0
        base = float(getattr(cfg, "inflection_setup_base", 0.45))
        W = float(getattr(cfg, "inflection_setup_weight", 20.0))
        cap = float(getattr(cfg, "inflection_setup_cap", 20.0))
        if (direction or "").upper() == "LONG":
            val = W * (at_high * (up - base) + at_low * (base + up))
        else:
            val = W * (at_high * (base - up) + at_low * (-base - up))
        return max(-cap, min(cap, round(val, 1)))
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _inflection_setup: %s", exc)
        return 0.0


def _retest_setup(client: Any, market: str, direction: str, cfg: Any) -> float:
    """㉓ pivot breakout over 5m 60 bars then retracement + turning = good-entry-spot bonus.
    Verbatim port of futures _compute_retest_setup(focus_manager.py:3812-3884)
    (_pivots ±W swing·BRK breakout·post extremum·retr=(post-price)/(post-level)·q soft·FAIL breakaway·RETR_HI+0.3 slack).
    Only _get_mtf_kline→_kl adapted. reject=0.0 (same meaning as futures' (0.0,""))."""
    try:
        raw = _kl(client, market, "5", 60)
        if not raw or len(raw) < 20:
            return 0.0
        hi = [float(r[2]) for r in raw if len(r) >= 5]
        lo = [float(r[3]) for r in raw if len(r) >= 5]
        cl = [float(r[4]) for r in raw if len(r) >= 5]
        op = [float(r[1]) for r in raw if len(r) >= 5]
        if len(cl) < 20:
            return 0.0
        n = len(cl); price = cl[-1]
        W = int(getattr(cfg, "retest_pivot_width", 2))
        BRK = 0.001
        RETR_LO = float(getattr(cfg, "retest_retr_lo", 0.30))
        RETR_HI = float(getattr(cfg, "retest_retr_hi", 0.90))
        FAIL = float(getattr(cfg, "retest_fail_pct", 0.005))
        W_RET = float(getattr(cfg, "retest_setup_weight", 12.0))
        TURN = float(getattr(cfg, "retest_setup_turn_bonus", 4.0))
        up = cl[-1] > op[-1] or (len(cl) >= 2 and cl[-1] > cl[-2])
        dn = cl[-1] < op[-1] or (len(cl) >= 2 and cl[-1] < cl[-2])

        def _pivots(vals, ishigh):
            out = []
            for i in range(W, len(vals) - W):
                seg = vals[i - W:i + W + 1]
                if ishigh and vals[i] == max(seg):
                    out.append((i, vals[i]))
                if (not ishigh) and vals[i] == min(seg):
                    out.append((i, vals[i]))
            return out

        if (direction or "").upper() == "LONG":
            broken = None
            for idx, res in _pivots(hi, True):
                if any(hi[j] > res * (1 + BRK) for j in range(idx + 1, n)):
                    broken = (idx, res)
            if not broken:
                return 0.0
            ridx, res = broken
            post_hi = max(hi[ridx:])
            if post_hi <= res:
                return 0.0
            retr = (post_hi - price) / (post_hi - res)
            if price < res * (1 - FAIL):
                return 0.0
            if retr < RETR_LO or retr > RETR_HI + 0.3:
                return 0.0
            q = max(0.0, 1 - abs(retr - 0.6) / 0.5)
            sc = W_RET * q + (TURN if up else 0.0)
        else:
            broken = None
            for idx, sup in _pivots(lo, False):
                if any(lo[j] < sup * (1 - BRK) for j in range(idx + 1, n)):
                    broken = (idx, sup)
            if not broken:
                return 0.0
            sidx, sup = broken
            post_lo = min(lo[sidx:])
            if post_lo >= sup:
                return 0.0
            retr = (price - post_lo) / (sup - post_lo)
            if price > sup * (1 + FAIL):
                return 0.0
            if retr < RETR_LO or retr > RETR_HI + 0.3:
                return 0.0
            q = max(0.0, 1 - abs(retr - 0.6) / 0.5)
            sc = W_RET * q + (TURN if dn else 0.0)
        if sc <= 0.0:
            return 0.0
        return round(sc, 1)
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _retest_setup: %s", exc)
        return 0.0


# ============================================================
# adx_entry_gate — port of the futures ADX state-machine entry gate (focus_manager.py:13266+/7417+)
#   macro gate for "does the market have a trend". When adx_filter_enabled, primary_tf ADX < min_adx_entry → reject.
#   ★ fail-open: if no data, pass (do not block).
# ============================================================
def adx_entry_gate(
    client: Any,
    market: str,
    cfg: Any,
    primary_tf: Optional[str] = None,
) -> Tuple[bool, str]:
    """SIDEWAYS/low-ADX rejection gate. Source _fetch_primary_adx(7417-7453) + DORMANT transition(13283+).

    Returns (allowed: bool, reason: str).
      ─ adx_filter_enabled=False → (True, "adx_filter_off")  [★spec: recommended to keep True in live]
      ─ adx < min_adx_entry → (False, reason)  [block low-ADX junk]
      ─ insufficient data/error → (True, fail-open)  [do not block]
    """
    if not getattr(cfg, "adx_filter_enabled", True):
        return True, "adx_filter_off"
    # ★ [2026-06-19 owner] gate-specific TF — prefer adx_entry_tf(H1) over primary_tf(H4).
    #   if caller specifies primary_tf use that, else cfg.adx_entry_tf(H1), else primary_tf(H4).
    tf = str(primary_tf or getattr(cfg, "adx_entry_tf", None) or getattr(cfg, "primary_tf", "240"))
    try:
        from app.strategy import indicators
        raw = _kl(client, market, tf, 60)
        highs = [float(r[2]) for r in raw if len(r) >= 5]
        lows = [float(r[3]) for r in raw if len(r) >= 5]
        closes = [float(r[4]) for r in raw if len(r) >= 5]
        if len(closes) < 2 * 14 + 1:        # ADX(14) minimum 29 bars
            return True, "adx_no_data"       # fail-open
        a = indicators.adx(highs, lows, closes)
        if not a:
            return True, "adx_calc_none"     # fail-open
        adx = float(a.get("adx", 0.0) or 0.0)
        min_entry = float(getattr(cfg, "min_adx_entry", 17))
        if adx < min_entry:
            # ★ breakout exemption — even at low ADX (trend just starting), pass if the last *closed* bar broke above the recent N-bar high.
            #   allows box-recovery/breakout entry before ADX catches up (owner's 'TRUST recovery entry' case).
            #   tail-end chasing is blocked separately by the top overext/blowoff (daily) gates, so it's safe.
            if getattr(cfg, "adx_entry_breakout_exempt", True):
                try:
                    _lb = int(getattr(cfg, "adx_entry_breakout_lookback", 12) or 12)
                    _ch, _hh = closes[:-1], highs[:-1]   # exclude forming bar (closed bars only)
                    if len(_ch) > _lb + 1:
                        _last = _ch[-1]
                        _prior_hi = max(_hh[-(_lb + 1):-1])   # highest of the N bars *before* the last bar
                        if _last > _prior_hi:
                            return True, f"breakout exempt(close {_last:.4g}>last {_lb} bars high {_prior_hi:.4g}, adx {adx:.1f})"
                except Exception:
                    pass
            return False, f"adx {adx:.1f}<{min_entry:.0f} (SIDEWAYS/low-ADX no trend)"
        return True, f"adx_ok({adx:.1f}≥{min_entry:.0f})"
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] adx_entry_gate %s: %s", market, exc)
        return True, "adx_error"             # fail-open
