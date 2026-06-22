"""Context Engine — FOCUS 진입 판단 보조 레이어 (2026-04-19)

기존 `_compute_conviction_score()` 는 단일 코인의 H4 지표만 본다.
이 엔진은 "시장 맥락" 을 추가로 본다:

    1. BTC Regime        — BTC 4H 추세 → 역방향 진입 페널티
    2. Direction Memory  — 최근 같은 코인+방향 N회 결과 → 연패 시 차단/페널티
    3. Market Bias       — 다중 코인 최근 방향 집계 → 쏠림 시 거스름 페널티
    4. Session Profile   — KST 시간대 (새벽/아침) → conviction 가산

전부 default OFF. FocusConfig 토글로 개별 ON/OFF.

# 사용
    engine = ContextEngine(config=focus_mgr.config)
    verdict = engine.evaluate(
        market="ETHUSDT",
        direction="SHORT",
        now_ts=time.time(),
        btc_candles=price_store.get_prices("BTCUSDT", count=60),
    )
    # verdict.delta, verdict.block, verdict.reason, verdict.details

철학:
- 절대 하드차단은 최소화 (direction_memory 연패만)
- 대부분은 conviction 가감 (-2 ~ +2)
- 모듈별 독립 — 하나 OFF 해도 나머지 동작
- side-effect 없음 (기록·학습은 FocusManager 가 담당)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .btc_regime import BtcRegimeModule
from .direction_memory import DirectionMemoryModule
from .market_bias import MarketBiasModule
from .session_profile import SessionProfileModule

logger = logging.getLogger(__name__)


@dataclass
class Verdict:
    """Context evaluation 결과."""
    delta: int = 0                     # conviction 가감 (음수=페널티)
    block: bool = False                # True 면 진입 완전 차단
    reason: str = ""                   # block=True 일 때 차단 사유
    details: Dict[str, Any] = field(default_factory=dict)  # 모듈별 기여

    def summary(self) -> str:
        parts = []
        for mod, info in self.details.items():
            d = info.get("delta", 0)
            if d != 0 or info.get("block"):
                # ★ [2026-05-17] Phase 5 산식 float 변환 부작용 — int() 강제
                parts.append(f"{mod}{int(d):+d}" + ("*" if info.get("block") else ""))
        return "/".join(parts) if parts else "neutral"


class ContextEngine:
    """4개 컨텍스트 모듈을 통합 평가."""

    def __init__(self, config: Any):
        self.config = config
        self.session = SessionProfileModule(config)
        self.direction = DirectionMemoryModule(config)
        self.btc = BtcRegimeModule(config)
        self.bias = MarketBiasModule(config)

    def evaluate(
        self,
        market: str,
        direction: str,
        now_ts: float,
        btc_candles: Optional[List[Any]] = None,
    ) -> Verdict:
        """통합 평가. 순서: 차단 우선 → 페널티/가점 합산.

        Args:
            market: "ETHUSDT" 형식
            direction: "LONG" / "SHORT"
            now_ts: 현재 유닉스 타임스탬프 (테스트 주입 가능)
            btc_candles: BTC H4 캔들 리스트 (없으면 BTC 모듈 스킵)

        Returns:
            Verdict (delta, block, reason, details)
        """
        v = Verdict()
        if not direction:
            return v  # 방향 미정 시 스킵 (스캐너 사전 단계)

        mkt = market.upper()
        dir_u = direction.upper()

        # 1) Direction Memory — 연패 차단이 최우선
        try:
            dm = self.direction.evaluate(mkt, dir_u, now_ts)
            v.details["direction_memory"] = dm
            if dm.get("block"):
                v.block = True
                v.reason = dm.get("reason", "direction_memory_block")
                return v  # hard block — 뒤 모듈 평가 불필요
            v.delta += int(dm.get("delta", 0))
        except Exception as exc:
            logger.warning("[ctx] direction_memory error: %s", exc)
            v.details["direction_memory"] = {"error": str(exc)}

        # 2) BTC Regime — 방향 역정렬 페널티
        try:
            bt = self.btc.evaluate(dir_u, btc_candles, now_ts)
            v.details["btc_regime"] = bt
            v.delta += int(bt.get("delta", 0))
        except Exception as exc:
            logger.warning("[ctx] btc_regime error: %s", exc)
            v.details["btc_regime"] = {"error": str(exc)}

        # 3) Market Bias — 쏠림 거스름 페널티
        try:
            mb = self.bias.evaluate(mkt, dir_u, now_ts)
            v.details["market_bias"] = mb
            v.delta += int(mb.get("delta", 0))
        except Exception as exc:
            logger.warning("[ctx] market_bias error: %s", exc)
            v.details["market_bias"] = {"error": str(exc)}

        # 4) Session Profile — 시간대 가감
        try:
            sp = self.session.evaluate(dir_u, now_ts)
            v.details["session_profile"] = sp
            v.delta += int(sp.get("delta", 0))
        except Exception as exc:
            logger.warning("[ctx] session_profile error: %s", exc)
            v.details["session_profile"] = {"error": str(exc)}

        return v


__all__ = ["ContextEngine", "Verdict"]
