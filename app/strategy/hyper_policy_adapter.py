# ===============================================================
# File: autocoin/core/hyper_policy_adapter.py
# Autocoin OS v3-H — Policy Adapter / Translator
# ===============================================================

from __future__ import annotations
from typing import Dict, Any


class HyperPolicyAdapter:
    """
    Policy translation engine that converts policies of differing structures
    into a single standard form usable by HyperEngine.

    Standard policy structure:
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
    # Policy translation
    # -----------------------------------------------------------
    def normalize(self, policy: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert a policy from Manager/Strategy/Preset into the standard form.
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
    # merge preset + user_policy + engine_policy
    # -----------------------------------------------------------
    def merge(self, *policies: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merge multiple policies into a single standard policy.
        Priority: the last argument has the highest precedence.
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
