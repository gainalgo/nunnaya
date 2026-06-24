"""Market Bias — detect market direction skew from recent multi-coin EXIT results.

Observations:
- Recent N min / M EXITs where LONG profit dominates = uptrend → SHORT goes against it
- Conversely, SHORT profit dominating = downtrend → LONG goes against it
- Partly overlaps BTC Regime but is **based on real results** (the direction that
  actually worked, not theoretical EMA)

Computation:
    From the most recent lookback_trades EXIT records:
        long_wins  = count of (direction=LONG, pnl_net>0)
        long_loses = count of (direction=LONG, pnl_net<0)
        short_wins, short_loses likewise
    long_score  = long_wins - long_loses
    short_score = short_wins - short_loses
    bias = sign(long_score - short_score)  -- positive=LONG dominant, negative=SHORT dominant
    dominance = |long_score - short_score| / total

    If dominance > threshold and direction is opposite → penalty

Effect:
    LONG-dominant market × LONG entry = +0 (already dominant)
    LONG-dominant market × SHORT entry = -1 (against it)
    SHORT-dominant market × LONG entry = -1
    SHORT-dominant market × SHORT entry = +0

Difference from BTC Regime:
    - BTC: theoretical indicators (EMA / structure)
    - Market Bias: real results (recent EXIT logs)
    - When both point the same way it's certain → double penalty (combined)
    - When they diverge, neither is trustworthy → partial penalty only
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
        """Recent EXIT records across all coins (newest first).

        now_ts: injectable for tests (for real-data verification)
        """
        out: List[Dict[str, Any]] = []
        if not os.path.exists(JOURNAL_PATH):
            return out
        cutoff = now_ts - lookback_hours * 3600.0

        # If the file is large (>8MB), read backward from the end — here just forward
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
                    # lower bound: cutoff (lookback boundary)
                    # upper bound: now_ts (accuracy for past-time test simulation)
                    if ts < cutoff or ts > now_ts:
                        continue
                    out.append(rec)
        except Exception as exc:
            logger.warning("[mb] journal read failed: %s", exc)

        out.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return out[:lookback_count]

    def _compute_bias(self, exits: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compute bias metrics from EXIT records."""
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
        """Penalize when direction goes against the market bias.

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
        penalty_delta = float(getattr(cfg, "mb_against_delta", -10.0))  # [2026-05-17 100-pt scale ×10] -1→-10
        min_total = int(getattr(cfg, "mb_min_total", 4))

        if bias_info["total"] < min_total:
            return out  # insufficient sample
        if bias_info["dominance"] < threshold:
            return out  # dominance below threshold

        dir_u = direction.upper()
        if bias_info["bias"] == "LONG" and dir_u == "SHORT":
            out["delta"] = penalty_delta
        elif bias_info["bias"] == "SHORT" and dir_u == "LONG":
            out["delta"] = penalty_delta

        return out
