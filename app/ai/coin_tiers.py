# ============================================================
# File: app/ai/coin_tiers.py
# Autocoin OS v3-H — strategy-based coin weighting system
# ============================================================
"""
Integrates with the existing strategy system (reserved_selector, topn_selector)
to reflect per-strategy characteristics during AI training.

Strategy profiles (see topn_selector.PROFILE_WEIGHTS):
- PINGPONG: range / volatility scalping
- LADDER: DCA accumulation in downtrends
- LIGHTNING: momentum breakout
- GAZUA: strong uptrend + holding
- AUTOROPE: liquidity + adaptive

Purpose:
1. Assign per-strategy sample weights during training
2. Add strategy fitness as an AI feature
3. Adjust prediction confidence
"""

from __future__ import annotations

from typing import Dict, Optional, Any
from enum import Enum


class StrategyProfile(str, Enum):
    """Strategy profile"""
    PINGPONG = "pingpong"
    LADDER = "ladder"
    LIGHTNING = "lightning"
    GAZUA = "gazua"
    AUTOROPE = "autorope"
    AUTOLOOP = "autoloop"
    CONTRARIAN = "contrarian"
    UNKNOWN = "unknown"


# Per-strategy sample weights (applied during training)
# - Higher weight: model focuses more on that strategy's pattern
# - Lower weight: reduces the impact of noisy data
STRATEGY_SAMPLE_WEIGHTS: Dict[str, float] = {
    # Stable strategies (higher weight)
    "pingpong": 1.5,      # stable in ranging markets
    "autoloop": 1.5,      # proven automation

    # Trend strategies (medium weight)
    "ladder": 1.2,        # DCA pattern
    "gazua": 1.0,         # momentum holding
    "lightning": 1.0,     # breakout trading

    # Experimental strategies (lower weight)
    "autorope": 0.8,
    "contrarian": 0.7,    # contrarian (noisy)
    "unknown": 0.5,
}

# Per-strategy prediction confidence scale
STRATEGY_CONFIDENCE_SCALE: Dict[str, float] = {
    "pingpong": 1.2,      # clear prediction pattern
    "autoloop": 1.2,
    "ladder": 1.1,
    "lightning": 1.0,
    "gazua": 0.9,         # high volatility
    "autorope": 0.9,
    "contrarian": 0.8,    # hard to predict
    "unknown": 0.7,
}


# Per-strategy AI action thresholds
# ai_buy_threshold: score must be at or above this to allow buying
# ai_sell_threshold: at or below this, block buying or consider exit
STRATEGY_AI_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "pingpong": {
        "ai_buy_threshold": 0.40,   # slightly conservative for ranging markets
        "ai_sell_threshold": 0.30,
        "ai_tp_scale_high": 1.3,    # high AI -> TP +30%
        "ai_tp_scale_low": 0.8,     # low AI -> TP -20%
        "ai_sl_scale_high": 1.2,    # high AI -> looser SL
        "ai_sl_scale_low": 0.7,     # low AI -> tighter SL
    },
    "autoloop": {
        "ai_buy_threshold": 0.45,
        "ai_sell_threshold": 0.35,
        "ai_tp_scale_high": 1.4,
        "ai_tp_scale_low": 0.7,
        "ai_sl_scale_high": 1.3,
        "ai_sl_scale_low": 0.6,
    },
    "lightning": {
        "ai_buy_threshold": 0.55,   # momentum: only when confident
        "ai_sell_threshold": 0.40,
        "ai_tp_scale_high": 1.5,
        "ai_tp_scale_low": 0.6,
        "ai_sl_scale_high": 1.2,
        "ai_sl_scale_low": 0.5,
    },
    "gazua": {
        "ai_buy_threshold": 0.50,
        "ai_sell_threshold": 0.35,
        "ai_tp_scale_high": 1.6,    # holding -> higher target
        "ai_tp_scale_low": 0.7,
        "ai_sl_scale_high": 1.4,
        "ai_sl_scale_low": 0.6,
    },
    "ladder": {
        "ai_buy_threshold": 0.35,   # DCA -> easier entry
        "ai_sell_threshold": 0.25,
        "ai_tp_scale_high": 1.2,
        "ai_tp_scale_low": 0.9,
        "ai_sl_scale_high": 1.1,
        "ai_sl_scale_low": 0.8,
    },
    "contrarian": {
        "ai_buy_threshold": 0.30,   # contrarian: may go against the AI
        "ai_sell_threshold": 0.20,
        "ai_tp_scale_high": 1.5,
        "ai_tp_scale_low": 0.8,
        "ai_sl_scale_high": 1.3,
        "ai_sl_scale_low": 0.7,
    },
    "autorope": {
        "ai_buy_threshold": 0.45,
        "ai_sell_threshold": 0.35,
        "ai_tp_scale_high": 1.3,
        "ai_tp_scale_low": 0.8,
        "ai_sl_scale_high": 1.2,
        "ai_sl_scale_low": 0.7,
    },
    "unknown": {
        "ai_buy_threshold": 0.50,
        "ai_sell_threshold": 0.35,
        "ai_tp_scale_high": 1.0,
        "ai_tp_scale_low": 1.0,
        "ai_sl_scale_high": 1.0,
        "ai_sl_scale_low": 1.0,
    },
}


