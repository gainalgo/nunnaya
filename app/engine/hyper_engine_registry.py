# ============================================================
# File: app/engine/hyper_engine_registry.py
# Autocoin OS v3-H — Engine Registry (Final Edition)
# ============================================================

from __future__ import annotations
from typing import Dict, Optional

from app.engine.hyper_engine_base import HyperEngineBase


class HyperEngineRegistry:
    """
    엔진을 이름으로 등록하고 조회하는 단일 레지스트리.
    v3-H에서는 엔진이 하나이므로 중복 등록을 허용한다.
    """

    def __init__(self):
        self._engines: Dict[str, HyperEngineBase] = {}

    # --------------------------------------------------------
    def register(self, name: str, engine: HyperEngineBase):
        """
        엔진 중복 등록을 허용한다.
        HyperSystem()을 여러 번 생성해도 에러가 나지 않아야 한다.
        """
        # 이미 등록된 엔진이면 무시
        if name in self._engines:
            return
        self._engines[name] = engine

    # --------------------------------------------------------
    def get(self, name: str) -> Optional[HyperEngineBase]:
        return self._engines.get(name)

    # --------------------------------------------------------
    def exists(self, name: str) -> bool:
        return name in self._engines

    # --------------------------------------------------------
    def list(self):
        return list(self._engines.keys())


# 글로벌 싱글턴 인스턴스
engine_registry = HyperEngineRegistry()
