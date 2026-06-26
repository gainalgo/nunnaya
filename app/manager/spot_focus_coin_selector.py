# ============================================================
# Upbit FOCUS Coin Selector — Triple-Confirmation (long_only)
# ------------------------------------------------------------
# Source 1: Upbit KRW market top turnover (per-exchange — newly implemented)
# Source 2: GreenPen multi-TF analysis  ← reuses focus_coin_selector
# Source 3: structural filter           ← reuses focus_coin_selector
#
# Source 2/3 only depend on client.get_kline(), and UpbitTradeClient returns
# candles in the same format as Bybit (oldest-first), so it inherits directly.
# direction_mode is always "long_only" (spot).
# 0 candidates = "not entering is also a strategy" (North Star)
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Liquidity/volatility filter (Upbit KRW basis)
_MIN_TURNOVER_KRW = 5_000_000_000.0   # 24h turnover >= 5B KRW (slippage prevention)
_MAX_CHANGE = 0.20                     # exclude 24h extreme moves (±20%)
_MIN_CHANGE = 0.005                    # too quiet => TP won't be reached (±0.5%)

# ── Per-exchange branch (Bybit spot=USDT) — Upbit/Bithumb(KRW) path is 100% unchanged ──
#   EXCHANGE_TYPE: BybitTradeClient="bybit" / Upbit·Bithumb=none("") → 'KRW'.
_MIN_TURNOVER_USDT = 3_000_000.0       # 24h turnover ~3M USDT (≈5B KRW equivalent, Bybit/Binance spot)


def _exchange_quote(client: Any) -> str:
    """client quote currency — 'USDT'(Bybit/Binance) or 'KRW'(Upbit/Bithumb)."""
    et = str(getattr(client, "EXCHANGE_TYPE", "")).lower()
    return "USDT" if et in ("bybit", "binance") else "KRW"


def _btc_ref(client: Any) -> str:
    """BTC reference symbol (for guard_score BTC alignment)."""
    return "BTCUSDT" if _exchange_quote(client) == "USDT" else "KRW-BTC"


def _accepts_market(client: Any, mk: Any) -> bool:
    """Is this a market in this exchange's quote currency (KRW- prefix vs USDT suffix)?"""
    m = str(mk).upper()
    return m.endswith("USDT") if _exchange_quote(client) == "USDT" else m.startswith("KRW-")


def _min_turnover(client: Any) -> float:
    return _MIN_TURNOVER_USDT if _exchange_quote(client) == "USDT" else _MIN_TURNOVER_KRW


def _status_to_gate(status: str, passed: bool) -> str:
    """scanner status (incl. dynamic number/paren suffix) → stable gate key (for GateLedger aggregation).
    Normalizes so the same gate aggregates into one counter even when it carries a different
    number (score / headroom %) each time.
    ★ 0 new judgment — only maps the already-computed row["status"] to a label (observation).

    NOTE: the keyword/gate strings below are load-bearing — they are matched as substrings
    against row["status"] (set in scan_spot_focus_candidates) and the gate values are dict
    keys aggregated by GateLedger. Do not translate them."""
    if passed:
        return "PASS"
    s = str(status or "").strip()
    if not s:
        return "기타"
    # Representative keyword (substring match) → stable key. Dynamic suffix (number/paren) ignored.
    table = (
        ("데이터부족", "데이터부족"),
        ("신호 없음", "신호없음"),
        ("역추세", "역추세차단"),
        ("SHORT", "SHORT제외"),
        ("약함", "약한신호"),
        ("구조", "구조탈락"),
        ("문턱", "conf문턱미달"),
        ("저항", "저항근접(headroom)"),
        ("점수", "점수미달(guard_score)"),
        ("ADX", "ADX미달"),
        ("투자유의", "투자유의차단"),
        ("주의환기", "주의환기차단"),
        ("오류", "스캔오류"),
    )
    for kw, gate in table:
        if kw in s:
            return gate
    return s[:24]


