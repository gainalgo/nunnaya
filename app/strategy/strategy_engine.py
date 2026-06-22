# ============================================================
# File: app/strategy/strategy_engine.py
# ------------------------------------------------------------
# StrategyEngine
# - 전략 정책(refine_policy)을 관리하고 자동 미세 조정 수행
# - 각 마켓별 가격 히스토리를 유지
# ============================================================

from __future__ import annotations
from typing import Dict, Any


class StrategyEngine:
    """
    전략 정책 저장 및 자동 미세 조정 담당.
    Brain/Judge/Risk/Optimizer보다 상위 단계에서 정책을 다룬다.
    """

    def __init__(self):
        # 시장별 가격 기록 (초간단 버전)
        self.price_history: Dict[str, list[float]] = {}

    # --------------------------------------------------------
    # 가격 기록
    # --------------------------------------------------------
    def _record(self, market: str, price: float) -> list[float]:
        arr = self.price_history.setdefault(market, [])
        arr.append(price)

        # History 길이 제한 (20개 유지)
        if len(arr) > 20:
            arr.pop(0)

        return arr

    # --------------------------------------------------------
    # 정책 자동 미세 조정
    # --------------------------------------------------------
    def refine_policy(self, policy: Dict[str, Any], market: str, price: float) -> Dict[str, Any]:
        """
        시장 변동성, 최근 가격 변화 등을 바탕으로 정책을 자동 조정하는 간단한 로직.

        refine 결과는 Brain/Judge/Risk/Optimizer에서 그대로 이어받아 사용된다.
        """

        refined = dict(policy)  # 기존 정책 복사

        prices = self._record(market, price)
        if len(prices) < 5:
            return refined  # 데이터가 부족하면 조정하지 않음

        avg = sum(prices) / len(prices)
        # Guard: avoid division-by-zero / invalid prices
        if avg <= 0 or prices[0] == 0:
            return refined
        variance = sum((p - avg) ** 2 for p in prices) / len(prices)
        vol = (variance ** 0.5) / avg * 100  # 변동성(%)

        trend = (prices[-1] - prices[0]) / prices[0] * 100  # 상승/하락 %

        # 변동성이 크면 TP/SL 자동완화
        if vol > 3:
            refined["tp"] = refined.get("tp", 1.2) * 1.02
            refined["sl"] = refined.get("sl", -3.0) * 1.02

        # 상승 추세는 TP 강화
        if trend > 1:
            refined["tp"] = refined.get("tp", 1.2) * 1.01

        # 하락 추세는 SL 보수적으로
        if trend < -1:
            refined["sl"] = refined.get("sl", -3.0) * 0.99

        return refined
