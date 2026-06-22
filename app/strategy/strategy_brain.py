# ============================================================
# File: app/strategy/strategy_brain.py
# Strategy Brain v3-H — AI Market Sensor Module (RSI + MACD HIST 포함)
# ------------------------------------------------------------
# PATCH 2026-01-01
# - volatility/momentum을 "가격 단위"가 아닌 "퍼센트(%)" 스케일로 정규화
#
# PATCH 2026-01-11 (AI Gate)
# - app/data/ai_gate_settings.json (strictness) + ai_market_scoreboard.json 기반으로
#   마켓별 AI 영향도를 안전하게 0으로 만들기 위해,
#   성능 기준 미달 시 ai_prediction=0.5, ai_confidence=0.0으로 중립화한다.
# - 대시보드 슬라이더로 strictness를 조절 가능(0~100).
#
# PATCH 2026-01-28 (Feature Alignment)
# - 학습/추론 feature 일치를 위해 app.ai.features 모듈 사용
# - LightGBM 모델 지원 추가
# ============================================================

from __future__ import annotations

from typing import Dict, Any, List
import logging
import math
import os
import json
import time
import pickle

logger = logging.getLogger(__name__)

from app.strategy import indicators

# Feature extractor (학습/추론 일치용)
try:
    from app.ai.features import extract_features_from_runtime, get_feature_names
except ImportError:
    logging.getLogger(__name__).warning("features module not available, using fallback", exc_info=True)
    def extract_features_from_runtime(*args, **kwargs): return {}
    def get_feature_names(): return []


# ------------------------------------------------------------
# AI Gate (market-wise performance gate)
# ------------------------------------------------------------
_AI_DATA_DIR = os.path.join("app", "data")
_AI_GATE_PATH = os.path.join(_AI_DATA_DIR, "ai_gate_settings.json")
_AI_SCOREBOARD_PATH = os.path.join(_AI_DATA_DIR, "ai_market_scoreboard.json")

_GATE_CACHE: Dict[str, Any] = {"ts": 0.0, "mtime": 0.0, "data": None}
_SCORE_CACHE: Dict[str, Any] = {"ts": 0.0, "mtime": 0.0, "data": None}


def _thresholds_from_strictness(strictness: int) -> Dict[str, Any]:
    s = max(0, min(100, int(strictness)))
    # Linear mappings (operational defaults)
    min_test_samples = int(round(150 + (800 - 150) * (s / 100.0)))
    min_acc_mean = float(0.52 + (0.60 - 0.52) * (s / 100.0))
    min_high_conf_acc_mean = float(0.55 + (0.65 - 0.55) * (s / 100.0))
    return {
        "strictness": s,
        "min_test_samples": min_test_samples,
        "min_acc_mean": min_acc_mean,
        "min_high_conf_acc_mean": min_high_conf_acc_mean,
    }


def _load_gate_settings_cached(max_age_sec: float = 5.0) -> Dict[str, Any]:
    now = time.time()
    default = _thresholds_from_strictness(60)
    default["updated_ts"] = 0.0

    try:
        # fresh cache
        if _GATE_CACHE["data"] is not None and (now - float(_GATE_CACHE["ts"] or 0.0)) < max_age_sec:
            return _GATE_CACHE["data"]  # type: ignore

        if not os.path.exists(_AI_GATE_PATH):
            _GATE_CACHE.update({"ts": now, "mtime": 0.0, "data": default})
            return default

        mtime = float(os.path.getmtime(_AI_GATE_PATH))
        if _GATE_CACHE["data"] is not None and float(_GATE_CACHE["mtime"] or 0.0) == mtime:
            _GATE_CACHE["ts"] = now
            return _GATE_CACHE["data"]  # type: ignore

        with open(_AI_GATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, dict):
            strict = int(raw.get("strictness", 60))
            data = _thresholds_from_strictness(strict)
            data["updated_ts"] = float(raw.get("updated_ts") or 0.0)
        else:
            data = default

        _GATE_CACHE.update({"ts": now, "mtime": mtime, "data": data})
        return data
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[AI Gate] settings 로드 실패", exc_info=True)
        _GATE_CACHE.update({"ts": now, "mtime": 0.0, "data": default})
        return default