def _entry_score_threshold(cfg: Any, guard_score_threshold: float) -> float:
    """Spot final entry-score threshold.

    After Phase 2 wiring, `guard_score_threshold` keeps its name but its meaning is
    the `base_conviction + guard_modifier` final-score threshold. 0 means the score
    gate is OFF, as before.
    """
    try:
        th = float(guard_score_threshold or 0.0)
    except (TypeError, ValueError):
        th = 0.0
    if th <= 0:
        return 0.0
    return th


def _final_entry_score_for(client: Any, market: str, cfg: Any,
                           btc_dir: str = "NEUTRAL") -> Optional[Dict[str, Any]]:
    """Phase 2 spot score: base conviction + guard-chain modifier.

    The returned fields are shared so scan/select/UI all see the same values.
    Returns None on failure (caller handles it where fail-open is allowed).
    """
    try:
        from app.manager.spot_conviction import compute_base_conviction
        from app.manager.spot_guard_chain import compute_entry_guard_score

        base, base_bd = compute_base_conviction(client, market, cfg, direction="LONG", btc_dir=btc_dir)
        mod, mod_bd = compute_entry_guard_score(client, market, cfg, direction="LONG", btc_dir=btc_dir)
        final = float(base) + float(mod)
        return {
            "base_conviction": round(float(base), 2),
            "guard_modifier": round(float(mod), 2),
            "final_score": round(final, 2),
            "conviction_score": round(final, 2),
            "base_breakdown": base_bd,
            "guard_breakdown": mod_bd,
        }
    except Exception as exc:
        logger.debug("[SPOT_SELECT] final score computation failed %s: %s", market, exc)
        return None


def select_spot_focus_coin(
    system: Any,
    client: Any,
    *,
    primary_tf: str = "240",
    top_n: int = 10,
    min_conf: float = 0.4,
    exclude: Any = None,
    headroom_gate_pct: float = 0.0,
    overext_range_pos_pct: float = 0.0,
    overext_min_move_pct: float = 8.0,
    blowoff_move_pct: float = 0.0,
    guard_score_mode_enabled: bool = False,
    guard_score_threshold: float = 0.0,
    guard_score_total_cap: float = 0.0,
    block_warning: bool = False,
    block_caution: bool = False,
    record: Any = None,
) -> Optional[Dict]:
    """Triple-confirmation scan → single highest-final-score LONG candidate (None if none).
    §② gate: top candidate that passes headroom + overext + blowoff + final score (when threshold>0).
    block_warning/block_caution: block entry on exchange investment-warning / caution-flagged coins.
    record(market, gate, passed): optional callback — when on, aggregates per-coin gate pass/reject
      into GateLedger ('why did it stay silent' control panel). None=aggregation OFF (0 behavior change)."""
    rows = scan_spot_focus_candidates(
        system, client,
        primary_tf=primary_tf, top_n=top_n, min_conf=min_conf, exclude=exclude,
        headroom_gate_pct=headroom_gate_pct,
        guard_score_mode_enabled=guard_score_mode_enabled,
        guard_score_threshold=guard_score_threshold,
        guard_score_total_cap=guard_score_total_cap,
        block_warning=block_warning, block_caution=block_caution,
        record=record,
    )
    if not rows:
        logger.info("[SPOT_SELECT] no candidates from scan")
        return None

    # ★ §② entry-quality gate — headroom (overhead resistance room) + overext (chasing 24H top).
    #   Pick the top candidate that passes. Gate value <=0 always passes (=0 behavior change).
    from app.manager.spot_entry_quality import (
        check_headroom, check_overextension, check_blowoff,
    )
    # 24H metrics — only when overext/blowoff is on; batch-fetch the final candidates once (avoid duplicate fetch).
    metrics: Dict[str, Dict] = {}
    if overext_range_pos_pct > 0 or blowoff_move_pct > 0:
        metrics = _fetch_24h_metrics(client, [c.get("market") for c in rows if c.get("market")])
    best = None
    for c in rows:
        if not c.get("passed"):
            continue
        mk = c.get("market")
        price = float(c.get("price", 0) or 0)
        ok, reason = check_headroom(price, c.get("zones", []), min_headroom_pct=headroom_gate_pct)
        if not ok:
            logger.info("[SPOT_SELECT] %s %s — skipped", mk, reason)
            continue
        m = metrics.get(mk) or {}
        if overext_range_pos_pct > 0:
            ok2, reason2 = check_overextension(
                m.get("last", price), m.get("hi", 0.0), m.get("lo", 0.0), m.get("move", 0.0),
                range_pos_pct=overext_range_pos_pct, min_move_pct=overext_min_move_pct,
            )
            if not ok2:
                logger.info("[SPOT_SELECT] %s %s — skipped", mk, reason2)
                continue
        if blowoff_move_pct > 0:
            ok3, reason3 = check_blowoff(
                m.get("move", 0.0), blowoff_move_pct=blowoff_move_pct, direction="LONG",
            )
            if not ok3:
                logger.info("[SPOT_SELECT] %s %s — skipped", mk, reason3)
                continue
        best = c
        break
    if best is None:
        logger.info("[SPOT_SELECT] all candidates blocked by entry-quality gate (ceiling/overextended/parabolic) — skip")
        return None

    if best.get("confidence", 0) < min_conf:
        logger.info("[SPOT_SELECT] best %s conf=%.2f < min_conf %.2f — skip",
                    best.get("market"), best.get("confidence", 0), min_conf)
        return None

    # Spot long_only safeguard: SHORT candidates must never pass
    if best.get("direction") != "LONG":
        logger.info("[SPOT_SELECT] best %s dir=%s != LONG — blocked (spot long_only)",
                    best.get("market"), best.get("direction"))
        return None

    logger.info("[SPOT_SELECT] Selected: %s LONG (conf=%.2f final=%.1f base=%.1f mod=%.1f)",
                best.get("market"), best.get("confidence", 0),
                float(best.get("final_score", best.get("guard_score", 0)) or 0),
                float(best.get("base_conviction", 0) or 0),
                float(best.get("guard_modifier", 0) or 0))
    return best


