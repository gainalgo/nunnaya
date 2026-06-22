# ============================================================
# File: app/ai/features.py
# Autocoin OS v3-H — 공용 Feature Extractor
# ============================================================
"""
학습(training)과 추론(inference)에서 동일한 feature set을 사용하도록 보장.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# 학습에 사용할 numeric feature 목록 (training_dataset.csv 컬럼 기반)
# 제외 대상: 문자열, 미래 정보(price_next, ret, target), 메타데이터(market, ts, event)
TRAINING_FEATURES = [
    # 기본 가격/시간
    "price",
    "bar_sec",
    "bars",
    # 국면 (one-hot으로 변환 필요)
    # "regime",  # -> regime_BULL, regime_BEAR, regime_NEUTRAL
    "regime_spread_pct",
    # 모멘텀/추세
    "momentum_pct",
    "dev_pct",
    "dev_prev_pct",
    # RSI
    "rsi",
    "rsi_prev",
    # MACD
    "macd_hist",
    "macd_hist_prev",
    # 통계
    "anchor",
    "z",
    "vol_pct",
    "pb_slope_pct",
    # 거래량 (있으면 사용)
    "notional_quote_1m",
    "vol_base_1m",
    "volume",
    # === 멀티 윈도우 수익률 ===
    "ret_1",
    "ret_3",
    "ret_5",
    "ret_10",
    "ret_20",
    # === EMA 관련 ===
    "ema_fast",
    "ema_slow",
    "ema_gap_pct",
    "ema_slope",
    # === 변동성 ===
    "atr_pct",
    "bb_width",
    "realized_vol_5",
    "realized_vol_20",
    # === 거래량 Z-score ===
    "notional_z",
    "vol_ratio",
    # === 시장 베타 ===
    "btc_ret_pct",
    "coin_vs_btc",
]

# 국면 one-hot 인코딩용
REGIME_CATEGORIES = ["BULL", "BEAR", "NEUTRAL", "UNKNOWN"]

# 전략 one-hot 인코딩용
STRATEGY_CATEGORIES = ["PINGPONG", "AUTOLOOP", "LIGHTNING", "GAZUA", "LADDER", "CONTRARIAN", "UNKNOWN"]

def encode_regime_onehot(regime: Optional[str]) -> Dict[str, float]:
    """국면을 one-hot 인코딩으로 변환"""
    result = {}
    regime_str = str(regime or "").upper().strip()
    if regime_str not in REGIME_CATEGORIES:
        regime_str = "UNKNOWN"
    
    for cat in REGIME_CATEGORIES:
        result[f"regime_{cat}"] = 1.0 if cat == regime_str else 0.0
    return result

def encode_strategy_onehot(strategy: Optional[str]) -> Dict[str, float]:
    """전략을 one-hot 인코딩으로 변환"""
    result = {}
    s = str(strategy or "").upper().strip()
    if s not in STRATEGY_CATEGORIES:
        s = "UNKNOWN"
    for cat in STRATEGY_CATEGORIES:
        result[f"strategy_{cat}"] = 1.0 if cat == s else 0.0
    return result

def extract_features_from_row(row: Dict[str, Any]) -> Dict[str, float]:
    """
    training_dataset.csv의 한 행(dict)에서 feature를 추출.
    학습 시 사용.
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
    EMA slope + volatility로 BULL/BEAR/NEUTRAL 판정.
    - trend_60 양수 & vol 낮음 → BULL
    - trend_60 음수 & vol 높음 → BEAR
    - 그 외 → NEUTRAL
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
    실시간 추론 시 feature 추출.
    StrategyBrain 출력과 context에서 feature 생성.
    
    [2026-01-30 최적화] 
    - 무거운 indicator 계산 최소화
    - 기본 feature만 빠르게 계산
    """
    features: Dict[str, float] = {}
    
    # 기본 가격
    features["price"] = float(price) if price else 0.0
    
    # price_history에서 계산 (리스트 변환 최적화)
    hist = [float(p) for p in (price_history or []) 
            if isinstance(p, (int, float)) and p > 0]
    
    n_hist = len(hist)
    features["bars"] = float(n_hist)
    features["bar_sec"] = 180.0
    
    # 멀티 윈도우 수익률 계산 (가벼움)
    for n in [1, 3, 5, 10, 20]:
        if n_hist >= n and hist[-n] > 0:
            features[f"ret_{n}"] = (hist[-1] - hist[-n]) / hist[-n] * 100.0
        else:
            features[f"ret_{n}"] = 0.0
    
    # EMA는 필수만 계산 (12, 26)
    # [최적화] 간단한 근사 EMA 사용
    if n_hist >= 26:
        ema_fast_val = sum(hist[-12:]) / 12.0  # 간단한 SMA 근사
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
    
    # EMA slope 간소화
    if n_hist >= 13:
        prev_avg = sum(hist[-13:-1]) / 12.0
        features["ema_slope"] = (ema_fast_val - prev_avg) / prev_avg * 100.0 if prev_avg > 0 else 0.0
    else:
        features["ema_slope"] = 0.0
    
    # [2026-01-30 최적화] 간단한 통계로 대체 (indicator 호출 최소화)
    if n_hist >= 5:
        # 모멘텀 (단순 변화율)
        momentum = (hist[-1] - hist[-3]) / hist[-3] * 100.0 if hist[-3] > 0 else 0.0
        features["momentum_pct"] = float(momentum)
        
        # 추세 (60봉 또는 가용 데이터)
        lookback = min(n_hist, 60)
        trend_60 = (hist[-1] - hist[-lookback]) / hist[-lookback] * 100.0 if hist[-lookback] > 0 else 0.0
        features["dev_pct"] = float(trend_60)
        features["dev_prev_pct"] = float(trend_60)
        
        # RSI 간소화 (up/down 비율 근사)
        gains = sum(max(0, hist[i] - hist[i-1]) for i in range(-14, 0) if i-1 >= -n_hist)
        losses = sum(max(0, hist[i-1] - hist[i]) for i in range(-14, 0) if i-1 >= -n_hist)
        if gains + losses > 0:
            rsi = 100.0 * gains / (gains + losses)
        else:
            rsi = 50.0
        features["rsi"] = float(rsi)
        features["rsi_prev"] = float(rsi)
        
        # MACD는 EMA gap으로 대체 (이미 계산됨)
        features["macd_hist"] = features["ema_gap_pct"]
        features["macd_hist_prev"] = features["ema_gap_pct"]
        
        # 변동성 (표준편차 간소화)
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
        
        # Anchor (20일 평균)
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
        # 기본값
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
    
    # ATR% 간소화 (평균 변동폭)
    if n_hist >= 15:
        atr = sum(abs(hist[i] - hist[i-1]) for i in range(-14, 0)) / 14.0
        features["atr_pct"] = atr / price * 100.0 if price > 0 else 0.0
    else:
        features["atr_pct"] = 0.0
    
    # BB Width 간소화 (vol_pct 기반)
    features["bb_width"] = features["vol_pct"] * 4.0  # 2 std * 2 = 4
    
    # Realized Vol은 vol_pct 재사용
    features["realized_vol_5"] = features["vol_pct"]
    features["realized_vol_20"] = features["vol_pct"]
    
    # Regime 자체 계산 (context에서 못 가져올 경우 대비)
    trend_60 = features.get("dev_pct", 0.0)
    vol_pct = features.get("vol_pct", 0.0)
    ema_gap_pct = features.get("ema_gap_pct", 0.0)
    
    regime = _compute_regime_from_indicators(trend_60, vol_pct, ema_gap_pct)
    
    # context에서 regime 정보가 있으면 덮어쓰기
    if context is not None:
        if hasattr(context, "strategy_state"):
            ss = getattr(context, "strategy_state", {}) or {}
            if isinstance(ss, dict):
                ctx_regime = ss.get("regime")
                if ctx_regime and str(ctx_regime).upper() in ["BULL", "BEAR", "NEUTRAL"]:
                    regime = str(ctx_regime).upper()
    
    # Regime spread (context에서)
    features["regime_spread_pct"] = 0.0
    
    # 국면 one-hot
    regime_encoded = encode_regime_onehot(regime)
    features.update(regime_encoded)
    
    # 전략 정보 (context에서)
    strategy = "UNKNOWN"
    if context is not None:
        ctrls = getattr(context, "controls", None) or {}
        if isinstance(ctrls, dict):
            strat_cfg = ctrls.get("strategy", {}) or {}
            strategy = str(strat_cfg.get("name", "") or "").upper()
    strategy_encoded = encode_strategy_onehot(strategy)
    features.update(strategy_encoded)
    
    # 포지션 유무
    has_pos = False
    if context is not None and hasattr(context, "position"):
        pos = getattr(context, "position", None)
        has_pos = pos is not None and pos.get("qty", 0) > 0
    features["has_pos"] = 1.0 if has_pos else 0.0
    
    # 거래량 (기본값)
    features["vol_base_1m"] = 0.0
    features["volume"] = 0.0
    
    # 거래대금 정보 추출 시도
    notional = 0.0
    try:
        from app.core.hyper_price_store import price_store
        market = ""
        if context is not None:
            market = getattr(context, "market", "") or getattr(context, "code", "") or ""
        if market:
            notional = float(price_store.get_candle_1m_notional(market, 0.0) or 0.0)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[features] %s: %s", '거래대금 정보 추출 시도', exc, exc_info=True)
    features["notional_quote_1m"] = notional
    
    # vol_ratio와 notional_z는 히스토리 필요하므로 0.0 기본값
    features["notional_z"] = 0.0
    features["vol_ratio"] = 1.0
    
    # 시장 베타 (BTC 수익률 대비)
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
        logger.warning("[features] %s: %s", '시장 베타 (BTC 수익률 대비)', exc, exc_info=True)
    
    return features

def get_feature_names() -> List[str]:
    """학습에 사용되는 전체 feature 이름 목록 반환"""
    names = list(TRAINING_FEATURES)
    names.append("has_pos")
    for cat in REGIME_CATEGORIES:
        names.append(f"regime_{cat}")
    for cat in STRATEGY_CATEGORIES:
        names.append(f"strategy_{cat}")
    return names
