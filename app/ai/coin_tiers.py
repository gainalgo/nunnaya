# ============================================================
# File: app/ai/coin_tiers.py
# Autocoin OS v3-H — 전략 기반 코인 가중치 시스템
# ============================================================
"""
기존 전략 시스템(reserved_selector, topn_selector)과 통합하여
AI 학습 시 전략별 특성을 반영합니다.

전략 프로필 (topn_selector.PROFILE_WEIGHTS 참조):
- PINGPONG: 횡보/변동성 스캘핑
- LADDER: 하락 추세 DCA 매집
- LIGHTNING: 모멘텀 돌파
- GAZUA: 강한 상승 + 홀딩
- AUTOROPE: 유동성 + 적응형

용도:
1. 학습 시 전략별 샘플 가중치 부여
2. 전략 적합성을 AI feature로 추가
3. 예측 신뢰도 조정
"""

from __future__ import annotations

from typing import Dict, Optional, Any
from enum import Enum


class StrategyProfile(str, Enum):
    """전략 프로필"""
    PINGPONG = "pingpong"
    LADDER = "ladder"
    LIGHTNING = "lightning"
    GAZUA = "gazua"
    AUTOROPE = "autorope"
    AUTOLOOP = "autoloop"
    CONTRARIAN = "contrarian"
    UNKNOWN = "unknown"


# 전략별 샘플 가중치 (학습 시 적용)
# - 높은 가중치: 모델이 해당 전략 패턴을 더 집중 학습
# - 낮은 가중치: 노이즈 많은 데이터 영향 감소
STRATEGY_SAMPLE_WEIGHTS: Dict[str, float] = {
    # 안정적인 전략 (높은 가중치)
    "pingpong": 1.5,      # 횡보장에서 안정적
    "autoloop": 1.5,      # 검증된 자동화
    
    # 추세 전략 (중간 가중치)
    "ladder": 1.2,        # DCA 패턴
    "gazua": 1.0,         # 모멘텀 홀딩
    "lightning": 1.0,     # 돌파 트레이딩
    
    # 실험적 전략 (낮은 가중치)
    "autorope": 0.8,
    "contrarian": 0.7,    # 역발상 (노이즈 많음)
    "unknown": 0.5,
}

# 전략별 예측 신뢰도 스케일
STRATEGY_CONFIDENCE_SCALE: Dict[str, float] = {
    "pingpong": 1.2,      # 예측 패턴 명확
    "autoloop": 1.2,
    "ladder": 1.1,
    "lightning": 1.0,
    "gazua": 0.9,         # 변동성 높음
    "autorope": 0.9,
    "contrarian": 0.8,    # 예측 어려움
    "unknown": 0.7,
}


