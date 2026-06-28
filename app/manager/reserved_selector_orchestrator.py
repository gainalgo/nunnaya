# ============================================================
# File: app/manager/reserved_selector_orchestrator.py
# Autocoin OS — Main orchestrator function extracted from
# reserved_selector.py (L2985-5051) without logic changes.
# ============================================================

from __future__ import annotations
import logging
import math
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.core.currency import Q

from app.manager.reserved_selector_utils import (
    _sf,
    _si,
    _clamp,
    _norm,
    _currency,
    _csv_upper,
    _global_exclude_bases,
    _global_exclude_markets,
    _chunks,
    _execution_quality_penalty,
    _calc_spread_bps,
    _calc_depth_notional,
    _normalize_market,
    SharedMarketData,
    MarketSnapshot,
    _snapshot_from_ticker_and_ob,
    BYBIT_BASE,
    DEFAULT_TIMEOUT,
)

from app.manager.reserved_selector_fetchers import (
    build_watchlist,
    fetch_candles_minutes,
    _fetch_candles_minutes_cached,
    fetch_highlow_for_lookback,
    fetch_recent_trades_count,
    fetch_markets_details,
    fetch_tickers,
    fetch_orderbooks,
)
import app.manager.reserved_selector_fetchers as _fetchers_mod

from app.manager.reserved_selector_analysis import (
    _extract_ai_features_from_candles,
    _calc_ema_simple,
    _check_ema_cross,
    _calc_rsi_macd_from_candles,
)

from app.manager.reserved_selector_scoring import (
    _score_pingpong,
    _score_ladder,
    _score_lightning,
    _score_sniper,
    _score_gazua,
    _score_autoloop,
    _score_contrarian,
    _score_contrarian_live,
    _score_ladder_ai,
    _score_lightning_ai,
    _score_gazua_ai,
    _ai_score_heuristic,
    _get_market_performance_score,
    _load_market_pnl_cache,
    _calc_sniper_params,
    _confidence_pingpong,
    _confidence_autoloop,
    _confidence_ladder,
    _confidence_lightning,
    _confidence_gazua,
    _confidence_contrarian,
)

from app.manager.reserved_selector_budget import _suggest_budget

from app.monitor.volume_spike_detector import get_volume_spike_detector
from app.monitor.time_volatility_adjuster import get_time_volatility_adjuster
from app.monitor.btc_leading_signal import get_btc_leading_detector
from app.monitor.whale_detector import get_whale_detector

_logger = logging.getLogger(__name__)
logger = _logger  # alias used in some code paths


