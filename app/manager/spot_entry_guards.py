# ============================================================
# Spot Entry Guards — *copy*-port of the futures FOCUS entry timing gates (Phase 1)
# ------------------------------------------------------------
# Source: the validated entry gates in focus_manager.py (live futures untouched, copied here).
#   - check_gap            ← gap_check       (focus_manager.py:16694-16737)
#   - check_micro_1m       ← _check_micro_1m (focus_manager.py:8013-8094)
#   - check_momentum_reversal ← _compute_momentum_reversal_penalty (8096-8162)
# Preservation rules: long-only (SHORT branch kept but spot only passes LONG) · ADX exemption not applied (prevents ceiling leakage).
# Pure functions — zero self state, only (client, market, direction, cfg). Candles via client.get_kline (TTL cache).
# All default OFF (cfg.*_enabled=False) → turn ON after paper observation.
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Tuple

logger = logging.getLogger(__name__)


def check_gap(client: Any, market: str, direction: str, price: float, atr: float, cfg: Any) -> Tuple[bool, str]:
    """Pre-entry gap check — if distance to the high (LONG) over the chosen TF×N bars < required gap → block (no entry right below the ceiling).
    required gap = max(min_pct, ATR%×atr_mult) capped at atr_cap_pct (high-volatility coins only allowed further below).
    Source focus_manager.py:16694-16737. fail-open (pass if no data)."""
    if not getattr(cfg, "gap_check_enabled", False):
        return True, ""
    min_pct = float(getattr(cfg, "gap_check_min_pct", 0.3) or 0.0)
    if min_pct <= 0 or price <= 0:
        return True, ""
    try:
        tf = str(getattr(cfg, "gap_check_tf", "15"))
        bars = int(getattr(cfg, "gap_check_lookback_bars", 12))
        raw = client.get_kline(market, interval=tf, limit=bars + 2)
        if not raw or len(raw) < bars:
            return True, "gap_no_data"
        recent = raw[-bars:]
        if (direction or "").upper() == "LONG":
            wall = max(float(r[2]) for r in recent if len(r) >= 5)   # high over the upper N bars
            gap = (wall - price) / price * 100.0
        else:
            wall = min(float(r[3]) for r in recent if len(r) >= 5)
            gap = (price - wall) / price * 100.0
        eff = min_pct
        if getattr(cfg, "gap_check_atr_adaptive", True) and atr > 0:
            atrp = atr / price * 100.0
            need = atrp * float(getattr(cfg, "gap_check_atr_mult", 0.7))
            cap = float(getattr(cfg, "gap_check_atr_cap_pct", 1.5))
            eff = max(min_pct, min(need, cap))
        # ★ [2026-06-20] Breakout exemption — if the prior (last) bar broke above the high of the preceding N-1 bars (new high) = a real breakout, not ceiling-chasing → pass.
        #   A breaking-out coin is always right next to its own high, a case the anchor alone can't unblock (e.g. KERNEL BOS_BULLISH·room10% but gap 0.25%<1.03% blocked).
        #   Pump-tops/blow-offs are separately blocked by the headroom·overextension·micro_1m gates, so exempting gap alone is safe.
        if (direction or "").upper() == "LONG" and getattr(cfg, "gap_check_breakout_exempt", True):
            prior = recent[:-1]
            if len(prior) >= 2:
                prior_wall = max(float(r[2]) for r in prior if len(r) >= 5)
                last_high = float(recent[-1][2]) if len(recent[-1]) >= 5 else 0.0
                if prior_wall > 0 and last_high > prior_wall:
                    return True, f"gap_breakout_exempt(new high {last_high:.4f}>prior {prior_wall:.4f} breakout)"
        if gap < eff:
            return False, f"gap {gap:.2f}%<{eff:.2f}% ({tf}M×{bars}bars wall={wall:.4f} ceiling-chasing)"
        return True, "gap_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] gap_check fail-open: %s", exc)
        return True, "gap_error"


