# ============================================================
# File: app/strategy/strategy_store.py
# ------------------------------------------------------------
# 전략 정책 중앙 저장소.
# system/config_store → strategy_store → engine 흐름에서 정책을 전달한다.
# ============================================================

from __future__ import annotations
from typing import Dict, Any

from .strategy_types import StrategyPolicy
from ..core.hyper_config_store import config_store


class StrategyStore:
    """
    전략 정책을 저장하고 제공하는 중앙 저장소.
    """

    def __init__(self):
        # 시장별 현재 적용 정책
        self._policies: Dict[str, StrategyPolicy] = {}

        # 글로벌 기본 정책 (strategy.json + presets 기반)
        base = config_store.get("strategy", {})
        self.base_policy = StrategyPolicy(base)

    # --------------------------------------------------------
    # 정책 가져오기
    # --------------------------------------------------------
    def get_policy(self, market: str) -> StrategyPolicy:
        """
        시장별 정책을 반환한다.
        없으면 기본 정책을 기반으로 생성한다.
        """
        if market not in self._policies:
            self._policies[market] = StrategyPolicy(self.base_policy.to_dict())

        return self._policies[market]

    # --------------------------------------------------------
    # 정책 업데이트
    # --------------------------------------------------------
    def update_policy(self, market: str, updates: Dict[str, Any]):
        """
        시장 정책 일부를 업데이트한다.
        """
        policy = self.get_policy(market)
        policy.update(updates)

    # --------------------------------------------------------
    # 전체 정책 조회
    # --------------------------------------------------------
    def all_policies(self) -> Dict[str, StrategyPolicy]:
        return dict(self._policies)


# ------------------------------------------------------------
# 글로벌 인스턴스
# ------------------------------------------------------------
strategy_store = StrategyStore()