# Per-regime strategy fitness (0.0 ~ 1.0)
# Higher means the strategy works better in that regime
REGIME_STRATEGY_FIT: Dict[str, Dict[str, float]] = {
    "BULL": {
        "pingpong": 0.6,    # ranging is inefficient in a bull market
        "autoloop": 0.7,
        "lightning": 0.9,   # momentum strategy is optimal
        "gazua": 0.95,      # holding strategy is optimal
        "ladder": 0.5,      # DCA is inefficient in a bull market
        "contrarian": 0.3,  # contrarian is risky in a bull market
        "autorope": 0.7,
    },
    "BEAR": {
        "pingpong": 0.5,
        "autoloop": 0.5,
        "lightning": 0.3,   # momentum is risky in a bear market
        "gazua": 0.2,       # holding is risky in a bear market
        "ladder": 0.8,      # DCA finds opportunity in a bear market
        "contrarian": 0.9,  # contrarian is optimal
        "autorope": 0.6,
    },
    "NEUTRAL": {
        "pingpong": 0.95,   # optimal in ranging markets
        "autoloop": 0.9,
        "lightning": 0.5,
        "gazua": 0.4,
        "ladder": 0.6,
        "contrarian": 0.5,
        "autorope": 0.8,
    },
    "UNKNOWN": {
        "pingpong": 0.7,
        "autoloop": 0.7,
        "lightning": 0.5,
        "gazua": 0.5,
        "ladder": 0.6,
        "contrarian": 0.5,
        "autorope": 0.6,
    },
}


def normalize_strategy(strategy: Optional[str]) -> str:
    """Normalize a strategy name"""
    if not strategy:
        return "unknown"
    s = str(strategy).lower().strip()
    # Handle aliases
    aliases = {
        "auto_loop": "autoloop",
        "ping_pong": "pingpong",
        "auto_rope": "autorope",
    }
    return aliases.get(s, s)


def get_sample_weight(strategy: Optional[str] = None, market: Optional[str] = None) -> float:
    """
    Return the sample weight used during training.

    Args:
        strategy: strategy name (extracted from reason)
        market: market name (optional, reserved for future use)

    Returns:
        weight (0.5 ~ 1.5)
    """
    s = normalize_strategy(strategy)
    return STRATEGY_SAMPLE_WEIGHTS.get(s, 1.0)


def get_confidence_scale(strategy: Optional[str] = None) -> float:
    """
    Return the confidence scale used during prediction.

    Returns:
        scale (0.7 ~ 1.2)
    """
    s = normalize_strategy(strategy)
    return STRATEGY_CONFIDENCE_SCALE.get(s, 1.0)


def extract_strategy_from_reason(reason: Optional[str]) -> str:
    """
    Extract the strategy name from buy_reason/sell_reason.

    e.g.: "engine_buy:lightning" → "lightning"
          "ladder_entry" → "ladder"
    """
    if not reason:
        return "unknown"

    r = str(reason).lower()

    # Pattern matching
    for strat in ["pingpong", "autoloop", "ladder", "lightning", "gazua", "autorope", "contrarian"]:
        if strat in r:
            return strat
    
    return "unknown"


def get_strategy_feature_weights() -> Dict[str, Dict[str, float]]:
    """
    Return per-strategy indicator weights matching topn_selector.PROFILE_WEIGHTS.
    Used as a reference when generating AI features.
    """
    return {
        "pingpong": {
            "volatility": 0.45,
            "liquidity": 0.35,
            "trend_abs": -0.30,
            "range_ratio": 0.10,
            "choppiness": 0.25,
        },
        "ladder": {
            "momentum": 0.45,
            "liquidity": 0.30,
            "volatility": 0.10,
            "trend_slope": 0.20,
            "trend_abs": -0.05,
        },
        "lightning": {
            "momentum": 0.35,
            "volatility": 0.35,
            "liquidity": 0.30,
            "trend_slope": 0.15,
        },
        "autorope": {
            "liquidity": 0.40,
            "volatility": 0.25,
            "momentum": 0.15,
            "range_ratio": 0.10,
            "choppiness": 0.10,
        },
        "gazua": {
            "momentum": 0.55,
            "liquidity": 0.30,
            "volatility": 0.20,
            "trend_slope": 0.20,
            "trend_abs": -0.05,
        },
    }