def select_spot_contrarian_coin(
    system: Any,
    client: Any,
    *,
    top_n: int = 10,
    exclude: Any = None,
    coin_up_th: float = 3.0,
    coin_up_cap: float = 15.0,
    regime_gate: bool = True,
    block_warning: bool = False,
    block_caution: bool = False,
) -> Optional[Dict]:
    """Single CONTRARIAN spot candidate — a coin with *relative strength vs BTC* when BTC is non-trending (neutral/down).
    Opposite regime to FOCUS (trend-following): OFF in uptrend (regime_gate), only in neutral/down = the FOCUS churn zone.
    Signal = (coin 24h move − BTC 24h move) ≥ coin_up_th  ∧  move ≤ coin_up_cap (exclude parabolic pumps).
    ★ v1 = coarse 24h relative-strength proxy (reuses existing _fetch_24h_metrics, 0 new candle fetch).
       To be refined with short-term (minute) relative strength after paper observation. long_only (spot).
    0 candidates = "not entering is also a strategy" (North Star)."""
    # ── regime-gate: uptrend is FOCUS territory → contrarian OFF ──
    btc_dir = _btc_direction(client)
    if regime_gate and btc_dir == "UP":
        logger.info("[SPOT_CONTRA] BTC UPTREND — contrarian skip (FOCUS territory)")
        return None
    markets = _source1_spot_volume(client, top_n=top_n, exclude=exclude,
                                   block_warning=block_warning, block_caution=block_caution)
    if not markets:
        logger.info("[SPOT_CONTRA] Source 1: no candidates from volume scan")
        return None
    btc_ref = _btc_ref(client)
    metrics = _fetch_24h_metrics(client, list(markets) + [btc_ref])
    if not metrics:
        logger.info("[SPOT_CONTRA] no 24h metrics — skip (prevent blind entry)")
        return None
    btc_move = float((metrics.get(btc_ref) or {}).get("move", 0.0))
    best = None
    for mk in markets:
        m = metrics.get(mk) or {}
        last = float(m.get("last", 0.0) or 0.0)
        move = float(m.get("move", 0.0) or 0.0)
        if last <= 0:
            continue
        rel = move - btc_move                      # excess return vs BTC (relative strength)
        if rel < coin_up_th:                        # must be strong enough vs BTC to qualify for contrarian entry
            continue
        if coin_up_cap > 0 and move > coin_up_cap:  # absolute 24h pump = exclude exit-liquidity trap
            continue
        if best is None or rel > best["_rel"]:
            best = {"market": mk, "price": last, "move": move, "_rel": rel,
                    "direction": "LONG", "confidence": 0.0, "btc_dir": btc_dir,
                    "_source": "CONTRARIAN"}
    if best is None:
        logger.info("[SPOT_CONTRA] 0 contrarian candidates (BTC=%s %.2f%%, none with relative strength ≥%.1f%%)",
                    btc_dir, btc_move, coin_up_th)
        return None
    logger.info("[SPOT_CONTRA] Selected %s LONG move=%.2f%% rel=%.2f%% (btc=%s %.2f%%)",
                best["market"], best["move"], best["_rel"], btc_dir, btc_move)
    return best


