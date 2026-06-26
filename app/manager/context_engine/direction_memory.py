"""Direction Memory — track the last N results for the same coin + same direction.

Observation (2026-04-18 evening):
- ETH SHORT on a 4-loss streak, yet the engine kept entering SHORT
- Each coin has directions that "don't fit the market" — often right after a regime flip
- The opposite direction on the same coin is still allowed (ETH LONG is OK)

Rules:
- If losses among the last N (default 4) are >= K (default 3) → conviction penalty
- If the loss streak (consecutive) is >= K (default 3) → hard block (default OFF)
- Independent of the existing direction_block file-based mechanism — this is a softer penalty

Data source: `runtime/focus_harpoon_journal.jsonl`
Record format:
    {"ts": 1775997261.6, "event": "EXIT", "market": "ETHUSDT",
     "direction": "LONG", "pnl_net": -0.91, ...}

Note:
- Owner principle "no coin blacklisting" — this is not per-coin but per **coin+direction**
- The opposite direction on the same coin is unaffected (ETH SHORT 4 losses → ETH LONG still free)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

JOURNAL_PATH = os.path.join("runtime", "focus_harpoon_journal.jsonl")


class DirectionMemoryModule:
    def __init__(self, config: Any):
        self.config = config
        # Cache: {(MARKET, DIRECTION): (ts, result_dict)}
        # Eases the cost of a 9MB+ journal forward scan — avoids redundant scans across N scanners per tick
        self._cache: Dict[tuple, tuple] = {}

    def _load_recent_exits(
        self, market: str, direction: str, lookback_days: float, now_ts: float
    ) -> List[Dict[str, Any]]:
        """Recent EXIT records for this coin+direction (oldest first).

        Only those within lookback_days. Scans the file from the end backwards, stopping past cutoff.
        now_ts: injectable for tests (for live verification)
        """
        out: List[Dict[str, Any]] = []
        if not os.path.exists(JOURNAL_PATH):
            return out

        cutoff = now_ts - lookback_days * 86400.0
        mkt_u = market.upper()
        dir_u = direction.upper()

        # Simple forward scan — sufficient since the journal is under 8MB
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
                    if (rec.get("market") or "").upper() != mkt_u:
                        continue
                    if (rec.get("direction") or "").upper() != dir_u:
                        continue
                    ts = rec.get("ts", 0)
                    # lower bound: cutoff (lookback boundary)
                    # upper bound: now_ts (accuracy of past-simulation tests)
                    if ts < cutoff or ts > now_ts:
                        continue
                    out.append(rec)
        except Exception as exc:
            logger.warning("[dm] journal read failed: %s", exc)

        # Sort newest first (ts desc)
        out.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return out

    def evaluate(self, market: str, direction: str, now_ts: float) -> Dict[str, Any]:
        """Last N results → decide delta / block.

        ★ [2026-04-25 owner "avoid being a sucker"] dm_streak_block_opposite option:
           If True, on a loss streak in this direction, block the opposite direction too (both-side rest)
           Default False → block only the current direction, opposite is free (allows flipping to SHORT)

        Returns:
            {
              "delta": int,
              "block": bool,
              "reason": str,
              "recent_n": int,
              "loss_count": int,
              "streak": int,  # consecutive loss streak (from newest)
              "opposite_blocked": bool,  # if True, the opposite direction is blocked too
            }
        """
        out: Dict[str, Any] = {
            "delta": 0, "block": False, "reason": "",
            "recent_n": 0, "loss_count": 0, "streak": 0,
            "opposite_blocked": False,
        }
        cfg = self.config
        if not getattr(cfg, "direction_memory_enabled", False):
            return out

        window_n = int(getattr(cfg, "dm_window_count", 4))
        lookback_days = float(getattr(cfg, "dm_lookback_days", 3.0))
        loss_penalty_k = int(getattr(cfg, "dm_loss_count_penalty", 3))            # count-type, keep as int
        loss_penalty_delta = float(getattr(cfg, "dm_loss_count_delta", -20.0))    # [2026-05-17 100-point scale ×10] -2→-20
        streak_block_k = int(getattr(cfg, "dm_streak_block", 4))
        block_enabled = bool(getattr(cfg, "dm_streak_block_enabled", False))
        ttl = float(getattr(cfg, "dm_cache_ttl_sec", 180.0))

        # Cache check — independent TTL per coin+direction
        key = (market.upper(), direction.upper())
        cached = self._cache.get(key)
        if cached and (now_ts - cached[0]) < ttl:
            return dict(cached[1])  # copy (protect cache from caller mutation)

        recent = self._load_recent_exits(market, direction, lookback_days, now_ts)
        recent = recent[:window_n]  # newest N only
        out["recent_n"] = len(recent)
        if not recent:
            self._cache[key] = (now_ts, dict(out))
            return out

        # loss decision: pnl_net < 0
        losses = [r for r in recent if float(r.get("pnl_net", 0)) < 0]
        out["loss_count"] = len(losses)

        # streak: consecutive losses from newest
        streak = 0
        for r in recent:
            if float(r.get("pnl_net", 0)) < 0:
                streak += 1
            else:
                break
        out["streak"] = streak

        # hard block?
        # ★ [2026-04-24] owner's asymmetry fix: permanent block → time-based block
        # unit = hours (unified with the Profit Exit Block UI, owner decision 2026-04-24)
        # block_hours=0 means permanent (legacy); >0 blocks for only N hours from the last loss ts
        if block_enabled and streak >= streak_block_k:
            block_hours = float(getattr(cfg, "dm_streak_block_hours", 0.0))
            if block_hours > 0:
                # time check based on the last loss time (recent[0].ts)
                last_loss_ts = float(recent[0].get("ts", 0) or 0)
                block_sec = block_hours * 3600.0
                elapsed = now_ts - last_loss_ts
                if elapsed >= block_sec:
                    # time expired → release block (no block fired, learning effect already sufficient)
                    logger.info("[dm] EXPIRED %s %s: streak=%d but %.1fh>=%.1fh → release",
                                market, direction, streak, elapsed/3600.0, block_hours)
                    self._cache[key] = (now_ts, dict(out))
                    return out
                # block stays — show remaining time in reason
                remain_h = (block_sec - elapsed) / 3600.0
                out["block"] = True
                out["reason"] = f"direction_memory: {market} {direction} {streak} losses in a row → {block_hours:.1f}h pause ({remain_h:.1f}h left)"
                logger.info("[dm] BLOCK %s %s: streak=%d, %.1fh remain", market, direction, streak, remain_h)
            else:
                # permanent block (legacy behavior)
                out["block"] = True
                out["reason"] = f"direction_memory: {market} {direction} {streak} losses in a row (streak≥{streak_block_k}, permanent)"
                logger.info("[dm] BLOCK %s %s: streak=%d (PERMANENT)", market, direction, streak)
            self._cache[key] = (now_ts, dict(out))
            return out

        # ★ [2026-04-25 owner "avoid being a sucker"] opposite-direction check
        # Not blocked in the current direction, but if dm_streak_block_opposite=True,
        # also check whether the opposite direction is in a blocked state → block both sides
        if block_enabled and getattr(cfg, "dm_streak_block_opposite", False):
            opp_dir = "SHORT" if direction.upper() == "LONG" else "LONG"
            opp_recent = self._load_recent_exits(market, opp_dir, lookback_days, now_ts)
            opp_recent = opp_recent[:window_n]
            opp_streak = 0
            for r in opp_recent:
                if float(r.get("pnl_net", 0)) < 0:
                    opp_streak += 1
                else:
                    break
            if opp_streak >= streak_block_k and opp_recent:
                block_hours = float(getattr(cfg, "dm_streak_block_hours", 0.0))
                opp_last_loss_ts = float(opp_recent[0].get("ts", 0) or 0)
                if block_hours > 0:
                    block_sec = block_hours * 3600.0
                    elapsed = now_ts - opp_last_loss_ts
                    if elapsed < block_sec:
                        remain_h = (block_sec - elapsed) / 3600.0
                        out["block"] = True
                        out["opposite_blocked"] = True
                        out["reason"] = (f"direction_memory(opposite): {market} {opp_dir} {opp_streak} losses in a row → "
                                         f"{direction} also blocked {block_hours:.1f}h ({remain_h:.1f}h left)")
                        logger.info("[dm] BLOCK(opp) %s %s: %s streak=%d, %.1fh remain — both sides blocked",
                                    market, direction, opp_dir, opp_streak, remain_h)
                        self._cache[key] = (now_ts, dict(out))
                        return out
                else:
                    # permanent opposite block (both-side version of legacy)
                    out["block"] = True
                    out["opposite_blocked"] = True
                    out["reason"] = (f"direction_memory(opposite): {market} {opp_dir} {opp_streak} losses in a row → "
                                     f"{direction} also permanently blocked")
                    logger.info("[dm] BLOCK(opp) %s %s: %s streak=%d (PERMANENT)",
                                market, direction, opp_dir, opp_streak)
                    self._cache[key] = (now_ts, dict(out))
                    return out

        # soft penalty?
        if out["loss_count"] >= loss_penalty_k:
            out["delta"] = loss_penalty_delta
            logger.info("[dm] PENALTY %s %s: loss=%d/%d → delta=%d",
                        market, direction, out["loss_count"], len(recent), loss_penalty_delta)

        self._cache[key] = (now_ts, dict(out))
        return out
