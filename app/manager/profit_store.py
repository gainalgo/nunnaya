# ============================================================
# File: app/manager/profit_store.py
# ------------------------------------------------------------
# ProfitStore
# - 시장별 수익(Realized Profit)을 계산하고 저장하는 모듈.
# - 엔진에서 발생한 신호(매수/매도)를 받아 수익을 업데이트한다.
# ============================================================

from __future__ import annotations
from typing import Dict, Any


class ProfitStore:
    """
    시장별 포지션 상태와 실현 수익을 관리한다.
    구조:
        trades = {
            "XRPUSDT": {
                "position": None or "long",
                "entry_price": float,
                "realized_profit": float
            }
        }
    """

    def __init__(self):
        self.trades: Dict[str, Dict[str, Any]] = {}

    # --------------------------------------------------------
    # 포지션 구조 초기화
    # --------------------------------------------------------
    def _ensure(self, market: str):
        if market not in self.trades:
            self.trades[market] = {
                "position": None,
                "entry_price": 0.0,
                "realized_profit": 0.0
            }

    # --------------------------------------------------------
    # 메인 업데이트 로직
    # --------------------------------------------------------
    def update(self, market: str, signal: str, price: float):
        """
        signal: "buy", "sell", "hold"
        """

        self._ensure(market)
        state = self.trades[market]

        pos = state["position"]
        entry = state["entry_price"]

        # --------------------------
        # BUY
        # --------------------------
        if signal == "buy":
            if pos is None:
                state["position"] = "long"
                state["entry_price"] = price

        # --------------------------
        # SELL
        # --------------------------
        elif signal == "sell":
            if pos == "long":
                # 수익 계산
                profit = price - entry
                state["realized_profit"] += profit

                # 포지션 종료
                state["position"] = None
                state["entry_price"] = 0.0

        # HOLD → 아무 것도 안함

    # --------------------------------------------------------
    # 시장별 상태 조회
    # --------------------------------------------------------
    def get(self, market: str) -> Dict[str, Any]:
        self._ensure(market)
        return dict(self.trades[market])

    # --------------------------------------------------------
    # 전체 상태 조회
    # --------------------------------------------------------
    def all(self) -> Dict[str, Any]:
        return {m: dict(v) for m, v in self.trades.items()}


# ------------------------------------------------------------
# 글로벌 인스턴스
# ------------------------------------------------------------
profit_store = ProfitStore()
