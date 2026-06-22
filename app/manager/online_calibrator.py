# ============================================================
# File: app/manager/online_calibrator.py
# Phase 3-A: Online Calibration — Self-Evolving Parameter Tuning
# ============================================================
"""
6-cell Bucket System: Volatility(Low/Mid/High) × Regime(Range/Trend)

각 버킷은 해당 시장 조건에서의 거래 결과를 축적하고,
PINGPONG/AUTOLOOP의 TP/SL/진입 파라미터를 점진적으로 조정한다.

조정 범위는 ×0.7 ~ ×1.4 로 제한되어 극단적 파라미터 이탈을 방지한다.
최소 10회 거래 후부터 보정값을 반환한다.
"""

from __future__ import annotations

import json
import math
import os
import time
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_STATE_PATH = os.getenv("OMA_CALIBRATOR_STATE_PATH", "runtime/online_calibrator.json")
_MIN_TRADES = 10
_VOL_LOW = 1.5   # ATR% 경계
_VOL_HIGH = 4.0

def _sf(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        logger.warning("[Calibrator] _sf: conversion failed for %r", x, exc_info=True)
        return default

def _default_bucket() -> Dict[str, Any]:
    return {
        "trades": 0,
        "wins": 0,
        "total_pnl_pct": 0.0,
        "ema_tp_pct": 2.5,
        "ema_sl_pct": -2.5,
        "ema_hold_sec": 3600.0,
        "last_update_ts": 0.0,
    }

class OnlineCalibrator:
    """시장 조건별 전략 파라미터 온라인 보정기.

    Bucket: Volatility(Low/Mid/High) × Regime(Range/Trend) × Strategy
    → 총 12개 셀 (6 조건 × PP/AL 2개 전략)
    """

    def __init__(self, state_path: str = _STATE_PATH):
        self._state_path = state_path
        self._buckets: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ── Persistence ──
    def _load(self) -> None:
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                buckets = data.get("buckets")
                if isinstance(buckets, dict):
                    self._buckets = buckets
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[online_calibrator] %s: %s", 'online_calibrator._load fallback', exc, exc_info=True)

    def _save(self) -> None:
        if not self._state_path:
            return
        try:
            from app.core.io_utils import safe_write_json
            safe_write_json(self._state_path, {"buckets": self._buckets, "ts": time.time()})
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"[Calibrator] save failed: {e}", exc_info=True)

    # ── Classification ──
    def classify_bucket(self, atr_pct: float, regime: str) -> str:
        """ATR%와 국면으로 버킷 키 결정.

        Returns: 'LOW_RANGE', 'MID_TREND', 'HIGH_RANGE' 등
        """
        if atr_pct < _VOL_LOW:
            vol = "LOW"
        elif atr_pct < _VOL_HIGH:
            vol = "MID"
        else:
            vol = "HIGH"
        reg = "TREND" if str(regime).upper() in ("TREND", "BULL", "BEAR") else "RANGE"
        return f"{vol}_{reg}"

    # ── Trade Recording ──
    def record_trade(
        self,
        bucket_key: str,
        strategy: str,
        pnl_pct: float,
        tp_pct: float = 0.0,
        sl_pct: float = 0.0,
        hold_sec: float = 0.0,
    ) -> None:
        """거래 결과를 해당 버킷에 기록.

        EMA 방식으로 최근 결과에 더 높은 가중치.
        """
        key = f"{bucket_key}:{strategy.upper()}"
        b = self._buckets.setdefault(key, _default_bucket())
        b["trades"] = int(b.get("trades", 0)) + 1
        b["total_pnl_pct"] = _sf(b.get("total_pnl_pct"), 0.0) + pnl_pct
        if pnl_pct > 0:
            b["wins"] = int(b.get("wins", 0)) + 1

        alpha = min(0.2, 2.0 / (int(b["trades"]) + 1))
        if pnl_pct > 0 and tp_pct > 0:
            b["ema_tp_pct"] = _sf(b.get("ema_tp_pct"), 2.5) * (1 - alpha) + tp_pct * alpha
        if pnl_pct < 0 and sl_pct < 0:
            b["ema_sl_pct"] = _sf(b.get("ema_sl_pct"), -2.5) * (1 - alpha) + sl_pct * alpha
        if hold_sec > 0:
            b["ema_hold_sec"] = _sf(b.get("ema_hold_sec"), 3600.0) * (1 - alpha) + hold_sec * alpha
        b["last_update_ts"] = time.time()
        self._save()

    # ── Calibrated Parameter Retrieval ──
    def get_adjustments(
        self, bucket_key: str, strategy: str
    ) -> Optional[Dict[str, float]]:
        """버킷 기반 보정 배율 반환.

        Returns None if 거래 수 부족 (< MIN_TRADES).
        PP: pp_tp_mult, pp_sl_mult, pp_gap_mult
        AL: al_rsi_shift, al_trail_mult
        """
        key = f"{bucket_key}:{strategy.upper()}"
        b = self._buckets.get(key)
        if not b or int(b.get("trades", 0)) < _MIN_TRADES:
            return None

        trades = max(1, int(b["trades"]))
        wins = int(b.get("wins", 0))
        win_rate = wins / trades
        strat = strategy.upper()

        if strat == "PINGPONG":
            # 승률 높으면 TP 확대, SL 유지
            # 승률 낮으면 SL 타이트, TP 축소
            tp_mult = 1.0 + (win_rate - 0.5) * 0.4
            sl_mult = 1.0 - (win_rate - 0.5) * 0.2
            gap_mult = tp_mult
            return {
                "pp_tp_mult": max(0.7, min(1.4, tp_mult)),
                "pp_sl_mult": max(0.7, min(1.3, sl_mult)),
                "pp_gap_mult": max(0.8, min(1.3, gap_mult)),
            }
        elif strat == "AUTOLOOP":
            # 승률 높으면 RSI 매수 기준 완화 (진입 쉽게), 트레일링 확대
            # 승률 낮으면 RSI 기준 강화 (진입 어렵게), 트레일링 축소
            rsi_shift = (win_rate - 0.5) * 10.0
            trail_mult = 1.0 + (win_rate - 0.5) * 0.3
            return {
                "al_rsi_shift": max(-8.0, min(8.0, rsi_shift)),
                "al_trail_mult": max(0.7, min(1.4, trail_mult)),
            }
        return None

    # ── Bulk Update from Ledger ──
    def update_from_trades(
        self, trades: List[Dict[str, Any]]
    ) -> int:
        """과거 거래 기록 일괄 반영. Returns 반영된 건수."""
        count = 0
        for t in trades:
            try:
                strat = str(t.get("strategy") or "").upper()
                if strat not in ("PINGPONG", "AUTOLOOP"):
                    continue
                pnl_pct = _sf(t.get("pnl_pct"), 0.0)
                tp_pct = _sf(t.get("tp_pct"), 0.0)
                sl_pct = _sf(t.get("sl_pct"), 0.0)
                hold_sec = _sf(t.get("hold_sec"), 0.0)
                atr_pct = _sf(t.get("atr_pct"), 2.0)
                regime = str(t.get("regime") or "RANGE")
                bucket = self.classify_bucket(atr_pct, regime)
                self.record_trade(bucket, strat, pnl_pct, tp_pct, sl_pct, hold_sec)
                count += 1
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[online_calibrator] %s: %s", 'online_calibrator.update_from_trades except-> continue', exc, exc_info=True)
                continue
        return count

    # ── Summary ──
    def summary(self) -> Dict[str, Any]:
        """전체 버킷 요약."""
        out: Dict[str, Any] = {}
        for key, b in self._buckets.items():
            trades = int(b.get("trades", 0))
            wins = int(b.get("wins", 0))
            out[key] = {
                "trades": trades,
                "wins": wins,
                "win_rate": round(wins / max(1, trades), 3),
                "total_pnl_pct": round(_sf(b.get("total_pnl_pct")), 3),
                "ema_tp_pct": round(_sf(b.get("ema_tp_pct")), 3),
                "ema_sl_pct": round(_sf(b.get("ema_sl_pct")), 3),
                "calibrated": trades >= _MIN_TRADES,
            }
        return out

# ── Module-level Singleton ──
_instance: Optional[OnlineCalibrator] = None

def get_calibrator() -> OnlineCalibrator:
    global _instance
    if _instance is None:
        _instance = OnlineCalibrator()
    return _instance