def check_micro_1m(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """1M micro timing — validates "this very moment". Defer this tick on an opposing candle / volume exhaustion / RSI overheat.
    Source focus_manager.py:8013-8094. ★ADX exemption not applied (spot: even small waves count). fail-open."""
    if not getattr(cfg, "micro_1m_check_enabled", False):
        return True, ""
    dir_up = (direction or "").upper()
    try:
        raw = client.get_kline(market, interval="1", limit=16)
        if not raw or len(raw) < 6:
            return True, "1m_no_data"
        # ① Direction of the last 1M bar — ★ [2026-06-20] body_min noise-doji exemption (fix for spot-only zero entries):
        #   if the prior 1M body is below body_min% (=noise doji), pass regardless of color. Only a clear opposing candle (|body|≥body_min) blocks.
        #   Previously it looked only at color (c<o) and blocked LONG even on a -0.02% doji → 1m_candle_against was one axis of zero entries.
        #   With body_min=0 (un-migrated), |body|≥0 is always true = prior behavior 100% unchanged (backward compatible).
        last = raw[-1]
        if len(last) >= 5:
            o, c = float(last[1]), float(last[4])
            body_min = float(getattr(cfg, "micro_1m_body_min_pct", 0.0) or 0.0)
            body_pct = abs(c - o) / o * 100.0 if o > 0 else 0.0
            if body_pct >= body_min:
                if dir_up == "LONG" and c < o:
                    return False, f"1m_candle_against(LONG 1M down-candle o={o:.4f} c={c:.4f} body={body_pct:.3f}%≥{body_min})"
                if dir_up == "SHORT" and c > o:
                    return False, f"1m_candle_against(SHORT 1M up-candle body={body_pct:.3f}%≥{body_min})"
        # ② Volume declining in a row (momentum exhaustion)
        n = int(getattr(cfg, "micro_1m_vol_decline_bars", 3))
        if len(raw) >= n + 1:
            vols = [float(r[5]) if len(r) >= 6 else 0 for r in raw[-(n + 1):]]
            if all(v > 0 for v in vols) and all(vols[i] > vols[i + 1] for i in range(len(vols) - 1)):
                return False, f"1m_vol_decline({n} bars declining in a row)"
        # ③ RSI extreme (overheat)
        closes = [float(r[4]) for r in raw if len(r) >= 5]
        if len(closes) >= 15:
            gains = sum(max(0, closes[i] - closes[i - 1]) for i in range(1, len(closes)))
            losses = sum(max(0, closes[i - 1] - closes[i]) for i in range(1, len(closes)))
            ag, al = gains / 14.0, losses / 14.0
            rsi = 100.0 - (100.0 / (1.0 + ag / al)) if al > 0 else 100.0
            long_max = float(getattr(cfg, "micro_1m_rsi_long_max", 70.0))
            short_min = float(getattr(cfg, "micro_1m_rsi_short_min", 30.0))
            if dir_up == "LONG" and rsi > long_max:
                return False, f"1m_rsi_overheat(LONG RSI={rsi:.1f}>{long_max})"
            if dir_up == "SHORT" and rsi < short_min:
                return False, f"1m_rsi_overheat(SHORT RSI={rsi:.1f}<{short_min})"
        return True, "1m_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] micro_1m fail-open: %s", exc)
        return True, "1m_error"


def check_momentum_reversal(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """Block on adverse movement over the last 5M 1~3 bars — avoid entering "after it has already moved / into a falling knife".
    Ports only the *strong reversal* of _compute_momentum_reversal_penalty(8096-8162) as a BLOCK
    (medium/cumulative weak-reversal score penalties go to Phase 2 guard_score). fail-open."""
    if not getattr(cfg, "momentum_reversal_enabled", False):
        return True, ""
    dir_up = (direction or "").upper()
    if dir_up not in ("LONG", "SHORT"):
        return True, "no-dir"
    try:
        raw = client.get_kline(market, interval="5", limit=20)
        if not raw or len(raw) < 5:
            return True, "no-data"
        highs = [float(r[2]) for r in raw[-15:] if len(r) >= 5]
        lows = [float(r[3]) for r in raw[-15:] if len(r) >= 5]
        closes = [float(r[4]) for r in raw[-15:] if len(r) >= 5]
        lookback = max(1, min(int(getattr(cfg, "momentum_reversal_lookback_bars", 3)), 5))
        if len(closes) < lookback + 2:
            return True, "no-data"
        # ATR (true range, period 14) inline — the futures source's indicators.atr import was broken (no-op) → compute robustly inline.
        trs, pc = [], None
        for h, l, c in zip(highs, lows, closes):
            tr = (h - l) if pc is None else max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
            pc = c
        atr_val = sum(trs[-14:]) / min(len(trs), 14) if trs else 0.0
        if not atr_val or atr_val <= 0:
            return True, "no-atr"
        last_change = closes[-1] - closes[-2]
        adverse_1bar = -last_change if dir_up == "LONG" else last_change   # positive = adverse
        strong_thr = float(getattr(cfg, "momentum_reversal_strong_atr", 1.0)) * atr_val
        if adverse_1bar >= strong_thr:
            return False, f"strong_rev ({adverse_1bar / atr_val:+.1f}ATR prior-bar reversal)"
        # ★ [2026-06-20] Removed the cum{N}_rev hard block (fix for spot-only zero entries):
        #   Futures uses momentum_reversal only as a *score penalty* (-20, _compute_momentum_reversal_penalty)
        #   and does not hard-block. This also matches this function's docstring intent ("cumulative weak reversal goes to guard_score").
        #   The spot port hard-blocked even cumulative reversals (return False), permanently blocking pullback entries → only a strong 1-bar reversal BLOCKs.
        return True, "mom_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] momentum_reversal fail-open: %s", exc)
        return True, "mom_error"