def scan_spot_focus_candidates(
    system: Any,
    client: Any,
    *,
    primary_tf: str = "240",
    top_n: int = 10,
    min_conf: float = 0.4,
    exclude: Any = None,
    headroom_gate_pct: float = 0.0,
    guard_score_mode_enabled: bool = False,
    guard_score_threshold: float = 0.0,
    guard_score_total_cap: float = 0.0,
    block_warning: bool = False,
    block_caution: bool = False,
    record: Any = None,
) -> List[Dict]:
    """Prospective-candidate snapshot — diagnose the top-N by turnover with GreenPen and return *all* (for display).
    Unlike select, also shows blocked ones with their reason (mirrors the Bybit GreenPen Scanner).
    When headroom_gate_pct>0, candidates with insufficient overhead room are flagged as "chasing the ceiling" (gate preview).
    When guard_score_mode_enabled, shows the Phase2 final-score (base+modifier) column; when threshold>0, marks below-threshold blocks.
    """
    from app.strategy.greenpen import full_analysis
    from app.strategy.greenpen.pa_detector import OHLCV
    from app.manager.focus_coin_selector import _source3_structural_filter

    # Display path does not block — shows *all* (with warning/caution badges); blocking only happens at entry (select).
    markets = _source1_spot_volume(client, top_n=top_n, exclude=exclude)
    # Exchange warning flags (TTL cache — almost no extra fetch)
    try:
        warn = client.get_market_warnings()
    except Exception:
        warn = {}
    # BTC alignment — once per scan (shared by all candidates, cached)
    _btc_dir = _btc_direction(client) if guard_score_mode_enabled else "NEUTRAL"
    _cfg = getattr(system, "config", None)
    _score_threshold = _entry_score_threshold(_cfg, guard_score_threshold)
    rows: List[Dict] = []
    for market in markets:
        wf = warn.get(market, {}) if isinstance(warn, dict) else {}
        row = {
            "market": market, "price": 0.0, "direction": "—", "pa_pattern": None,
            "trend": "—", "confidence": 0.0, "status": "", "passed": False,
            "headroom_pct": None, "guard_score": None, "legacy_guard_score": None,
            "base_conviction": None, "guard_modifier": None, "final_score": None,
            "warning": bool(wf.get("warning")), "caution": bool(wf.get("caution")),
            "caution_kinds": wf.get("kinds", []),
        }
        try:
            raw = client.get_kline(market, interval=primary_tf, limit=31)
            if raw and len(raw) >= 2:
                raw = raw[:-1]  # Phase2: PA/trend judgment uses the closed candle.
            candles = []
            for r in raw:
                try:
                    candles.append(OHLCV(
                        open=float(r[1]), high=float(r[2]), low=float(r[3]),
                        close=float(r[4]), volume=float(r[5]) if len(r) > 5 else 0,
                        ts=float(r[0]) / 1000 if r[0] else 0,
                    ))
                except (IndexError, TypeError, ValueError):
                    continue
            if len(candles) < 15:
                row["status"] = "데이터부족"
                rows.append(row)
                continue
            gp = full_analysis(candles)
            trend = gp.structure.trend.value
            row["trend"] = trend
            row["price"] = candles[-1].close
            row["atr"] = gp.atr
            row["zones"] = _zser(gp.zones)
            # guard_score G1 (ADX+trend conf) — reuse candles (no fetch), for display
            if guard_score_mode_enabled:
                try:
                    from app.manager.spot_guard_score import compute_guard_score, gs_weights_from_config
                    _gpa = gp.pa_signals[0] if gp.pa_signals else None
                    gsc, _bd = compute_guard_score(
                        [c.high for c in candles], [c.low for c in candles],
                        [c.close for c in candles], gp.structure.confidence,
                        trend=trend,
                        pa_direction=(_gpa.direction.value if _gpa else ""),
                        pa_confidence=(float(_gpa.confidence) if _gpa else 0.0),
                        btc_direction=_btc_dir, price=row["price"],
                        zones=_zser(gp.zones), atr=gp.atr,
                        volumes=[c.volume for c in candles],
                        total_cap=guard_score_total_cap,
                        weights=gs_weights_from_config(getattr(system, "config", None)),
                    )
                    row["legacy_guard_score"] = gsc
                    row["guard_score"] = gsc   # default display = legacy gs8 (no fetch). final overwrites after LONG is confirmed.
                except Exception:
                    pass
            pa = gp.pa_signals[0] if gp.pa_signals else None
            if not pa:
                row["status"] = "신호 없음"
                rows.append(row)
                continue
            direction = pa.direction.value
            conf = float(pa.confidence)
            row["direction"] = direction
            row["pa_pattern"] = pa.pattern.value
            row["confidence"] = round(conf, 3)
            row["primary_sig"] = {
                "pattern": pa.pattern.value,
                "direction": direction,
                "confidence": conf,
                "atr": gp.atr,
            }
            # ★ final entry score (base conviction + guard modifier) — computed **only for confirmed-LONG candidates**.
            #   (Perf: per candidate base+modifier = get_kline 30 calls/2s → full-computing no-signal/SHORT too explodes to top_n×30.
            #    Measured ~300 calls/scan → Scanner stalls/timeouts. Only LONG-signal candidates (usually 0~3) = explosion resolved.)
            if guard_score_mode_enabled and direction == "LONG":
                score_row = _final_entry_score_for(client, market, _cfg, btc_dir=_btc_dir)
                if score_row:
                    row.update(score_row)
                    row["guard_score"] = score_row["final_score"]  # display/gate = final (base+modifier)
            # headroom (% room to the nearest overhead RESISTANCE) — display + gate preview
            overhead = [
                z.price_low for z in gp.zones
                if (z.type.value if hasattr(z.type, "value") else str(z.type)).upper() == "RESISTANCE"
                and z.price_low > row["price"]
            ]
            if overhead and row["price"] > 0:
                row["headroom_pct"] = round((min(overhead) - row["price"]) / row["price"] * 100.0, 2)
            # ADX (same basis as entry adx_entry_gate) — reuse candles (no fetch). matches display + status.
            _adxv = _adx_value(candles)
            row["adx"] = round(_adxv, 1) if _adxv is not None else None
            # Gate (same decision as select) — annotate the block reason
            if direction == "LONG" and trend == "DOWNTREND":
                row["status"] = "역추세 차단"
            elif direction == "SHORT":
                row["status"] = "SHORT(현물 제외)"
            elif conf < 0.3:
                row["status"] = "약함(<0.3)"
            else:
                cand = {"market": market, "confidence": conf, "trend": trend}
                try:
                    s3 = _source3_structural_filter(client, cand, system)
                except Exception:
                    s3 = False
                if not s3:
                    row["status"] = "구조 탈락"
                elif conf < min_conf:
                    row["status"] = f"문턱 미달(<{min_conf:.2f})"
                elif (headroom_gate_pct > 0 and row["headroom_pct"] is not None
                        and row["headroom_pct"] < headroom_gate_pct):
                    # ★ The headroom gate only looks at *overhead resistance being close* (not whether the coin is overextended / at range top).
                    #   → calling a mid-range coin "chasing the ceiling" is an overstatement. Precisely "near resistance" (room = distance to resistance).
                    row["status"] = f"저항 근접(여유 {row['headroom_pct']:.1f}%)"
                elif (_score_threshold > 0 and row["guard_score"] is not None
                        and row["guard_score"] < _score_threshold):
                    row["status"] = f"점수 미달({row['guard_score']:.0f}<{_score_threshold:.0f})"
                else:
                    # ★ [2026-06-19 owner] Display using the same decision as the real entry gate (adx_entry_gate: H1 ADX + breakout exemption).
                    #   Reaching here = only finalists that passed every other gate → few calls (no fetch storm). Replaces _adx_blocks_entry(H4).
                    try:
                        from app.manager.spot_guard_chain import adx_entry_gate as _adx_gate
                        _adx_ok, _adx_why = _adx_gate(client, market, _cfg)
                    except Exception:
                        _adx_ok, _adx_why = True, "adx_gate_err"
                    if not _adx_ok:
                        row["status"] = f"ADX 미달({_adx_why})"
                    else:
                        row["status"] = "✅ 진입 가능"
                        row["passed"] = True
        except Exception as exc:
            logger.warning("[UPBIT_SCAN] %s diagnosis failed: %s", market, exc)
            row["status"] = "오류"
        # ★ Exchange warning — top priority. Investment-warning (block_warning) overrides to block entry; caution is display-only.
        if row["warning"] and block_warning:
            row["status"] = "⛔ 투자유의(차단)"
            row["passed"] = False
        elif row["caution"] and block_caution:
            row["status"] = "⛔ 주의환기(차단)"
            row["passed"] = False
        # ★ [2026-06-21] GateLedger aggregation — which gate this coin hit (observation only, does not affect entry).
        #   The record callback swallows exceptions itself, but wrap once more so it never breaks the scan flow.
        if record is not None:
            try:
                record(market, _status_to_gate(row.get("status", ""), bool(row.get("passed"))),
                       bool(row.get("passed")))
            except Exception:
                pass
        rows.append(row)

    # Enterable first, then by confidence descending
    rows.sort(key=lambda x: (not x["passed"],
                             -float(x.get("guard_score") or 0),
                             -float(x.get("confidence") or 0)))
    return rows