def build_reserved_candidates(
    system: Any,
    *,
    pingpong_n: int = 5,
    autoloop_n: int = 5,
    ladder_n: int = 0,
    lightning_n: int = 0,
    gazua_n: int = 0,
    contrarian_n: int = 0,
    sniper_n: int = 0,
    scan_top_pingpong: int | None = None,
    scan_top_autoloop: int | None = None,
    force_fill: bool = False,
    shared_data: Optional[SharedMarketData] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (items, summary) for Reserved Queue.

    The function is synchronous and safe to call from an API route.
    It intentionally limits expensive endpoints (orderbook/trades).
    """

    # -------------------------
    # configuration defaults
    # -------------------------
    pp_n = max(0, int(pingpong_n))
    al_n = max(0, int(autoloop_n))
    ld_n = max(0, int(ladder_n))
    lt_n = max(0, int(lightning_n))
    gz_n = max(0, int(gazua_n))
    ct_n = max(0, int(contrarian_n))
    sn_n = max(0, int(sniper_n))

    # Scan universe constraints
    # 0 = full-universe (default): use all USDT markets as the candidate pool
    pp_scan = int(scan_top_pingpong) if scan_top_pingpong is not None else _si(os.getenv("OMA_SELECTOR_PINGPONG_SCAN_TOP", "0"), 0)
    al_scan = int(scan_top_autoloop) if scan_top_autoloop is not None else _si(os.getenv("OMA_SELECTOR_AUTOLOOP_SCAN_TOP", "0"), 0)
    pp_scan = max(0, min(pp_scan, 500))
    al_scan = max(0, min(al_scan, 500))

    # Reserved Settings: shared candidate price-band filter (USDT)
    # [2026-03-03] candidate_price_min_usdt default set to $0.001 (exclude low-price coins)
    _price_min_default = "0.001" if Q.is_usdt else "100"
    candidate_price_min_usdt = _sf(getattr(system, "reserved_candidate_price_min_usdt", 0.0) or 0.0, 0.0)
    if candidate_price_min_usdt <= 0:
        candidate_price_min_usdt = _sf(os.getenv("OMA_SELECTOR_CANDIDATE_PRICE_MIN_USDT", _price_min_default), float(_price_min_default))
    candidate_price_max_usdt = _sf(getattr(system, "reserved_candidate_price_max_usdt", 0.0) or 0.0, 0.0)
    if candidate_price_min_usdt > 0 and candidate_price_max_usdt > 0 and candidate_price_max_usdt < candidate_price_min_usdt:
        candidate_price_min_usdt, candidate_price_max_usdt = candidate_price_max_usdt, candidate_price_min_usdt

    # Eligibility thresholds (USDT scale vs legacy scale)
    exclude_caution = os.getenv("OMA_SELECTOR_EXCLUDE_CAUTION", "1").strip() != "0"
    exclude_delisting = os.getenv("OMA_SELECTOR_EXCLUDE_DELISTING", "1").strip() != "0"
    _pp_vol_default = "500000" if Q.is_usdt else "1000000000"
    _al_vol_default = "200000" if Q.is_usdt else "500000000"
    min_vol24_pp = _sf(os.getenv("OMA_SELECTOR_PINGPONG_MIN_VOL24_USDT", _pp_vol_default), float(_pp_vol_default))
    min_vol24_al = _sf(os.getenv("OMA_SELECTOR_AUTOLOOP_MIN_VOL24_USDT", _al_vol_default), float(_al_vol_default))

    # Trade activity (optional)
    recent_minutes = _si(os.getenv("OMA_SELECTOR_RECENT_TRADE_MINUTES", "5"), 5)
    min_recent_trades_pp = _si(os.getenv("OMA_SELECTOR_PINGPONG_MIN_RECENT_TRADES", "0"), 0)
    tradecheck_top = _si(os.getenv("OMA_SELECTOR_PINGPONG_TRADECHECK_TOP", "12"), 12)
    tradecheck_top = max(0, min(tradecheck_top, 30))

    # Budget suggestion
    min_order_usdt = float(system.effective_min_order_usdt())
    pp_base = _sf(os.getenv("OMA_SELECTOR_PINGPONG_BASE_USDT", "100"), 100.0)
    al_base = _sf(os.getenv("OMA_SELECTOR_AUTOLOOP_BASE_USDT", "120"), 120.0)
    ld_base = _sf(os.getenv("OMA_SELECTOR_LADDER_BASE_USDT", "150"), 150.0)
    lt_base = _sf(os.getenv("OMA_SELECTOR_LIGHTNING_BASE_USDT", "120"), 120.0)
    gz_base = _sf(os.getenv("OMA_SELECTOR_GAZUA_BASE_USDT", "150"), 150.0)
    ct_base = _sf(os.getenv("OMA_SELECTOR_CONTRARIAN_BASE_USDT", "50"), 50.0)
    sn_base = _sf(os.getenv("OMA_SELECTOR_SNIPER_BASE_USDT", "100"), 100.0)

    sn_max = _sf(os.getenv("OMA_SELECTOR_SNIPER_MAX_USDT", "200"), 200.0)
    pp_max = _sf(os.getenv("OMA_SELECTOR_PINGPONG_MAX_USDT", "250"), 250.0)
    al_max = _sf(os.getenv("OMA_SELECTOR_AUTOLOOP_MAX_USDT", "300"), 300.0)
    ld_max = _sf(os.getenv("OMA_SELECTOR_LADDER_MAX_USDT", "400"), 400.0)
    lt_max = _sf(os.getenv("OMA_SELECTOR_LIGHTNING_MAX_USDT", "250"), 250.0)
    gz_max = _sf(os.getenv("OMA_SELECTOR_GAZUA_MAX_USDT", "300"), 300.0)
    ct_max = _sf(os.getenv("OMA_SELECTOR_CONTRARIAN_MAX_USDT", "100"), 100.0)

    # -------------------------
    # [2026-02-01] Load PnL cache for historical-performance-based score adjustment
    # -------------------------
    pnl_cache: Dict[str, Dict[str, Any]] = {}
    use_pnl_history = os.getenv("OMA_SELECTOR_USE_PNL_HISTORY", "1").strip() != "0"
    if use_pnl_history and system:
        pnl_cache = _load_market_pnl_cache(system)

    # -------------------------
    # Dynamic budget allocation: look up total capital
    # -------------------------
    total_capital_usdt = 0.0
    existing_markets_count = 0
    dynamic_budget_enabled = bool(os.getenv("OMA_SELECTOR_DYNAMIC_BUDGET", "1").strip() != "0")

    if dynamic_budget_enabled and system:
        try:
            # equity * deploy_ratio
            equity = _sf(getattr(system, "_last_equity_usdt", 0.0), 0.0)
            if equity <= 0:
                equity = _sf(getattr(system, "equity", 0.0), 0.0)
            # fallback: use cash
            if equity <= 0:
                equity = _sf(getattr(system, "_last_cash_usdt", 0.0), 0.0)
            deploy_ratio = _sf(getattr(system, "deploy_ratio", 1.0), 1.0)
            total_capital_usdt = equity * deploy_ratio

            _logger.info(
                f"[reserved_selector] equity={equity:.0f}, deploy_ratio={deploy_ratio:.2f}, total_capital_usdt={total_capital_usdt:.0f}"
            )

            # Count of existing active markets
            try:
                oma = getattr(system, "oma_registry", None)
                if oma and hasattr(oma, "snapshot"):
                    snap = oma.snapshot()
                    existing_markets_count = len(snap.get("active") or [])
            except (KeyError, AttributeError, TypeError):
                logger.warning("[Selector] existing_markets_count lookup failed", exc_info=True)
                existing_markets_count = 0
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Selector] total_capital_usdt calculation failed", exc_info=True)
            total_capital_usdt = 0.0

    # Reuse entry guard parameters (single source of truth)
    entry_qty_guard_on = bool(getattr(system, "entry_qty_guard", False))
    entry_max_qty = _sf(getattr(system, "entry_max_qty", 0.0) or 0.0, 0.0)

    entry_ob_max_spread_bps = _sf(getattr(system, "entry_ob_max_spread_bps", 0.0) or 0.0, 0.0)
    entry_ob_depth_bps = _sf(getattr(system, "entry_ob_depth_bps", 0.0) or 0.0, 0.0)
    entry_ob_depth_factor = _sf(getattr(system, "entry_ob_depth_factor", 0.0) or 0.0, 0.0)

    # [2026-03-08] Separate spread threshold for recommendation selection.
    # The buy-entry guard (entry_ob_max_spread_bps) is applied at actual order time, so here
    # the recommendation candidate pool should be kept wide. Default 80bps (separate from the 25bps entry guard)
    _selector_spread_bps = _sf(
        os.getenv("OMA_SELECTOR_MAX_SPREAD_BPS", "0"), 0.0
    )
    if _selector_spread_bps <= 0:
        _selector_spread_bps = max(80.0, float(entry_ob_max_spread_bps) * 3.0) if entry_ob_max_spread_bps > 0 else 80.0

    # Autoloop spread/depth defaults are slightly looser.
    al_max_spread_bps = _sf(os.getenv("OMA_SELECTOR_AUTOLOOP_MAX_SPREAD_BPS", "0"), 0.0)
    if al_max_spread_bps <= 0:
        al_max_spread_bps = _selector_spread_bps * 1.2
    al_depth_factor = _sf(os.getenv("OMA_SELECTOR_AUTOLOOP_DEPTH_FACTOR", "0"), 0.0)
    if al_depth_factor <= 0 and entry_ob_depth_factor > 0:
        al_depth_factor = max(1.0, float(entry_ob_depth_factor) * 0.6)

    skip_currencies = []
    try:
        skip_currencies = list(getattr(system, "skip_currencies", []) or [])
    except (KeyError, AttributeError, TypeError):
        logger.warning("[Selector] skip_currencies load failed", exc_info=True)
        skip_currencies = []
    skip_currencies = [str(x).upper() for x in skip_currencies if str(x).strip()]
    global_exclude_bases = _global_exclude_bases()
    global_exclude_markets = _global_exclude_markets()
    if global_exclude_bases:
        skip_currencies = sorted(set(skip_currencies) | set(global_exclude_bases))

    # SNIPER default excluded base coins (default: BTC)
    sniper_exclude_bases_raw = os.getenv("OMA_SNIPER_EXCLUDE_BASES", "BTC")
    sniper_exclude_bases = {x.strip().upper() for x in sniper_exclude_bases_raw.split(",") if x.strip()}

    # Existing tracked markets → exclude (prevent duplicate suggestions)
    #
    # IMPORTANT:
    # - For price-subscription/basic-management purposes, OMA may fill WATCH with "all USDT markets"
    #   during the boot/periodic loop. (i.e., the WATCH list may equal the entire market)
    # - If the Reserved candidate scanner excludes even WATCH entirely, universe_filtered=0 and
    #   candidates would never be produced.
    #
    # Default policy:
    # - Exclude only ACTIVE / RECOVERY (prevent duplicate suggestions)
    # - WATCH exclusion is performed only when the option (OMA_SELECTOR_EXCLUDE_WATCH=1) is set
    exclude_watch = os.getenv("OMA_SELECTOR_EXCLUDE_WATCH", "0").strip().lower() in ("1", "true", "yes", "y", "on")

    existing_active: set[str] = set()
    existing_recovery: set[str] = set()
    existing_watch: set[str] = set()

    try:
        snap = system.oma.snapshot() if hasattr(system, "oma") else system.oma_registry.snapshot()

        for row in (snap.get("active") or []):
            if isinstance(row, dict):
                existing_active.add(_norm(row.get("market")))
            elif isinstance(row, str):
                existing_active.add(_norm(row))

        for row in (snap.get("recovery") or []):
            if isinstance(row, dict):
                existing_recovery.add(_norm(row.get("market")))
            elif isinstance(row, str):
                existing_recovery.add(_norm(row))

        if exclude_watch:
            for row in (snap.get("watch") or []):
                if isinstance(row, dict):
                    existing_watch.add(_norm(row.get("market")))
                elif isinstance(row, str):
                    existing_watch.add(_norm(row))
    except (KeyError, AttributeError, TypeError) as _ew_err:
        logger.warning("[Selector] existing markets load failed: %s", _ew_err)

    existing = set(existing_active) | set(existing_recovery) | (set(existing_watch) if exclude_watch else set())

    # -------------------------
    # Autopilot cooldown exclusion
    # - Purpose: prevent a loop where a just-evicted (demoted) coin is immediately re-picked as a candidate
    # - Use it if HyperSystem provides it; otherwise (older version) ignore.
    cooldown_markets: set[str] = set()
    try:
        fn = getattr(system, 'get_autopilot_cooldown_markets', None)
        if callable(fn):
            cooldown_markets = set(fn())
        else:
            cd = getattr(system, 'autopilot_cooldown', {}) or {}
            if isinstance(cd, dict):
                now_ts = time.time()
                for mk, v in cd.items():
                    try:
                        until_ts = float((v or {}).get('until_ts') or 0.0) if isinstance(v, dict) else float(v or 0.0)
                    except (KeyError, AttributeError, TypeError, ValueError):
                        logger.warning("[Selector] cooldown until_ts parse failed for %s", mk, exc_info=True)
                        until_ts = 0.0
                    if until_ts and until_ts > now_ts:
                        cooldown_markets.add(_norm(mk))
    except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
        logger.warning("[Selector] cooldown markets load failed", exc_info=True)
        cooldown_markets = set()

    # -------------------------
    # Fetch market universe (or reuse shared_data from build_watchlist)
    # -------------------------
    _watchlist_ttl = max(30.0, _sf(os.getenv("OMA_WATCHLIST_TTL_SEC", "120"), 120.0))
    _sd = shared_data if (shared_data and shared_data.is_valid(_watchlist_ttl)) else None
    if _sd is None:
        _sd = SharedMarketData.get_or_refresh(system, ttl_sec=_watchlist_ttl)
    if _sd is None:
        _sd = build_watchlist(system, entry_ob_depth_bps=entry_ob_depth_bps)

    names_map = _sd.names_map
    caution_map = _sd.caution_map
    delisting_map = _sd.delisting_map
    delisting_date_map = _sd.delisting_date_map
    tmap = dict(_sd.tmap)
    obmap = dict(_sd.obmap)
    smap: Dict[str, MarketSnapshot] = dict(_sd.smap)
    universe = list(_sd.universe)

    def vol24(m: str) -> float:
        t = tmap.get(m) or {}
        return _sf(t.get("acc_trade_price_24h"), 0.0)

    ranked_by_vol = list(_sd.ranked_by_vol)
    sess = requests.Session()

    pp_scan_list = ranked_by_vol if pp_scan <= 0 else ranked_by_vol[:pp_scan]
    al_scan_list = ranked_by_vol if al_scan <= 0 else ranked_by_vol[:al_scan]
    scan_union = sorted(set(pp_scan_list + al_scan_list))

    # -------------------------
    # PINGPONG: eligibility filter (cheap)
    # -------------------------
    pp_pre: List[MarketSnapshot] = []
    pp_drop: Dict[str, int] = {}

    for m in pp_scan_list:
        s = smap.get(m)
        if s is None:
            pp_drop["no_snapshot"] = pp_drop.get("no_snapshot", 0) + 1
            continue
        if s.vol24_usdt < float(min_vol24_pp):
            pp_drop["min_vol24"] = pp_drop.get("min_vol24", 0) + 1
            continue
        if _selector_spread_bps > 0 and s.spread_bps > float(_selector_spread_bps):
            pp_drop["spread"] = pp_drop.get("spread", 0) + 1
            continue
        if entry_ob_depth_bps > 0 and entry_ob_depth_factor > 0:
            req = float(pp_base) * float(entry_ob_depth_factor)
            if min(s.depth_ask_usdt, s.depth_bid_usdt) < req:
                pp_drop["depth"] = pp_drop.get("depth", 0) + 1
                continue
        if entry_qty_guard_on and entry_max_qty > 0:
            min_price = float(pp_base) / float(entry_max_qty)
            if s.price < min_price:
                pp_drop["qty_guard"] = pp_drop.get("qty_guard", 0) + 1
                continue
        pp_pre.append(s)

    # Optional: trade-activity check for top K candidates
    pp_pre.sort(key=lambda x: x.vol24_usdt, reverse=True)
    pp_trade_checked: List[MarketSnapshot] = []
    pp_trade_skipped = 0

    for i, s in enumerate(pp_pre):
        if tradecheck_top <= 0 or i >= tradecheck_top:
            pp_trade_skipped += 1
            pp_trade_checked.append(s)
            continue
        try:
            c = fetch_recent_trades_count(sess, s.market, minutes=recent_minutes)
            s.recent_trades = int(c)
        except (TypeError, ValueError):
            logger.warning("[Selector] recent_trades parse failed for %s", s.market, exc_info=True)
            s.recent_trades = None
        pp_trade_checked.append(s)

    # enforce min_recent_trades if configured
    pp_final_pool: List[MarketSnapshot] = []
    if min_recent_trades_pp > 0:
        for s in pp_trade_checked:
            if (s.recent_trades or 0) >= int(min_recent_trades_pp):
                pp_final_pool.append(s)
            else:
                pp_drop["recent_trades"] = pp_drop.get("recent_trades", 0) + 1
    else:
        pp_final_pool = list(pp_trade_checked)

    # ---------------------------------------------------------
    # [2026-03-08] Shared data pool: initialized before PP/AL scoring
    # Collect candles/indicators once for all coins in scan_union, and
    # all strategies reference the same data.
    # =========================================================
    _FALLBACK_AI = {"trend": 0.0, "momentum": 0.0, "volatility": 0.0, "volume_surge": 0.0, "data_valid": False}
    _FALLBACK_RSI = {
        "rsi": 50.0, "macd_line": 0.0, "macd_signal": 0.0,
        "macd_histogram": 0.0, "macd_trend": "neutral", "change_24h": 0.0, "data_valid": False
    }

    # Shared cache: referenced by all strategies
    ai_features_cache: Dict[str, Dict[str, float]] = {}
    ld_lt_gz_rsi_macd_cache: Dict[str, Dict[str, Any]] = {}

    # Fetch candles for all of scan_union ordered by trade value (cache TTL 120s to minimize API load)
    from app.core.technical_indicators import compute_indicators as _compute_ti
    _pool_targets = sorted(
        [m for m in scan_union if smap.get(m)],
        key=lambda m: smap[m].vol24_usdt,
        reverse=True,
    )
    for m in _pool_targets:
        try:
            candles = _fetch_candles_minutes_cached(sess, m, unit=15, count=30)
            ai_features_cache[m] = _extract_ai_features_from_candles(candles)
            ld_lt_gz_rsi_macd_cache[m] = _calc_rsi_macd_from_candles(candles)
            if candles and m in smap:
                ti = _compute_ti(m, candles)
                smap[m].atr_pct = ti.get("atr_pct", 0.0)
                smap[m].bb_width_pct = ti.get("bb_width_pct", 0.0)
                smap[m].bb_upper = ti.get("bb_upper", 0.0)
                smap[m].bb_middle = ti.get("bb_middle", 0.0)
                smap[m].bb_lower = ti.get("bb_lower", 0.0)
        except (KeyError, AttributeError, TypeError):
            logger.warning("[Selector] AI features/RSI extraction failed for %s", m, exc_info=True)
            ai_features_cache[m] = dict(_FALLBACK_AI)
            ld_lt_gz_rsi_macd_cache[m] = dict(_FALLBACK_RSI)

    _pool_valid_count = sum(1 for v in ai_features_cache.values() if v.get("data_valid"))
    _pool_total_count = len(_pool_targets)
    _logger.info(
        f"[shared_data_pool] collected {_pool_valid_count}/{_pool_total_count} "
        f"coins with valid candle data (cache_ttl=120s)"
    )

    _fetchers_mod._last_prefetch_markets = list(_pool_targets)

    # score + rank (apply historical performance + multi-stage confidence)
    pp_scored: List[Tuple[float, MarketSnapshot]] = []
    for s in pp_final_pool:
        base_score = _score_pingpong(s)
        perf_adj = _get_market_performance_score(s.market, "PINGPONG", pnl_cache)
        final_score = base_score + (perf_adj * 5.0)
        # [2026-03-08] Compute and store multi-stage confidence
        _pp_ai = ai_features_cache.get(s.market, dict(_FALLBACK_AI))
        _pp_rsi = ld_lt_gz_rsi_macd_cache.get(s.market, dict(_FALLBACK_RSI))
        _pp_conf = _confidence_pingpong(s, _pp_ai, _pp_rsi)
        setattr(s, "_confidence", _pp_conf)
        # Prioritize data_valid: keep coins with valid data ranked higher
        if not _pp_ai.get("data_valid", False):
            final_score -= 50.0  # rank invalid coins lower
        pp_scored.append((final_score, s))
    pp_scored.sort(key=lambda x: x[0], reverse=True)

    # budget suggestion uses median vol of pool
    vols_pp = sorted([x[1].vol24_usdt for x in pp_scored if x[1].vol24_usdt > 0])
    vol_med_pp = vols_pp[len(vols_pp) // 2] if vols_pp else 0.0

    picked_pp: List[MarketSnapshot] = []
    used_markets: set[str] = set()
    if pp_n > 0:
        for score, s in pp_scored:
            b = _suggest_budget(
                strategy="PINGPONG",
                base_usdt=float(pp_base),
                vol24_usdt=float(s.vol24_usdt),
                vol_median_usdt=float(vol_med_pp),
                min_order_usdt=float(min_order_usdt),
                max_budget_usdt=float(pp_max),
                price=float(s.price),
                entry_qty_guard_on=bool(entry_qty_guard_on),
                entry_max_qty=float(entry_max_qty),
                depth_factor=float(entry_ob_depth_factor),
                depth_ask_usdt=float(s.depth_ask_usdt),
                depth_bid_usdt=float(s.depth_bid_usdt),
                # dynamic budget allocation
                total_capital_usdt=float(total_capital_usdt),
                existing_markets_count=int(existing_markets_count + len(used_markets)),
                spread_bps=float(s.spread_bps),
                range_ratio_24h=float(s.range_ratio_24h),
                trend_score=0.0,  # [2026-02-03] PINGPONG is trend-agnostic (mean-reversion strategy)
            )
            if b is None:
                pp_drop["budget"] = pp_drop.get("budget", 0) + 1
                continue
            setattr(s, "_suggested_budget", float(b))
            picked_pp.append(s)
            used_markets.add(s.market)
            if len(picked_pp) >= pp_n:
                break

    # -------------------------
    # AUTOLOOP: eligibility
    # -------------------------
    al_pre: List[MarketSnapshot] = []
    al_drop: Dict[str, int] = {}

    for m in al_scan_list:
        if m in used_markets:
            continue
        s = smap.get(m)
        if s is None:
            al_drop["no_snapshot"] = al_drop.get("no_snapshot", 0) + 1
            continue
        if s.vol24_usdt < float(min_vol24_al):
            al_drop["min_vol24"] = al_drop.get("min_vol24", 0) + 1
            continue
        if al_max_spread_bps > 0 and s.spread_bps > float(al_max_spread_bps):
            al_drop["spread"] = al_drop.get("spread", 0) + 1
            continue
        # depth requirement is intentionally looser; if no orderbook, allow.
        if al_depth_factor > 0 and entry_ob_depth_bps > 0 and s.depth_ask_usdt > 0 and s.depth_bid_usdt > 0:
            req = float(al_base) * float(al_depth_factor)
            if min(s.depth_ask_usdt, s.depth_bid_usdt) < req:
                al_drop["depth"] = al_drop.get("depth", 0) + 1
                continue
        al_pre.append(s)

    # [2026-02-01] AUTOLOOP also applies historical performance
    al_scored: List[Tuple[float, MarketSnapshot]] = []
    for s in al_pre:
        _al_ai = ai_features_cache.get(s.market, dict(_FALLBACK_AI))
        _al_rsi = ld_lt_gz_rsi_macd_cache.get(s.market, dict(_FALLBACK_RSI))
        base_score = _score_autoloop(s, rsi_macd=_al_rsi)
        perf_adj = _get_market_performance_score(s.market, "AUTOLOOP", pnl_cache)
        final_score = base_score + (perf_adj * 5.0)
        # [2026-03-08] Compute and store multi-stage confidence
        _al_conf = _confidence_autoloop(s, _al_ai, _al_rsi)
        setattr(s, "_confidence", _al_conf)
        if not _al_ai.get("data_valid", False):
            final_score -= 50.0
        al_scored.append((final_score, s))
    al_scored.sort(key=lambda x: x[0], reverse=True)

    vols_al = sorted([x[1].vol24_usdt for x in al_scored if x[1].vol24_usdt > 0])
    vol_med_al = vols_al[len(vols_al) // 2] if vols_al else 0.0

    picked_al: List[MarketSnapshot] = []
    if al_n > 0:
        for score, s in al_scored:
            b = _suggest_budget(
                strategy="AUTOLOOP",
                base_usdt=float(al_base),
                vol24_usdt=float(s.vol24_usdt),
                vol_median_usdt=float(vol_med_al),
                min_order_usdt=float(min_order_usdt),
                max_budget_usdt=float(al_max),
                price=float(s.price),
                entry_qty_guard_on=False,  # AUTOLOOP: relaxed qty_guard
                entry_max_qty=float(entry_max_qty),
                depth_factor=float(al_depth_factor),
                depth_ask_usdt=float(s.depth_ask_usdt),
                depth_bid_usdt=float(s.depth_bid_usdt),
                # dynamic budget allocation
                total_capital_usdt=float(total_capital_usdt),
                existing_markets_count=int(existing_markets_count + len(used_markets) + len(picked_pp)),
                spread_bps=float(s.spread_bps),
                range_ratio_24h=float(s.range_ratio_24h),
                trend_score=0.0,  # AUTOLOOP is trend-agnostic (scaled buy/sell)
            )
            if b is None:
                al_drop["budget"] = al_drop.get("budget", 0) + 1
                continue
            setattr(s, "_suggested_budget", float(b))
            picked_al.append(s)
            used_markets.add(s.market)  # add AUTOLOOP to used_markets too
            if len(picked_al) >= al_n:
                break

    # Attach shared pool data to the picked PP/AL coins as well
    for s in picked_pp + picked_al:
        setattr(s, "_rsi_macd", ld_lt_gz_rsi_macd_cache.get(s.market, dict(_FALLBACK_RSI)))
        setattr(s, "_ai_features", ai_features_cache.get(s.market, dict(_FALLBACK_AI)))

    # -------------------------
    # LADDER / LIGHTNING / GAZUA (AI-Enhanced Selection)
    # -------------------------
    remaining = [m for m in scan_union if (m not in used_markets)]
    picked_ld: List[MarketSnapshot] = []
    picked_lt: List[MarketSnapshot] = []
    picked_gz: List[MarketSnapshot] = []

    def _pick_ai_enhanced(
        strategy: str,
        n: int,
        base_usdt: float,
        max_usdt: float,
        max_spread_bps: float,
        depth_factor: float,
        score_fn_ai
    ):
        """Select per-strategy candidates based on AI features."""
        pool: List[MarketSnapshot] = []
        drops: Dict[str, int] = {}
        for m in remaining:
            if m in used_markets:
                continue
            s = smap.get(m)
            if s is None:
                drops["no_snapshot"] = drops.get("no_snapshot", 0) + 1
                continue
            if max_spread_bps > 0 and s.spread_bps > float(max_spread_bps):
                drops["spread"] = drops.get("spread", 0) + 1
                continue
            if entry_ob_depth_bps > 0 and depth_factor > 0:
                req = float(base_usdt) * float(depth_factor)
                if min(s.depth_ask_usdt, s.depth_bid_usdt) < req:
                    drops["depth"] = drops.get("depth", 0) + 1
                    continue
            pool.append(s)

        # AI-feature-based scoring — sort with data_valid first
        scored: List[Tuple[float, MarketSnapshot, Dict[str, float]]] = []
        scored_invalid: List[Tuple[float, MarketSnapshot, Dict[str, float]]] = []
        for s in pool:
            ai_feat = ai_features_cache.get(s.market, dict(_FALLBACK_AI))
            ai_heur = _ai_score_heuristic(s)

            _rsi_data = ld_lt_gz_rsi_macd_cache.get(s.market, dict(_FALLBACK_RSI))
            if strategy == "LADDER":
                sc = _score_ladder_ai(s, ai_feat)
                _conf = _confidence_ladder(s, ai_feat, _rsi_data)
            elif strategy == "LIGHTNING":
                _lt_t = tmap.get(s.market) or {}
                _lt_chg = float(_lt_t.get("signed_change_rate") or 0.0) * 100.0
                sc = _score_lightning_ai(s, ai_feat, price_change_pct=_lt_chg)
                _conf = _confidence_lightning(s, ai_feat, _rsi_data)
            elif strategy == "GAZUA":
                _btc_t = tmap.get("BTCUSDT") or {}
                _btc_ret = float(_btc_t.get("signed_change_rate") or 0.0) * 100.0
                _coin_t = tmap.get(s.market) or {}
                _coin_ret = float(_coin_t.get("signed_change_rate") or 0.0) * 100.0
                sc = _score_gazua_ai(s, ai_feat, ai_heur, coin_ret_24h=_coin_ret, btc_ret_24h=_btc_ret)
                _conf = _confidence_gazua(s, ai_feat, _rsi_data, coin_ret_24h=_coin_ret, btc_ret_24h=_btc_ret)
            else:
                sc = float(score_fn_ai(s))
                _conf = {"confidence": 0.0, "stages": {}, "stages_passed": 0}

            # [2026-03-08] Store confidence
            setattr(s, "_confidence", _conf)

            # [2026-02-01] Apply historical-performance bonus/penalty
            perf_adj = _get_market_performance_score(s.market, strategy, pnl_cache)
            sc = sc + (perf_adj * 5.0)

            # [2026-02-04] Apply strategy priority based on Market Regime phase
            try:
                from app.manager.regime_strategy import RegimeStrategyManager
                regime_mgr = RegimeStrategyManager()
                mapping = regime_mgr.get_strategy_mapping(market=s.market)
                score_mult = mapping.get_score_multiplier(strategy)
                if score_mult != 1.0:
                    orig_sc = sc
                    sc = sc * score_mult
                    _logger.debug(
                        f"[Regime Strategy] {s.market} ({strategy}): {orig_sc:.2f} → {sc:.2f} "
                        f"(regime={mapping.regime.value}, conf={mapping.confidence:.2f}, mult={score_mult:.2f})"
                    )
            except (AttributeError, TypeError, ValueError) as e:
                _logger.warning(f"Regime strategy scoring failed for {s.market}: {e}", exc_info=True)

            # [2026-02-04] Apply Cross Exchange signal (GAZUA/SNIPER/AUTOLOOP strategies only)
            if strategy in ["GAZUA", "SNIPER", "AUTOLOOP"]:
                try:
                    from app.manager.cross_exchange_signal import get_cross_exchange_signal_provider
                    from app.manager.cross_exchange_scoring import adjust_score_for_cross_exchange

                    signal_provider = get_cross_exchange_signal_provider()
                    coin = s.market.replace("USDT", "")
                    cross_signal = signal_provider.get_signal(coin)

                    if cross_signal:
                        # Get adjusted score (0~1 range)
                        result = adjust_score_for_cross_exchange(sc / 100.0, strategy, coin, cross_signal)
                        adjusted_score = result["adjusted_score"] * 100.0

                        if result["reasons"]:
                            _logger.debug(
                                f"[Cross Exchange] {s.market}: {sc:.2f} → {adjusted_score:.2f} "
                                f"(mult={result['multiplier']:.2f}, reasons={result['reasons'][:3]})"
                            )
                        sc = adjusted_score
                except (OSError, KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
                    _logger.warning(f"Cross Exchange scoring failed for {s.market}: {e}", exc_info=True)

            # [2026-02-04] Volume Spike detection
            # For LADDER, scaled buying / average-cost management takes priority over chasing spikes,
            # so it is excluded from the volume-spike bonus to reduce pump-coin bias.
            if strategy != "LADDER":
                try:
                    volume_detector = get_volume_spike_detector()
                    if volume_detector:
                        orig_sc = sc
                        sc = volume_detector.adjust_score_for_volume_spike(s.market, sc, strategy)
                        if abs(sc - orig_sc) > 0.1:
                            _logger.debug(
                                f"[VolumeSpike] {s.market} ({strategy}): {orig_sc:.2f} → {sc:.2f}"
                            )
                except (TypeError, ValueError) as e:
                    _logger.warning(f"Volume Spike scoring failed for {s.market}: {e}", exc_info=True)

            # [2026-02-04] Apply Time Volatility (per-hour volatility)
            # For LADDER, consistency of price-band scaled entry matters more than chasing
            # time-of-day swings, so it is excluded.
            if strategy != "LADDER":
                try:
                    time_adjuster = get_time_volatility_adjuster()
                    if time_adjuster:
                        orig_sc = sc
                        sc = time_adjuster.adjust_score_for_time(sc, strategy)
                        if abs(sc - orig_sc) > 0.1:
                            time_ctx = time_adjuster.get_time_context()
                            _logger.debug(
                                f"[TimeVolatility] {s.market} ({strategy}): {orig_sc:.2f} → {sc:.2f} "
                                f"(hour={time_ctx['hour']}, mult={time_ctx['volatility_multiplier']:.2f})"
                            )
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    _logger.warning(f"Time Volatility scoring failed for {s.market}: {e}", exc_info=True)

            # [2026-02-04] Apply BTC Leading Signal
            # LADDER is excluded from this bonus to reduce bias toward chasing BTC spikes.
            if strategy != "LADDER":
                try:
                    btc_detector = get_btc_leading_detector()
                    if btc_detector:
                        orig_sc = sc
                        sc = btc_detector.adjust_score_for_btc_signal(sc, strategy)
                        if abs(sc - orig_sc) > 0.1:
                            signal = btc_detector.detect_signal()
                            if signal:
                                _logger.debug(
                                    f"[BTCLeading] {s.market} ({strategy}): {orig_sc:.2f} → {sc:.2f} "
                                    f"(dir={signal.direction}, strength={signal.strength:.2f})"
                                )
                except (TypeError, ValueError) as e:
                    _logger.warning(f"BTC Leading scoring failed for {s.market}: {e}", exc_info=True)

            # [2026-03-08] Data Quality Gate: split by data_valid
            if ai_feat.get("data_valid", False):
                scored.append((sc, s, ai_feat))
            else:
                drops["no_ai_data"] = drops.get("no_ai_data", 0) + 1
                scored_invalid.append((sc, s, ai_feat))

        # PINGPONG: 0.8x penalty on the top-3 volume coins (prevent BTC/ETH/single-coin dominance)
        # The liq cap suppresses the score, but an extra penalty is also applied to relative rank within the pool
        if strategy == "PINGPONG" and scored:
            top3_markets = {
                s.market
                for _, s, _ in sorted(scored, key=lambda x: x[1].vol24_usdt, reverse=True)[:3]
            }
            scored = [
                (sc * 0.8, s, ai_feat) if s.market in top3_markets else (sc, s, ai_feat)
                for sc, s, ai_feat in scored
            ]

        # Sort data_valid coins first, supplement with invalid coins if short
        scored.sort(key=lambda x: x[0], reverse=True)
        scored_invalid.sort(key=lambda x: x[0], reverse=True)
        scored.extend(scored_invalid)  # append invalid after valid

        vols = sorted([x[1].vol24_usdt for x in scored if x[1].vol24_usdt > 0])
        vol_med = vols[len(vols)//2] if vols else 0.0
        ladder_anti_pump_enabled = (
            strategy == "LADDER"
            and str(os.getenv("OMA_SELECTOR_LADDER_ANTI_PUMP_ENABLED", "true")).strip().lower() in ("1", "true", "yes", "on")
        )
        ladder_lookback_min = max(
            1440,
            min(
                60 * 24 * 60,
                int(_sf(os.getenv("OMA_SELECTOR_LADDER_ANTI_PUMP_LOOKBACK_MIN", "28800"), 28800.0)),
            ),
        )
        ladder_near_high_pct = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_NEAR_HIGH_PCT", "4.0"), 4.0),
            0.5,
            25.0,
        )
        ladder_pump_24h_pct = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_PUMP_24H_PCT", "12.0"), 12.0),
            2.0,
            80.0,
        )
        ladder_trend_max = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_TREND_MAX", "0.6"), 0.6),
            0.1,
            5.0,
        )
        ladder_momentum_max = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_MOMENTUM_MAX", "1.2"), 1.2),
            0.1,
            10.0,
        )
        ladder_hot_24h_pct = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_HOT_24H_PCT", "4.0"), 4.0),
            0.5,
            30.0,
        )
        ladder_near_24h_high_pct = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_NEAR_24H_HIGH_PCT", "1.5"), 1.5),
            0.1,
            10.0,
        )
        ladder_trend_entry_max = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_TREND_ENTRY_MAX", "0.25"), 0.25),
            0.05,
            3.0,
        )
        ladder_momentum_entry_max = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_MOMENTUM_ENTRY_MAX", "0.7"), 0.7),
            0.05,
            5.0,
        )
        ladder_stable_only = (
            strategy == "LADDER"
            and str(os.getenv("OMA_SELECTOR_LADDER_STABLE_ONLY", "true")).strip().lower() in ("1", "true", "yes", "on")
        )
        ladder_max_abs_change_24h_pct = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_MAX_ABS_CHANGE_24H_PCT", "8.0"), 8.0),
            1.0,
            40.0,
        )
        ladder_min_range_24h_pct = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_MIN_RANGE_24H_PCT", "1.2"), 1.2),
            0.2,
            20.0,
        )
        ladder_max_range_24h_pct = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_MAX_RANGE_24H_PCT", "15.0"), 15.0),
            2.0,
            60.0,
        )
        ladder_max_abs_trend = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_MAX_ABS_TREND", "0.55"), 0.55),
            0.1,
            5.0,
        )
        ladder_max_abs_momentum = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_MAX_ABS_MOMENTUM", "1.4"), 1.4),
            0.1,
            10.0,
        )
        ladder_max_volume_surge = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_MAX_VOLUME_SURGE", "1.2"), 1.2),
            0.2,
            10.0,
        )
        ladder_min_history_hours = _clamp(
            _sf(os.getenv("OMA_SELECTOR_LADDER_MIN_HISTORY_HOURS", "72"), 72.0),
            0.0,
            24.0 * 60.0,
        )
        ladder_highcheck_max_calls = max(
            0,
            min(
                120,
                int(_sf(os.getenv("OMA_SELECTOR_LADDER_NEAR_HIGH_MAX_CALLS", "36"), 36.0)),
            ),
        )
        ladder_highcheck_calls = 0

        # In a BULL phase, relax some LADDER filters (avoid blocking healthy uptrending coins)
        # Dual check: primary based on tmap (always valid) + secondary based on ai_features_cache
        if ladder_anti_pump_enabled:
            _btc_tmap = tmap.get("BTCUSDT") or {}
            _btc_chg_24h = float(_btc_tmap.get("signed_change_rate") or 0.0) * 100.0
            _btc_ai_trend = float((ai_features_cache.get("BTCUSDT") or {}).get("trend", 0.0))
            _is_bull = _btc_chg_24h > 3.0 or _btc_ai_trend > 2.0
            if _is_bull:
                ladder_near_high_pct = max(ladder_near_high_pct, 7.0)   # 4% → 7%
                ladder_max_abs_change_24h_pct = max(ladder_max_abs_change_24h_pct, 12.0)  # 8% → 12%

        picked: List[MarketSnapshot] = []
        for sc, s, ai_feat in scored:
            if ladder_anti_pump_enabled:
                t = tmap.get(s.market) or {}
                chg24_pct = _sf(t.get("signed_change_rate"), 0.0) * 100.0
                hi24 = _sf(t.get("high_price"), 0.0)
                dist24_from_high_pct = ((float(s.price) / hi24) - 1.0) * 100.0 if hi24 > 0 else -999.0
                trend_now = float(ai_feat.get("trend", 0.0))
                momentum_now = float(ai_feat.get("momentum", 0.0))
                volume_surge_now = float(ai_feat.get("volume_surge", 0.0))
                range24_pct = float(s.range_ratio_24h) * 100.0

                # Immediately exclude from LADDER zones that are a 24h vertical spike + near the 24h high
                if (
                    chg24_pct >= float(ladder_hot_24h_pct)
                    and dist24_from_high_pct >= -float(ladder_near_24h_high_pct)
                ):
                    drops["ladder_hot_near_24h_high"] = drops.get("ladder_hot_near_24h_high", 0) + 1
                    continue

                # Exclude from LADDER entry if uptrend/momentum is too strong
                if trend_now > float(ladder_trend_entry_max) and momentum_now > float(ladder_momentum_entry_max):
                    drops["ladder_uptrend_momentum"] = drops.get("ladder_uptrend_momentum", 0) + 1
                    continue

                # Stability-first mode:
                # - Remove coins with sharp swings / trend / volume explosion from LADDER
                # - Remove one-directional (vertical-spike) coins
                if ladder_stable_only:
                    if abs(chg24_pct) > float(ladder_max_abs_change_24h_pct):
                        drops["ladder_abs_change_24h"] = drops.get("ladder_abs_change_24h", 0) + 1
                        continue
                    if range24_pct < float(ladder_min_range_24h_pct) or range24_pct > float(ladder_max_range_24h_pct):
                        drops["ladder_range_24h"] = drops.get("ladder_range_24h", 0) + 1
                        continue
                    if abs(trend_now) > float(ladder_max_abs_trend):
                        drops["ladder_abs_trend"] = drops.get("ladder_abs_trend", 0) + 1
                        continue
                    if abs(momentum_now) > float(ladder_max_abs_momentum):
                        drops["ladder_abs_momentum"] = drops.get("ladder_abs_momentum", 0) + 1
                        continue
                    if abs(volume_surge_now) > float(ladder_max_volume_surge):
                        drops["ladder_volume_surge"] = drops.get("ladder_volume_surge", 0) + 1
                        continue
                    # Even when rising, remove one-way ascents without oscillation (round trips)
                    if chg24_pct > 0 and range24_pct < max(0.8, abs(chg24_pct) * 0.55):
                        drops["ladder_oneway_up"] = drops.get("ladder_oneway_up", 0) + 1
                        continue

                # Immediately exclude from LADDER zones that are a surge + strengthening uptrend
                if (
                    chg24_pct >= float(ladder_pump_24h_pct)
                    and (trend_now >= float(ladder_trend_max) or momentum_now >= float(ladder_momentum_max))
                ):
                    drops["ladder_pump_24h"] = drops.get("ladder_pump_24h", 0) + 1
                    continue

                # Additionally block near-high zones based on the 20-day (default) high
                needs_high_check = (
                    (ladder_stable_only and ladder_min_history_hours > 0)
                    or
                    dist24_from_high_pct > -8.0
                    or chg24_pct >= (float(ladder_pump_24h_pct) * 0.6)
                    or trend_now >= float(ladder_trend_max)
                    or momentum_now >= float(ladder_momentum_max)
                )
                if needs_high_check and ladder_highcheck_calls < ladder_highcheck_max_calls:
                    ladder_highcheck_calls += 1
                    highlow = fetch_highlow_for_lookback(sess, s.market, int(ladder_lookback_min))
                    if ladder_stable_only and ladder_min_history_hours > 0:
                        cnt = _sf(highlow.get("candle_count"), 0.0)
                        unit_min = _sf(highlow.get("unit_min"), 0.0)
                        hist_hours = (cnt * unit_min / 60.0) if (cnt > 0 and unit_min > 0) else 0.0
                        # Exclude newly-listed/ultra-short-term pump coins: drop if below minimum history
                        if hist_hours > 0 and hist_hours < float(ladder_min_history_hours):
                            drops["ladder_short_history"] = drops.get("ladder_short_history", 0) + 1
                            continue
                    lb_high = _sf(highlow.get("high"), 0.0)
                    if lb_high > 0:
                        from_high_pct = ((float(s.price) / lb_high) - 1.0) * 100.0
                        if from_high_pct >= -float(ladder_near_high_pct):
                            drops["ladder_near_high"] = drops.get("ladder_near_high", 0) + 1
                            continue

            b = _suggest_budget(
                strategy=strategy,
                base_usdt=float(base_usdt),
                vol24_usdt=float(s.vol24_usdt),
                vol_median_usdt=float(vol_med),
                min_order_usdt=float(min_order_usdt),
                max_budget_usdt=float(max_usdt),
                price=float(s.price),
                entry_qty_guard_on=bool(entry_qty_guard_on),
                entry_max_qty=float(entry_max_qty),
                depth_factor=float(depth_factor),
                depth_ask_usdt=float(s.depth_ask_usdt),
                depth_bid_usdt=float(s.depth_bid_usdt),
                # dynamic budget allocation
                total_capital_usdt=float(total_capital_usdt),
                existing_markets_count=int(existing_markets_count + len(used_markets)),
                spread_bps=float(s.spread_bps),
                range_ratio_24h=float(s.range_ratio_24h),
                trend_score=float(ai_feat.get("trend", 0.0)),  # [2026-02-03] pass AI trend
            )
            if b is None:
                drops["budget"] = drops.get("budget", 0) + 1
                continue
            setattr(s, "_suggested_budget", float(b))
            setattr(s, "_score_override", float(sc))
            setattr(s, "_ai_features", ai_feat)
            # Add RSI/MACD indicators (same info as PINGPONG/AUTOLOOP)
            setattr(s, "_rsi_macd", ld_lt_gz_rsi_macd_cache.get(s.market, dict(_FALLBACK_RSI)))
            picked.append(s)
            used_markets.add(s.market)
            if len(picked) >= int(n):
                break
        return picked, drops

    # LADDER: ICAG v3 — moderate volatility + sideways/mild decline (grid mean-reversion)
    picked_ld: List[MarketSnapshot] = []
    ld_drop: Dict[str, int] = {}
    if ld_n > 0:
        max_sp = float(_selector_spread_bps)
        picked_ld, ld_drop = _pick_ai_enhanced("LADDER", ld_n, ld_base, ld_max, max_sp, float(entry_ob_depth_factor), _score_ladder)
        picked_ld = list(picked_ld)

    # LIGHTNING: strong momentum breakout (burst trading)
    picked_lt: List[MarketSnapshot] = []
    lt_drop: Dict[str, int] = {}
    if lt_n > 0:
        max_sp = float(_selector_spread_bps)
        picked_lt, lt_drop = _pick_ai_enhanced("LIGHTNING", lt_n, lt_base, lt_max, max_sp, float(entry_ob_depth_factor), _score_lightning)
        picked_lt = list(picked_lt)

    # GAZUA: AI upside prediction + long-term potential (swing/hold)
    picked_gz: List[MarketSnapshot] = []
    gz_drop: Dict[str, int] = {}
    if gz_n > 0:
        max_sp = float(_selector_spread_bps) * 0.9
        picked_gz, gz_drop = _pick_ai_enhanced("GAZUA", gz_n, gz_base, gz_max, max_sp, float(entry_ob_depth_factor), _score_gazua)
        picked_gz = list(picked_gz)

    # CONTRARIAN: market-contrarian coins (linked to real-time scanner)
    picked_ct: List[MarketSnapshot] = []
    ct_drop: Dict[str, Any] = {}
    ct_strict_unified = str(os.getenv("OMA_SELECTOR_CONTRARIAN_STRICT_UNIFIED", "true")).strip().lower() == "true"
    ct_relaxed_on_sideways = str(os.getenv("OMA_SELECTOR_CONTRARIAN_RELAXED_ON_SIDEWAYS", "false")).strip().lower() == "true"
    ct_relaxed_min_score = max(1, min(3, int(_sf(os.getenv("OMA_SELECTOR_CONTRARIAN_RELAXED_MIN_SCORE", "1"), 1.0))))
    ct_relaxed_ai_min = _clamp(_sf(os.getenv("OMA_SELECTOR_CONTRARIAN_RELAXED_AI_MIN", "0.62"), 0.62), 0.0, 1.0)
    ct_relaxed_rs_diff_min = max(0.0, _sf(os.getenv("OMA_SELECTOR_CONTRARIAN_RELAXED_RS_DIFF_MIN", "0.15"), 0.15))
    ct_relaxed_budget_factor = _clamp(_sf(os.getenv("OMA_SELECTOR_CONTRARIAN_RELAXED_BUDGET_FACTOR", "0.6"), 0.6), 0.1, 1.0)
    ct_benchmark_valid = ("BTC", "MARKET_AVG", "ETH", "FEAR_GREED")
    ct_benchmark_order_raw = str(os.getenv("OMA_SELECTOR_CONTRARIAN_BENCHMARK_ORDER", "BTC"))
    ct_benchmark_order: List[str] = []
    for tok in ct_benchmark_order_raw.split(","):
        b = str(tok or "").strip().upper()
        if b in ct_benchmark_valid and b not in ct_benchmark_order:
            ct_benchmark_order.append(b)
    if not ct_benchmark_order:
        ct_benchmark_order = list(ct_benchmark_valid)
    if ct_strict_unified:
        # Fix to the same criteria as the execution/notification paths to keep signal consistency.
        ct_relaxed_on_sideways = False
        ct_benchmark_order = ["BTC"]

    if ct_n > 0:
        try:
            from app.core.contrarian_scanner import get_contrarian_scanner
            scanner = get_contrarian_scanner()

            # universe is List[str] (list of market codes)
            scanner.set_markets(universe)
            ct_attempts: List[Dict[str, Any]] = []

            def _collect_ct_candidates(
                scan_result: Any,
                *,
                benchmark: str,
                phase: str,
                allow_relaxed: bool,
            ) -> List[Tuple[float, MarketSnapshot]]:
                candidate_map: Dict[str, Dict[str, Any]] = {}
                local_drop: Dict[str, int] = {}

                if not scan_result.candidates:
                    ct_drop[f"{phase}:{benchmark}"] = "no_candidates"
                    return []

                relaxed_mode = bool(allow_relaxed) and (not bool(scan_result.market_down)) and ct_relaxed_on_sideways
                # [2026-02-23] If early-detection candidates exist, market_down is not required
                has_early = any(getattr(c, "early_signal", False) for c in scan_result.candidates)
                if (not bool(scan_result.market_down)) and (not relaxed_mode) and (not has_early):
                    ct_drop[f"{phase}:{benchmark}"] = "market_not_down"
                    return []

                required_score = 2 if bool(scan_result.market_down) else ct_relaxed_min_score
                for candidate in scan_result.candidates:
                    candidate_map[candidate.market] = {
                        "score": candidate.score,
                        "rs": candidate.rs,
                        "rs_diff": candidate.rs_diff,
                        "corr": candidate.corr,
                        "coin_ret_pct": candidate.coin_ret_pct,
                        "benchmark_ret_pct": candidate.benchmark_ret_pct,
                        "benchmark_type": benchmark,
                        "benchmark_label": getattr(scan_result, "benchmark_label", benchmark),
                        "volume_ratio": candidate.volume_ratio,
                        "volume_spike": candidate.volume_spike,
                        "ai_score": candidate.ai_score,
                        "tf_score": candidate.tf_score,
                        "mode": "bear" if bool(scan_result.market_down) else ("early" if getattr(candidate, "early_signal", False) else "sideways_relaxed"),
                        "early_signal": bool(getattr(candidate, "early_signal", False)),
                        "rs_momentum": float(getattr(candidate, "rs_momentum", 0.0)),
                        "acceleration": float(getattr(candidate, "acceleration", 0.0)),
                    }

                # Median trade value of the CONTRARIAN candidate pool, used by budget suggestion.
                vols_ct: List[float] = []
                for mk in candidate_map.keys():
                    snap = smap.get(mk)
                    if snap is None:
                        continue
                    v = float(snap.vol24_usdt or 0.0)
                    if v > 0:
                        vols_ct.append(v)
                vols_ct.sort()
                vol_med_ct = vols_ct[len(vols_ct) // 2] if vols_ct else 0.0

                ct_candidates_local: List[Tuple[float, MarketSnapshot]] = []
                for market in universe:
                    if market in used_markets:
                        local_drop["already_used"] = local_drop.get("already_used", 0) + 1
                        continue
                    # The CONTRARIAN benchmark axis (BTC) is always excluded from trading targets
                    if market == Q.market("BTC"):
                        local_drop["btc_benchmark_excluded"] = local_drop.get("btc_benchmark_excluded", 0) + 1
                        continue
                    ct_data = candidate_map.get(market)
                    if not ct_data:
                        continue

                    s = smap.get(market)
                    if s is None:
                        local_drop["no_snapshot"] = local_drop.get("no_snapshot", 0) + 1
                        continue

                    ct_score_val = int(ct_data.get("score", 0) or 0)
                    is_early = bool(ct_data.get("early_signal", False))
                    # [2026-02-23] Early-detection coins pass even with a score of 1
                    effective_required = 1 if is_early else required_score
                    if ct_score_val < effective_required:
                        local_drop["low_score"] = local_drop.get("low_score", 0) + 1
                        continue

                    if relaxed_mode:
                        # When the market is not a clear downtrend, require at least one secondary signal.
                        tf_score_val = int(ct_data.get("tf_score", 0) or 0)
                        ai_score_val = float(ct_data.get("ai_score") or 0.0)
                        rs_diff_val = float(ct_data.get("rs_diff") or 0.0)
                        volume_spike_val = bool(ct_data.get("volume_spike"))
                        if not (volume_spike_val or tf_score_val >= 1 or ai_score_val >= ct_relaxed_ai_min or rs_diff_val >= ct_relaxed_rs_diff_min):
                            local_drop["relaxed_guard_failed"] = local_drop.get("relaxed_guard_failed", 0) + 1
                            continue

                    if _selector_spread_bps > 0 and s.spread_bps > _selector_spread_bps:
                        local_drop["spread"] = local_drop.get("spread", 0) + 1
                        continue

                    _ct_btc_t = tmap.get("BTCUSDT") or {}
                    _ct_btc_ret = float(_ct_btc_t.get("signed_change_rate") or 0.0) * 100.0
                    _ct_coin_t = tmap.get(s.market) or {}
                    _ct_coin_ret = float(_ct_coin_t.get("signed_change_rate") or 0.0) * 100.0
                    _ct_rsi_macd = ld_lt_gz_rsi_macd_cache.get(s.market, dict(_FALLBACK_RSI))
                    final_score = _score_contrarian_live(s, ct_score_val, ct_data, coin_ret_24h=_ct_coin_ret, btc_ret_24h=_ct_btc_ret, rsi_macd=_ct_rsi_macd)
                    # [2026-02-01] Apply historical performance
                    perf_adj = _get_market_performance_score(s.market, "CONTRARIAN", pnl_cache)
                    final_score = final_score + (perf_adj * 5.0)
                    s._score_override = final_score
                    s._contrarian_data = ct_data
                    # Add RSI/MACD info (same as other strategies)
                    setattr(s, "_rsi_macd", ld_lt_gz_rsi_macd_cache.get(s.market, dict(_FALLBACK_RSI)))
                    # [2026-03-08] CONTRARIAN confidence
                    _ct_ai = ai_features_cache.get(s.market, dict(_FALLBACK_AI))
                    _ct_rsi = ld_lt_gz_rsi_macd_cache.get(s.market, dict(_FALLBACK_RSI))
                    setattr(s, "_confidence", _confidence_contrarian(s, _ct_ai, _ct_rsi, contrarian_score=ct_score_val))

                    # [2026-02-03] CONTRARIAN trend-based budget adjustment
                    benchmark_ret = float(ct_data.get("benchmark_ret_pct", 0.0) or 0.0)
                    trend_score_ct = benchmark_ret / 3.0  # scale conversion: -15% → -5.0, -3% → -1.0

                    budget = _suggest_budget(
                        strategy="CONTRARIAN",
                        base_usdt=ct_base,
                        vol24_usdt=s.vol24_usdt,
                        vol_median_usdt=float(vol_med_ct),
                        min_order_usdt=min_order_usdt,
                        max_budget_usdt=ct_max,
                        price=s.price,
                        entry_qty_guard_on=entry_qty_guard_on,
                        entry_max_qty=entry_max_qty,
                        depth_factor=float(entry_ob_depth_factor),
                        depth_ask_usdt=s.depth_ask_usdt,
                        depth_bid_usdt=s.depth_bid_usdt,
                        total_capital_usdt=total_capital_usdt,
                        existing_markets_count=int(existing_markets_count + len(used_markets) + len(picked_ct)),
                        spread_bps=float(s.spread_bps),
                        range_ratio_24h=float(s.range_ratio_24h),
                        trend_score=trend_score_ct,  # [2026-02-03] pass trend based on benchmark return
                    )
                    if budget is None:
                        local_drop["budget"] = local_drop.get("budget", 0) + 1
                        continue
                    if relaxed_mode:
                        budget = max(float(min_order_usdt), float(budget) * float(ct_relaxed_budget_factor))
                    s._suggested_budget = budget

                    ct_candidates_local.append((final_score, s))

                ct_candidates_local.sort(key=lambda x: -x[0])
                if local_drop:
                    ct_drop[f"{phase}:{benchmark}:drop"] = local_drop
                return ct_candidates_local

            phase_plan: List[Tuple[str, bool]] = [("strict", False)]
            if ct_relaxed_on_sideways:
                phase_plan.append(("relaxed", True))

            for phase, allow_relaxed in phase_plan:
                if len(picked_ct) >= ct_n:
                    break
                for benchmark in ct_benchmark_order:
                    if len(picked_ct) >= ct_n:
                        break
                    scan_result = scanner.scan(force=True, benchmark_type=benchmark)
                    ct_attempts.append({
                        "phase": phase,
                        "benchmark": benchmark,
                        "market_down": bool(getattr(scan_result, "market_down", False)),
                        "benchmark_ret_pct": float(getattr(scan_result, "benchmark_ret_pct", 0.0) or 0.0),
                        "candidate_count": int(len(getattr(scan_result, "candidates", []) or [])),
                        "error": getattr(scan_result, "error", None),
                    })
                    if getattr(scan_result, "error", None):
                        ct_drop[f"{phase}:{benchmark}"] = f"scan_error:{scan_result.error}"
                        continue
                    picked_from_pass = _collect_ct_candidates(
                        scan_result,
                        benchmark=benchmark,
                        phase=phase,
                        allow_relaxed=allow_relaxed,
                    )
                    for _, s in picked_from_pass:
                        if s.market in used_markets:
                            continue
                        picked_ct.append(s)
                        used_markets.add(s.market)
                        if len(picked_ct) >= ct_n:
                            break

            ct_drop["_attempts"] = ct_attempts
            if len(picked_ct) < ct_n:
                ct_drop["_global"] = f"insufficient:{len(picked_ct)}/{ct_n}"
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"[reserved_selector] CONTRARIAN scan failed: {e}")
            ct_drop["_error"] = str(e)

    # -------------------------
    # SNIPER: oversold + near the N-hour low (sniper buy/sell)
    # -------------------------
    picked_sn: List[MarketSnapshot] = []
    sn_drop: Dict[str, int] = {}
    sniper_params_map: Dict[str, Dict[str, Any]] = {}

    sn_scan_multiplier = max(1, _si(os.getenv("OMA_SELECTOR_SNIPER_SCAN_MULTIPLIER", "4"), 4))
    sn_scan_cap = _si(os.getenv("OMA_SELECTOR_SNIPER_SCAN_CAP", "24"), 24)
    # [FIX 2026-03-05] Prefer sn_scan_cap as the target
    # Before: max(sn_n, sn_n*mult)=only 8 scanned → all others without EMA cache dropped as no_ema_data
    # Fix: scan toward cap=24; if cap is unset, compute based on multiplier
    sn_scan_limit = sn_scan_cap if sn_scan_cap > 0 else max(sn_n, sn_n * sn_scan_multiplier)

    if sn_n > 0:
        sn_pool: List[MarketSnapshot] = []

        # SNIPER looks for oversold coins near their low
        remaining_for_sn = [m for m in scan_union if m not in used_markets and _currency(m) not in sniper_exclude_bases]
        if sniper_exclude_bases:
            excluded_base_n = sum(1 for m in scan_union if m not in used_markets and _currency(m) in sniper_exclude_bases)
            if excluded_base_n > 0:
                sn_drop["excluded_base"] = sn_drop.get("excluded_base", 0) + excluded_base_n

        # Build RSI cache for SNIPER (fetch only markets not already cached)
        # [FIX 2026-03-05] Already computed externally based on sn_scan_cap. Do not recompute.
        # sn_scan_limit = sn_scan_cap (=24) → after FIX M4/M5, missing cache drops immediately, so
        # we must scan wide enough for SNIPER candidates to survive.
        remaining_by_vol_sn = sorted(remaining_for_sn, key=lambda m: smap.get(m).vol24_usdt if smap.get(m) else 0.0, reverse=True)

        # [2026-02-03] EMA cross pre-filter (optional)
        ema_cross_filter = bool(os.getenv("OMA_SNIPER_EMA_FILTER", "true").lower() == "true")
        ema_cache: Dict[str, tuple[bool, float, float]] = {}

        for m in remaining_by_vol_sn[:sn_scan_limit]:
            if m not in ld_lt_gz_rsi_macd_cache:
                try:
                    candles = _fetch_candles_minutes_cached(sess, m, unit=15, count=30)
                    ld_lt_gz_rsi_macd_cache[m] = _calc_rsi_macd_from_candles(candles)
                    ai_features_cache[m] = _extract_ai_features_from_candles(candles)

                    # [2026-02-23] SNIPER-only ATR/BB enrichment (markets missed by the ld_lt_gz loop)
                    if candles and m in smap:
                        ti = _compute_ti(m, candles)
                        smap[m].atr_pct = ti.get("atr_pct", 0.0)
                        smap[m].bb_width_pct = ti.get("bb_width_pct", 0.0)
                        smap[m].bb_upper = ti.get("bb_upper", 0.0)
                        smap[m].bb_middle = ti.get("bb_middle", 0.0)
                        smap[m].bb_lower = ti.get("bb_lower", 0.0)

                    # [2026-02-03] Check EMA cross (prefer golden cross)
                    if ema_cross_filter and len(candles) >= 26:
                        is_golden, ema_fast, ema_slow = _check_ema_cross(candles, fast=12, slow=26)
                        ema_cache[m] = (is_golden, ema_fast, ema_slow)

                except (KeyError, AttributeError, TypeError):
                    logger.warning("[Selector] LD/LT/GZ candle analysis failed for %s", m, exc_info=True)
                    ld_lt_gz_rsi_macd_cache[m] = dict(_FALLBACK_RSI)
                    ai_features_cache[m] = {"trend": 0.0, "momentum": 0.0, "volatility": 2.0, "volume_surge": 0.0, "data_valid": False}

        for m in remaining_for_sn:
            s = smap.get(m)
            if s is None:
                sn_drop["no_snapshot"] = sn_drop.get("no_snapshot", 0) + 1
                continue

            # Spread check (use the recommendation-selection threshold)
            max_sp = float(_selector_spread_bps)
            if max_sp > 0 and s.spread_bps > max_sp:
                sn_drop["spread"] = sn_drop.get("spread", 0) + 1
                continue

            # Look up RSI/AI features
            # [FIX M5] Treat missing cache as None (prevent the RSI=50 default from passing the filter)
            if m not in ld_lt_gz_rsi_macd_cache:
                sn_drop["no_rsi_data"] = sn_drop.get("no_rsi_data", 0) + 1
                continue
            rsi_macd = ld_lt_gz_rsi_macd_cache[m]
            ai_feat = ai_features_cache.get(m, {"trend": 0.0, "momentum": 0.0, "volatility": 2.0, "data_valid": False})
            rsi = float(rsi_macd.get("rsi", 50.0))

            # SNIPER filter: RSI < 55 (near oversold) or 24h drop > 1%
            # [2026-02-02] Relaxed filter: RSI 50→55, drop -3%→-1% to secure more candidates
            change_24h = float(rsi_macd.get("change_24h", 0.0))
            if rsi >= 55 and change_24h > -1.0:
                sn_drop["not_oversold"] = sn_drop.get("not_oversold", 0) + 1
                continue

            # [FIX 2026-03-05] At the selector level, do not block dead crosses.
            # compute_scope_score (lowest-point entry method) assigns high scores to dead-cross + near-bottom coins.
            # If the selector allows only golden crosses, all compute_scope_score-preferred candidates drop → 0 candidates.
            # The actual EMA check at buy entry is performed in strategy_plugins.py (execution level).
            # ema_cache data is still collected because it is used in compute_scope_score's internal scoring.

            sn_pool.append(s)

        # Scoring — [2026-03-03] unified via compute_scope_score
        # Use the same 6-stage confidence/rank_score as longshort_multi_scan
        # → resolves score mismatch between recommended coins and active coins
        sn_scored: List[Tuple[float, MarketSnapshot, Dict[str, float], float, Optional[Dict[str, Any]]]] = []
        try:
            from app.api.strategy_router import compute_scope_score
        except ImportError:
            logger.warning("[Selector] compute_scope_score import failed", exc_info=True)
            compute_scope_score = None  # type: ignore

        # Look up BTC regime only once
        _btc_regime = "TREND"
        try:
            _btc_det = get_btc_leading_detector()
            if _btc_det:
                _btc_regime = _btc_det.get_regime_for_lightning()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as _br_err:
            logger.warning("[LIGHTNING] BTC regime detect failed: %s", _br_err, exc_info=True)

        for s in sn_pool:
            ai_feat = ai_features_cache.get(s.market, {"trend": 0.0, "momentum": 0.0, "volatility": 2.0, "data_valid": False})
            rsi_macd = ld_lt_gz_rsi_macd_cache.get(s.market, {"rsi": 50.0})
            rsi = float(rsi_macd.get("rsi", 50.0))

            # Stage 1: scope score (based on proximity to the candle low, unified 0-100 scale)
            scope_data: Optional[Dict[str, Any]] = None
            score: float = 0.0
            if compute_scope_score is not None:
                try:
                    scope_data = compute_scope_score(s.market, btc_regime=_btc_regime)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                    logger.warning("[Selector] compute_scope_score failed for %s", s.market, exc_info=True)
                    scope_data = None

            if scope_data and float(scope_data.get("rank_score") or 0) > 0:
                score = float(scope_data["rank_score"])
            else:
                # fallback: legacy _score_sniper → normalized to a 0-100 scale
                # [FIX #8] raw*2.0 causes top-end clustering → changed to linear normalization
                raw = _score_sniper(s, ai_feat, rsi)
                score = max(0.0, min(100.0, (raw + 10.0) / 50.0 * 100.0))  # -10~40 → 0~100 even distribution

            # [2026-02-01] Apply historical performance (adjusted within the 0-100 scale)
            perf_adj = _get_market_performance_score(s.market, "SNIPER", pnl_cache)
            score = max(0.0, min(100.0, score + (perf_adj * 15.0)))  # [FIX 2026-03-05] 3.0 → 15.0 (meaningful effect on the 0-100 scale)

            # [2026-03-05] SNIPER: apply 4 extra signals (same as _pick_ai_enhanced)
            # Apply Cross Exchange signal
            try:
                from app.manager.cross_exchange_signal import get_cross_exchange_signal_provider
                from app.manager.cross_exchange_scoring import adjust_score_for_cross_exchange
                _sn_signal_provider = get_cross_exchange_signal_provider()
                _sn_coin = s.market.replace("USDT", "")
                _sn_cross_signal = _sn_signal_provider.get_signal(_sn_coin)
                if _sn_cross_signal:
                    _sn_result = adjust_score_for_cross_exchange(score / 100.0, "SNIPER", _sn_coin, _sn_cross_signal)
                    score = _sn_result["adjusted_score"] * 100.0
            except (OSError, KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[SELECTOR] cross exchange signal: %s", exc, exc_info=True)

            # Apply Volume Spike
            try:
                _sn_vol_detector = get_volume_spike_detector()
                if _sn_vol_detector:
                    score = _sn_vol_detector.adjust_score_for_volume_spike(s.market, score, "SNIPER")
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[SELECTOR] volume spike adjust: %s", exc, exc_info=True)

            # Apply Time Volatility (per-hour volatility)
            try:
                _sn_time_adj = get_time_volatility_adjuster()
                if _sn_time_adj:
                    score = _sn_time_adj.adjust_score_for_time(score, "SNIPER")
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[SELECTOR] time volatility adjust: %s", exc, exc_info=True)

            # Apply BTC Leading Signal
            try:
                _sn_btc_det = get_btc_leading_detector()
                if _sn_btc_det:
                    score = _sn_btc_det.adjust_score_for_btc_signal(score, "SNIPER")
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[SELECTOR] BTC leading signal: %s", exc, exc_info=True)

            sn_scored.append((score, s, ai_feat, rsi, scope_data))

        sn_scored.sort(key=lambda x: x[0], reverse=True)

        # Compute budget and parameters
        vols_sn = sorted([x[1].vol24_usdt for x in sn_scored if x[1].vol24_usdt > 0])
        vol_med_sn = vols_sn[len(vols_sn) // 2] if vols_sn else 0.0

        for score, s, ai_feat, rsi, scope_data in sn_scored:
            if len(picked_sn) >= sn_n:
                break

            budget = _suggest_budget(
                strategy="SNIPER",
                base_usdt=float(sn_base),
                vol24_usdt=float(s.vol24_usdt),
                vol_median_usdt=float(vol_med_sn),
                min_order_usdt=float(min_order_usdt),
                max_budget_usdt=float(sn_max),
                price=float(s.price),
                entry_qty_guard_on=bool(entry_qty_guard_on),
                entry_max_qty=float(entry_max_qty),
                depth_factor=float(entry_ob_depth_factor),
                depth_ask_usdt=float(s.depth_ask_usdt),
                depth_bid_usdt=float(s.depth_bid_usdt),
                total_capital_usdt=float(total_capital_usdt),
                existing_markets_count=int(existing_markets_count + len(used_markets)),
                spread_bps=float(s.spread_bps),
                range_ratio_24h=float(s.range_ratio_24h),
                trend_score=float(ai_feat.get("trend", 0.0)),  # [2026-02-03] pass trend
            )

            if budget is None:
                sn_drop["budget"] = sn_drop.get("budget", 0) + 1
                continue

            # Auto-compute SNIPER parameters (includes fetching actual high/low)
            sniper_params = _calc_sniper_params(s, ai_feat, rsi, session=sess)
            sniper_params_map[s.market] = sniper_params

            setattr(s, "_suggested_budget", float(budget))
            setattr(s, "_score_override", float(score))
            setattr(s, "_ai_features", ai_feat)
            setattr(s, "_rsi_macd", ld_lt_gz_rsi_macd_cache.get(s.market, {"rsi": 50.0}))
            setattr(s, "_sniper_params", sniper_params)
            # [2026-03-03] Attach scope score data (unified rank_score/confidence)
            if scope_data:
                setattr(s, "_scope_data", scope_data)

            picked_sn.append(s)
            used_markets.add(s.market)

    # -------------------------
    # Strict Strategy-Fit gate
    # - Keep only candidates sufficiently close to each strategy's best score.
    # - This favors "most strategy-fit first" over filling slot count.
    # -------------------------
    strict_fit_enabled = (
        str(os.getenv("OMA_SELECTOR_STRICT_FIT_ENABLED", "true")).strip().lower()
        in ("1", "true", "yes", "on")
    )
    strict_fit_disable_force_fill = (
        str(os.getenv("OMA_SELECTOR_STRICT_FIT_DISABLE_FORCE_FILL", "true")).strip().lower()
        in ("1", "true", "yes", "on")
    )

    # [FIX 2026-03-05] Lower the strict_fit_rel threshold
    # From 0.60~0.75 → 0.30~0.50: for strategies with evenly distributed scores, keep only top 25~70%
    # Fixes the issue of only 1 surviving per strategy (relaxed so as many candidates survive as there are slots)
    strict_fit_rel_default = _clamp(
        _sf(os.getenv("OMA_SELECTOR_STRICT_FIT_REL_DEFAULT", "0.35"), 0.35),
        0.0,
        1.0,
    )
    strict_fit_rel_map = {
        "PINGPONG": _clamp(_sf(os.getenv("OMA_SELECTOR_STRICT_FIT_REL_PINGPONG", str(strict_fit_rel_default)), strict_fit_rel_default), 0.0, 1.0),
        "AUTOLOOP": _clamp(_sf(os.getenv("OMA_SELECTOR_STRICT_FIT_REL_AUTOLOOP", str(strict_fit_rel_default)), strict_fit_rel_default), 0.0, 1.0),
        "LADDER": _clamp(_sf(os.getenv("OMA_SELECTOR_STRICT_FIT_REL_LADDER", "0.40"), 0.40), 0.0, 1.0),
        "LIGHTNING": _clamp(_sf(os.getenv("OMA_SELECTOR_STRICT_FIT_REL_LIGHTNING", "0.40"), 0.40), 0.0, 1.0),
        "GAZUA": _clamp(_sf(os.getenv("OMA_SELECTOR_STRICT_FIT_REL_GAZUA", "0.40"), 0.40), 0.0, 1.0),
        "CONTRARIAN": _clamp(_sf(os.getenv("OMA_SELECTOR_STRICT_FIT_REL_CONTRARIAN", "0.30"), 0.30), 0.0, 1.0),
        "SNIPER": _clamp(_sf(os.getenv("OMA_SELECTOR_STRICT_FIT_REL_SNIPER", "0.40"), 0.40), 0.0, 1.0),
    }
    strict_fit_abs_default = _sf(os.getenv("OMA_SELECTOR_STRICT_FIT_ABS_DEFAULT", "0"), 0.0)

    def _fit_score_of(s: MarketSnapshot, strategy: str) -> float:
        try:
            v = getattr(s, "_score_override", None)
            if v is None:
                su = str(strategy).upper()
                if su == "PINGPONG":
                    v = _score_pingpong(s)
                elif su == "AUTOLOOP":
                    v = _score_autoloop(s)
                elif su == "LADDER":
                    v = _score_ladder(s)
                elif su == "LIGHTNING":
                    v = _score_lightning(s)
                elif su == "GAZUA":
                    v = _score_gazua(s)
                elif su == "CONTRARIAN":
                    v = _score_contrarian(s)
                elif su == "SNIPER":
                    v = _score_sniper(s, {}, 50.0)
                else:
                    v = 0.0
            return float(v or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Selector] _score_for_strategy failed", exc_info=True)
            return 0.0

    def _apply_strict_fit(
        strategy: str,
        picked: List[MarketSnapshot],
        drop_dict: Dict[str, Any],
    ) -> List[MarketSnapshot]:
        if not strict_fit_enabled or not picked:
            return picked

        su = str(strategy).upper()
        rel_floor = float(strict_fit_rel_map.get(su, strict_fit_rel_default))
        abs_floor = float(_sf(os.getenv(f"OMA_SELECTOR_STRICT_FIT_ABS_{su}", str(strict_fit_abs_default)), strict_fit_abs_default))

        scores = [_fit_score_of(s, su) for s in picked]
        top_score = max(scores) if scores else 0.0
        rel_cut = (top_score * rel_floor) if top_score > 0 else None

        kept: List[MarketSnapshot] = []
        dropped_abs = 0
        dropped_rel = 0

        for s, sc in zip(picked, scores):
            if sc < abs_floor:
                dropped_abs += 1
                continue
            if rel_cut is not None and sc < rel_cut:
                dropped_rel += 1
                continue
            kept.append(s)

        if dropped_abs > 0:
            drop_dict["strict_fit_abs"] = int(drop_dict.get("strict_fit_abs", 0)) + dropped_abs
        if dropped_rel > 0:
            drop_dict["strict_fit_rel"] = int(drop_dict.get("strict_fit_rel", 0)) + dropped_rel
        if dropped_abs > 0 or dropped_rel > 0:
            _logger.info(
                f"[reserved_selector] strict_fit {su}: kept={len(kept)}/{len(picked)} "
                f"(top={top_score:.2f}, rel_floor={rel_floor:.2f}, abs_floor={abs_floor:.2f}, "
                f"drop_rel={dropped_rel}, drop_abs={dropped_abs})"
            )
        return kept

    picked_pp = _apply_strict_fit("PINGPONG", picked_pp, pp_drop)
    picked_al = _apply_strict_fit("AUTOLOOP", picked_al, al_drop)
    picked_ld = _apply_strict_fit("LADDER", picked_ld, ld_drop)
    picked_lt = _apply_strict_fit("LIGHTNING", picked_lt, lt_drop)
    picked_gz = _apply_strict_fit("GAZUA", picked_gz, gz_drop)
    picked_ct = _apply_strict_fit("CONTRARIAN", picked_ct, ct_drop)
    picked_sn = _apply_strict_fit("SNIPER", picked_sn, sn_drop)

    # Rebuild used_markets after strict fit (reflect markets dropped by filtering)
    used_markets = {s.market for s in (picked_pp + picked_al + picked_ld + picked_lt + picked_gz + picked_ct + picked_sn)}

    # -------------------------
    # [2026-02-01] force_fill mode: force-fill missing slots from the top of the volume ranking
    # -------------------------
    force_fill_skipped_by_strict = bool(force_fill and strict_fit_enabled and strict_fit_disable_force_fill)
    if force_fill and strict_fit_enabled and strict_fit_disable_force_fill:
        _logger.info("[reserved_selector] strict_fit enabled: force_fill skipped to preserve strategy fitness")
    elif force_fill:
        _logger.info("[reserved_selector] force_fill=True: starting force-fill of missing slots")

        # Compute the shortage per strategy (including PINGPONG, AUTOLOOP)
        shortages = {
            "PINGPONG": (pp_n - len(picked_pp), pp_base, pp_max, picked_pp),
            "AUTOLOOP": (al_n - len(picked_al), al_base, al_max, picked_al),
            "LADDER": (ld_n - len(picked_ld), ld_base, ld_max, picked_ld),
            "LIGHTNING": (lt_n - len(picked_lt), lt_base, lt_max, picked_lt),
            "GAZUA": (gz_n - len(picked_gz), gz_base, gz_max, picked_gz),
            "CONTRARIAN": (ct_n - len(picked_ct), ct_base, ct_max, picked_ct),
            "SNIPER": (sn_n - len(picked_sn), sn_base, sn_max, picked_sn),
        }

        # Available market pool - force_fill searches candidates from the full volume ranking (ranked_by_vol)
        # scan_union is the PP/AL pool and too small, so expand based on the full universe
        remaining_for_force = [m for m in ranked_by_vol if m not in used_markets]

        # Build a snapshot from ticker even for markets not in smap
        def _get_or_make_snapshot(m: str) -> Optional[MarketSnapshot]:
            s = smap.get(m)
            if s is not None:
                return s
            # Build snapshot from ticker only (force_fill possible without orderbook)
            tk = tmap.get(m)
            if tk is None:
                return None
            return _snapshot_from_ticker_and_ob(
                m,
                tk,
                None,  # no orderbook
                depth_bps=0.0,
                caution=bool(caution_map.get(m, False)),
                delisting=bool(delisting_map.get(m, False)),
                delisting_date=delisting_date_map.get(m),
                names=names_map.get(m),
            )

        remaining_by_vol = sorted(
            remaining_for_force,
            key=lambda m: (smap.get(m).vol24_usdt if smap.get(m) else (tmap.get(m, {}).get("acc_trade_price_24h", 0) or 0)),
            reverse=True
        )

        _logger.info(f"[reserved_selector] force_fill: remaining={len(remaining_by_vol)}")

        ladder_force_fill_enabled = str(os.getenv("OMA_SELECTOR_LADDER_FORCE_FILL", "false")).strip().lower() in ("1", "true", "yes", "on")
        for strategy, (shortage, base_usdt, max_usdt, picked_list) in shortages.items():
            if shortage <= 0:
                continue
            if strategy == "LADDER" and not ladder_force_fill_enabled:
                _logger.info("[reserved_selector] force_fill: LADDER disabled (OMA_SELECTOR_LADDER_FORCE_FILL=false), skipped")
                continue

            _logger.info("[reserved_selector] force_fill: %s short by %s", strategy, shortage)

            filled = 0
            for m in remaining_by_vol:
                if filled >= shortage:
                    break
                if m in used_markets:
                    continue
                # The CONTRARIAN benchmark axis (BTC) is never included, even in force_fill
                if strategy == "CONTRARIAN" and _currency(m) == "BTC":
                    continue
                if strategy == "SNIPER" and _currency(m) in sniper_exclude_bases:
                    continue

                s = _get_or_make_snapshot(m)
                if s is None:
                    continue
                # Add to smap so later logic can use it too
                smap[m] = s

                # In force_fill mode use the global vol_median (per-strategy local vars are not accessible)
                vol_med_for_strategy = vol_med_pp or vol_med_al or vol_med_sn or 0.0

                # Set default budget
                budget = _suggest_budget(
                    strategy=strategy,
                    base_usdt=float(base_usdt),
                    vol24_usdt=float(s.vol24_usdt),
                    vol_median_usdt=float(vol_med_for_strategy),
                    min_order_usdt=float(min_order_usdt),
                    max_budget_usdt=float(max_usdt),
                    price=float(s.price),
                    entry_qty_guard_on=bool(entry_qty_guard_on),
                    entry_max_qty=float(entry_max_qty),
                    depth_factor=0.0,  # ignore depth in forced mode
                    depth_ask_usdt=float(s.depth_ask_usdt),
                    depth_bid_usdt=float(s.depth_bid_usdt),
                    total_capital_usdt=float(total_capital_usdt),
                    existing_markets_count=int(existing_markets_count + len(used_markets)),
                    trend_score=0.0,  # force_fill mode: no trend info
                )

                s._suggested_budget = budget or base_usdt
                s._score_override = float(s.vol24_usdt) / 1e9  # volume-based score
                s._forced = True  # forced-selection flag
                setattr(s, "_rsi_macd", ld_lt_gz_rsi_macd_cache.get(s.market, {"rsi": 50.0}))
                setattr(s, "_ai_features", ai_features_cache.get(s.market, {}))

                # CONTRARIAN/SNIPER special attributes
                if strategy == "CONTRARIAN":
                    s._contrarian_data = {"score": 0, "forced": True}
                elif strategy == "SNIPER":
                    # [FIX #13] Use cached ai_features/rsi (remove hardcoding)
                    _ff_ai = ai_features_cache.get(s.market, {})
                    _ff_rsi_data = ld_lt_gz_rsi_macd_cache.get(s.market, {"rsi": 50.0})
                    _ff_rsi = float(_ff_rsi_data.get("rsi", 50.0))
                    s._sniper_params = _calc_sniper_params(s, _ff_ai, _ff_rsi, session=sess)

                picked_list.append(s)
                used_markets.add(s.market)
                filled += 1

            if filled > 0:
                _logger.info("[reserved_selector] force_fill: %s +%s", strategy, filled)

    # -------------------------
    # Build output items
    # -------------------------
    items: List[Dict[str, Any]] = []
    ts = time.time()

    def _mk_item(s: MarketSnapshot, *, strategy: str) -> Dict[str, Any]:
        rid = str(uuid.uuid4())
        budget = float(getattr(s, "_suggested_budget", 0.0) or 0.0)
        ai_score = float(_ai_score_heuristic(s))
        ai_features = getattr(s, "_ai_features", None) or {}

        item = {
            "id": rid,
            "ts": ts,
            "market": s.market,
            "strategy": str(strategy).upper(),
            "suggested_budget_usdt": budget,
            "score": float(
                getattr(s, "_score_override", None)
                or (_score_pingpong(s) if str(strategy).upper() == "PINGPONG" else _score_autoloop(s))
            ),
            "ai_score": ai_score,
            "names": s.names or {},
            "metrics": {
                "price": s.price,
                "vol24_usdt": s.vol24_usdt,
                "range_ratio_24h": s.range_ratio_24h,
                "spread_bps": s.spread_bps,
                "best_bid": s.best_bid,
                "best_ask": s.best_ask,
                "depth_ask_usdt": s.depth_ask_usdt,
                "depth_bid_usdt": s.depth_bid_usdt,
                "recent_trades": s.recent_trades,
                "caution": bool(s.caution),
                "delisting": bool(s.delisting),
                "delisting_date": s.delisting_date,
            },
        }

        # [2026-03-03] SNIPER: add rank_score/confidence based on scope score
        _scope = getattr(s, "_scope_data", None)
        if _scope and str(strategy).upper() == "SNIPER":
            item["rank_score"] = float(_scope.get("rank_score") or 0.0)
            item["confidence"] = float(_scope.get("confidence") or 0.0)
            item["fire_level"] = str(_scope.get("fire_level") or "HOLD")
            item["stages_passed"] = int(_scope.get("stages_passed") or 0)

        # [2026-03-08] Multi-stage confidence for all strategies (except SNIPER — uses its own scope_data)
        _multi_conf = getattr(s, "_confidence", None)
        if _multi_conf and str(strategy).upper() != "SNIPER":
            item["confidence"] = float(_multi_conf.get("confidence", 0.0))
            item["stages_passed"] = int(_multi_conf.get("stages_passed", 0))
            item["confidence_stages"] = _multi_conf.get("stages", {})

        # Add RSI/MACD indicators (all strategies)
        rsi_macd = getattr(s, "_rsi_macd", None) or {}
        # Always add, even defaults (used by the frontend)
        item["metrics"]["rsi"] = float(rsi_macd.get("rsi", 50.0))
        item["metrics"]["macd_trend"] = str(rsi_macd.get("macd_trend", "neutral"))
        item["metrics"]["macd_histogram"] = float(rsi_macd.get("macd_histogram", 0.0))
        item["metrics"]["change_24h"] = float(rsi_macd.get("change_24h", 0.0))
        item["metrics"]["rsi_data_valid"] = bool(rsi_macd.get("data_valid", False))

        # Add AI feature info (all strategies)
        _pool_ai = ai_features_cache.get(s.market, ai_features) if ai_features_cache else ai_features
        if _pool_ai or ai_features:
            _src = _pool_ai or ai_features
            item["ai_features"] = {
                "trend": float(_src.get("trend", 0.0)),
                "momentum": float(_src.get("momentum", 0.0)),
                "volatility": float(_src.get("volatility", 0.0)),
                "volume_surge": float(_src.get("volume_surge", 0.0)),
                "data_valid": bool(_src.get("data_valid", False)),
            }

        # Add per-strategy recommended parameters
        strat_upper = str(strategy).upper()
        # Volatility: use the 24h range_ratio (0.05 = 5%)
        # ai_features' volatility is based on 5-min candles and too small
        range_ratio = float(s.range_ratio_24h) if s.range_ratio_24h else 0.0
        volatility_pct = range_ratio * 100.0  # ratio → percent (0.05 → 5%)

        # fallback: ai_features' volatility (already in % units)
        if volatility_pct < 0.5:
            volatility_pct = float(ai_features.get("volatility", 0.0)) if ai_features else 0.0

        # Fetch for RSI-based adjustment
        rsi_val = float(rsi_macd.get("rsi", 50.0)) if rsi_macd else 50.0

        trend = float(ai_features.get("trend", 0.0)) if ai_features else 0.0
        momentum = float(ai_features.get("momentum", 0.0)) if ai_features else 0.0

        if strat_upper == "LADDER":
            # LADDER: volatility-based recommended parameters
            # Recommend ATR when volatility > 5%
            use_atr = volatility_pct > 5.0
            # step_pct: proportional to volatility (0.5 ~ 3.0%)
            step_pct = round(max(0.5, min(3.0, volatility_pct * 0.3)), 2)
            # TP: proportional to volatility (1.5 ~ 8.0%)
            tp_pct = round(max(1.5, min(8.0, volatility_pct * 0.6)), 1)
            # Steps: proportional to budget (5 ~ 20)
            steps = max(5, min(20, int(budget / 15)))
            # Martingale: proportional to volatility (1.0 ~ 1.2)
            martingale = round(max(1.0, min(1.2, 1.0 + volatility_pct * 0.02)), 2)

            # RSI-based adjustment
            if rsi_val < 30:  # oversold → more aggressive
                steps = min(steps + 3, 25)
                martingale = min(martingale + 0.05, 1.25)
            elif rsi_val > 70:  # overbought → conservative
                steps = max(steps - 2, 5)
                tp_pct = max(tp_pct - 1.0, 1.5)

            item["recommended_params"] = {
                "step_pct": step_pct,
                "use_atr": use_atr,
                "atr_mult": 1.5 if use_atr else 1.0,
                "tp_pct": tp_pct,
                "steps": steps,
                "martingale": martingale,
            }
        elif strat_upper == "LIGHTNING":
            # LIGHTNING: momentum + volatility-based recommended parameters
            # TP: proportional to volatility (2.0 ~ 10.0%)
            tp_pct = round(max(2.0, min(10.0, volatility_pct * 0.8 + abs(momentum) * 0.5)), 1)
            # SL: 50% of TP (stop-loss ratio)
            sl_pct = round(-abs(tp_pct * 0.5), 1)

            # RSI-based adjustment
            if rsi_val < 30:  # oversold → expect bounce
                tp_pct = min(tp_pct + 1.0, 12.0)
            elif rsi_val > 70:  # overbought → conservative
                tp_pct = max(tp_pct - 1.0, 2.0)
                sl_pct = max(sl_pct + 0.5, -1.0)

            item["recommended_params"] = {
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "manual_exit": False,
            }
        elif strat_upper == "GAZUA":
            # GAZUA: trend + volatility-based recommended parameters
            # TP: proportional to volatility (5.0 ~ 20.0%)
            tp_pct = round(max(5.0, min(20.0, volatility_pct * 1.5 + abs(trend) * 5.0)), 1)
            # SL: 40% of TP
            sl_pct = round(-abs(tp_pct * 0.4), 1)
            profile_mode = "trend" if trend > 0.2 else "sideways"
            sideways_ai_min = 0.58 if volatility_pct < 6.0 else 0.60
            trail_dist = 3.0 if profile_mode == "sideways" else 3.8

            # RSI-based adjustment
            if rsi_val < 30:  # oversold → expect a large bounce
                tp_pct = min(tp_pct + 3.0, 25.0)
            elif rsi_val > 70:  # overbought → take profit early
                tp_pct = max(tp_pct - 3.0, 5.0)
                sl_pct = max(sl_pct + 1.0, -3.0)

            item["recommended_params"] = {
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "manual_exit": False,
                "buy_now": False,
                "hold_sell": False,
                "user_sell_only": False,
                "sell_fraction": 0.5,
                "trail_tp_enabled": True,
                "trail_dist_pct": trail_dist,
                "profile_mode": profile_mode,
                "sideways_ai_score_min": sideways_ai_min,
                "sideways_rsi_max": 55,
                "sideways_bounce_pct_min": 0.15,
                "sideways_momentum_min": 0.05,
                "sideways_ema_cross_required": False,
                "trend_ai_score_min": 0.68,
                "trend_rsi_max": 60,
                "trend_bounce_pct_min": 0.25,
                "trend_momentum_min": 0.15,
                "trend_ema_cross_required": True,
                "scale_in_enabled": True,
                "entry_probe_frac": 0.35,
                "entry_confirm_frac": 0.65,
                "confirm_window_sec": 1200,
                "confirm_profit_pct": 0.25,
                "confirm_ai_threshold": 0.64,
                "confirm_momentum_min": 0.05,
                "add_buy_cooldown_sec": 180,
            }
        elif strat_upper == "PINGPONG":
            # PINGPONG: range trading - volatility-based TP/SL
            # TP: proportional to volatility (1.5 ~ 8.0%)
            tp_pct = round(max(1.5, min(8.0, volatility_pct * 0.5 + 1.5)), 1)
            # SL: 60% of TP
            sl_pct = round(-abs(tp_pct * 0.6), 1)

            # RSI buy/sell thresholds: adjusted by volatility
            rsi_buy = 30 if volatility_pct < 5.0 else 25
            rsi_sell = 70 if volatility_pct < 5.0 else 75

            item["recommended_params"] = {
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "rsi_buy": rsi_buy,
                "rsi_sell": rsi_sell,
            }
        elif strat_upper == "AUTOLOOP":
            # AUTOLOOP: scaled buying + take-profit - based on volatility + AI confidence
            # TP: proportional to volatility (1.0 ~ 5.0%)
            tp_pct = round(max(1.0, min(5.0, volatility_pct * 0.4 + 1.0)), 1)
            # Scaling steps: proportional to budget (3 ~ 12)
            steps = max(3, min(12, int(budget / 25)))
            # AI-confidence-based multiplier (HIGH: 1.3, MEDIUM: 1.0, LOW: 0.8)
            conf_tier = "high" if ai_score >= 0.8 else ("medium" if ai_score >= 0.6 else "low")
            budget_mult = 1.3 if conf_tier == "high" else (1.0 if conf_tier == "medium" else 0.8)

            # RSI-based adjustment
            if rsi_val < 30:
                tp_pct = min(tp_pct + 0.5, 6.0)
                budget_mult = min(budget_mult + 0.1, 1.5)
            elif rsi_val > 70:
                tp_pct = max(tp_pct - 0.5, 1.0)

            item["recommended_params"] = {
                "tp_pct": tp_pct,
                "steps": steps,
                "budget_multiplier": round(budget_mult, 2),
                "confidence_tier": conf_tier,
            }
        elif strat_upper == "CONTRARIAN":
            # CONTRARIAN: consistency-first profile (fast recovery + optional trail)
            tp_pct = round(max(0.1, _sf(os.getenv("OMA_CONTRARIAN_MIN_TP_PCT", "15.0"), 15.0)), 2)
            sl_pct = round(-abs(_sf(os.getenv("OMA_CONTRARIAN_DEFAULT_SL_PCT", "50.0"), 50.0)), 2)
            trail_enabled = str(os.getenv("OMA_CONTRARIAN_FORCE_TRAIL", "false")).strip().lower() == "true"
            trail_dist_pct = round(max(0.05, _sf(os.getenv("OMA_CONTRARIAN_TRAIL_DIST_PCT", "0.3"), 0.3)), 2)
            bypass_ob_guard = str(os.getenv("OMA_CONTRARIAN_BYPASS_OB_GUARD", "true")).strip().lower() == "true"
            item["recommended_params"] = {
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "trail_tp_enabled": bool(trail_enabled),
                "trail_dist_pct": float(trail_dist_pct),
                "use_atr": False,
                "rsi_filter": False,
                "rsi_max": 70,
                "ema_cross_enabled": False,
                "min_score": 1,
                "cooldown_sec": 300,
                "hold_sell": False,
                "user_sell_only": False,
                "entry_ob_guard_enabled": (not bool(bypass_ob_guard)),
            }
        elif strat_upper == "SNIPER":
            # SNIPER: sniper buy/sell - use parameters auto-computed by AI
            sniper_params = getattr(s, "_sniper_params", {})
            if sniper_params:
                item["recommended_params"] = sniper_params
                # Add lookback time-unit display
                lookback_min = sniper_params.get("entry_lookback_min", 360)
                if lookback_min >= 1440:
                    item["lookback_display"] = f"{lookback_min // 1440}d"
                elif lookback_min >= 60:
                    item["lookback_display"] = f"{lookback_min // 60}h"
                else:
                    item["lookback_display"] = f"{lookback_min}m"
            else:
                # default
                item["recommended_params"] = {
                    "entry_enabled": True,
                    "entry_lookback_min": 360,
                    "entry_threshold_pct": 0.5,
                    "exit_enabled": True,
                    "exit_lookback_min": 360,
                    "exit_threshold_pct": 0.5,
                    "tp_pct": 3.0,
                    "sl_pct": 2.0,
                    "trail_tp": True,
                    "trail_dist_pct": 1.2,
                    "ai_gate_enabled": True,
                    "ai_min_score": 0.45,
                    "rsi_entry_enabled": True,
                    "use_limit": True,
                    "fallback_to_market": True,
                    "expiry_min": 180,
                }
                item["lookback_display"] = "6h"

        return item

    # ── [2026-03-18] Auction approach: when specialist strategy slots are unfilled, generalist strategies yield coins ──
    _auction_enabled = os.getenv("OMA_AUCTION_ENABLED", "1").strip() != "0"
    if _auction_enabled:
        _specialist = {
            "SNIPER": (picked_sn, sn_n, None),
            "CONTRARIAN": (picked_ct, ct_n, None),
            "LIGHTNING": (picked_lt, lt_n, None),
            "LADDER": (picked_ld, ld_n, None),
            "GAZUA": (picked_gz, gz_n, None),
        }
        _generalist = {"PINGPONG": picked_pp, "AUTOLOOP": picked_al}
        _auction_swaps = 0
        for spec_name, (spec_picked, spec_need, _) in _specialist.items():
            if len(spec_picked) >= spec_need or spec_need <= 0:
                continue
            shortage = spec_need - len(spec_picked)
            for gen_name, gen_picked in _generalist.items():
                if shortage <= 0:
                    break
                swap_candidates = []
                for idx, gs in enumerate(gen_picked):
                    m = gs.market
                    if m in used_markets and m not in {x.market for x in gen_picked}:
                        continue
                    _cached_ai = ai_features_cache.get(m) or {}
                    try:
                        if spec_name == "LIGHTNING":
                            spec_sc = _score_lightning(gs, ai_features=_cached_ai)
                        elif spec_name == "LADDER":
                            spec_sc = _score_ladder(gs) if 'gs' else 0.0
                        elif spec_name == "GAZUA":
                            spec_sc = _score_gazua(gs) if 'gs' else 0.0
                        else:
                            continue
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("[SELECTOR] auction swap scoring: %s", exc, exc_info=True)
                        continue
                    gen_sc = _score_pingpong(gs) if gen_name == "PINGPONG" else _score_autoloop(gs)
                    if gen_sc > 0 and spec_sc > gen_sc * 1.3:
                        swap_candidates.append((spec_sc / max(gen_sc, 0.01), idx, gs))
                swap_candidates.sort(key=lambda x: x[0], reverse=True)
                for _, idx, gs in swap_candidates[:shortage]:
                    spec_picked.append(gs)
                    gen_picked.remove(gs)
                    _auction_swaps += 1
                    shortage -= 1
                    logger.info(f"[Auction] {gs.market}: {gen_name} → {spec_name} (ratio={_:.1f})")
        if _auction_swaps > 0:
            logger.info("[Auction] %s swaps completed", _auction_swaps)

    for s in picked_pp:
        items.append(_mk_item(s, strategy="PINGPONG"))
    for s in picked_al:
        items.append(_mk_item(s, strategy="AUTOLOOP"))
    for s in picked_ld:
        items.append(_mk_item(s, strategy="LADDER"))
    for s in picked_lt:
        items.append(_mk_item(s, strategy="LIGHTNING"))
    for s in picked_gz:
        items.append(_mk_item(s, strategy="GAZUA"))
    for s in picked_ct:
        item = _mk_item(s, strategy="CONTRARIAN")
        ct_data = getattr(s, "_contrarian_data", None)
        if ct_data:
            item["contrarian_data"] = ct_data
        items.append(item)
    for s in picked_sn:
        item = _mk_item(s, strategy="SNIPER")
        sniper_params = getattr(s, "_sniper_params", None)
        if sniper_params:
            item["sniper_params"] = sniper_params
        items.append(item)

    summary = {
        "universe_usdt": len(universe),
        "universe_filtered": len(universe),
        "cooldown_active": len(cooldown_markets),
        "excluded_cooldown": int(_sd.filter_stats.get("excluded_cooldown", 0)),
        "excluded_skip_currency": int(_sd.filter_stats.get("excluded_skip_currency", 0)),
        "excluded_global_market": int(_sd.filter_stats.get("excluded_global_market", 0)),
        "excluded_price_min": int(_sd.filter_stats.get("excluded_price_min", 0)),
        "excluded_price_max": int(_sd.filter_stats.get("excluded_price_max", 0)),
        "tickers_loaded": len(tmap),
        "orderbooks_loaded": len(obmap),
        "existing_active": len(existing_active),
        "existing_recovery": len(existing_recovery),
        "existing_watch": len(existing_watch),
        "exclude_watch": bool(exclude_watch),
        "scan_top_pingpong": pp_scan,
        "scan_top_autoloop": al_scan,
        "scan_size_pingpong": len(pp_scan_list),
        "scan_size_autoloop": len(al_scan_list),
        "scan_union_size": len(scan_union),
        "shared_pool_total": _pool_total_count,
        "shared_pool_valid": _pool_valid_count,
        "picked_pingpong": len(picked_pp),
        "picked_autoloop": len(picked_al),
        "picked_ladder": len(picked_ld),
        "picked_lightning": len(picked_lt),
        "picked_gazua": len(picked_gz),
        "picked_contrarian": len(picked_ct),
        "picked_sniper": len(picked_sn),
        "dropped_ladder": ld_drop,
        "dropped_lightning": lt_drop,
        "dropped_gazua": gz_drop,
        "dropped_contrarian": ct_drop,
        "dropped_sniper": sn_drop,
        "dropped_pingpong": pp_drop,
        "dropped_autoloop": al_drop,
        "strict_fit": {
            "enabled": bool(strict_fit_enabled),
            "disable_force_fill": bool(strict_fit_disable_force_fill),
            "force_fill_skipped": bool(force_fill_skipped_by_strict),
            "rel_floor": dict(strict_fit_rel_map),
            "abs_floor_default": float(strict_fit_abs_default),
        },
        "params": {
            "entry_ob_max_spread_bps": entry_ob_max_spread_bps,
            "selector_spread_bps": _selector_spread_bps,
            "entry_ob_depth_bps": entry_ob_depth_bps,
            "entry_ob_depth_factor": entry_ob_depth_factor,
            "entry_qty_guard": entry_qty_guard_on,
            "entry_max_qty": entry_max_qty,
            "exclude_caution": exclude_caution,
            "skip_currencies": skip_currencies,
            "global_exclude_bases": global_exclude_bases,
            "global_exclude_markets": sorted(global_exclude_markets),
            "candidate_price_min_usdt": float(candidate_price_min_usdt),
            "candidate_price_max_usdt": float(candidate_price_max_usdt),
            "min_vol24_pingpong": min_vol24_pp,
            "min_vol24_autoloop": min_vol24_al,
            "pp_al_candle_top": 0,
            "ai_scan_limit": 0,
            "ai_scan_cap": len(_pool_targets),
            "sniper_scan_limit": sn_scan_limit,
            "sniper_scan_cap": sn_scan_cap,
            "candle_cache_ttl_sec": _sf(os.getenv("OMA_SELECTOR_CANDLE_CACHE_TTL_SEC", "45"), 45.0),
            "candle_fail_cooldown_sec": _sf(os.getenv("OMA_SELECTOR_CANDLE_FAIL_COOLDOWN_SEC", "6"), 6.0),
            "recent_minutes": recent_minutes,
            "min_recent_trades_pingpong": min_recent_trades_pp,
            "tradecheck_top": tradecheck_top,
            "budget_base_pingpong": pp_base,
            "budget_base_autoloop": al_base,
        },
        # dynamic budget allocation info
        "dynamic_budget": {
            "enabled": dynamic_budget_enabled,
            "total_capital_usdt": total_capital_usdt,
            "existing_markets": existing_markets_count,
            "total_allocated": sum(getattr(s, "_suggested_budget", 0.0) for s in picked_pp + picked_al + picked_ld + picked_lt + picked_gz),
        },
    }

    # Prevent session leak
    try:
        sess.close()
    except Exception:
        pass

    return items, summary