def check_dca_stabilized(client: Any, market: str, cfg: Any) -> Tuple[bool, str]:
    """DCA-only falling-knife gate — if the prior 5M bar is dropping hard, defer the DCA add.
    Reuses the check_momentum_reversal core for entry under DCA's own flag (dca_stabilize_gate_enabled).
    Spot=long_only → only looks at drops. Returns True=stable (DCA OK) / False=knife falling (defer). fail-open.
    ★ Lets the profitable pullback DCA (stalled knife) pass, blocking only freefall knife-catching to prevent a risk/reward flip."""
    if not getattr(cfg, "dca_stabilize_gate_enabled", False):
        return True, ""
    try:
        raw = client.get_kline(market, interval="5", limit=20)
        if not raw or len(raw) < 5:
            return True, "no-data"
        highs = [float(r[2]) for r in raw[-15:] if len(r) >= 5]
        lows = [float(r[3]) for r in raw[-15:] if len(r) >= 5]
        closes = [float(r[4]) for r in raw[-15:] if len(r) >= 5]
        if len(closes) < 3:
            return True, "no-data"
        trs, pc = [], None
        for h, l, c in zip(highs, lows, closes):
            tr = (h - l) if pc is None else max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
            pc = c
        atr_val = sum(trs[-14:]) / min(len(trs), 14) if trs else 0.0
        if not atr_val or atr_val <= 0:
            return True, "no-atr"
        drop_1bar = closes[-2] - closes[-1]   # positive = prior-bar drop magnitude
        strong_thr = float(getattr(cfg, "dca_stabilize_strong_atr", 1.0)) * atr_val
        if drop_1bar >= strong_thr:
            return False, f"falling_knife ({drop_1bar / atr_val:.1f}ATR prior-bar plunge) → DCA deferred"
        return True, "stable"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] dca_stabilized fail-open: %s", exc)
        return True, "dca_stab_error"


def check_raw_body(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """Block when the last 5M N-bar open→close net (raw energy) opposes the entry — filters spots where
    raw price action is opposing even if processed RSI/MACD pass. Source focus_manager.py:11205-11246.
    ★Conservative: min_net_pct>0 by default (ignore noise net, block only clear opposing energy = avoids wipeout). fail-open."""
    if not getattr(cfg, "raw_body_enabled", False):
        return True, ""
    lookback = max(1, int(getattr(cfg, "raw_body_lookback", 3)))
    min_net = float(getattr(cfg, "raw_body_min_net_pct", 0.05))
    try:
        raw = client.get_kline(market, interval="5", limit=lookback + 1)
        if not raw or len(raw) < lookback + 1:
            return True, "raw_no_data"
        recent = raw[-(lookback + 1):-1]   # last N completed bars excluding the last (in-progress) one (oldest-first)
        if not recent:
            return True, "raw_no_data"
        ref = float(recent[-1][4]) or 0.0
        if ref <= 0:
            return True, "raw_no_data"
        net_pct = sum(float(b[4]) - float(b[1]) for b in recent) / ref * 100.0
        du = (direction or "").upper()
        if du == "LONG" and net_pct < -abs(min_net):
            return False, f"raw_body {lookback}bars net={net_pct:+.2f}%(sell energy)→LONG blocked"
        if du == "SHORT" and net_pct > abs(min_net):
            return False, f"raw_body net={net_pct:+.2f}%(buy)→SHORT blocked"
        return True, "raw_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] raw_body fail-open: %s", exc)
        return True, "raw_error"


