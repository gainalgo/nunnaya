# ============================================================
# File: app/ai/features.py
# Autocoin OS v3-H — Shared Feature Extractor
# ============================================================
"""
Ensures training and inference use the same feature set.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# List of numeric features used for training (based on training_dataset.csv columns)
# Excluded: strings, future info (price_next, ret, target), metadata (market, ts, event)
TRAINING_FEATURES = [
    # Basic price/time
    "price",
    "bar_sec",
    "bars",
    # Regime (needs one-hot conversion)
    # "regime",  # -> regime_BULL, regime_BEAR, regime_NEUTRAL
    "regime_spread_pct",
    # Momentum/trend
    "momentum_pct",
    "dev_pct",
    "dev_prev_pct",
    # RSI
    "rsi",
    "rsi_prev",
    # MACD
    "macd_hist",
    "macd_hist_prev",
    # Statistics
    "anchor",
    "z",
    "vol_pct",
    "pb_slope_pct",
    # Volume (used if available)
    "notional_quote_1m",
    "vol_base_1m",
    "volume",
    # === Multi-window returns ===
    "ret_1",
    "ret_3",
    "ret_5",
    "ret_10",
    "ret_20",
    # === EMA related ===
    "ema_fast",
    "ema_slow",
    "ema_gap_pct",
    "ema_slope",
    # === Volatility ===
    "atr_pct",
    "bb_width",
    "realized_vol_5",
    "realized_vol_20",
    # === Volume Z-score ===
    "notional_z",
    "vol_ratio",
    # === Market beta ===
    "btc_ret_pct",
    "coin_vs_btc",
]

# For regime one-hot encoding
REGIME_CATEGORIES = ["BULL", "BEAR", "NEUTRAL", "UNKNOWN"]

# For strategy one-hot encoding
STRATEGY_CATEGORIES = ["PINGPONG", "AUTOLOOP", "LIGHTNING", "GAZUA", "LADDER", "CONTRARIAN", "UNKNOWN"]

def encode_regime_onehot(regime: Optional[str]) -> Dict[str, float]:
    """Convert regime to one-hot encoding"""
    result = {}
    regime_str = str(regime or "").upper().strip()
    if regime_str not in REGIME_CATEGORIES:
        regime_str = "UNKNOWN"
    
    for cat in REGIME_CATEGORIES:
        result[f"regime_{cat}"] = 1.0 if cat == regime_str else 0.0
    return result

def encode_strategy_onehot(strategy: Optional[str]) -> Dict[str, float]:
    """Convert strategy to one-hot encoding"""
    result = {}
    s = str(strategy or "").upper().strip()
    if s not in STRATEGY_CATEGORIES:
        s = "UNKNOWN"
    for cat in STRATEGY_CATEGORIES:
        result[f"strategy_{cat}"] = 1.0 if cat == s else 0.0
    return result

def extract_features_from_row(row: Dict[str, Any]) -> Dict[str, float]:
    """
    Extract features from a single row (dict) of training_dataset.csv.
    Used during training.
    """
    features: Dict[str, float] = {}
    
    # Numeric features
    for feat in TRAINING_FEATURES:
        val = row.get(feat)
        if val is None or val == "":
            features[feat] = float("nan")
        else:
            try:
                features[feat] = float(val)
            except (TypeError, ValueError):
                logger.warning("Feature %s float conversion failed for value %r", feat, val, exc_info=True)
                features[feat] = float("nan")
    
    # Boolean -> float
    has_pos = row.get("has_pos")
    if has_pos is not None:
        if isinstance(has_pos, bool):
            features["has_pos"] = 1.0 if has_pos else 0.0
        elif isinstance(has_pos, str):
            features["has_pos"] = 1.0 if has_pos.lower() == "true" else 0.0
        else:
            try:
                features["has_pos"] = float(has_pos)
            except (TypeError, ValueError):
                logger.warning("has_pos float conversion failed for value %r", has_pos, exc_info=True)
                features["has_pos"] = 0.0
    else:
        features["has_pos"] = 0.0
    
    # Regime one-hot
    regime = row.get("regime")
    regime_encoded = encode_regime_onehot(regime)
    features.update(regime_encoded)
    
    # Strategy one-hot (if present in row)
    strategy = row.get("strategy")
    strategy_encoded = encode_strategy_onehot(strategy)
    features.update(strategy_encoded)
    
    return features

def _compute_regime_from_indicators(
    trend_60: float,
    vol_pct: float,
    ema_gap_pct: float,
) -> str:
    """
    Determine BULL/BEAR/NEUTRAL from EMA slope + volatility.
    - trend_60 positive & vol low → BULL
    - trend_60 negative & vol high → BEAR
    - otherwise → NEUTRAL
    """
    vol_threshold_high = 3.0
    vol_threshold_low = 1.5
    trend_threshold = 1.0

    if trend_60 > trend_threshold and vol_pct < vol_threshold_low:
        return "BULL"
    if trend_60 < -trend_threshold and vol_pct > vol_threshold_high:
        return "BEAR"
    if ema_gap_pct > 0.5 and vol_pct < vol_threshold_low:
        return "BULL"
    if ema_gap_pct < -0.5 and vol_pct > vol_threshold_high:
        return "BEAR"
    return "NEUTRAL"

def extract_features_from_runtime(
    price: float,
    price_history: List[float],
    context: Any = None,
    brain_output: Any = None,
) -> Dict[str, float]:
    """
    Extract features during real-time inference.
    Generate features from StrategyBrain output and context.

    [2026-01-30 optimization]
    - Minimize heavy indicator computation
    - Quickly compute only basic features
    """
    features: Dict[str, float] = {}

    # Basic price
    features["price"] = float(price) if price else 0.0

    # Compute from price_history (list conversion optimization)
    hist = [float(p) for p in (price_history or []) 
            if isinstance(p, (int, float)) and p > 0]
    
    n_hist = len(hist)
    features["bars"] = float(n_hist)
    features["bar_sec"] = 180.0
    
    # Multi-window return calc (lightweight)
    for n in [1, 3, 5, 10, 20]:
        if n_hist >= n and hist[-n] > 0:
            features[f"ret_{n}"] = (hist[-1] - hist[-n]) / hist[-n] * 100.0
        else:
            features[f"ret_{n}"] = 0.0
    
    # Compute only essential EMAs (12, 26)
    # [optimization] Use simple approximate EMA
    if n_hist >= 26:
        ema_fast_val = sum(hist[-12:]) / 12.0  # simple SMA approximation
        ema_slow_val = sum(hist[-26:]) / 26.0
    elif n_hist >= 12:
        ema_fast_val = sum(hist[-12:]) / 12.0
        ema_slow_val = ema_fast_val
    else:
        ema_fast_val = price
        ema_slow_val = price
    
    features["ema_fast"] = float(ema_fast_val)
    features["ema_slow"] = float(ema_slow_val)
    features["ema_gap_pct"] = (ema_fast_val - ema_slow_val) / price * 100.0 if price > 0 else 0.0
    
    # EMA slope simplified
    if n_hist >= 13:
        prev_avg = sum(hist[-13:-1]) / 12.0
        features["ema_slope"] = (ema_fast_val - prev_avg) / prev_avg * 100.0 if prev_avg > 0 else 0.0
    else:
        features["ema_slope"] = 0.0
    
    # [2026-01-30 optimization] Replace with simple statistics (minimize indicator calls)
    if n_hist >= 5:
        # Momentum (simple rate of change)
        momentum = (hist[-1] - hist[-3]) / hist[-3] * 100.0 if hist[-3] > 0 else 0.0
        features["momentum_pct"] = float(momentum)
        
        # Trend (60 bars or available data)
        lookback = min(n_hist, 60)
        trend_60 = (hist[-1] - hist[-lookback]) / hist[-lookback] * 100.0 if hist[-lookback] > 0 else 0.0
        features["dev_pct"] = float(trend_60)
        features["dev_prev_pct"] = float(trend_60)
        
        # RSI simplified (up/down ratio approximation)
        gains = sum(max(0, hist[i] - hist[i-1]) for i in range(-14, 0) if i-1 >= -n_hist)
        losses = sum(max(0, hist[i-1] - hist[i]) for i in range(-14, 0) if i-1 >= -n_hist)
        if gains + losses > 0:
            rsi = 100.0 * gains / (gains + losses)
        else:
            rsi = 50.0
        features["rsi"] = float(rsi)
        features["rsi_prev"] = float(rsi)
        
        # MACD replaced with EMA gap (already computed)
        features["macd_hist"] = features["ema_gap_pct"]
        features["macd_hist_prev"] = features["ema_gap_pct"]
        
        # Volatility (standard deviation simplified)
        if n_hist >= 20:
            returns = [(hist[i] - hist[i-1]) / hist[i-1] for i in range(-19, 0) if hist[i-1] > 0]
            if returns:
                mean_ret = sum(returns) / len(returns)
                vol = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5 * 100.0
            else:
                vol = 0.0
        else:
            vol = 0.0
        features["vol_pct"] = float(vol)
        
        # Anchor (20-period average)
        anchor = sum(hist[-min(20, n_hist):]) / min(20, n_hist)
        features["anchor"] = float(anchor)
        
        # Z-score
        if anchor > 0 and vol > 0:
            std = vol * anchor / 100.0
            features["z"] = (price - anchor) / std if std > 0 else 0.0
        else:
            features["z"] = 0.0
        
        # pb_slope
        if n_hist >= 10:
            avg_recent = sum(hist[-5:]) / 5.0
            avg_prev = sum(hist[-10:-5]) / 5.0
            features["pb_slope_pct"] = ((avg_recent - avg_prev) / avg_prev * 100.0) if avg_prev > 0 else 0.0
        else:
            features["pb_slope_pct"] = 0.0
    else:
        # Default values
        features["momentum_pct"] = 0.0
        features["dev_pct"] = 0.0
        features["dev_prev_pct"] = 0.0
        features["rsi"] = 50.0
        features["rsi_prev"] = 50.0
        features["macd_hist"] = 0.0
        features["macd_hist_prev"] = 0.0
        features["vol_pct"] = 0.0
        features["anchor"] = price
        features["z"] = 0.0
        features["pb_slope_pct"] = 0.0
    
    # ATR% simplified (average range)
    if n_hist >= 15:
        atr = sum(abs(hist[i] - hist[i-1]) for i in range(-14, 0)) / 14.0
        features["atr_pct"] = atr / price * 100.0 if price > 0 else 0.0
    else:
        features["atr_pct"] = 0.0
    
    # BB Width simplified (based on vol_pct)
    features["bb_width"] = features["vol_pct"] * 4.0  # 2 std * 2 = 4
    
    # Realized Vol reuses vol_pct
    features["realized_vol_5"] = features["vol_pct"]
    features["realized_vol_20"] = features["vol_pct"]
    
    # Compute regime ourselves (fallback if not available from context)
    trend_60 = features.get("dev_pct", 0.0)
    vol_pct = features.get("vol_pct", 0.0)
    ema_gap_pct = features.get("ema_gap_pct", 0.0)
    
    regime = _compute_regime_from_indicators(trend_60, vol_pct, ema_gap_pct)
    
    # Override if regime info is available from context
    if context is not None:
        if hasattr(context, "strategy_state"):
            ss = getattr(context, "strategy_state", {}) or {}
            if isinstance(ss, dict):
                ctx_regime = ss.get("regime")
                if ctx_regime and str(ctx_regime).upper() in ["BULL", "BEAR", "NEUTRAL"]:
                    regime = str(ctx_regime).upper()
    
    # Regime spread (from context)
    features["regime_spread_pct"] = 0.0
    
    # Regime one-hot
    regime_encoded = encode_regime_onehot(regime)
    features.update(regime_encoded)

    # Strategy info (from context)
    strategy = "UNKNOWN"
    if context is not None:
        ctrls = getattr(context, "controls", None) or {}
        if isinstance(ctrls, dict):
            strat_cfg = ctrls.get("strategy", {}) or {}
            strategy = str(strat_cfg.get("name", "") or "").upper()
    strategy_encoded = encode_strategy_onehot(strategy)
    features.update(strategy_encoded)
    
    # Position presence
    has_pos = False
    if context is not None and hasattr(context, "position"):
        pos = getattr(context, "position", None)
        has_pos = pos is not None and pos.get("qty", 0) > 0
    features["has_pos"] = 1.0 if has_pos else 0.0
    
    # Volume (default values)
    features["vol_base_1m"] = 0.0
    features["volume"] = 0.0
    
    # Attempt to extract notional turnover info
    notional = 0.0
    try:
        from app.core.hyper_price_store import price_store
        market = ""
        if context is not None:
            market = getattr(context, "market", "") or getattr(context, "code", "") or ""
        if market:
            notional = float(price_store.get_candle_1m_notional(market, 0.0) or 0.0)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[features] %s: %s", 'notional turnover extraction attempt', exc, exc_info=True)
    features["notional_quote_1m"] = notional
    
    # vol_ratio and notional_z need history, so default to 0.0
    features["notional_z"] = 0.0
    features["vol_ratio"] = 1.0
    
    # Market beta (vs BTC return)
    features["btc_ret_pct"] = 0.0
    features["coin_vs_btc"] = 0.0
    try:
        from app.core.hyper_price_store import price_store
        btc_hist = price_store.get_prices("BTCUSDT", 20)
        if btc_hist and len(btc_hist) >= 5:
            btc_prices = [float(p) for p in btc_hist if p and float(p) > 0]
            if len(btc_prices) >= 5 and btc_prices[-5] > 0:
                btc_ret = (btc_prices[-1] - btc_prices[-5]) / btc_prices[-5] * 100.0
                features["btc_ret_pct"] = btc_ret
                coin_ret = features.get("ret_5", 0.0)
                features["coin_vs_btc"] = coin_ret - btc_ret
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[features] %s: %s", 'market beta (vs BTC return)', exc, exc_info=True)
    
    return features

def get_feature_names() -> List[str]:
    """Return the full list of feature names used for training"""
    names = list(TRAINING_FEATURES)
    names.append("has_pos")
    for cat in REGIME_CATEGORIES:
        names.append(f"regime_{cat}")
    for cat in STRATEGY_CATEGORIES:
        names.append(f"strategy_{cat}")
    return names