def encode_strategy_onehot(strategy: Optional[str]) -> Dict[str, float]:
    """Convert a strategy to one-hot encoding (for AI features)"""
    s = normalize_strategy(strategy)
    strategies = ["pingpong", "ladder", "lightning", "gazua", "autoloop", "autorope", "contrarian"]
    
    result = {}
    for strat in strategies:
        result[f"strategy_{strat}"] = 1.0 if s == strat else 0.0
    
    return result


def get_strategy_thresholds(strategy: Optional[str] = None) -> Dict[str, float]:
    """Return per-strategy AI thresholds"""
    s = normalize_strategy(strategy)
    return STRATEGY_AI_THRESHOLDS.get(s, STRATEGY_AI_THRESHOLDS["unknown"])


def get_regime_fit(regime: str, strategy: Optional[str] = None) -> float:
    """Return regime-strategy fitness (0.0 ~ 1.0)"""
    regime = str(regime or "").upper().strip()
    if regime not in REGIME_STRATEGY_FIT:
        regime = "UNKNOWN"
    
    s = normalize_strategy(strategy)
    return REGIME_STRATEGY_FIT[regime].get(s, 0.5)


def adjust_ai_score_for_strategy(
    ai_score: float,
    strategy: Optional[str] = None,
    regime: Optional[str] = None
) -> Dict[str, Any]:
    """
    Adjust the AI score for the given strategy and regime.

    Returns:
        {
            "adjusted_score": float,  # adjusted score
            "should_buy": bool,       # whether buying is allowed
            "should_sell": bool,      # whether to consider exit
            "tp_scale": float,        # TP adjustment multiplier
            "sl_scale": float,        # SL adjustment multiplier
            "confidence": float,      # confidence (based on regime fitness)
        }
    """
    thresholds = get_strategy_thresholds(strategy)
    regime_fit = get_regime_fit(regime, strategy)

    # Buy/exit decision
    should_buy = ai_score >= thresholds["ai_buy_threshold"]
    should_sell = ai_score <= thresholds["ai_sell_threshold"]

    # Adjust by regime fitness
    # Lower fitness -> stricter buy condition
    if regime_fit < 0.5:
        should_buy = ai_score >= (thresholds["ai_buy_threshold"] + 0.1)

    # TP/SL scale (based on AI score)
    if ai_score >= 0.6:
        tp_scale = thresholds["ai_tp_scale_high"]
        sl_scale = thresholds["ai_sl_scale_high"]
    elif ai_score <= 0.4:
        tp_scale = thresholds["ai_tp_scale_low"]
        sl_scale = thresholds["ai_sl_scale_low"]
    else:
        # Linear interpolation
        t = (ai_score - 0.4) / 0.2  # 0~1
        tp_scale = thresholds["ai_tp_scale_low"] + t * (thresholds["ai_tp_scale_high"] - thresholds["ai_tp_scale_low"])
        sl_scale = thresholds["ai_sl_scale_low"] + t * (thresholds["ai_sl_scale_high"] - thresholds["ai_sl_scale_low"])

    # Further adjust scale by regime fitness
    tp_scale *= (0.7 + regime_fit * 0.6)  # 0.7 ~ 1.3
    sl_scale *= (0.8 + regime_fit * 0.4)  # 0.8 ~ 1.2
    
    return {
        "adjusted_score": ai_score * regime_fit,
        "should_buy": should_buy,
        "should_sell": should_sell,
        "tp_scale": tp_scale,
        "sl_scale": sl_scale,
        "confidence": regime_fit,
        "strategy": normalize_strategy(strategy),
        "regime": str(regime or "UNKNOWN").upper(),
    }


def get_all_strategy_info() -> Dict[str, Any]:
    """Return all strategy info (for debugging/dashboard)"""
    return {
        "sample_weights": STRATEGY_SAMPLE_WEIGHTS,
        "confidence_scales": STRATEGY_CONFIDENCE_SCALE,
        "feature_weights": get_strategy_feature_weights(),
        "ai_thresholds": STRATEGY_AI_THRESHOLDS,
        "regime_fit": REGIME_STRATEGY_FIT,
    }