def check_momentum_deriv(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """Block when the 5M RSI/MACD rate of change (recent N avg − prior N avg) accelerates against the entry — distinguishes
    "same RSI=50 but rising vs rolling over". Source focus_manager.py:11264-11344.
    ★Conservative: require_both default True (block only when both RSI+MACD oppose = avoids wipeout). fail-open."""
    if not getattr(cfg, "momentum_deriv_enabled", False):
        return True, ""
    du = (direction or "").upper()
    if du not in ("LONG", "SHORT"):
        return True, "no-dir"
    lookback = max(2, int(getattr(cfg, "momentum_deriv_lookback", 5)))
    rsi_thr = float(getattr(cfg, "momentum_deriv_rsi_slope", 2.0))
    macd_thr = float(getattr(cfg, "momentum_deriv_macd_slope", 0.0))
    require_both = bool(getattr(cfg, "momentum_deriv_require_both", True))
    try:
        from app.strategy.indicators import rsi_series, macd_hist_series
        need = 14 + lookback * 2 + 5
        raw = client.get_kline(market, interval="5", limit=need)
        if not raw or len(raw) < need:
            return True, "no-data"
        closes = [float(r[4]) for r in raw if len(r) >= 5]   # oldest-first (spot already sorted)
        rsi_vals = rsi_series(closes, 14)
        macd_hist = macd_hist_series(closes, 12, 26, 9)
        if not rsi_vals or len(rsi_vals) < lookback * 2:
            return True, "no-rsi"
        if not macd_hist or len(macd_hist) < lookback * 2:
            return True, "no-macd"
        rsi_delta = sum(rsi_vals[-lookback:]) / lookback - sum(rsi_vals[-2 * lookback:-lookback]) / lookback
        macd_delta = sum(macd_hist[-lookback:]) / lookback - sum(macd_hist[-2 * lookback:-lookback]) / lookback
        if du == "LONG":
            rsi_against = rsi_delta < -abs(rsi_thr)
            macd_against = macd_delta < -abs(macd_thr)
        else:
            rsi_against = rsi_delta > abs(rsi_thr)
            macd_against = macd_delta > abs(macd_thr)
        blocked = (rsi_against and macd_against) if require_both else (rsi_against or macd_against)
        if blocked:
            return False, (f"momentum_deriv {du} opposing(RSIΔ={rsi_delta:+.1f} MACDΔ={macd_delta:+.4f} "
                           f"{'AND' if require_both else 'OR'})")
        return True, "mderiv_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] momentum_deriv fail-open: %s", exc)
        return True, "mderiv_error"