def _parse_exclude(exclude: Any) -> set:
    """Normalize comma/list input into a market set (add KRW- prefix, uppercase)."""
    if not exclude:
        return set()
    items = exclude.split(",") if isinstance(exclude, str) else list(exclude)
    out = set()
    for it in items:
        s = str(it).strip().upper()
        if not s:
            continue
        if not s.startswith("KRW-"):
            s = "KRW-" + s
        out.add(s)
    return out


def _btc_direction(client: Any) -> str:
    """KRW-BTC structural trend → 'UP'/'DOWN'/'NEUTRAL'. For guard_score BTC alignment (once per scan, cached). NEUTRAL on failure."""
    try:
        from app.strategy.greenpen.market_structure import analyze_structure
        from app.strategy.greenpen.pa_detector import OHLCV
        raw = client.get_kline(_btc_ref(client), interval="240", limit=30)
        candles = [OHLCV(open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]))
                   for r in raw if len(r) >= 5]
        if len(candles) < 15:
            return "NEUTRAL"
        t = analyze_structure(candles).trend.value
        return {"UPTREND": "UP", "DOWNTREND": "DOWN"}.get(t, "NEUTRAL")
    except Exception:
        return "NEUTRAL"


def _zser(zones) -> List[dict]:
    """Serialize GreenPen zone objects for guard_score Anchor use."""
    out = []
    for z in (zones or []):
        try:
            out.append({
                "type": z.type.value if hasattr(z.type, "value") else str(z.type),
                "price_low": z.price_low, "price_high": z.price_high,
            })
        except Exception:
            continue
    return out


