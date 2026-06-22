"""
BTC Leading Signal
BTC 움직임 기반 알트코인 선행 매매 신호
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class BTCLeadingSignal:
    """BTC 선행 신호"""
    direction: str  # 'UP' | 'DOWN' | 'NEUTRAL'
    btc_change_5m: float  # 5분 변화율
    btc_change_15m: float  # 15분 변화율
    strength: float  # 신호 강도 (0.0 ~ 1.0)
    confidence: float  # 신뢰도 (0.0 ~ 1.0)
    follow_altcoins: bool  # 알트코인 추종 예상 여부
    timestamp: float

class BTCLeadingSignalDetector:
    """
    BTC 선행 신호 감지기
    
    원리:
    - BTC 5분봉 +2% 돌파 → 알트코인 추종 (30초~2분 지연)
    - BTC 15분봉 +5% 돌파 → 강한 상승 추세 → 알트 본격 상승
    - BTC -2% 하락 → 알트 급락 (선제 대응)
    """
    
    def __init__(
        self,
        trade_client: Any,
        btc_market: str = "BTCUSDT",
        threshold_5m: float = 2.0,    # 5분 임계값 (%)
        threshold_15m: float = 5.0,   # 15분 임계값 (%)
        history_size: int = 100,
    ):
        self.trade_client = trade_client
        self.btc_market = btc_market
        self.threshold_5m = threshold_5m
        self.threshold_15m = threshold_15m
        
        # BTC 가격 히스토리
        self.price_history: deque = deque(maxlen=history_size)

        # 과호출 방지: 가격 수집/신호판단 최소 간격
        try:
            self.price_update_min_sec = max(
                0.2, float(os.getenv("OMA_BTC_SIGNAL_PRICE_MIN_SEC", "1.0") or 1.0)
            )
        except (TypeError, ValueError):
            logger.warning("[BTCLeading] price_update_min_sec 파싱 실패", exc_info=True)
            self.price_update_min_sec = 1.0
        try:
            self.detect_cache_sec = max(
                0.2, float(os.getenv("OMA_BTC_SIGNAL_CACHE_SEC", "1.0") or 1.0)
            )
        except (TypeError, ValueError):
            logger.warning("[BTCLeading] detect_cache_sec 파싱 실패", exc_info=True)
            self.detect_cache_sec = 1.0
        self._last_price_fetch_ts: float = 0.0
        self._last_detect_ts: float = 0.0
        self._last_detect_signal: Optional[BTCLeadingSignal] = None
        
        # 최근 신호
        self.last_signal: Optional[BTCLeadingSignal] = None
        self.signal_cooldown_sec = 120  # 2분 (300→120 단축, 급반전 대응)
        
        logger.info(
            f"BTCLeading: threshold_5m={threshold_5m}%, "
            f"threshold_15m={threshold_15m}% "
            f"(price_min={self.price_update_min_sec:.2f}s, cache={self.detect_cache_sec:.2f}s)"
        )
    
    def update_btc_price(self, *, force: bool = False) -> bool:
        """
        BTC 가격 업데이트
        """
        now = time.time()
        if not force and (now - self._last_price_fetch_ts) < self.price_update_min_sec:
            return False

        try:
            ticker = self.trade_client.get_ticker(self.btc_market)
            self._last_price_fetch_ts = now
            if ticker:
                price = float(
                    ticker.get("trade_price")
                    or ticker.get("lastPrice")
                    or ticker.get("last_price")
                    or 0
                )
                if price > 0:
                    self.price_history.append({
                        "timestamp": now,
                        "price": price,
                    })
                    return True
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error(f"BTCLeading: update error - {e}")
        return False
    
    def get_price_change(self, minutes: int) -> Optional[float]:
        """
        N분간 가격 변화율 계산
        
        Args:
            minutes: 시간 범위 (분)
        
        Returns:
            변화율 (%) 또는 None
        """
        if len(self.price_history) < 2:
            return None
        
        now = time.time()
        target_time = now - (minutes * 60)
        
        # 최신 가격
        latest = self.price_history[-1]
        
        # N분 전 가격 찾기
        past_price = None
        for entry in reversed(self.price_history):
            if entry["timestamp"] <= target_time:
                past_price = entry["price"]
                break
        
        if past_price is None or past_price <= 0:
            return None
        
        change_pct = (latest["price"] / past_price - 1.0) * 100.0
        return change_pct
    
    def detect_signal(self, *, force_refresh: bool = False) -> Optional[BTCLeadingSignal]:
        """
        BTC 선행 신호 감지
        """
        now = time.time()

        # 짧은 캐시: 같은 순간 다중 호출(전략/가드/스코어링)을 1회 계산으로 통합
        if (
            not force_refresh
            and self._last_detect_signal is not None
            and (now - self._last_detect_ts) < self.detect_cache_sec
        ):
            return self._last_detect_signal
        
        # 쿨다운 체크 (반전 감지 시 쿨다운 무시)
        if not force_refresh and self.last_signal:
            elapsed = now - self.last_signal.timestamp
            if elapsed < self.signal_cooldown_sec:
                _quick_chg = self.get_price_change(5)
                _prev_dir = self.last_signal.direction
                _reversed = (
                    _quick_chg is not None
                    and (
                        (_prev_dir == "UP" and _quick_chg < -self.threshold_5m * 0.5)
                        or (_prev_dir == "DOWN" and _quick_chg > self.threshold_5m * 0.5)
                    )
                )
                if not _reversed:
                    self._last_detect_signal = self.last_signal
                    self._last_detect_ts = now
                    return self.last_signal
        
        try:
            # BTC 가격 업데이트
            self.update_btc_price(force=force_refresh)
            
            # 5분/15분 변화율
            change_5m = self.get_price_change(5)
            change_15m = self.get_price_change(15)
            
            if change_5m is None:
                # 데이터가 막 쌓이는 구간에서는 직전 값을 잠깐 재사용해 호출 폭주를 완화
                if (
                    self._last_detect_signal is not None
                    and (now - self._last_detect_ts) < max(self.detect_cache_sec, 2.0)
                ):
                    return self._last_detect_signal
                return None
            if change_15m is None:
                change_15m = 0.0
            
            # 신호 판단
            direction = "NEUTRAL"
            strength = 0.0
            confidence = 0.5
            follow_altcoins = False
            
            # 상승 신호
            if change_5m >= self.threshold_5m:
                direction = "UP"
                strength = min(1.0, change_5m / self.threshold_5m / 2.0)
                confidence = 0.7
                follow_altcoins = True
                
                # 15분봉도 강한 상승
                if change_15m >= self.threshold_15m:
                    strength = min(1.0, strength + 0.3)
                    confidence = 0.9
            
            # 하락 신호
            elif change_5m <= -self.threshold_5m:
                direction = "DOWN"
                strength = min(1.0, abs(change_5m) / self.threshold_5m / 2.0)
                confidence = 0.8  # 하락은 더 빠르게 전파
                follow_altcoins = True
                
                # 15분봉도 강한 하락
                if change_15m <= -self.threshold_15m:
                    strength = min(1.0, strength + 0.3)
                    confidence = 0.95
            
            # 약한 상승 (1~2%)
            elif 1.0 <= change_5m < self.threshold_5m:
                direction = "UP"
                strength = 0.3
                confidence = 0.5
                follow_altcoins = True
            
            # 약한 하락 (-1~-2%)
            elif -self.threshold_5m < change_5m <= -1.0:
                direction = "DOWN"
                strength = 0.3
                confidence = 0.6
                follow_altcoins = True
            
            signal = BTCLeadingSignal(
                direction=direction,
                btc_change_5m=change_5m,
                btc_change_15m=change_15m,
                strength=strength,
                confidence=confidence,
                follow_altcoins=follow_altcoins,
                timestamp=now,
            )
            
            # 유의미한 신호만 저장
            if direction != "NEUTRAL":
                self.last_signal = signal
                logger.info(
                    f"BTCLeading: {direction} - "
                    f"5m={change_5m:+.2f}%, 15m={change_15m:+.2f}%, "
                    f"Strength={strength:.2f}, Confidence={confidence:.2f}"
                )

            self._last_detect_signal = signal
            self._last_detect_ts = now
            
            return signal
        
        except (AttributeError, TypeError, ValueError) as e:
            logger.error(f"BTCLeading: detect error - {e}")
            return None
    
    def adjust_score_for_btc_signal(
        self,
        base_score: float,
        strategy: str,
    ) -> float:
        """
        BTC 선행 신호 기반 스코어 조정
        
        Args:
            base_score: 기본 스코어
            strategy: 전략명
        
        Returns:
            조정된 스코어
        """
        signal = self.detect_signal()
        if not signal or not signal.follow_altcoins:
            return base_score
        
        # 전략별 BTC 민감도
        btc_sensitivity = {
            "PINGPONG": 1.5,   # 빠른 회전 → BTC 추종 활용
            "AUTOLOOP": 1.4,   # 중속 회전 → BTC 추종 활용
            "LIGHTNING": 1.6,  # 변동성 전략 → BTC 추종 핵심
            "SNIPER": 1.3,     # 저격 전략 → BTC 추종 참고
            "LADDER": 0.9,     # DCA → BTC 무관
            "GAZUA": 0.8,      # 장기 → BTC 무관
            "CONTRARIAN": 0.7, # 역발상 → BTC 역행
        }
        
        sensitivity = btc_sensitivity.get(strategy, 1.0)
        
        # 방향성 보너스
        bonus = 1.0
        if signal.direction == "UP":
            bonus = 1.0 + (signal.strength * signal.confidence * 0.3 * sensitivity)
        elif signal.direction == "DOWN":
            # 하락 신호 → 진입 회피
            bonus = 1.0 - (signal.strength * signal.confidence * 0.4 * sensitivity)
        
        adjusted = base_score * bonus
        
        logger.debug(
            f"BTCLeading: {strategy} Score {base_score:.2f} "
            f"→ {adjusted:.2f} (bonus={bonus:.2f})"
        )
        
        return adjusted
    
    def should_delay_entry(self) -> Tuple[bool, float]:
        """
        진입 지연 여부
        
        Returns:
            (지연 여부, 지연 시간 초)
        """
        signal = self.detect_signal()
        if not signal:
            return False, 0.0
        
        # BTC 급등 중 → 30초~2분 대기 (알트 추종 대기)
        if signal.direction == "UP" and signal.strength > 0.7:
            delay_sec = 30.0 + (signal.strength * 90.0)  # 30~120초
            return True, delay_sec
        
        # BTC 급락 중 → 진입 회피
        if signal.direction == "DOWN" and signal.strength > 0.7:
            return True, 300.0  # 5분 대기
        
        return False, 0.0

    @property
    def drift_mode(self) -> bool:
        """1시간 누적 -1.5% 이상 하락 감지 (Guard 미발동 구간).
        
        BTC가 급락은 아니지만 서서히 미끄러지는 구간.
        Guard는 5분 -2% 기준이라 이 구간을 못 잡음.
        """
        try:
            c1h = self.get_price_change(60)
            if c1h is None:
                return False
            return c1h <= -1.5
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("[BTCLeading] should_delay_entry 실패", exc_info=True)
            return False

    def get_regime_for_lightning(self) -> str:
        """Lightning v2 전용 국면 판정.
        
        Returns:
            "SHOCK"    — Guard 발동 수준 급락
            "DRIFT"    — 서서히 하락 (Guard 미발동)
            "RECOVERY" — 반등 초입
            "TREND"    — 평시
        """
        try:
            signal = self.detect_signal()
            if signal and signal.direction == "DOWN" and signal.strength > 0.7:
                return "SHOCK"
            if self.drift_mode:
                return "DRIFT"
            if signal and signal.direction == "UP" and signal.strength > 0.5:
                return "RECOVERY"
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[btc_leading_signal] %s: %s", 'btc_leading_signal.get_regime_for_lightning fallback', exc, exc_info=True)
        return "TREND"

    # ── BTC 레짐 → 전략별 행동 매핑 테이블 ──
    # 각 전략이 BTC 국면에 따라 어떤 행동을 취해야 하는지 정의
    _REGIME_STRATEGY_ACTIONS: Dict[str, Dict[str, Dict[str, Any]]] = {
        "SHOCK": {
            "LIGHTNING": {"entry": "halt", "size_mult": 0.5, "trailing_mult": 0.5},
            "GAZUA":     {"entry": "normal", "trailing_mult": 0.5, "dca_enabled": True},
            "SNIPER":    {"entry": "tighten", "threshold_mult": 1.5, "size_mult": 0.7},
            "PINGPONG":  {"entry": "halt", "size_mult": 0.5},
            "AUTOLOOP":  {"entry": "halt", "size_mult": 0.6},
            "CONTRARIAN": {"entry": "aggressive", "size_mult": 1.3},
            "LADDER":    {"entry": "normal"},
        },
        "DRIFT": {
            "LIGHTNING": {"entry": "cautious", "threshold_mult": 1.5, "size_mult": 0.7},
            "GAZUA":     {"entry": "normal", "trailing_mult": 0.7},
            "SNIPER":    {"entry": "cautious", "threshold_mult": 1.2},
            "PINGPONG":  {"entry": "cautious", "size_mult": 0.8},
            "AUTOLOOP":  {"entry": "cautious", "size_mult": 0.8},
            "CONTRARIAN": {"entry": "normal"},
            "LADDER":    {"entry": "normal"},
        },
        "RECOVERY": {
            "LIGHTNING": {"entry": "aggressive", "size_mult": 1.2},
            "GAZUA":     {"entry": "normal", "trailing_mult": 1.0},
            "SNIPER":    {"entry": "aggressive", "threshold_mult": 0.8, "size_mult": 1.1},
            "PINGPONG":  {"entry": "aggressive", "size_mult": 1.1},
            "AUTOLOOP":  {"entry": "aggressive", "size_mult": 1.1},
            "CONTRARIAN": {"entry": "cautious", "size_mult": 0.7},
            "LADDER":    {"entry": "normal"},
        },
        "TREND": {
            "LIGHTNING": {"entry": "normal"},
            "GAZUA":     {"entry": "normal"},
            "SNIPER":    {"entry": "normal"},
            "PINGPONG":  {"entry": "normal"},
            "AUTOLOOP":  {"entry": "normal"},
            "CONTRARIAN": {"entry": "normal"},
            "LADDER":    {"entry": "normal"},
        },
    }

    def get_strategy_action(self, strategy: str) -> Dict[str, Any]:
        """BTC 현재 국면에 따른 전략별 행동 권장값 반환.

        Returns:
            dict with keys like "entry", "size_mult", "trailing_mult", "threshold_mult"
            - entry: "normal" | "halt" | "cautious" | "aggressive" | "tighten"
            - size_mult: 진입 사이즈 배율 (1.0 = 100%)
            - trailing_mult: trailing stop 콜백 배율 (0.5 = 50% 타이트닝)
            - threshold_mult: 진입 임계값 배율 (1.5 = 50% 상향)
        """
        regime = self.get_regime_for_lightning()
        strat = str(strategy).upper()
        regime_map = self._REGIME_STRATEGY_ACTIONS.get(regime, self._REGIME_STRATEGY_ACTIONS["TREND"])
        action = dict(regime_map.get(strat, {"entry": "normal"}))
        action["regime"] = regime
        return action

# 싱글톤 인스턴스
_DETECTOR_INSTANCE: Optional[BTCLeadingSignalDetector] = None

def get_btc_leading_detector() -> Optional[BTCLeadingSignalDetector]:
    """
    BTC Leading Signal Detector 싱글톤 인스턴스 반환
    """
    global _DETECTOR_INSTANCE
    return _DETECTOR_INSTANCE

def initialize_btc_leading_detector(trade_client: Any) -> BTCLeadingSignalDetector:
    """
    BTC Leading Signal Detector 초기화
    """
    global _DETECTOR_INSTANCE
    if _DETECTOR_INSTANCE is None:
        try:
            thr_5m = float(os.getenv("OMA_BTC_SIGNAL_THRESHOLD_5M", "2.0") or 2.0)
        except (TypeError, ValueError):
            logger.warning("[BTCLeading] threshold_5m 파싱 실패", exc_info=True)
            thr_5m = 2.0
        try:
            thr_15m = float(os.getenv("OMA_BTC_SIGNAL_THRESHOLD_15M", "5.0") or 5.0)
        except (TypeError, ValueError):
            logger.warning("[BTCLeading] threshold_15m 파싱 실패", exc_info=True)
            thr_15m = 5.0
        _DETECTOR_INSTANCE = BTCLeadingSignalDetector(
            trade_client,
            threshold_5m=max(0.5, abs(thr_5m)),
            threshold_15m=max(1.0, abs(thr_15m)),
        )
        try:
            cooldown = int(float(os.getenv("OMA_BTC_SIGNAL_COOLDOWN_SEC", "120") or 120))
            _DETECTOR_INSTANCE.signal_cooldown_sec = max(10, cooldown)
        except (TypeError, ValueError) as exc:
            logger.warning("[btc_leading_signal] %s: %s", 'btc_leading_signal.initialize_btc_leading_detector fallback', exc, exc_info=True)
        logger.info("BTC Leading Signal Detector initialized")
    return _DETECTOR_INSTANCE
