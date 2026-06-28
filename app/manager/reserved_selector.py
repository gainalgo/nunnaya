# ============================================================
# File: app/manager/reserved_selector.py
# Autocoin OS v3-H — Re-export Hub
# ------------------------------------------------------------
# All public names from the 6 submodules are re-exported here
# for 100% backward compatibility with existing imports like:
#   from app.manager.reserved_selector import build_reserved_candidates
# ============================================================

from __future__ import annotations

# ── Utils: helpers, constants, data classes ──────────────────
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
    _SCORE_EXCLUDED,
    SharedMarketData,
    MarketSnapshot,
    _snapshot_from_ticker_and_ob,
    BYBIT_BASE,
    DEFAULT_TIMEOUT,
)

# ── Fetchers: market data acquisition ───────────────────────
from app.manager.reserved_selector_fetchers import (
    build_watchlist,
    fetch_candles_minutes,
    _fetch_candles_minutes_cached,
    fetch_highlow_for_lookback,
    fetch_recent_trades_count,
    fetch_markets_details,
    fetch_tickers,
    fetch_orderbooks,
    _last_prefetch_markets,
)

# ── Analysis: AI features, EMA, RSI/MACD ────────────────────
from app.manager.reserved_selector_analysis import (
    _extract_ai_features_from_candles,
    _calc_ema_simple,
    _check_ema_cross,
    _calc_rsi_macd_from_candles,
)

# ── Scoring: all strategy scores + confidence ────────────────
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

# ── Budget: allocation helpers ───────────────────────────────
from app.manager.reserved_selector_budget import (
    _suggest_budget,
    _suggest_budget_dynamic,
)

# ── Orchestrator: main entry point ───────────────────────────
from app.manager.reserved_selector_orchestrator import (
    build_reserved_candidates,
)

__all__ = [
    # Orchestrator
    "build_reserved_candidates",
    # Utils
    "_sf", "_si", "_clamp", "_norm", "_currency", "_csv_upper",
    "_global_exclude_bases", "_global_exclude_markets", "_chunks",
    "_execution_quality_penalty", "_calc_spread_bps", "_calc_depth_notional",
    "_normalize_market", "_SCORE_EXCLUDED",
    "SharedMarketData", "MarketSnapshot", "_snapshot_from_ticker_and_ob",
    "BYBIT_BASE", "DEFAULT_TIMEOUT",
    # Fetchers
    "build_watchlist", "fetch_candles_minutes", "_fetch_candles_minutes_cached",
    "fetch_highlow_for_lookback", "fetch_recent_trades_count",
    "fetch_markets_details", "fetch_tickers", "fetch_orderbooks",
    "_last_prefetch_markets",
    # Analysis
    "_extract_ai_features_from_candles", "_calc_ema_simple",
    "_check_ema_cross", "_calc_rsi_macd_from_candles",
    # Scoring
    "_score_pingpong", "_score_ladder", "_score_lightning",
    "_score_sniper", "_score_gazua", "_score_autoloop",
    "_score_contrarian", "_score_contrarian_live",
    "_score_ladder_ai", "_score_lightning_ai", "_score_gazua_ai",
    "_ai_score_heuristic",
    "_get_market_performance_score", "_load_market_pnl_cache",
    "_calc_sniper_params",
    "_confidence_pingpong", "_confidence_autoloop", "_confidence_ladder",
    "_confidence_lightning", "_confidence_gazua", "_confidence_contrarian",
    # Budget
    "_suggest_budget", "_suggest_budget_dynamic",
]