def _adx_value(candles) -> Optional[float]:
    """candles (OHLCV, oldest-first) → primary ADX. None if insufficient/failed.
    So the scanner display/status and the entry adx_entry_gate use the same basis (reuse candles = no fetch)."""
    try:
        from app.strategy import indicators
        if not candles or len(candles) < 29:   # ADX(14) needs >= 29 bars
            return None
        a = indicators.adx([c.high for c in candles], [c.low for c in candles],
                           [c.close for c in candles])
        if not a:
            return None
        return float(a.get("adx", 0.0) or 0.0)
    except Exception:
        return None


def _adx_blocks_entry(system, adxv: Optional[float]) -> bool:
    """If adx_filter_enabled (default True) and adx < min_adx_entry(17), block entry (=SIDEWAYS junk).
    Same decision as the entry path spot_guard_chain.adx_entry_gate — to match the scanner display. adxv None=fail-open."""
    cfg = getattr(system, "config", None)
    if cfg is None or adxv is None or not getattr(cfg, "adx_filter_enabled", True):
        return False
    return adxv < float(getattr(cfg, "min_adx_entry", 17))


def _guard_score_for(client: Any, market: str, primary_tf: str, total_cap: float,
                     btc_dir: str = "NEUTRAL", weights: dict = None):
    """guard_score for the select gate. primary kline (cached) + full_analysis (structure+PA) → score. None on failure."""
    try:
        from app.strategy.greenpen import full_analysis
        from app.strategy.greenpen.pa_detector import OHLCV
        from app.manager.spot_guard_score import compute_guard_score
        raw = client.get_kline(market, interval=primary_tf, limit=30)
        candles = [OHLCV(open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                         volume=float(r[5]) if len(r) > 5 else 0)
                   for r in raw if len(r) >= 5]
        if len(candles) < 15:
            return None
        gp = full_analysis(candles)
        _pa = gp.pa_signals[0] if gp.pa_signals else None
        gsc, _ = compute_guard_score(
            [c.high for c in candles], [c.low for c in candles], [c.close for c in candles],
            gp.structure.confidence, trend=gp.structure.trend.value,
            pa_direction=(_pa.direction.value if _pa else ""),
            pa_confidence=(float(_pa.confidence) if _pa else 0.0),
            btc_direction=btc_dir, price=candles[-1].close,
            zones=_zser(gp.zones), atr=gp.atr,
            volumes=[c.volume for c in candles], total_cap=total_cap,
            weights=weights,
        )
        return gsc
    except Exception as exc:
        logger.debug("[SPOT_SELECT] guard_score computation failed %s: %s", market, exc)
        return None


