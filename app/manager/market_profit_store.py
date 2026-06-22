# ============================================================
# File: app/manager/market_profit_store.py
# ------------------------------------------------------------
# MarketProfitStore
# - 각 시장별 신호 통계를 저장하고 관리하는 모듈.
# - buy/sell/hold 횟수를 카운트하여 UI/분석에 활용.
# ============================================================

from __future__ import annotations
from typing import Dict, Any


class MarketProfitStore:
    """
    시장별 신호 통계를 기록하는 저장소.
    구조 예시:
        stats = {
            "XRPUSDT": { "buy": 10, "sell": 9, "hold": 21 }
        }
    """

    def __init__(self):
        self.stats: Dict[str, Dict[str, int]] = {}

    # --------------------------------------------------------
    # 시장 초기화
    # --------------------------------------------------------
    def _ensure(self, market: str):
        if market not in self.stats:
            self.stats[market] = {"buy": 0, "sell": 0, "hold": 0}

    # --------------------------------------------------------
    # 신호 업데이트
    # --------------------------------------------------------
    def update(self, market: str, signal: str):
        self._ensure(market)

        if signal not in ("buy", "sell", "hold"):
            return

        self.stats[market][signal] += 1

    # --------------------------------------------------------
    # 조회
    # --------------------------------------------------------
    def get(self, market: str) -> Dict[str, int]:
        self._ensure(market)
        return dict(self.stats[market])

    # --------------------------------------------------------
    # 전체 조회
    # --------------------------------------------------------
    def all(self) -> Dict[str, Dict[str, int]]:
        return {m: dict(v) for m, v in self.stats.items()}


# ------------------------------------------------------------
# 글로벌 인스턴스
# ------------------------------------------------------------
market_profit_store = MarketProfitStore()