def check_mtf_align(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """MTF final block — block when higher/short TF structure is *clearly opposed* to the entry (spot only had a score, no block).
    Core of focus_manager.py:_final_d1_alignment_check(2791)+_regime_align_override(2696):
    "if any of D1/30M/15M clearly opposes = conflicting → block entry" (SIDEWAYS passes = short-term noise). long-only. fail-open.
    ※ D1('D') has different interval support per exchange so excluded by default (minute TFs only). Add to cfg.mtf_align_tfs where supported."""
    if not getattr(cfg, "mtf_align_enabled", False):
        return True, ""
    du = (direction or "").upper()
    try:
        from app.strategy.greenpen.pa_detector import OHLCV
        from app.strategy.greenpen.market_structure import analyze_structure
        tfs = [t.strip() for t in str(getattr(cfg, "mtf_align_tfs", "240,30,15")).split(",") if t.strip()]
        for tf in tfs:
            raw = client.get_kline(market, interval=tf, limit=40)
            if not raw or len(raw) < 15:
                continue
            candles = [OHLCV(float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                       for r in raw if len(r) >= 5]
            if len(candles) < 15:
                continue
            trend = str(analyze_structure(candles).trend.value).upper()
            if du == "LONG" and trend.startswith("DOWN"):
                return False, f"MTF {tf}=DOWNTREND vs LONG (higher TF conflict)"
            if du == "SHORT" and trend.startswith("UP"):
                return False, f"MTF {tf}=UPTREND vs SHORT"
        return True, "mtf_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] mtf_align fail-open: %s", exc)
        return True, "mtf_error"


def check_entry_expectation(client: Any, market: str, direction: str, price: float, atr: float, cfg: Any) -> Tuple[bool, str]:
    """Entry expectation gate — block if reward (reachable potential) is insufficient or risk (loss span) is excessive.
    ★ *Reuses* app/manager/entry_expectation.py:compute_entry_expectation (exchange-neutral, shared util with futures · core untouched).
    Source gate focus_manager.py:16653-16689. long-only. fail-open."""
    if not getattr(cfg, "entry_expectation_enabled", False):
        return True, ""
    if price <= 0:
        return True, ""
    try:
        from app.manager.entry_expectation import compute_entry_expectation
        from app.strategy.greenpen.pa_detector import OHLCV
        tf = str(getattr(cfg, "primary_tf", "240"))
        raw = client.get_kline(market, interval=tf, limit=60)
        if not raw or len(raw) < 20:
            return True, "ee_no_data"
        candles = [OHLCV(float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                   for r in raw if len(r) >= 5]
        if len(candles) < 20:
            return True, "ee_no_data"
        exp = compute_entry_expectation(direction, price, candles, atr or price * 0.02)
        min_rr = float(getattr(cfg, "entry_expectation_min_rr", 1.0))
        min_reward = float(getattr(cfg, "entry_expectation_min_reward_pct", 0.8))
        max_risk = float(getattr(cfg, "entry_expectation_max_risk_pct", 6.0))
        # ★ [2026-06-20] RR floor port (fix for a spot-only port omission): the futures EE gate (focus_manager.py:16653~)
        #   blocks on rr_ratio<min_rr, but the spot port checked only reward/risk and dropped the RR floor.
        #   Keeps a quality floor so bad-RR junk doesn't leak when other gates are loosened to open entries (top-tier-university model).
        if exp.rr_ratio < min_rr:
            return False, f"ee_rr {exp.rr_ratio:.2f}<{min_rr} (RR insufficient)"
        if exp.reward_pct < min_reward:
            return False, f"ee_reward {exp.reward_pct:.2f}%<{min_reward}% (reachable potential insufficient)"
        if exp.risk_pct > max_risk:
            return False, f"ee_risk {exp.risk_pct:.2f}%>{max_risk}% (loss span excessive)"
        return True, f"ee_ok(rr={exp.rr_ratio:.2f})"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] entry_expectation fail-open: %s", exc)
        return True, "ee_error"


def check_microtiming_5m(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """5M micro timing — scores 3 RSI/MACD/BB *inflection* signals; below 2/3 defer this tick (re-evaluate next scan).
    "even at conv 100, wait if it's not an inflection spot". Source focus_manager.py:7911-8007. long-only. fail-open.
    ★ WAIT not BLOCK — re-evaluated on the next scan (not a permanent block)."""
    if not getattr(cfg, "microtiming_5m_enabled", False):
        return True, ""
    du = (direction or "").upper()
    if du not in ("LONG", "SHORT"):
        return True, "no-dir"
    try:
        from app.strategy.indicators import rsi_with_prev, macd_hist_pair, bollinger_bands
        raw = client.get_kline(market, interval="5", limit=30)
        if not raw or len(raw) < 27:
            return True, "mt5_short"   # insufficient fetch → gate void (fail-safe side)
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
        # LONG (spot): RSI oversold→upturn inflection / MACD hist negative→positive (or shrinking) / BB lower band→recovery
        if rsi_prev is not None and rsi_now is not None and rsi_prev <= rsi_thr and rsi_now > rsi_prev:
            rsi_s = 1
        if hist_prev is not None and hist_now is not None and hist_prev < 0 and (hist_now > 0 or hist_now > hist_prev):
            macd_s = 1
        if bb_prev_pct is not None and bb_now_pct is not None and bb_prev_pct < bb_low and bb_now_pct >= bb_rec:
            bb_s = 1
        total = rsi_s + macd_s + bb_s
        min_score = int(getattr(cfg, "microtiming_5m_min_score", 2))
        if total < min_score:
            return False, f"microtiming_5m {total}/3<{min_score} (rsi{rsi_s}/macd{macd_s}/bb{bb_s} inflection insufficient)"
        return True, f"mt5_ok({total}/3)"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] microtiming_5m fail-open: %s", exc)
        return True, "mt5_error"