def _load_scoreboard_cached(max_age_sec: float = 5.0) -> Dict[str, Any]:
    now = time.time()
    try:
        if _SCORE_CACHE["data"] is not None and (now - float(_SCORE_CACHE["ts"] or 0.0)) < max_age_sec:
            return _SCORE_CACHE["data"]  # type: ignore

        if not os.path.exists(_AI_SCOREBOARD_PATH):
            _SCORE_CACHE.update({"ts": now, "mtime": 0.0, "data": {}})
            return {}

        mtime = float(os.path.getmtime(_AI_SCOREBOARD_PATH))
        if _SCORE_CACHE["data"] is not None and float(_SCORE_CACHE["mtime"] or 0.0) == mtime:
            _SCORE_CACHE["ts"] = now
            return _SCORE_CACHE["data"]  # type: ignore

        with open(_AI_SCOREBOARD_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}

        _SCORE_CACHE.update({"ts": now, "mtime": mtime, "data": raw})
        return raw
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[AI Scoreboard] 로드 실패", exc_info=True)
        _SCORE_CACHE.update({"ts": now, "mtime": 0.0, "data": {}})
        return {}


def _ai_gate_decide(market: str) -> Dict[str, Any]:
    gate = _load_gate_settings_cached()
    sb = _load_scoreboard_cached()

    markets = sb.get("markets") if isinstance(sb.get("markets"), dict) else {}
    rec = markets.get(market) if isinstance(markets, dict) else None
    if not isinstance(rec, dict):
        return {"enabled": True, "reason": "no_score", "gate": gate}

    min_samples = int(gate.get("min_test_samples") or 0)
    min_acc = float(gate.get("min_acc_mean") or 0.0)
    min_hc = float(gate.get("min_high_conf_acc_mean") or 0.0)

    test_samples = float(rec.get("test_samples") or 0.0)
    acc_mean = float(rec.get("acc_mean") or 0.0)
    hc_acc_mean = float(rec.get("high_conf_acc_mean") or 0.0)

    if test_samples < min_samples:
        return {"enabled": False, "reason": f"samples<{min_samples}", "gate": gate, "acc_mean": acc_mean, "high_conf_acc_mean": hc_acc_mean, "test_samples": test_samples}
    if acc_mean < min_acc:
        return {"enabled": False, "reason": f"acc<{min_acc:.3f}", "gate": gate, "acc_mean": acc_mean, "high_conf_acc_mean": hc_acc_mean, "test_samples": test_samples}
    if hc_acc_mean < min_hc:
        return {"enabled": False, "reason": f"high_conf<{min_hc:.3f}", "gate": gate, "acc_mean": acc_mean, "high_conf_acc_mean": hc_acc_mean, "test_samples": test_samples}

    return {"enabled": True, "reason": "ok", "gate": gate, "acc_mean": acc_mean, "high_conf_acc_mean": hc_acc_mean, "test_samples": test_samples}


class StrategyBrainOutput:
    """Brain 분석 결과 직렬화 객체."""
    def __init__(
        self,
        trend=0,
        momentum=0,
        volatility=0,
        rsi=50,
        macd_histogram=0,
        ai_prediction=0.5,
        ai_confidence=0.0,
        sma_fast=0,
        sma_slow=0,
        volume_change_pct=0,
        ai_gate_enabled=True,
        ai_gate_reason="",
        ai_gate_strictness=60,
    ):
        self.trend = trend
        self.momentum = momentum
        self.volatility = volatility
        self.rsi = rsi
        self.macd_histogram = macd_histogram
        self.ai_prediction = ai_prediction
        self.ai_confidence = ai_confidence
        self.sma_fast = sma_fast
        self.sma_slow = sma_slow
        self.volume_change_pct = volume_change_pct
        self.ai_gate_enabled = bool(ai_gate_enabled)
        self.ai_gate_reason = str(ai_gate_reason or "")
        self.ai_gate_strictness = int(ai_gate_strictness or 60)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trend": self.trend,
            "momentum": self.momentum,
            "volatility": self.volatility,
            "rsi": self.rsi,
            "macd_histogram": self.macd_histogram,
            "ai_prediction": self.ai_prediction,
            "ai_confidence": self.ai_confidence,
            "sma_fast": self.sma_fast,
            "sma_slow": self.sma_slow,
            "volume_change_pct": self.volume_change_pct,
            "ai_gate_enabled": self.ai_gate_enabled,
            "ai_gate_reason": self.ai_gate_reason,
            "ai_gate_strictness": self.ai_gate_strictness,
        }


