"""Market Bias — 다중 코인 최근 EXIT 결과로 시장 방향 쏠림 감지.

관찰:
- 최근 N분 / M건 EXIT 에서 LONG 수익이 압도적 = 상승장 → SHORT 는 거스름
- 반대로 SHORT 수익이 압도적 = 하락장 → LONG 은 거스름
- BTC Regime 과 부분 중복이지만 **실전 결과 기반** (이론 EMA 말고 실제 먹힌 방향)

계산:
    최근 lookback_trades 건의 EXIT 레코드에서
        long_wins  = (direction=LONG, pnl_net>0) 건수
        long_loses = (direction=LONG, pnl_net<0)
        short_wins, short_loses 동일
    long_score  = long_wins - long_loses
    short_score = short_wins - short_loses
    bias = sign(long_score - short_score)  -- 양수=LONG 우세, 음수=SHORT 우세
    dominance = |long_score - short_score| / total

    dominance > threshold 이고 direction 이 반대면 → 페널티

효과:
    LONG 우세 장 × LONG 진입 = +0 (이미 우세)
    LONG 우세 장 × SHORT 진입 = -1 (거스름)
    SHORT 우세 장 × LONG 진입 = -1
    SHORT 우세 장 × SHORT 진입 = +0

BTC Regime 과 차이:
    - BTC: 이론 지표 (EMA / structure)
    - Market Bias: 실전 결과 (직전 EXIT 로그)
    - 둘 다 같은 방향을 가리키면 확실 → double penalty (합산)
    - 엇갈리면 어느 쪽도 신뢰 못함 → 부분 페널티만
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

JOURNAL_PATH = os.path.join("runtime", "focus_harpoon_journal.jsonl")


class MarketBiasModule:
    def __init__(self, config: Any):
        self.config = config
        # Cache
        self._cache_ts: float = 0.0
        self._cache: Dict[str, Any] = {}

    def _load_recent_all_exits(
        self, lookback_count: int, lookback_hours: float, now_ts: float
    ) -> List[Dict[str, Any]]:
        """모든 코인의 최근 EXIT 레코드 (최신순).

        now_ts: 테스트 주입 가능 (실측 검증용)
        """
        out: List[Dict[str, Any]] = []
        if not os.path.exists(JOURNAL_PATH):
            return out
        cutoff = now_ts - lookback_hours * 3600.0

        # 파일 크기가 크면(>8MB) 끝에서 역순 읽기 — 여기선 단순 forward
        try:
            with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("event") != "EXIT":
                        continue
                    ts = rec.get("ts", 0)
                    # lower bound: cutoff (lookback 경계)
                    # upper bound: now_ts (테스트 과거 시뮬레이션 정확성)
                    if ts < cutoff or ts > now_ts:
                        continue
                    out.append(rec)
        except Exception as exc:
            logger.warning("[mb] journal read failed: %s", exc)

        out.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return out[:lookback_count]

    def _compute_bias(self, exits: List[Dict[str, Any]]) -> Dict[str, Any]:
        """EXIT 레코드에서 bias 지표 계산."""
        long_wins = long_loses = short_wins = short_loses = 0
        for r in exits:
            d = (r.get("direction") or "").upper()
            pnl = float(r.get("pnl_net", 0))
            if d == "LONG":
                if pnl > 0: long_wins += 1
                elif pnl < 0: long_loses += 1
            elif d == "SHORT":
                if pnl > 0: short_wins += 1
                elif pnl < 0: short_loses += 1

        long_score = long_wins - long_loses
        short_score = short_wins - short_loses
        diff = long_score - short_score
        total = max(1, long_wins + long_loses + short_wins + short_loses)
        dominance = abs(diff) / total

        if diff > 0:
            bias = "LONG"
        elif diff < 0:
            bias = "SHORT"
        else:
            bias = "NEUTRAL"

        return {
            "bias": bias,
            "dominance": round(dominance, 3),
            "long_score": long_score,
            "short_score": short_score,
            "total": total,
            "breakdown": {
                "long_wins": long_wins, "long_loses": long_loses,
                "short_wins": short_wins, "short_loses": short_loses,
            },
        }

    def evaluate(self, market: str, direction: str, now_ts: float) -> Dict[str, Any]:
        """direction 이 market bias 에 반하면 페널티.

        Returns:
            {"delta": int, "bias": str, "dominance": float, "details": {...}}
        """
        out: Dict[str, Any] = {"delta": 0, "bias": "NEUTRAL", "dominance": 0.0}
        cfg = self.config
        if not getattr(cfg, "market_bias_enabled", False):
            return out

        ttl = float(getattr(cfg, "mb_cache_ttl_sec", 180.0))
        if self._cache and (now_ts - self._cache_ts) < ttl:
            bias_info = self._cache
        else:
            lookback_count = int(getattr(cfg, "mb_lookback_trades", 12))
            lookback_hours = float(getattr(cfg, "mb_lookback_hours", 6.0))
            exits = self._load_recent_all_exits(lookback_count, lookback_hours, now_ts)
            bias_info = self._compute_bias(exits)
            self._cache = bias_info
            self._cache_ts = now_ts
            logger.info("[mb] bias=%s dominance=%.2f (%d exits)",
                        bias_info["bias"], bias_info["dominance"], bias_info["total"])

        out["bias"] = bias_info["bias"]
        out["dominance"] = bias_info["dominance"]
        out["details"] = bias_info.get("breakdown", {})

        threshold = float(getattr(cfg, "mb_dominance_threshold", 0.5))
        penalty_delta = float(getattr(cfg, "mb_against_delta", -10.0))  # [2026-05-17 100점 ×10] -1→-10
        min_total = int(getattr(cfg, "mb_min_total", 4))

        if bias_info["total"] < min_total:
            return out  # sample 부족
        if bias_info["dominance"] < threshold:
            return out  # dominance 미달

        dir_u = direction.upper()
        if bias_info["bias"] == "LONG" and dir_u == "SHORT":
            out["delta"] = penalty_delta
        elif bias_info["bias"] == "SHORT" and dir_u == "LONG":
            out["delta"] = penalty_delta

        return out
