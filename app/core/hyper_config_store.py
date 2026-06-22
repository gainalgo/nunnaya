# ============================================================
# File: app/core/hyper_config_store.py
# ------------------------------------------------------------
# HyperConfigStore
# - JSON으로 로딩한 다양한 설정(strategy, presets 등)을
#   시스템 전체에서 조회할 수 있도록 보관하는 중앙 저장소.
# ============================================================

from __future__ import annotations
import logging
from typing import Any, Dict

from app.core.hyper_config_loader import HyperConfigLoader
import os

logger = logging.getLogger(__name__)


class HyperConfigStore:
    """
    다양한 JSON 설정(strategy.json, presets, market lists, engine 설정 등)을
    system 전역에서 사용할 수 있도록 저장한다.
    """

    def __init__(self):
        base_path = os.path.join("app", "data")
        self.loader = HyperConfigLoader(base_path=base_path)
        self._store: Dict[str, Any] = {}

        self._load_all()

    # --------------------------------------------------------
    # 모든 설정 로딩
    # --------------------------------------------------------
    def _load_all(self):
        """
        JSON 파일들을 한 번에 로딩하여 내부 dict에 보관한다.
        필요에 따라 파일 추가/삭제 가능.
        """

        files = {
            "strategy": "strategy.json",
            "strategy_presets": "strategy_presets.json",
            "autoloop_config": "autoloop_config.json",
            "autoloop_markets": "autoloop_markets.json",
            "engine_presets": "engine_presets.json",
            "bybit_markets": "bybit_markets.json",
        }

        for key, filename in files.items():
            try:
                self._store[key] = self.loader.load(filename)
            except FileNotFoundError:
                # 없는 설정은 None 처리하여 사용 시 fallback 가능
                logger.info("[ConfigStore] Config file not found: %s (using fallback)", filename)
                self._store[key] = None

    # --------------------------------------------------------
    # 설정 조회
    # --------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """
        key: "strategy", "strategy_presets", ...
        """
        return self._store.get(key, default)

    # --------------------------------------------------------
    # 설정 갱신
    # --------------------------------------------------------
    def set(self, key: str, value: Any):
        self._store[key] = value

    # --------------------------------------------------------
    # 전체 조회
    # --------------------------------------------------------
    def all(self) -> Dict[str, Any]:
        return dict(self._store)


# ------------------------------------------------------------
# 글로벌 인스턴스
# ------------------------------------------------------------
config_store = HyperConfigStore()