def score_timeline(client: Any, market: str, primary_tf: str = "240",
                   count: int = 60, total_cap: float = 80.0,
                   weights: dict = None) -> List[Dict]:
    """Historical per-timestamp guard_score + conf trajectory (for score↔chart consistency checks).
    At each timestamp, recompute trailing-30-candle full_analysis + guard_score exactly as the live scan does.
    BTC direction is also reflected per-timestamp. Returns rows in oldest→newest order."""
    try:
        from app.strategy.greenpen import full_analysis
        from app.strategy.greenpen.pa_detector import OHLCV
        from app.strategy.greenpen.market_structure import analyze_structure
        from app.manager.spot_guard_score import compute_guard_score
    except Exception as exc:
        logger.warning("[SPOT_SELECT] score_timeline import failed: %s", exc)
        return []

    def _mk(raw):
        cs, ts = [], []
        for r in raw:
            if len(r) >= 5:
                cs.append(OHLCV(open=float(r[1]), high=float(r[2]), low=float(r[3]),
                                close=float(r[4]), volume=float(r[5]) if len(r) > 5 else 0))
                ts.append(int(r[0]))
        return cs, ts

    try:
        need = int(count) + 30
        wc, wts = _mk(client.get_kline(market, interval=primary_tf, limit=need))
        if len(wc) < 31:
            return []
        bc, bts = _mk(client.get_kline(_btc_ref(client), interval=primary_tf, limit=need))

        def btc_dir_at(t):
            idx = [i for i, x in enumerate(bts) if x <= t]
            if not idx:
                return "NEUTRAL"
            j = idx[-1]
            win = bc[max(0, j - 29):j + 1]
            if len(win) < 15:
                return "NEUTRAL"
            try:
                s = analyze_structure(win)
                return {"UPTREND": "UP", "DOWNTREND": "DOWN"}.get(s.trend.value, "NEUTRAL")
            except Exception:
                return "NEUTRAL"

        rows: List[Dict] = []
        for i in range(30, len(wc)):
            win = wc[i - 30:i]
            try:
                gp = full_analysis(win)
                pa = gp.pa_signals[0] if gp.pa_signals else None
                conf = float(pa.confidence) if pa else 0.0
                gsc, _ = compute_guard_score(
                    [c.high for c in win], [c.low for c in win], [c.close for c in win],
                    gp.structure.confidence, trend=gp.structure.trend.value,
                    pa_direction=(pa.direction.value if pa else ""),
                    pa_confidence=conf, btc_direction=btc_dir_at(wts[i - 1]),
                    price=win[-1].close, zones=_zser(gp.zones), atr=gp.atr,
                    volumes=[c.volume for c in win], total_cap=total_cap,
                    weights=weights,
                )
                rows.append({
                    "ts": wts[i - 1], "close": round(win[-1].close, 8),
                    "trend": gp.structure.trend.value, "conf": round(conf, 3),
                    "guard_score": gsc, "pa": (pa.pattern.value if pa else None),
                })
            except Exception:
                continue
        return rows
    except Exception as exc:
        logger.warning("[SPOT_SELECT] score_timeline %s failed: %s", market, exc)
        return []


