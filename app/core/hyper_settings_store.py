# ============================================================
# File: app/core/hyper_settings_store.py
# Autocoin OS v3-H — Runtime Settings Store (Dashboard overrides)
# ============================================================

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

def _norm_market(market: str) -> str:
    return str(market or "").strip().upper()

class HyperSettingsStore:
    """런타임(재시작 유지) 설정 저장소.

    파일 스키마(권장)
    {
      "version": 1,
      "ts": 0,
      "global": { ... },
      "markets": {
         "BTCUSDT": { ... },
         ...
      }
    }

    - ENV는 기본값
    - UI가 변경한 값은 여기 저장 → 재시작 후에도 최우선 적용
    """

    def __init__(self, *, path: str) -> None:
        self.path = str(path or "").strip()
        self.data: Dict[str, Any] = {
            "version": 1,
            "ts": time.time(),
            "global": {},
            "markets": {},
        }
        self.loaded: bool = False

        if self.path:
            self.load()

    # --------------------------------------------------------
    # Persistence
    # --------------------------------------------------------
    def load(self) -> bool:
        if not self.path or not os.path.exists(self.path):
            self.loaded = True
            return False

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("[HyperSettingsStore] Failed to load settings from %s", self.path, exc_info=True)
            self.loaded = True
            return False

        if not isinstance(obj, dict):
            self.loaded = True
            return False

        g = obj.get("global")
        m = obj.get("markets")
        if not isinstance(g, dict):
            g = {}
        if not isinstance(m, dict):
            m = {}

        # normalize market keys
        markets_norm: Dict[str, Any] = {}
        for k, v in m.items():
            mk = _norm_market(str(k))
            if not mk:
                continue
            if isinstance(v, dict):
                markets_norm[mk] = dict(v)

        try:
            ver = int(obj.get("version") or 1)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[HyperSettingsStore] Failed to parse version, defaulting to 1", exc_info=True)
            ver = 1

        self.data = {
            "version": ver,
            "ts": float(obj.get("ts") or 0.0) or time.time(),
            "global": dict(g),
            "markets": markets_norm,
        }
        self.loaded = True
        return True

    def save(self) -> bool:
        if not self.path:
            return False

        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)

            payload = {
                "version": int(self.data.get("version") or 1),
                "ts": time.time(),
                "global": dict(self.data.get("global") or {}),
                "markets": dict(self.data.get("markets") or {}),
            }

            # atomic write (same directory)
            base = os.path.basename(self.path)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=d or None,
                prefix=f".{base}.",
                suffix=".tmp",
            ) as tf:
                json.dump(payload, tf, ensure_ascii=False, indent=2)
                tf.flush()
                try:
                    os.fsync(tf.fileno())
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[hyper_settings_store] %s: %s", 'atomic write (same directory)', exc, exc_info=True)
                tmp_path = tf.name

            os.replace(tmp_path, self.path)
            self.data["ts"] = payload["ts"]
            return True
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
            logger.warning("[HyperSettingsStore] Failed to save settings to %s", self.path, exc_info=True)
            return False

    # --------------------------------------------------------
    # Global
    # --------------------------------------------------------
    def get_global(self) -> Dict[str, Any]:
        g = self.data.get("global")
        return dict(g) if isinstance(g, dict) else {}

    def set_global_patch(self, patch: Dict[str, Any], *, save: bool = True) -> Dict[str, Any]:
        if not isinstance(patch, dict):
            return self.get_global()

        g = self.data.get("global")
        if not isinstance(g, dict):
            g = {}

        for k, v in patch.items():
            if v is None:
                g.pop(str(k), None)
            else:
                g[str(k)] = v

        self.data["global"] = g
        if save:
            self.save()
        return dict(g)

    # --------------------------------------------------------
    # Market overrides
    # --------------------------------------------------------
    def get_market_overrides(self, market: str) -> Dict[str, Any]:
        mk = _norm_market(market)
        if not mk:
            return {}
        m = self.data.get("markets")
        if not isinstance(m, dict):
            return {}
        v = m.get(mk)
        return dict(v) if isinstance(v, dict) else {}

    def get_all_market_overrides(self) -> Dict[str, Dict[str, Any]]:
        m = self.data.get("markets")
        if not isinstance(m, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for k, v in m.items():
            if isinstance(v, dict):
                out[str(k)] = dict(v)
        return out

    def set_market_overrides_patch(
        self,
        market: str,
        patch: Dict[str, Any],
        *,
        save: bool = True,
    ) -> Dict[str, Any]:
        mk = _norm_market(market)
        if not mk:
            return {}

        if not isinstance(patch, dict):
            return self.get_market_overrides(mk)

        m = self.data.get("markets")
        if not isinstance(m, dict):
            m = {}

        cur = m.get(mk)
        if not isinstance(cur, dict):
            cur = {}

        for k, v in patch.items():
            kk = str(k)
            if v is None:
                cur.pop(kk, None)
            else:
                cur[kk] = v

        # keep storage lean
        if cur:
            m[mk] = cur
        else:
            m.pop(mk, None)

        self.data["markets"] = m
        if save:
            self.save()

        return dict(cur)

    def clear_market_overrides(self, market: str, *, save: bool = True) -> bool:
        mk = _norm_market(market)
        if not mk:
            return False

        m = self.data.get("markets")
        if not isinstance(m, dict):
            return False

        existed = mk in m
        m.pop(mk, None)
        self.data["markets"] = m

        if save:
            self.save()

        return existed
