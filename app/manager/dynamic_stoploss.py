# ============================================================
# File: app/manager/dynamic_stoploss.py
# Autocoin OS v3-H — Dynamic Stop-Loss System
# ------------------------------------------------------------
# 목적:
# - 시간 경과에 따른 손절 라인 동적 조정
# - 오래 보유할수록 타이트한 손절
# - 트레일링 스탑 지원
# ============================================================

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from app.core.constants import env_bool

# [MIGRATED 2026-01-23] Market Regime 연동
try:
    from app.core.market_regime import get_regime_detector, MarketRegime
except ImportError:
    logger.warning("[DynamicStopLoss] market_regime import failed", exc_info=True)
    get_regime_detector = None
    MarketRegime = None

REGIME_TP_SL_ENABLED = env_bool("OMA_REGIME_TP_SL_ENABLED", default=False)

if TYPE_CHECKING:
    from typing import Protocol

    class RegimeInfo(Protocol):
        """국면 정보 프로토콜 (타입 힌트용)."""
        @property
        def regime(self) -> Any: ...


# 변동성 기반 스케일링 상수
ATR_REF = 1.5  # 기준 변동성 (%)
VOL_SCALE_MIN = 0.7
VOL_SCALE_MAX = 1.8

# 국면별 SL 폭 배수
REGIME_SL_MULT: Dict[str, float] = {
    "BULL": 1.20,      # SL 넓게
    "BEAR": 0.75,      # SL 타이트
    "SIDEWAYS": 0.90,
    "VOLATILE": 1.30,
}

# TP 추천 상수
TP_RATIO = 1.5  # SL 대비 TP 비율
TP_MIN = 1.5    # 최소 TP %

REGIME_TP_MULT: Dict[str, float] = {
    "BULL": 1.25,
    "BEAR": 0.70,
    "SIDEWAYS": 0.85,
    "VOLATILE": 1.10,
}


class StopLossMode(Enum):
    """손절 모드."""
    FIXED = "fixed"              # 고정 손절
    TIME_DECAY = "time_decay"    # 시간 경과에 따라 타이트해짐
    TRAILING = "trailing"        # 트레일링 스탑
    HYBRID = "hybrid"            # 혼합 (시간 + 트레일링)


@dataclass
class PositionInfo:
    """포지션 정보."""
    market: str
    entry_price: float
    entry_ts: float
    current_price: float
    quantity: float
    highest_price: float = 0.0  # 보유 중 최고가
    lowest_price: float = 0.0   # 보유 중 최저가
    strategy: str = ""


@dataclass
class StopLossLevel:
    """손절 레벨."""
    market: str
    stop_price: float
    stop_pct: float          # 진입가 대비 손절 %
    trigger_price: float     # 트리거 가격 (트레일링용)
    mode: StopLossMode
    reason: str
    urgency: str             # "none", "watch", "close", "immediate"
    time_held_min: float
    details: Dict[str, Any]