def _fetch_24h_metrics(client: Any, markets: List[str]) -> Dict[str, Dict]:
    """Batch-fetch the final candidates' 24H metrics (last/hi/lo/move%) at once — for the overext gate.
    Failure/missing → empty dict → the gate passes as no_data (fail-open)."""
    out: Dict[str, Dict] = {}
    codes = [m for m in (markets or []) if m]
    if not codes:
        return out
    try:
        tickers = client.get_tickers(codes)
    except Exception as exc:
        logger.warning("[SPOT_SELECT] 24H metrics fetch failed (overext skip): %s", exc)
        return out
    for t in (tickers or []):
        if not isinstance(t, dict):
            continue
        mk = str(t.get("market", ""))
        if not mk:
            continue
        out[mk] = {
            "last": float(t.get("trade_price", 0) or 0),
            "hi": float(t.get("high_price", 0) or 0),
            "lo": float(t.get("low_price", 0) or 0),
            "move": float(t.get("signed_change_rate", 0) or 0) * 100.0,
        }
    return out


def _source1_spot_volume(client: Any, top_n: int = 10, exclude: Any = None,
                          block_warning: bool = False, block_caution: bool = False) -> List[str]:
    """Sort Upbit KRW markets by 24h turnover and take the top N. exclude=operationally excluded markets.
    block_warning=True excludes exchange investment-warning coins (delisting risk); block_caution=True also excludes caution-flagged.
    Warning flags are extracted from the get_all_markets(isDetails=true) response — no extra fetch."""
    skip = _parse_exclude(exclude)
    try:
        markets = client.get_all_markets()
        codes = []
        for m in markets:
            mk = m.get("market")
            if not _accepts_market(client, mk) or mk in skip:
                continue
            if block_warning or block_caution:
                try:
                    f = client._parse_market_flags(m)
                    if block_warning and f.get("warning"):
                        logger.info("[SPOT_SELECT] %s investment-warning coin — entry blocked", mk)
                        continue
                    if block_caution and f.get("caution"):
                        logger.info("[SPOT_SELECT] %s caution-flagged (%s) — entry blocked",
                                    mk, ",".join(f.get("kinds", [])))
                        continue
                except Exception:
                    pass
            codes.append(mk)
        if not codes:
            return []

        tickers: List[Dict] = []
        for i in range(0, len(codes), 100):  # batch to avoid URL length limits
            try:
                tickers.extend(client.get_tickers(codes[i:i + 100]))
            except Exception as exc:
                logger.warning("[SPOT_SELECT] ticker batch %d failed: %s", i, exc)

        scored = []
        for t in tickers:
            if not isinstance(t, dict):
                continue
            market = str(t.get("market", ""))
            turnover = float(t.get("acc_trade_price_24h", 0) or 0)   # KRW (Upbit) / USDT (Bybit)
            change = float(t.get("signed_change_rate", 0) or 0)
            if turnover < _min_turnover(client):
                continue
            if abs(change) > _MAX_CHANGE:
                continue
            if abs(change) < _MIN_CHANGE:
                continue
            scored.append((market, turnover, abs(change)))

        scored.sort(key=lambda x: (-x[1], -x[2]))
        return [s[0] for s in scored[:top_n]]
    except Exception as exc:
        logger.warning("[SPOT_SELECT] Source 1 volume scan error: %s", exc)
        return []
