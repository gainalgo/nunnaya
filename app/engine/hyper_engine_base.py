# ============================================================
# File: app/engine/hyper_engine_base.py
# Autocoin OS v3-H — Unified Base Engine (Final / Fixed)
# ============================================================

from __future__ import annotations
from typing import Any, Dict

from app.engine.hyper_engine_status import EngineStatus


class HyperEngineBase:
    """
    v3-H 엔진들의 공통 부모 클래스.
    하위 엔진은 _tick_impl() 하나만 구현하면 된다.
    """

    def __init__(self, name: str | None = None, engine_name: str | None = None):
        # 이름 규칙 통일
        if name is None and engine_name is not None:
            name = engine_name

        if name is None:
            cls = self.__class__.__name__
            base = cls.replace("Hyper", "").replace("Engine", "")
            name = base.lower()

        self.name = name
        self.status = EngineStatus(name=name)

        # v3-H에서는 state 문자열을 쓰지 않는다
        self.current_market: str | None = None

    # --------------------------------------------------------
    # 엔진 시작
    # --------------------------------------------------------
    def start(self, market: str):
        """
        v3-H 기준 start:
        - EngineStatus를 활성화
        - Context/Coordinator는 건드리지 않는다
        """
        self.current_market = market
        self.status.start()

    # --------------------------------------------------------
    # 엔진 정지
    # --------------------------------------------------------
    def stop(self):
        self.current_market = None
        self.status.stop()

    # --------------------------------------------------------
    # 메인 tick 루프
    # --------------------------------------------------------
    #엔진은 stateless,
    #Context만 stateful 해야 한다.

    # --------------------------------------------------------
    # 하위 엔진이 반드시 구현해야 하는 함수
    # --------------------------------------------------------
    def _tick_impl(self, market: str, price: float, context):
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _tick_impl()"
        )