class DynamicStopLossManager:
    """동적 손절 관리자.
    
    시간 기반 손절 로직:
    - 0-30분: -7% 손절 (넓은 여유)
    - 30분-1시간: -5% 손절
    - 1-2시간: -4% 손절
    - 2-4시간: -3% 손절
    - 4시간+: -2% 손절 (타이트)
    
    트레일링 스탑:
    - 최고가 대비 일정 % 하락 시 손절
    """

    def __init__(
        self,
        # 시간 기반 손절 (분, 손절%)
        time_decay_schedule: Optional[List[Tuple[int, float]]] = None,
        
        # 트레일링 스탑
        trailing_activation_pct: float = 3.0,  # 수익 3% 이상 시 활성화
        trailing_distance_pct: float = 2.0,    # 최고가 대비 2% 하락 시 손절
        
        # 최소/최대 손절
        min_stop_pct: float = 1.5,
        max_stop_pct: float = 10.0,
        
        # 전략별 보정
        strategy_adjustments: Optional[Dict[str, float]] = None,
    ):
        self.time_decay_schedule = time_decay_schedule or [
            (0, -7.0),      # 0-30분: -7%
            (30, -5.0),     # 30-60분: -5%
            (60, -4.0),     # 1-2시간: -4%
            (120, -3.0),    # 2-4시간: -3%
            (240, -2.5),    # 4-8시간: -2.5%
            (480, -2.0),    # 8시간+: -2%
        ]
        
        self.trailing_activation = trailing_activation_pct
        self.trailing_distance = trailing_distance_pct
        self.min_stop = min_stop_pct
        self.max_stop = max_stop_pct
        
        self.strategy_adjustments = strategy_adjustments or {
            "PINGPONG": 1.0,     # 기본
            "AUTOLOOP": 1.0,
            "LADDER": 1.5,       # 더 넓은 손절 (DCA 특성)
            "LIGHTNING": 0.7,    # 더 타이트 (빠른 손절)
            "GAZUA": 1.3,        # 더 넓은 손절 (장기 홀드)
        }

    def calculate_stop_level(
        self,
        position: PositionInfo,
        mode: StopLossMode = StopLossMode.HYBRID,
        regime: Optional["RegimeInfo"] = None,
        atr_pct: Optional[float] = None,
    ) -> StopLossLevel:
        """손절 레벨 계산."""
        now = time.time()
        held_sec = now - position.entry_ts
        held_min = held_sec / 60
        
        entry = position.entry_price
        current = position.current_price
        highest = position.highest_price if position.highest_price > 0 else current
        
        # 현재 손익률
        current_pnl_pct = ((current - entry) / entry) * 100 if entry > 0 else 0
        highest_pnl_pct = ((highest - entry) / entry) * 100 if entry > 0 else 0
        
        # 변동성 기반 스케일링
        if atr_pct is not None and atr_pct > 0:
            vol_scale = max(VOL_SCALE_MIN, min(VOL_SCALE_MAX, atr_pct / ATR_REF))
        else:
            vol_scale = 1.0
        
        # 1. 시간 기반 손절 %
        time_stop_pct = self._get_time_based_stop(held_min)
        
        # 2. 전략 보정
        strat_adj = self.strategy_adjustments.get(position.strategy.upper(), 1.0)
        time_stop_pct *= strat_adj
        
        # 국면별 트레일링 파라미터 조절
        if regime and regime.regime.value == "VOLATILE":
            effective_trailing_activation = max(1.0, self.trailing_activation * 0.7)
            effective_trailing_distance = max(0.8, self.trailing_distance * 0.6)
        else:
            effective_trailing_activation = self.trailing_activation
            effective_trailing_distance = self.trailing_distance
        
        # 3. 트레일링 스탑 체크
        trailing_stop_pct = None
        if highest_pnl_pct >= effective_trailing_activation:
            trailing_stop_pct = highest_pnl_pct - effective_trailing_distance
        
        # 4. 모드별 최종 손절 결정
        if mode == StopLossMode.FIXED:
            final_stop_pct = time_stop_pct
            reason = f"fixed:{time_stop_pct:.1f}%"
        elif mode == StopLossMode.TIME_DECAY:
            final_stop_pct = time_stop_pct
            reason = f"time_decay:{held_min:.0f}min→{time_stop_pct:.1f}%"
        elif mode == StopLossMode.TRAILING:
            if trailing_stop_pct is not None:
                final_stop_pct = trailing_stop_pct
                reason = f"trailing:high={highest_pnl_pct:.1f}%→stop={final_stop_pct:.1f}%"
            else:
                final_stop_pct = time_stop_pct
                reason = f"trailing_inactive:using_time={time_stop_pct:.1f}%"
        else:  # HYBRID
            if trailing_stop_pct is not None and trailing_stop_pct > time_stop_pct:
                final_stop_pct = trailing_stop_pct
                reason = f"hybrid_trailing:stop={final_stop_pct:.1f}%"
            else:
                final_stop_pct = time_stop_pct
                reason = f"hybrid_time:{held_min:.0f}min→{time_stop_pct:.1f}%"
        
        # 국면별 SL 폭 조절
        if regime is not None:
            sl_mult = REGIME_SL_MULT.get(regime.regime.value, 1.0)
            final_stop_pct *= sl_mult
        
        # 5. 최소/최대 제한
        final_stop_pct = max(-self.max_stop, min(-self.min_stop, final_stop_pct))
        
        # 6. 손절 가격 계산
        stop_price = entry * (1 + final_stop_pct / 100)
        
        # 7. 긴급도 판단
        urgency = self._determine_urgency(current_pnl_pct, final_stop_pct)
        
        # TP 추천값 계산
        regime_value = regime.regime.value if regime else None
        tp_mult = REGIME_TP_MULT.get(regime_value, 1.0) if regime_value else 1.0
        tp_pct = max(TP_MIN, abs(final_stop_pct) * TP_RATIO * tp_mult * vol_scale)
        
        details: Dict[str, Any] = {
            "entry_price": entry,
            "current_price": current,
            "highest_price": highest,
            "current_pnl_pct": round(current_pnl_pct, 2),
            "highest_pnl_pct": round(highest_pnl_pct, 2),
            "time_stop_pct": round(time_stop_pct, 2),
            "trailing_stop_pct": round(trailing_stop_pct, 2) if trailing_stop_pct else None,
            "strategy": position.strategy,
            "strategy_adj": strat_adj,
            "tp_pct": round(tp_pct, 2),
            "tp_price": round(entry * (1 + tp_pct / 100), 2),
            "vol_scale": round(vol_scale, 2),
            "regime": regime_value,
        }
        
        return StopLossLevel(
            market=position.market,
            stop_price=round(stop_price, 2),
            stop_pct=round(final_stop_pct, 2),
            trigger_price=highest if trailing_stop_pct else 0,
            mode=mode,
            reason=reason,
            urgency=urgency,
            time_held_min=round(held_min, 1),
            details=details,
        )

    def _get_time_based_stop(self, held_min: float) -> float:
        """시간 기반 손절 % 반환."""
        for i, (threshold_min, stop_pct) in enumerate(self.time_decay_schedule):
            if i + 1 < len(self.time_decay_schedule):
                next_threshold = self.time_decay_schedule[i + 1][0]
                if threshold_min <= held_min < next_threshold:
                    return stop_pct
            else:
                if held_min >= threshold_min:
                    return stop_pct
        
        # 기본값
        return self.time_decay_schedule[-1][1] if self.time_decay_schedule else -5.0

    def _determine_urgency(self, current_pnl_pct: float, stop_pct: float) -> str:
        """긴급도 판단."""
        distance_to_stop = current_pnl_pct - stop_pct
        
        if current_pnl_pct <= stop_pct:
            return "immediate"  # 이미 손절 레벨 도달
        elif distance_to_stop < 1.0:
            return "close"      # 손절까지 1% 미만
        elif distance_to_stop < 2.0:
            return "watch"      # 손절까지 2% 미만
        else:
            return "none"       # 여유 있음

    def should_stop_loss(
        self,
        position: PositionInfo,
        mode: StopLossMode = StopLossMode.HYBRID,
    ) -> Tuple[bool, StopLossLevel]:
        """손절 실행 여부."""
        level = self.calculate_stop_level(position, mode)
        should_stop = position.current_price <= level.stop_price
        return (should_stop, level)

    def batch_check(
        self,
        positions: List[PositionInfo],
        mode: StopLossMode = StopLossMode.HYBRID,
    ) -> List[Tuple[PositionInfo, StopLossLevel, bool]]:
        """여러 포지션 일괄 체크."""
        results = []
        for pos in positions:
            should_stop, level = self.should_stop_loss(pos, mode)
            results.append((pos, level, should_stop))
        return results

    # [MIGRATED 2026-01-23] Market Regime 연동
    def get_regime_scale(self, market: str = "BTCUSDT") -> Dict[str, float]:
        """현재 시장 국면에 따른 TP/SL 스케일 반환."""
        if not REGIME_TP_SL_ENABLED or get_regime_detector is None:
            return {"sl": 1.0, "tp": 1.0, "trail": 1.0}
        
        try:
            detector = get_regime_detector()
            result = detector.detect(market)
            return detector.get_tp_sl_scale(result.regime)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("[DynamicStopLoss] _get_regime_scale failed for %s", market, exc_info=True)
            return {"sl": 1.0, "tp": 1.0, "trail": 1.0}


def get_dynamic_stop_pct(
    entry_ts: float,
    strategy: str = "PINGPONG",
    highest_pnl_pct: float = 0.0,
) -> Tuple[float, str]:
    """간단한 동적 손절 % 계산.
    
    Returns:
        (stop_pct, reason)
    """
    manager = DynamicStopLossManager()
    
    now = time.time()
    held_min = (now - entry_ts) / 60
    
    # 시간 기반 손절
    time_stop = manager._get_time_based_stop(held_min)
    
    # 전략 보정
    strat_adj = manager.strategy_adjustments.get(strategy.upper(), 1.0)
    time_stop *= strat_adj
    
    # 트레일링 체크
    if highest_pnl_pct >= manager.trailing_activation:
        trailing_stop = highest_pnl_pct - manager.trailing_distance
        if trailing_stop > time_stop:
            return (trailing_stop, f"trailing:{highest_pnl_pct:.1f}%→{trailing_stop:.1f}%")
    
    return (time_stop, f"time:{held_min:.0f}min→{time_stop:.1f}%")