# 전략별 AI 행동 임계값
# ai_buy_threshold: 이 점수 이상이어야 매수 허용
# ai_sell_threshold: 이 점수 이하면 매수 차단 또는 청산 고려
STRATEGY_AI_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "pingpong": {
        "ai_buy_threshold": 0.40,   # 횡보장이라 약간 보수적
        "ai_sell_threshold": 0.30,
        "ai_tp_scale_high": 1.3,    # AI 높으면 TP +30%
        "ai_tp_scale_low": 0.8,     # AI 낮으면 TP -20%
        "ai_sl_scale_high": 1.2,    # AI 높으면 SL 완화
        "ai_sl_scale_low": 0.7,     # AI 낮으면 SL 타이트
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
        "ai_buy_threshold": 0.55,   # 모멘텀이라 확신 있을 때만
        "ai_sell_threshold": 0.40,
        "ai_tp_scale_high": 1.5,
        "ai_tp_scale_low": 0.6,
        "ai_sl_scale_high": 1.2,
        "ai_sl_scale_low": 0.5,
    },
    "gazua": {
        "ai_buy_threshold": 0.50,
        "ai_sell_threshold": 0.35,
        "ai_tp_scale_high": 1.6,    # 홀딩이라 더 높은 목표
        "ai_tp_scale_low": 0.7,
        "ai_sl_scale_high": 1.4,
        "ai_sl_scale_low": 0.6,
    },
    "ladder": {
        "ai_buy_threshold": 0.35,   # DCA라 진입 쉽게
        "ai_sell_threshold": 0.25,
        "ai_tp_scale_high": 1.2,
        "ai_tp_scale_low": 0.9,
        "ai_sl_scale_high": 1.1,
        "ai_sl_scale_low": 0.8,
    },
    "contrarian": {
        "ai_buy_threshold": 0.30,   # 역발상이라 AI와 반대로 갈 수도
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


# Regime별 전략 적합도 (0.0 ~ 1.0)
# 높을수록 해당 국면에서 전략이 잘 작동함
REGIME_STRATEGY_FIT: Dict[str, Dict[str, float]] = {
    "BULL": {
        "pingpong": 0.6,    # 상승장에서 횡보는 비효율
        "autoloop": 0.7,
        "lightning": 0.9,   # 모멘텀 전략 최적
        "gazua": 0.95,      # 홀딩 전략 최적
        "ladder": 0.5,      # DCA는 상승장에서 비효율
        "contrarian": 0.3,  # 역발상은 상승장에서 위험
        "autorope": 0.7,
    },
    "BEAR": {
        "pingpong": 0.5,
        "autoloop": 0.5,
        "lightning": 0.3,   # 모멘텀은 하락장 위험
        "gazua": 0.2,       # 홀딩은 하락장 위험
        "ladder": 0.8,      # DCA는 하락장에서 기회
        "contrarian": 0.9,  # 역발상 최적
        "autorope": 0.6,
    },
    "NEUTRAL": {
        "pingpong": 0.95,   # 횡보장 최적
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
    """전략명 정규화"""
    if not strategy:
        return "unknown"
    s = str(strategy).lower().strip()
    # 별칭 처리
    aliases = {
        "auto_loop": "autoloop",
        "ping_pong": "pingpong",
        "auto_rope": "autorope",
    }
    return aliases.get(s, s)


def get_sample_weight(strategy: Optional[str] = None, market: Optional[str] = None) -> float:
    """
    학습 시 샘플 가중치 반환.
    
    Args:
        strategy: 전략명 (reason에서 추출)
        market: 마켓명 (옵션, 향후 확장용)
    
    Returns:
        가중치 (0.5 ~ 1.5)
    """
    s = normalize_strategy(strategy)
    return STRATEGY_SAMPLE_WEIGHTS.get(s, 1.0)


def get_confidence_scale(strategy: Optional[str] = None) -> float:
    """
    예측 시 신뢰도 스케일 반환.
    
    Returns:
        스케일 (0.7 ~ 1.2)
    """
    s = normalize_strategy(strategy)
    return STRATEGY_CONFIDENCE_SCALE.get(s, 1.0)


def extract_strategy_from_reason(reason: Optional[str]) -> str:
    """
    buy_reason/sell_reason에서 전략명 추출.
    
    예: "engine_buy:lightning" → "lightning"
         "ladder_entry" → "ladder"
    """
    if not reason:
        return "unknown"
    
    r = str(reason).lower()
    
    # 패턴 매칭
    for strat in ["pingpong", "autoloop", "ladder", "lightning", "gazua", "autorope", "contrarian"]:
        if strat in r:
            return strat
    
    return "unknown"


def get_strategy_feature_weights() -> Dict[str, Dict[str, float]]:
    """
    topn_selector.PROFILE_WEIGHTS와 동일한 전략별 지표 가중치 반환.
    AI feature 생성 시 참조용.
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
    """전략을 one-hot 인코딩으로 변환 (AI feature용)"""
    s = normalize_strategy(strategy)
    strategies = ["pingpong", "ladder", "lightning", "gazua", "autoloop", "autorope", "contrarian"]
    
    result = {}
    for strat in strategies:
        result[f"strategy_{strat}"] = 1.0 if s == strat else 0.0
    
    return result


def get_strategy_thresholds(strategy: Optional[str] = None) -> Dict[str, float]:
    """전략별 AI 임계값 반환"""
    s = normalize_strategy(strategy)
    return STRATEGY_AI_THRESHOLDS.get(s, STRATEGY_AI_THRESHOLDS["unknown"])


def get_regime_fit(regime: str, strategy: Optional[str] = None) -> float:
    """국면-전략 적합도 반환 (0.0 ~ 1.0)"""
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
    AI 점수를 전략과 국면에 맞게 조정.
    
    Returns:
        {
            "adjusted_score": float,  # 조정된 점수
            "should_buy": bool,       # 매수 허용 여부
            "should_sell": bool,      # 청산 고려 여부
            "tp_scale": float,        # TP 조절 배수
            "sl_scale": float,        # SL 조절 배수
            "confidence": float,      # 신뢰도 (국면 적합도 기반)
        }
    """
    thresholds = get_strategy_thresholds(strategy)
    regime_fit = get_regime_fit(regime, strategy)
    
    # 매수/청산 판단
    should_buy = ai_score >= thresholds["ai_buy_threshold"]
    should_sell = ai_score <= thresholds["ai_sell_threshold"]
    
    # 국면 적합도로 신뢰도 조정
    # 적합도 낮으면 매수 조건 더 엄격
    if regime_fit < 0.5:
        should_buy = ai_score >= (thresholds["ai_buy_threshold"] + 0.1)
    
    # TP/SL 스케일 (AI 점수 기반)
    if ai_score >= 0.6:
        tp_scale = thresholds["ai_tp_scale_high"]
        sl_scale = thresholds["ai_sl_scale_high"]
    elif ai_score <= 0.4:
        tp_scale = thresholds["ai_tp_scale_low"]
        sl_scale = thresholds["ai_sl_scale_low"]
    else:
        # 선형 보간
        t = (ai_score - 0.4) / 0.2  # 0~1
        tp_scale = thresholds["ai_tp_scale_low"] + t * (thresholds["ai_tp_scale_high"] - thresholds["ai_tp_scale_low"])
        sl_scale = thresholds["ai_sl_scale_low"] + t * (thresholds["ai_sl_scale_high"] - thresholds["ai_sl_scale_low"])
    
    # 국면 적합도로 스케일 추가 조정
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
    """전체 전략 정보 반환 (디버깅/대시보드용)"""
    return {
        "sample_weights": STRATEGY_SAMPLE_WEIGHTS,
        "confidence_scales": STRATEGY_CONFIDENCE_SCALE,
        "feature_weights": get_strategy_feature_weights(),
        "ai_thresholds": STRATEGY_AI_THRESHOLDS,
        "regime_fit": REGIME_STRATEGY_FIT,
    }