class StrategyAISensor:
    """AI 모델 로드 및 추론을 전담하는 센서 클래스."""

    def __init__(self):
        self._model = None
        self._model_loaded = False
        self._model_meta: Dict[str, Any] = {}
        self._model_type: str = "unknown"
        self._cached_model_info = None

    def _load_model(self):
        if self._model_loaded:
            return self._model

        self._model_loaded = True

        # 메타데이터 로드 (feature 순서 등)
        meta_path = os.path.join("app", "data", "ai_model_meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    self._model_meta = json.load(f)
                    self._model_type = self._model_meta.get("model_type", "unknown")
            except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[strategy_brain] %s: %s", '메타데이터 로드 (feature 순서 등)', exc, exc_info=True)

        # 1) Pickle (sklearn/LightGBM)
        path_pkl = os.path.join("app", "data", "ai_model.pkl")
        if os.path.exists(path_pkl):
            try:
                with open(path_pkl, "rb") as f:
                    self._model = pickle.load(f)
                return self._model
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("[strategy_brain] %s: %s", '1) Pickle (sklearn/LightGBM)', exc, exc_info=True)

        # 2) Keras (optional)
        path_h5 = os.path.join("app", "data", "ai_model.h5")
        if os.path.exists(path_h5):
            try:
                import tensorflow as tf  # type: ignore
                self._model = tf.keras.models.load_model(path_h5)
                self._model_type = "Keras"
                return self._model
            except (ImportError, AttributeError, TypeError) as exc:
                logger.warning("[strategy_brain] %s: %s", '2) Keras (optional)', exc, exc_info=True)

        return None

    def predict(self, features: List[float], feature_names: List[str]) -> float:
        """
        AI 예측 수행.
        - LightGBM Booster: model.predict() 직접 호출
        - sklearn: predict_proba() 사용

        [PERF] 2026-03-21: pandas DataFrame → numpy array 직접 전달
        LightGBM/sklearn 모두 numpy 네이티브 지원. pandas 생성 오버헤드 제거 (~1-3ms/call).
        """
        model = self._load_model()
        if model is None:
            return 0.5

        try:
            import numpy as np
            X = np.array([features], dtype=np.float64)

            # LightGBM Booster인 경우
            if hasattr(model, "predict") and self._model_type == "LightGBM":
                # LightGBM Booster.predict()는 확률 반환
                prob = model.predict(X)[0]
                return float(prob)

            # sklearn 스타일 (predict_proba)
            if hasattr(model, "predict_proba"):
                return float(model.predict_proba(X)[0][1])

            # 일반 predict
            if hasattr(model, "predict"):
                out = model.predict(X)
                try:
                    return float(out[0][0])
                except (IndexError, TypeError):
                    logger.warning("[Brain] predict output 인덱싱 fallback: out[0]", exc_info=True)
                    return float(out[0])
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[strategy_brain] %s: %s", '일반 predict', exc, exc_info=True)

        return 0.5
    
    def predict_with_features(self, feature_dict: Dict[str, float]) -> float:
        """
        feature dict를 받아서 메타데이터의 feature 순서에 맞춰 예측.
        학습/추론 feature 일치 보장.
        """
        model = self._load_model()
        if model is None:
            return 0.5
        
        # 메타에서 feature 순서 가져오기
        expected_features = self._model_meta.get("features", [])
        if not expected_features:
            # fallback: 기존 방식
            expected_features = list(feature_dict.keys())
        
        # feature 순서 맞춰서 배열 생성
        features = []
        for feat in expected_features:
            val = feature_dict.get(feat)
            if val is None or (isinstance(val, float) and math.isnan(val)):
                features.append(float("nan"))  # LightGBM은 NaN 처리 가능
            else:
                features.append(float(val))
        
        return self.predict(features, expected_features)

    def reload_model(self) -> None:
        self._model = None
        self._model_loaded = False
        self._model_meta = {}
        self._model_type = "unknown"
        self._cached_model_info = None
        self._load_model()

    def get_model_info(self) -> Dict[str, Any]:
        """현재 로드된 모델 정보 반환 (캐시됨, reload_model 시 무효화)"""
        if self._cached_model_info is not None:
            return self._cached_model_info
        self._load_model()
        self._cached_model_info = {
            "type": self._model_type,
            "features": self._model_meta.get("features", []),
            "accuracy": self._model_meta.get("accuracy"),
            "auc": self._model_meta.get("auc"),
            "loaded": self._model is not None,
        }
        return self._cached_model_info


class StrategyBrain:
    """시장 분석 Brain 모듈."""

    def __init__(self):
        self.ai_sensor = StrategyAISensor()

    def reload_model(self):
        self.ai_sensor.reload_model()

    def analyze(
        self,
        market: str,
        price: float,
        price_history: List[float] | None = None,
        policy: Dict[str, Any] | None = None,
        context: Any | None = None,
    ) -> StrategyBrainOutput:

        if not price_history or len(price_history) < 5:
            return StrategyBrainOutput(
                trend=0.0,
                momentum=0.0,
                volatility=0.0,
                rsi=50.0,
                macd_histogram=0.0,
            )

        # sanitize history — strategy_initializer.py가 이미 정화 완료
        hist = list(price_history) if price_history else []

        if len(hist) < 5:
            return StrategyBrainOutput(
                trend=0.0,
                momentum=0.0,
                volatility=0.0,
                rsi=50.0,
                macd_histogram=0.0,
            )

        # Volume history (optional) - if context provides it
        vol_hist: List[float] = []
        if context is not None and hasattr(context, "volume_history"):
            try:
                vol_hist = list(getattr(context, "volume_history"))[-20:]
            except (KeyError, AttributeError, TypeError):
                logger.warning("[Brain] volume_history 접근 실패", exc_info=True)
                vol_hist = []

        params = (policy or {}).get("params", {}) or {}
        rsi_len = int(params.get("ai_rsi_len", 14))
        macd_fast = int(params.get("ai_macd_fast", 12))
        macd_slow = int(params.get("ai_macd_slow", 26))
        macd_signal = int(params.get("ai_macd_signal", 9))
        sma_fast_len = int(params.get("ai_sma_fast", 5))
        sma_slow_len = int(params.get("ai_sma_slow", 20))

        # Indicators
        trend_len = min(len(hist), 60)
        trend = indicators.trend(hist, trend_len) or 0.0
        momentum = indicators.trend(hist, 3) or 0.0
        vol_len = min(max(0, len(hist) - 1), 20)
        volatility = indicators.volatility(hist, vol_len) or 0.0
        rsi = indicators.rsi(hist, rsi_len) or 50.0

        _, _, macd_hist_val = indicators.macd(hist, macd_fast, macd_slow, macd_signal)
        macd_hist = 0.0
        if macd_hist_val is not None and price > 0:
            macd_hist = (float(macd_hist_val) / float(price)) * 100.0

        sma_fast = indicators.sma(hist, sma_fast_len) or 0.0
        sma_slow = indicators.sma(hist, sma_slow_len) or 0.0

        # Volume change pct (tick history only; candle notional is injected elsewhere)
        volume_change_pct = 0.0
        if len(vol_hist) >= 13:
            recent_vol = sum(vol_hist[-3:]) / 3.0
            prev_vol = sum(vol_hist[-13:-3]) / 10.0
            if prev_vol > 0:
                volume_change_pct = (recent_vol - prev_vol) / prev_vol * 100.0

        # AI inference
        # 새 feature extractor 사용 (학습/추론 일치)
        feature_dict = extract_features_from_runtime(
            price=price,
            price_history=hist,
            context=context,
            brain_output=None,
        )
        
        # 모델이 기대하는 feature 목록이 있으면 그것 사용, 없으면 fallback
        model_info = self.ai_sensor.get_model_info()
        expected_features = model_info.get("features", [])
        
        if expected_features:
            # 새 방식: feature dict를 모델 기대 순서에 맞춰 예측
            ai_score = self.ai_sensor.predict_with_features(feature_dict)
        else:
            # 기존 방식 fallback (모델 메타가 없는 경우)
            dev_pct = trend
            momentum_pct = momentum
            vol_pct = volatility
            features_list = [dev_pct, momentum_pct, vol_pct, rsi, macd_hist, volume_change_pct]
            feature_names = ["dev_pct", "momentum_pct", "vol_pct", "rsi", "macd_hist", "volume_change_pct"]
            ai_score = self.ai_sensor.predict(features_list, feature_names)
        # confidence = distance from neutral (clamped to [0.0, 1.0])
        ai_confidence = min(1.0, abs(float(ai_score) - 0.5) * 2.0)

        # ===== AI Gate (market-wise performance) =====
        gate_dec = _ai_gate_decide(str(market))
        ai_gate_enabled = bool(gate_dec.get("enabled", True))
        ai_gate_reason = str(gate_dec.get("reason") or "")
        ai_gate_strictness = int((gate_dec.get("gate") or {}).get("strictness", 60))

        # If disabled: neutralize AI output
        if not ai_gate_enabled:
            ai_score = 0.5
            ai_confidence = 0.0

        return StrategyBrainOutput(
            trend=float(trend),
            momentum=float(momentum),
            volatility=float(volatility),
            rsi=float(rsi),
            macd_histogram=float(macd_hist),
            ai_prediction=float(ai_score),
            ai_confidence=float(ai_confidence),
            sma_fast=float(sma_fast),
            sma_slow=float(sma_slow),
            volume_change_pct=float(volume_change_pct),
            ai_gate_enabled=bool(ai_gate_enabled),
            ai_gate_reason=str(ai_gate_reason),
            ai_gate_strictness=int(ai_gate_strictness),
        )
