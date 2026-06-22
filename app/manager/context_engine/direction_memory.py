"""Direction Memory — 같은 코인 + 같은 방향 최근 N회 결과 추적.

관찰 (2026-04-18 저녁):
- ETH SHORT 4회 연패 중 엔진은 계속 SHORT 진입
- 코인별 방향이 "안 맞는 장" 이 있음 — 레짐 전환 직후에 자주 발생
- 같은 코인이라도 반대 방향은 여전히 허용 (ETH LONG 은 OK)

규칙:
- 최근 N회 (기본 4) 중 손실이 K회 (기본 3) 이상이면 → conviction 페널티
- 손실이 streak (연속) K회 (기본 3) 이상이면 → hard block (기본 OFF)
- direction_block 파일 기반의 기존 메커니즘과 독립 — 이건 softer penalty

데이터 소스: `runtime/focus_harpoon_journal.jsonl`
레코드 포맷:
    {"ts": 1775997261.6, "event": "EXIT", "market": "ETHUSDT",
     "direction": "LONG", "pnl_net": -0.91, ...}

주의:
- 사용자 원칙 "코인 실패 배제 금지" — 이건 코인 단위가 아니라 **코인+방향** 단위
- 같은 코인 반대방향은 영향 없음 (ETH SHORT 4패여도 ETH LONG 은 자유)
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
        # Journal 9MB+ forward scan 비용 완화 — scanner N개 × tick 당 중복 스캔 방지
        self._cache: Dict[tuple, tuple] = {}

    def _load_recent_exits(
        self, market: str, direction: str, lookback_days: float, now_ts: float
    ) -> List[Dict[str, Any]]:
        """해당 코인+방향의 최근 EXIT 레코드 (오래된 순).

        lookback_days 내의 것만. 파일을 끝에서부터 역순 스캔하다가 cutoff 지나면 중단.
        now_ts: 테스트 주입 가능 (실측 검증용)
        """
        out: List[Dict[str, Any]] = []
        if not os.path.exists(JOURNAL_PATH):
            return out

        cutoff = now_ts - lookback_days * 86400.0
        mkt_u = market.upper()
        dir_u = direction.upper()

        # 단순 forward scan — 저널이 8MB 이하이므로 충분
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
                    # lower bound: cutoff (lookback 경계)
                    # upper bound: now_ts (테스트 과거 시뮬레이션 정확성)
                    if ts < cutoff or ts > now_ts:
                        continue
                    out.append(rec)
        except Exception as exc:
            logger.warning("[dm] journal read failed: %s", exc)

        # 최신순 정렬 (ts desc)
        out.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return out

    def evaluate(self, market: str, direction: str, now_ts: float) -> Dict[str, Any]:
        """최근 N회 결과 → delta / block 결정.

        ★ [2026-04-25 부모 "모지리 방지"] dm_streak_block_opposite 옵션:
           True 면 해당 방향 연패 시 반대 방향도 block (양방향 쉬는 시간)
           Default False → 현재 방향만 block, 반대 방향은 자유 (SHORT 전환 허용)

        Returns:
            {
              "delta": int,
              "block": bool,
              "reason": str,
              "recent_n": int,
              "loss_count": int,
              "streak": int,  # 연속 손실 streak (최신부터)
              "opposite_blocked": bool,  # True 면 반대 방향도 block
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
        loss_penalty_k = int(getattr(cfg, "dm_loss_count_penalty", 3))            # count 류, 그대로 int
        loss_penalty_delta = float(getattr(cfg, "dm_loss_count_delta", -20.0))    # [2026-05-17 100점 ×10] -2→-20
        streak_block_k = int(getattr(cfg, "dm_streak_block", 4))
        block_enabled = bool(getattr(cfg, "dm_streak_block_enabled", False))
        ttl = float(getattr(cfg, "dm_cache_ttl_sec", 180.0))

        # Cache check — coin+direction 별 독립 TTL
        key = (market.upper(), direction.upper())
        cached = self._cache.get(key)
        if cached and (now_ts - cached[0]) < ttl:
            return dict(cached[1])  # copy (호출자 변경으로부터 캐시 보호)

        recent = self._load_recent_exits(market, direction, lookback_days, now_ts)
        recent = recent[:window_n]  # 최신 N개만
        out["recent_n"] = len(recent)
        if not recent:
            self._cache[key] = (now_ts, dict(out))
            return out

        # loss 판정: pnl_net < 0
        losses = [r for r in recent if float(r.get("pnl_net", 0)) < 0]
        out["loss_count"] = len(losses)

        # streak: 최신부터 연속 손실 수
        streak = 0
        for r in recent:
            if float(r.get("pnl_net", 0)) < 0:
                streak += 1
            else:
                break
        out["streak"] = streak

        # hard block?
        # ★ [2026-04-24] 부모님 비대칭 해결: 영구 차단 → 시간 기반 차단으로 변경
        # 단위 = 시간 (Profit Exit Block UI 와 통일, 부모님 결정 2026-04-24)
        # block_hours=0 이면 영구 (legacy), >0 이면 마지막 손실 ts 기준 N시간만 차단
        if block_enabled and streak >= streak_block_k:
            block_hours = float(getattr(cfg, "dm_streak_block_hours", 0.0))
            if block_hours > 0:
                # 마지막 손실 시각 (recent[0].ts) 기준 시간 체크
                last_loss_ts = float(recent[0].get("ts", 0) or 0)
                block_sec = block_hours * 3600.0
                elapsed = now_ts - last_loss_ts
                if elapsed >= block_sec:
                    # 시간 만료 → 차단 해제 (block 발동 안 함, 학습 효과는 이미 충분)
                    logger.info("[dm] EXPIRED %s %s: streak=%d but %.1fh>=%.1fh → release",
                                market, direction, streak, elapsed/3600.0, block_hours)
                    self._cache[key] = (now_ts, dict(out))
                    return out
                # 차단 유지 — 남은 시간 reason 에 표시
                remain_h = (block_sec - elapsed) / 3600.0
                out["block"] = True
                out["reason"] = f"direction_memory: {market} {direction} {streak} 연패 → {block_hours:.1f}h 정지 ({remain_h:.1f}h 남음)"
                logger.info("[dm] BLOCK %s %s: streak=%d, %.1fh remain", market, direction, streak, remain_h)
            else:
                # 영구 차단 (legacy 동작)
                out["block"] = True
                out["reason"] = f"direction_memory: {market} {direction} {streak} 연패 (streak≥{streak_block_k}, 영구)"
                logger.info("[dm] BLOCK %s %s: streak=%d (PERMANENT)", market, direction, streak)
            self._cache[key] = (now_ts, dict(out))
            return out

        # ★ [2026-04-25 부모 "모지리 방지"] 반대 방향 opposite 체크
        # 현재 방향으로 block 안 됐지만, dm_streak_block_opposite=True 면
        # 반대 방향이 block 발동 상태인지도 조회 → 양방향 차단
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
                        out["reason"] = (f"direction_memory(opposite): {market} {opp_dir} {opp_streak}연패 → "
                                         f"{direction} 도 {block_hours:.1f}h block ({remain_h:.1f}h 남음)")
                        logger.info("[dm] BLOCK(opp) %s %s: %s streak=%d, %.1fh remain — 양방향 차단",
                                    market, direction, opp_dir, opp_streak, remain_h)
                        self._cache[key] = (now_ts, dict(out))
                        return out
                else:
                    # 영구 opposite block (legacy 의 양방향 버전)
                    out["block"] = True
                    out["opposite_blocked"] = True
                    out["reason"] = (f"direction_memory(opposite): {market} {opp_dir} {opp_streak}연패 → "
                                     f"{direction} 도 영구 block")
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
