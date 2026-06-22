# ============================================================
# File: app/core/hyper_config_loader.py
# ------------------------------------------------------------
# HyperConfigLoader
# - app/data 내부의 JSON 파일들을 읽어 ConfigStore에 공급한다.
# - 전략 정책, 프리셋, 엔진 설정 등 다양한 JSON을 로딩하는 공통 유틸리티.
# ============================================================

from __future__ import annotations
import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


class HyperConfigLoader:
    """
    JSON 기반 설정 로더.
    app/data 아래 JSON을 로딩하여 dict 형태로 반환한다.
    """

    def __init__(self, base_path: str):
        self.base_path = base_path

    # --------------------------------------------------------
    # JSON 파일 로딩
    # --------------------------------------------------------
    def load(self, filename: str) -> Dict[str, Any]:
        """
        filename: "strategy.json", "autoloop_config.json" 등
        """

        full_path = os.path.join(self.base_path, filename)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Config file not found: {full_path}")

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning("JSON parsing error in %s", filename, exc_info=True)
            raise ValueError(f"JSON parsing error in {filename}: {e}")

    # --------------------------------------------------------
    # 여러 파일 로딩 (옵션)
    # --------------------------------------------------------
    def load_multiple(self, files: list[str]) -> Dict[str, Any]:
        data = {}
        for f in files:
            data[f] = self.load(f)
        return data
