# ===============================================================
# File: autocoin/core/hyper_policy_adapter.py
# Autocoin OS v3-H — Policy Adapter / Translator
# ===============================================================

from __future__ import annotations
from typing import Dict, Any


class HyperPolicyAdapter:
    """
    서로 다른 구조의 정책들을 HyperEngine이 사용할 수 있도록
    하나의 표준 형태로 변환하는 정책 변환 엔진.

    표준 정책 구조:
    {
        "name": "nunnaya",
        "params": {
            "rsi_low": float,
            "rsi_high": float,
            "tp": float,
            "sl": float,
            "size": float
        }
    }
    """

    DEFAULTS = {
        "name": "nunnaya",
        "params": {
            "rsi_low": 25,
            "rsi_high": 75,
            "tp": 1.2,
            "sl": -2.5,
            "size": 1000,
            # AI Indicator Params
            "ai_rsi_len": 14,
            "ai_macd_fast": 12,
            "ai_macd_slow": 26,
            "ai_macd_signal": 9,
            "ai_sma_fast": 5,
            "ai_sma_slow": 20,
        }
    }

    # -----------------------------------------------------------
    # 정책 변환
    # -----------------------------------------------------------
    def normalize(self, policy: Dict[str, Any]) -> Dict[str, Any]:
        """
        Manager/Strategy/Preset에서 온 정책을 표준 정책 형태로 변환.
        """
        if not policy:
            return self.DEFAULTS.copy()

        normalized = self.DEFAULTS.copy()

        # name
        if "name" in policy:
            normalized["name"] = policy["name"]

        # params
        params = normalized["params"].copy()
        incoming = policy.get("params", {})

        for key, value in incoming.items():
            params[key] = value

        normalized["params"] = params
        return normalized

    # -----------------------------------------------------------
    # preset + user_policy + engine_policy 병합
    # -----------------------------------------------------------
    def merge(self, *policies: Dict[str, Any]) -> Dict[str, Any]:
        """
        여러 정책을 합쳐 하나의 표준 정책 생성.
        Priority: 마지막 인자가 가장 높은 우선순위
        """
        merged = self.DEFAULTS.copy()

        for p in policies:
            if not p:
                continue

            # name
            if "name" in p:
                merged["name"] = p["name"]

            # params
            mp = merged["params"]
            pp = p.get("params", {})
            for k, v in pp.items():
                mp[k] = v

        return merged
