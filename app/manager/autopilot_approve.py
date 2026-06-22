# ============================================================
# File: app/manager/autopilot_approve.py
# Autocoin OS — Autopilot Step 4: Auto Approve (Extracted Mixin)
# ============================================================

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

from app.manager.oma_market_registry import MarketState
from app.manager.reserved_queue import reserved_queue
from app.manager.market_controls import apply_engine_controls
from app.manager.autopilot_helpers import (
    extract_row_strategy as _extract_row_strategy,
)
from app.manager.autopilot_scanner import _fetch_strategy_recommendations

logger = logging.getLogger(__name__)


class ApproveMixin:
    """Mixin providing Step 4 (Auto Approve) logic for AutopilotManager."""

    async def _step_approve(
        self,
        *,
        snap: Dict[str, Any],
        active_markets: List[str],
        active_reason_map: Dict[str, List[str]],
        longhold_markets: Set[str],
        target_by_strategy: Dict[str, int],
        now: float,
        reason: str,
        pp_target: int,
        al_target: int,
        ld_target: int,
        lt_target: int,
        gz_target: int,
        ct_target: int,
        sn_target: int,
        wh_target: int,
        promote_to_active: bool,
        apply_budget: bool,
        auto_approve: bool,
        desired_by_strategy: Dict[str, List[str]],
    ) -> List[Dict[str, Any]]:
        """Step 4) Auto Approve — returns list of approved market dicts."""

        approved: List[Dict[str, Any]] = []
        if auto_approve and (pp_target > 0 or al_target > 0 or ld_target > 0 or lt_target > 0 or gz_target > 0 or ct_target > 0 or sn_target > 0 or wh_target > 0):
            try:
                # refresh active
                snap2 = self.system.oma_registry.snapshot()
                active_rows2 = snap2.get("active") or []
                active_markets2 = []
                active_reason_map2 = {}
                for row in active_rows2:
                    if isinstance(row, dict):
                        m = str(row.get("market") or "").strip().upper()
                        if not m: continue
                        active_markets2.append(m)
                        rs = row.get("reason")
                        if isinstance(rs, list):
                            active_reason_map2[m] = [str(x) for x in rs]
                        else:
                            active_reason_map2[m] = []
                    elif isinstance(row, str):
                        m = str(row).strip().upper()
                        if m: active_markets2.append(m)

                active_reason_map.clear()
                active_reason_map.update(active_reason_map2)
                active_markets = active_markets2

                # "한 코인 = 한 전략" 보장용 소유 맵
                # ACTIVE/RECOVERY 중 strategy 태그가 있는 마켓만 소유 잠금.
                # 2026-03-10: WATCH 제외 — 매도 후 WATCH로 내려간 코인은
                # 다른 전략에서도 선택할 수 있도록 소유권 해제.
                strategy_owner_map: Dict[str, str] = {}
                for bucket in ("active", "recovery"):
                    for row in (snap2.get(bucket) or []):
                        if not isinstance(row, dict):
                            continue
                        m = str(row.get("market") or "").strip().upper()
                        if not m:
                            continue
                        st = _extract_row_strategy(row)
                        if st:
                            strategy_owner_map[m] = st

                active_pp = 0
                active_al = 0
                active_ld = 0
                active_lt = 0
                active_gz = 0
                active_ct = 0
                for mkt in active_markets:
                    # [2026-03-14] LongHold 전환된 코인은 슬롯 차지 안 함
                    if mkt in longhold_markets:
                        continue
                    st = strategy_owner_map.get(mkt) or self._infer_strategy(mkt, active_reason_map)
                    if st == "PINGPONG": active_pp += 1
                    elif st == "AUTOLOOP": active_al += 1
                    elif st == "LADDER": active_ld += 1
                    elif st == "LIGHTNING": active_lt += 1
                    elif st == "GAZUA": active_gz += 1
                    elif st == "CONTRARIAN": active_ct += 1

                # [2026-03-23] LongHold된 슬롯도 전략 할당량에 포함 (무한 팽창 방지)
                lh_pp = 0; lh_al = 0; lh_ld = 0; lh_lt = 0; lh_gz = 0; lh_ct = 0
                for mkt in longhold_markets:
                    st = strategy_owner_map.get(mkt) or self._infer_strategy(mkt, active_reason_map)
                    if st == "PINGPONG": lh_pp += 1
                    elif st == "AUTOLOOP": lh_al += 1
                    elif st == "LADDER": lh_ld += 1
                    elif st == "LIGHTNING": lh_lt += 1
                    elif st == "GAZUA": lh_gz += 1
                    elif st == "CONTRARIAN": lh_ct += 1

                need_pp = max(0, pp_target - active_pp - lh_pp)
                need_al = max(0, al_target - active_al - lh_al)
                need_ld = max(0, ld_target - active_ld - lh_ld)
                need_lt = max(0, lt_target - active_lt - lh_lt)
                need_gz = max(0, gz_target - active_gz - lh_gz)
                need_ct = max(0, ct_target - active_ct - lh_ct)

                # [2026-02-02] SNIPER need 계산 추가
                # [FIX 2026-03-23 P1] LongHold된 SNIPER/WHALE도 할당량에 포함
                sniper_target = max(0, int(getattr(self.system, "reserved_sniper_n", 0) or 0))
                active_sn = 0
                for mkt in active_markets:
                    if mkt in longhold_markets:
                        continue
                    st = strategy_owner_map.get(mkt) or self._infer_strategy(mkt, active_reason_map)
                    if st == "SNIPER":
                        active_sn += 1
                lh_sn = sum(1 for mkt in longhold_markets
                            if (strategy_owner_map.get(mkt) or self._infer_strategy(mkt, active_reason_map)) == "SNIPER")
                need_sn = max(0, sniper_target - active_sn - lh_sn)

                # WHALE need 계산
                whale_target = max(0, int(getattr(self.system, "reserved_whale_n", 0) or 0))
                active_wh = 0
                for mkt in active_markets:
                    if mkt in longhold_markets:
                        continue
                    st = strategy_owner_map.get(mkt) or self._infer_strategy(mkt, active_reason_map)
                    if st == "WHALE":
                        active_wh += 1
                lh_wh = sum(1 for mkt in longhold_markets
                            if (strategy_owner_map.get(mkt) or self._infer_strategy(mkt, active_reason_map)) == "WHALE")
                need_wh = max(0, whale_target - active_wh - lh_wh)

                result_targets = {"PINGPONG": pp_target, "AUTOLOOP": al_target, "LADDER": ld_target, "LIGHTNING": lt_target, "GAZUA": gz_target, "CONTRARIAN": ct_target, "SNIPER": sniper_target, "WHALE": whale_target}
                result_active_counts = {"PINGPONG": active_pp, "AUTOLOOP": active_al, "LADDER": active_ld, "LIGHTNING": active_lt, "GAZUA": active_gz, "CONTRARIAN": active_ct, "SNIPER": active_sn, "WHALE": active_wh}
                result_needs = {"PINGPONG": need_pp, "AUTOLOOP": need_al, "LADDER": need_ld, "LIGHTNING": need_lt, "GAZUA": need_gz, "CONTRARIAN": need_ct, "SNIPER": need_sn, "WHALE": need_wh}

                if need_pp > 0 or need_al > 0 or need_ld > 0 or need_lt > 0 or need_gz > 0 or need_ct > 0 or need_sn > 0 or need_wh > 0:
                    # [2026-01-31] 전략별 추천 API에서 후보 가져오기 (멀티타임프레임 분석)
                    # 기존: reserved_queue.snapshot() → 통합 5분 캔들 분석
                    # 개선: 전략별 추천 API → 전략에 맞는 타임프레임 분석

                    # [2026-05-30] 부모님 발견: 후보 등록 시점에 ACTIVE 박지 않음 (가짜 Active 마켓 표시 사고)
                    # 진짜 진입 (Bybit qty > 0) 후 reconcile loop (hs_mixin_reconcile.py:622-688) 가 자동 ACTIVE 승격.
                    # 부모님 룰: Active 마켓 = 실제 거래 + 실시간 손익. 진입 X 코인이 Active 마켓에 표시되면 안 됨.
                    # promote_to_active flag UI 호환성 유지 — 의미만 변경 ("진입 후 자동 promote 활성" 의미로 해석)
                    to_state = MarketState.WATCH

                    # [2026-02-03] 모든 전략 자동 승인 활성화 (완전 자동 순환)
                    # PINGPONG/AUTOLOOP: 초기부터 자동화 (기본 True 유지)
                    # LADDER/LIGHTNING/CONTRARIAN/SNIPER: 검증 완료로 자동화 (False → True)
                    # GAZUA: demote 자체가 안 되므로 의미 없음 (False 유지)
                    aa_pp = bool(getattr(self.system, "autopilot_auto_approve_pingpong", True))
                    aa_al = bool(getattr(self.system, "autopilot_auto_approve_autoloop", True))
                    aa_ld = bool(getattr(self.system, "autopilot_auto_approve_ladder", True))
                    aa_lt = bool(getattr(self.system, "autopilot_auto_approve_lightning", True))
                    aa_gz = bool(getattr(self.system, "autopilot_auto_approve_gazua", False))  # GAZUA는 demote 안 됨
                    aa_ct = bool(getattr(self.system, "autopilot_auto_approve_contrarian", True))
                    aa_sn = bool(getattr(self.system, "autopilot_auto_approve_sniper", True))
                    aa_wh = bool(getattr(self.system, "autopilot_auto_approve_whale", True))

                    ai_gate_en = bool(getattr(self.system, "autopilot_ai_gate_enabled", False))
                    ai_gate_thr = float(getattr(self.system, "autopilot_ai_gate_threshold", 0.55) or 0.55)

                    # 전략별 최소 신뢰도 % 로드
                    _min_conf_map: Dict[str, float] = {
                        "PINGPONG": float(getattr(self.system, "autopilot_min_confidence_pingpong", 60.0) or 60.0),
                        "AUTOLOOP": float(getattr(self.system, "autopilot_min_confidence_autoloop", 60.0) or 60.0),
                        "LADDER": float(getattr(self.system, "autopilot_min_confidence_ladder", 60.0) or 60.0),
                        "LIGHTNING": float(getattr(self.system, "autopilot_min_confidence_lightning", 55.0) or 55.0),
                        "GAZUA": float(getattr(self.system, "autopilot_min_confidence_gazua", 55.0) or 55.0),
                        "CONTRARIAN": float(getattr(self.system, "autopilot_min_confidence_contrarian", 55.0) or 55.0),
                        "SNIPER": float(getattr(self.system, "autopilot_min_confidence_sniper", 65.0) or 65.0),
                        "WHALE": float(getattr(self.system, "autopilot_min_confidence_whale", 65.0) or 65.0),
                    }

                    # [2026-03-10] Night Mode: 진입 점수 문턱 상향
                    _night_active = False
                    try:
                        _night_active = bool(getattr(self.system, 'is_night_mode_active', lambda: False)())
                        if _night_active:
                            _boost_pct = float(getattr(self.system, 'night_mode_entry_score_boost_pct', 30.0) or 30.0)
                            _boost_mult = 1.0 + (_boost_pct / 100.0)
                            _min_conf_map = {k: min(95.0, v * _boost_mult) for k, v in _min_conf_map.items()}
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[APPROVE] Night Mode entry score boost: %s", exc, exc_info=True)

                    def _confidence_gate_pass(it: Dict[str, Any], strategy: str) -> bool:
                        """전략별 최소 신뢰도 % 이상인지 확인."""
                        min_conf = _min_conf_map.get(strategy.upper(), 0.0)
                        if min_conf <= 0:
                            return True
                        try:
                            conf = float(it.get("confidence") or 0.0)
                        except (KeyError, AttributeError, TypeError, ValueError):
                            logger.warning("[Autopilot] confidence parse failed", exc_info=True)
                            conf = 0.0
                        return conf >= min_conf

                    def _ai_gate_pass(it: Dict[str, Any]) -> bool:
                        if not ai_gate_en:
                            return True
                        try:
                            s = float(it.get("ai_score") or 0.0)
                        except (KeyError, AttributeError, TypeError, ValueError):
                            logger.warning("[Autopilot] ai_score parse failed", exc_info=True)
                            s = 0.0
                        return s >= ai_gate_thr

                    queue_snap = reserved_queue.snapshot()
                    queue_items = queue_snap.get("items") or []
                    queue_by_strategy: Dict[str, int] = {}
                    try:
                        for qi in queue_items:
                            qs = str((qi or {}).get("strategy") or (qi or {}).get("recommended_strategy") or "").strip().upper()
                            if not qs:
                                continue
                            queue_by_strategy[qs] = int(queue_by_strategy.get(qs, 0) + 1)
                    except (KeyError, AttributeError, TypeError, ValueError):
                        logger.warning("[Autopilot] queue_by_strategy build failed", exc_info=True)
                        queue_by_strategy = {}

                    need_by_strategy: Dict[str, int] = {
                        "PINGPONG": need_pp,
                        "AUTOLOOP": need_al,
                        "LADDER": need_ld,
                        "LIGHTNING": need_lt,
                        "GAZUA": need_gz,
                        "CONTRARIAN": need_ct,
                        "SNIPER": need_sn,
                        "WHALE": need_wh,
                    }
                    allow_by_strategy: Dict[str, bool] = {
                        "PINGPONG": aa_pp,
                        "AUTOLOOP": aa_al,
                        "LADDER": aa_ld,
                        "LIGHTNING": aa_lt,
                        "GAZUA": aa_gz,
                        "CONTRARIAN": aa_ct,
                        "SNIPER": aa_sn,
                        "WHALE": aa_wh,
                    }
                    selected_markets: Set[str] = set()
                    picked_counts: Dict[str, int] = {k: 0 for k in need_by_strategy.keys()}
                    approve_debug: Dict[str, Any] = {
                        "queue_total": int(len(queue_items)),
                        "queue_by_strategy": queue_by_strategy,
                        "need_by_strategy": dict(need_by_strategy),
                        "allow_by_strategy": dict(allow_by_strategy),
                        "picked_queue_by_strategy": {},
                        "picked_fallback_by_strategy": {},
                        "skip_owner_mismatch": 0,
                        "skip_active": 0,
                        "approve_errors": 0,
                        "approved_markets": [],
                    }

                    def _candidate_status(market: str, strategy: str) -> str:
                        mkt = str(market or "").strip().upper()
                        if not mkt:
                            return "empty"
                        if mkt in selected_markets:
                            return "picked_dup"
                        try:
                            if self.system.oma_registry.get_state(mkt) == MarketState.ACTIVE:
                                return "active"
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                            logger.warning("[APPROVE] _candidate_status fallback: %s", exc, exc_info=True)
                        owner = str(strategy_owner_map.get(mkt) or "").strip().upper()
                        if owner and owner != strategy:
                            return "owned_other"
                        return "ok"

                    def _get_from_queue(strategy: str, n: int, allowed: bool) -> List[Dict[str, Any]]:
                        """reserved_queue에서 전략별 후보 가져오기 + AI Gate + 전략 소유권 필터."""
                        strategy = str(strategy or "").strip().upper()
                        if n <= 0 or not allowed or not strategy:
                            logger.debug("[Autopilot] _get_from_queue(%s) skipped: n=%s, allowed=%s", strategy, n, allowed)
                            return []
                        try:
                            strategy_items = [
                                it for it in queue_items
                                if str(it.get("strategy") or it.get("recommended_strategy") or "").strip().upper() == strategy
                            ]
                            filtered: List[Dict[str, Any]] = []
                            ai_gate_blocked = 0
                            confidence_blocked = 0
                            active_blocked = 0
                            owner_blocked = 0
                            dup_blocked = 0
                            for it in strategy_items:
                                if not _ai_gate_pass(it):
                                    ai_gate_blocked += 1
                                    continue
                                if not _confidence_gate_pass(it, strategy):
                                    confidence_blocked += 1
                                    continue
                                mkt = str(it.get("market") or "").strip().upper()
                                st = _candidate_status(mkt, strategy)
                                if st == "active":
                                    active_blocked += 1
                                    continue
                                if st == "owned_other":
                                    owner_blocked += 1
                                    continue
                                if st == "picked_dup":
                                    dup_blocked += 1
                                    continue
                                if st != "ok":
                                    continue
                                filtered.append(it)
                                selected_markets.add(mkt)
                                picked_counts[strategy] = int(picked_counts.get(strategy, 0) + 1)
                                if len(filtered) >= n:
                                    break
                            logger.info(
                                f"[Autopilot] _get_from_queue({strategy}): need={n}, in_queue={len(strategy_items)}, "
                                f"ai_blocked={ai_gate_blocked}, conf_blocked={confidence_blocked}, active_blocked={active_blocked}, owner_blocked={owner_blocked}, "
                                f"dup_blocked={dup_blocked}, passed={len(filtered)}, min_conf={_min_conf_map.get(strategy, 0)}"
                            )
                            approve_debug["picked_queue_by_strategy"][strategy] = int(len(filtered))
                            return filtered
                        except (KeyError, AttributeError, TypeError, ValueError) as e:
                            logger.warning("[Autopilot] _get_from_queue(%s) error: %s", strategy, e)
                            approve_debug["picked_queue_by_strategy"][strategy] = 0
                            return []

                    def _get_from_fallback(strategy: str, n: int, allowed: bool) -> List[Dict[str, Any]]:
                        """queue 부족 시 전략 추천 API를 보조로 사용 (강제 매수 아님)."""
                        strategy = str(strategy or "").strip().upper()
                        if n <= 0 or not allowed or not strategy:
                            return []
                        try:
                            fetch_n = max(int(n) * 3, int(n))
                            candidates = _fetch_strategy_recommendations(strategy, n=fetch_n)
                            if not candidates:
                                return []
                            filtered: List[Dict[str, Any]] = []
                            ai_gate_blocked = 0
                            confidence_blocked = 0
                            active_blocked = 0
                            owner_blocked = 0
                            dup_blocked = 0
                            for it in candidates:
                                if not _ai_gate_pass(it):
                                    ai_gate_blocked += 1
                                    continue
                                if not _confidence_gate_pass(it, strategy):
                                    confidence_blocked += 1
                                    continue
                                mkt = str(it.get("market") or "").strip().upper()
                                st = _candidate_status(mkt, strategy)
                                if st == "active":
                                    active_blocked += 1
                                    continue
                                if st == "owned_other":
                                    owner_blocked += 1
                                    continue
                                if st == "picked_dup":
                                    dup_blocked += 1
                                    continue
                                if st != "ok":
                                    continue
                                cp = dict(it)
                                cp["strategy"] = strategy
                                filtered.append(cp)
                                selected_markets.add(mkt)
                                picked_counts[strategy] = int(picked_counts.get(strategy, 0) + 1)
                                if len(filtered) >= n:
                                    break
                            logger.info(
                                f"[Autopilot] _get_from_fallback({strategy}): need={n}, fetched={len(candidates)}, "
                                f"ai_blocked={ai_gate_blocked}, conf_blocked={confidence_blocked}, active_blocked={active_blocked}, owner_blocked={owner_blocked}, "
                                f"dup_blocked={dup_blocked}, passed={len(filtered)}, min_conf={_min_conf_map.get(strategy, 0)}"
                            )
                            approve_debug["picked_fallback_by_strategy"][strategy] = int(len(filtered))
                            return filtered
                        except (KeyError, AttributeError, TypeError, ValueError) as e:
                            logger.warning("[Autopilot] _get_from_fallback(%s) error: %s", strategy, e)
                            approve_debug["picked_fallback_by_strategy"][strategy] = 0
                            return []

                    picks: List[Dict[str, Any]] = []
                    strategy_order = ["PINGPONG", "AUTOLOOP", "LADDER", "LIGHTNING", "GAZUA", "CONTRARIAN", "SNIPER", "WHALE"]
                    for st in strategy_order:
                        picks.extend(_get_from_queue(st, int(need_by_strategy.get(st, 0) or 0), bool(allow_by_strategy.get(st, False))))

                    # queue 부족 시 전략별 보조 후보 조회 (WHALE은 실시간 스캐너 블록에서 처리)
                    # [2026-03-30] to_thread + timeout 30초 — 이벤트 루프 블로킹 + 스레드 고갈 방지
                    for st in strategy_order:
                        if st == "WHALE":
                            continue  # WHALE fallback은 아래 스캐너 블록에서 처리
                        remain = int(need_by_strategy.get(st, 0) or 0) - int(picked_counts.get(st, 0) or 0)
                        if remain <= 0:
                            continue
                        try:
                            fb = await asyncio.wait_for(
                                asyncio.to_thread(
                                    _get_from_fallback, st, remain,
                                    bool(allow_by_strategy.get(st, False))),
                                timeout=30.0)
                            picks.extend(fb)
                        except asyncio.TimeoutError:
                            logger.warning("[Autopilot] _get_from_fallback(%s) timeout 30s — skipped", st)

                    # ── WHALE 전용 스캐너 ──────────────────────────────────────────────
                    # WHALE은 reserved_queue 미사용: 전체 USDT 마켓을 3분봉으로 실시간 스캔
                    # queue에서 못 채운 나머지 WHALE 슬롯만 실시간 스캔으로 보충
                    _wh_remaining = max(0, need_wh - int(picked_counts.get("WHALE", 0) or 0))
                    if _wh_remaining > 0 and aa_wh:
                        try:
                            from app.strategy.strategy_plugins import get_plugin as _get_whale_plugin_fn
                            _whale_plugin = _get_whale_plugin_fn("WHALE")
                            _all_markets = [
                                str(k).strip().upper()
                                for k in self.system.coordinator.contexts.keys()
                                if str(k).strip().upper().endswith("USDT")
                            ]
                            _whale_excluded = set(active_markets) | set(selected_markets)
                            _whale_hits = await asyncio.to_thread(
                                _whale_plugin.scan_markets,
                                _all_markets,
                                {},
                                _whale_excluded,
                            )
                            logger.info(
                                f"[WHALE/Scanner] 스캔 완료 — {len(_all_markets)}개 마켓, "
                                f"신호 {len(_whale_hits)}개, remaining={_wh_remaining}"
                            )
                            for _wh in _whale_hits[:_wh_remaining]:
                                _wm = str(_wh.get("market") or "").strip().upper()
                                if not _wm:
                                    continue
                                if _candidate_status(_wm, "WHALE") != "ok":
                                    continue
                                _wowner = str(strategy_owner_map.get(_wm) or "").strip().upper()
                                if _wowner and _wowner != "WHALE":
                                    continue
                                # Risk Budget gate
                                try:
                                    _rb = getattr(self.risk_budget_manager, "_state", None)
                                    if _rb and not _rb.new_entry_allowed:
                                        continue
                                except (KeyError, AttributeError, TypeError) as exc:
                                    logger.warning("[APPROVE] Risk Budget gate: %s", exc, exc_info=True)
                                # BTC Guard
                                try:
                                    if bool(getattr(self.system, "btc_guard_enabled", False)) and \
                                            str(getattr(self.system, "btc_guard_mode", "") or "").upper() == "DEFENSE":
                                        continue
                                except (KeyError, AttributeError, TypeError) as exc:
                                    logger.warning("[APPROVE] BTC Guard: %s", exc, exc_info=True)
                                # [FIX 2026-03-23] WHALE은 AI confidence gate 면제
                                # WHALE 진입 조건(RSI≤30 + 구름두께≥1.5% + 2캔들 cloud_top 돌파
                                # + StochRSI %K>%D + 거래량 2배 이상) 자체가 엄격한 필터.
                                # confidence 0.0~1.0 스케일이라 AI 65% 기준과 호환 불가.
                                try:
                                    self.system.oma_set_market(
                                        market=_wm,
                                        state=to_state,
                                        reason=["whale_scan_approve", "autopilot_autoapprove", "strategy:WHALE"],
                                    )
                                    try:
                                        apply_engine_controls(self.system, _wm, "WHALE", None)
                                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                                        logger.warning("[APPROVE] WHALE apply_engine_controls: %s", exc, exc_info=True)
                                    selected_markets.add(_wm)
                                    approved.append({
                                        "market": _wm,
                                        "strategy": "WHALE",
                                        "to_state": str(to_state.value),
                                        "budget_usdt": None,
                                        "whale_reason": _wh.get("reason"),
                                        "whale_score": _wh.get("score"),
                                    })
                                    approve_debug["approved_markets"].append(_wm)
                                    try:
                                        from app.manager.autopilot_tracker import autopilot_tracker
                                        autopilot_tracker.record_decision(_wm, "WATCH", str(to_state.value), "WHALE", "whale_scan_approve")
                                    except (AttributeError, TypeError, ValueError) as exc:
                                        logger.warning("[APPROVE] WHALE tracker record_decision: %s", exc, exc_info=True)
                                    try:
                                        reserved_queue.add_history({
                                            "kind": "APPROVE",
                                            "source": "whale_scanner",
                                            "market": _wm,
                                            "strategy": "WHALE",
                                            "to_state": str(to_state.value),
                                            "auto": True,
                                            "whale_reason": _wh.get("reason"),
                                        })
                                    except (KeyError, AttributeError, TypeError) as exc:
                                        logger.warning("[APPROVE] WHALE add_history: %s", exc, exc_info=True)
                                    logger.info(f"[WHALE/Scanner] 🐋 승인: {_wm} → {to_state.value} | {_wh.get('reason')}")
                                except (KeyError, AttributeError, TypeError, ValueError) as _wexc:
                                    logger.warning("[WHALE/Scanner] 승인 실패: %s: %s", _wm, _wexc)
                        except (KeyError, AttributeError, TypeError, ValueError) as _wexc:
                            logger.warning("[WHALE/Scanner] 스캐너 오류: %s", _wexc, exc_info=True)
                    # ─────────────────────────────────────────────────────────────────

                    for it in picks:
                        # 전략별 추천 API 결과는 rid가 없으므로 market으로 처리
                        market = str(it.get("market") or "").strip().upper()
                        if not market:
                            continue
                        strategy = str(it.get("recommended_strategy") or it.get("strategy") or "").strip().upper()
                        if not strategy:
                            # API 응답에서 strategy_match가 True인 경우 해당 전략 사용
                            strategy = "AUTOLOOP"  # 기본값

                        # "한 코인 = 한 전략": 이미 다른 전략 소유면 자동 승인 스킵
                        owner = str(strategy_owner_map.get(market) or "").strip().upper()
                        if owner and owner != strategy:
                            approve_debug["skip_owner_mismatch"] = int(approve_debug.get("skip_owner_mismatch", 0) + 1)
                            continue

                        if self.system.oma_registry.get_state(market) == MarketState.ACTIVE:
                            approve_debug["skip_active"] = int(approve_debug.get("skip_active", 0) + 1)
                            continue

                        # [FIX 2026-03-23] Path B에도 confidence + AI gate 적용
                        # 이전: Path A(_get_from_queue)에서만 체크 → Path B는 무조건 approve
                        # 수정: 동일한 _confidence_gate_pass / _ai_gate_pass 적용
                        if not _ai_gate_pass(it):
                            approve_debug["pathB_ai_gate_blocked"] = int(approve_debug.get("pathB_ai_gate_blocked", 0) + 1)
                            continue
                        if not _confidence_gate_pass(it, strategy):
                            approve_debug["pathB_confidence_blocked"] = int(approve_debug.get("pathB_confidence_blocked", 0) + 1)
                            continue

                        # ── Phase 3-B: Risk Overlay Gate ──
                        _risk_blocked = False

                        # 1) Risk Budget: DEFENSE 모드면 신규 진입 차단
                        try:
                            _rb_state = getattr(self.risk_budget_manager, "_state", None)
                            if _rb_state and not _rb_state.new_entry_allowed:
                                approve_debug["risk_budget_blocked"] = int(approve_debug.get("risk_budget_blocked", 0) + 1)
                                _risk_blocked = True
                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[APPROVE] Risk Budget DEFENSE gate: %s", exc, exc_info=True)

                        # 2) [제거 2026-06-02] Correlation Guard 호출 청산.
                        #    correlation_guard 는 FOCUS/HARPOON 전용 "섹터 분산 conviction 페널티" 가드인데(allowed 항상 True),
                        #    autopilot 이 차단용으로 오용 + 존재하지 않는 check_entry_allowed() 호출 → 처음부터 항상 AttributeError 로 죽어있었음.
                        #    역할 분담: 섹터 분산 = FOCUS pair_block / autopilot 슬롯확장 시 portfolio_risk_manager.check_correlation_limit,
                        #             코인 충돌 = owner_blocked·dup_blocked 가 이미 담당. → 잘못 빌린 죽은 호출 제거.

                        # 3) Strategy Loss Cooldown: 3연패 시 30분 쿨다운
                        if not _risk_blocked:
                            try:
                                _cd_until = self._strategy_loss_cooldown_until.get(strategy, 0.0)
                                if _cd_until > 0 and now < _cd_until:
                                    approve_debug["loss_cooldown_blocked"] = int(approve_debug.get("loss_cooldown_blocked", 0) + 1)
                                    _risk_blocked = True
                            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[APPROVE] Strategy Loss Cooldown: %s", exc, exc_info=True)

                        # 4) BTC Guard: BTC 급락 시 CONTRARIAN 외 차단
                        if not _risk_blocked and strategy != "CONTRARIAN":
                            try:
                                _btc_guard_on = bool(getattr(self.system, "btc_guard_enabled", False))
                                _btc_mode = str(getattr(self.system, "btc_guard_mode", "") or "").upper()
                                if _btc_guard_on and _btc_mode == "DEFENSE":
                                    approve_debug["btc_guard_blocked"] = int(approve_debug.get("btc_guard_blocked", 0) + 1)
                                    _risk_blocked = True
                            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[APPROVE] BTC Guard DEFENSE gate: %s", exc, exc_info=True)

                        if _risk_blocked:
                            continue

                        # ── staleness TTL gate ──
                        _item_age_sec = time.time() - float(it.get("ts") or 0.0)
                        _max_age_sec = float(os.getenv("OMA_AUTOPILOT_ITEM_MAX_AGE_SEC", "600"))
                        if _item_age_sec > _max_age_sec:
                            approve_debug["skip_stale_item"] = int(approve_debug.get("skip_stale_item", 0)) + 1
                            logger.info("[Autopilot] Skip stale item %s (age=%.0fs > %.0fs)", market, _item_age_sec, _max_age_sec)
                            continue

                        # ── 예산 재계산 (승인 시점 equity 기반) ──
                        budget_usdt = None
                        if apply_budget:
                            try:
                                from app.manager.reserved_selector import _suggest_budget
                                _metrics = it.get("metrics") or {}
                                _eq = float(getattr(self.system, "_last_equity_usdt", 0) or 0)
                                if _eq <= 0:
                                    _eq = float(getattr(self.system, "equity_usdt", 0) or 0)
                                _dr = float(getattr(self.system, "deploy_ratio", 1.0) or 1.0)
                                _total_cap = _eq * _dr
                                # [2026-05-30] Per-strategy explicit budget — plugin 자체 풀 격리
                                # 부모님 결단: "각 전략별 예산을 정해주면 그 안에서 자동배분"
                                # budget > 0 → plugin 자체 풀 (다른 전략과 충돌 X)
                                # budget = 0 → 옛 자동 배분 (호환성 fallback)
                                _strat_budget_attr = f"reserved_{str(strategy or '').lower()}_budget_usdt"
                                _strat_budget = float(getattr(self.system, _strat_budget_attr, 0.0) or 0.0)
                                if _strat_budget > 0:
                                    _total_cap = min(_strat_budget, _total_cap)
                                    logger.info("[Autopilot] %s 자체 예산 적용: $%.0f (전체 자본 $%.0f 중)",
                                                 strategy, _strat_budget, _eq * _dr)
                                _active_n = len(self.system.oma_registry.list_active())

                                if _total_cap > 0 and _metrics:
                                    _recalc = _suggest_budget(
                                        strategy=strategy,
                                        base_usdt=0.0,
                                        vol24_usdt=float(_metrics.get("vol24_usdt") or 0),
                                        vol_median_usdt=float(_metrics.get("vol24_usdt") or 0),
                                        min_order_usdt=float(self.system.effective_min_order_usdt()),
                                        max_budget_usdt=_total_cap * 0.20,
                                        price=float(_metrics.get("price") or 0),
                                        entry_qty_guard_on=False,
                                        entry_max_qty=0.0,
                                        depth_factor=0.0,
                                        depth_ask_usdt=float(_metrics.get("depth_ask_usdt") or 0),
                                        depth_bid_usdt=float(_metrics.get("depth_bid_usdt") or 0),
                                        total_capital_usdt=_total_cap,
                                        existing_markets_count=_active_n,
                                        spread_bps=float(_metrics.get("spread_bps") or 0),
                                        range_ratio_24h=float(_metrics.get("range_ratio_24h") or 0),
                                    )
                                    if _recalc and _recalc > 0:
                                        budget_usdt = _recalc
                                        if getattr(self.system, "recovery_boost_active", False):
                                            _boost = float(getattr(self.system, "recovery_boost_budget_mult", 1.0) or 1.0)
                                            if _boost > 1.0:
                                                budget_usdt = round(budget_usdt * _boost, 0)
                                        logger.info("[Autopilot] Budget recalc %s: scan=%.0f → fresh=%.0f (cap=%.0f, active=%d)",
                                                     market, float(it.get("suggested_budget_usdt") or 0), budget_usdt, _total_cap, _active_n)
                            except (KeyError, AttributeError, TypeError, ValueError):
                                logger.warning("[Autopilot] Budget recalc failed for %s — falling back to scan-time value", market, exc_info=True)

                            if budget_usdt is None:
                                try:
                                    b = float(it.get("suggested_budget_usdt") or it.get("budget") or 0.0)
                                    if b > 0: budget_usdt = b
                                except (TypeError, ValueError):
                                    logger.warning("[Autopilot] Step4 예산 추출 실패: %s → budget=None", market)
                                    budget_usdt = None

                        # ── 2026-03-10: 승인 직전 최신 전략 소유 재확인 (중복 배정 방지) ──
                        try:
                            _fresh_snap = self.system.oma_registry.snapshot()
                            for _fb in ("active", "watch", "recovery"):
                                for _fr in (_fresh_snap.get(_fb) or []):
                                    if not isinstance(_fr, dict):
                                        continue
                                    _fm = str(_fr.get("market") or "").strip().upper()
                                    if _fm == market:
                                        _fo = _extract_row_strategy(_fr)
                                        if _fo and _fo != strategy:
                                            approve_debug["skip_fresh_owner"] = int(approve_debug.get("skip_fresh_owner", 0) + 1)
                                            raise StopIteration
                            _fresh_state = self.system.oma_registry.get_state(market)
                            if _fresh_state == MarketState.ACTIVE:
                                approve_debug["skip_fresh_active"] = int(approve_debug.get("skip_fresh_active", 0) + 1)
                                continue
                        except StopIteration:
                            logger.warning("[Autopilot] StopIteration during duplicate assignment check for %s", market, exc_info=True)
                            continue
                        except (KeyError, AttributeError, TypeError, ValueError):
                            logger.warning("[Autopilot] 중복 배정 방지 체크 실패: %s — 이중 배정 위험", market, exc_info=True)

                        try:
                            self.system.oma_set_market(
                                market=market,
                                state=to_state,
                                reason=["reserved_approve", "autopilot_autoapprove", f"strategy:{strategy}", "source:strategy_recommendations"],
                                budget_usdt=budget_usdt,
                            )
                            try:
                                from app.manager.autopilot_tracker import autopilot_tracker
                                autopilot_tracker.record_decision(market, "WATCH", str(to_state.value), strategy, "reserved_approve autopilot_autoapprove")
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[APPROVE] tracker record_decision: %s", exc, exc_info=True)
                            # 추천 파라미터 추출 및 적용 (전략별 추천 API에서 제공)
                            recommended_params = None
                            try:
                                rp = it.get("recommended_params")
                                if rp and isinstance(rp, dict):
                                    recommended_params = rp
                            except (KeyError, AttributeError, TypeError) as exc:
                                logger.warning("[APPROVE] recommended_params extract: %s", exc, exc_info=True)

                            try:
                                apply_engine_controls(self.system, market, strategy, recommended_params)
                            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                                logger.warning("[APPROVE] apply_engine_controls: %s", exc, exc_info=True)

                            approved.append({
                                "market": market,
                                "strategy": strategy,
                                "to_state": str(to_state.value),
                                "budget_usdt": budget_usdt,
                                "recommended_params": recommended_params,
                                "ai_score": it.get("ai_score"),
                                "rsi": it.get("rsi"),
                            })
                            approve_debug["approved_markets"].append(market)
                            try:
                                self._deploy_scores[market] = float(it.get("rank_score") or it.get("score") or 0.0)
                            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[APPROVE] deploy_scores update: %s", exc, exc_info=True)
                            try:
                                reserved_queue.add_history({
                                    "kind": "APPROVE",
                                    "source": "autopilot_strategy_api",
                                    "market": market,
                                    "strategy": strategy,
                                    "to_state": str(to_state.value),
                                    "auto": True,
                                })
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[APPROVE] add_history fallback: %s", exc, exc_info=True)

                            logger.info(f"[Autopilot] Auto-approved {market} → {strategy} (budget={budget_usdt}, ai={it.get('ai_score')}, rsi={it.get('rsi')})")

                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                            approve_debug["approve_errors"] = int(approve_debug.get("approve_errors", 0) + 1)
                            try:
                                self.system.ledger.append("AUTOPILOT_APPROVE_ERROR", market=market, error=str(exc))
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[APPROVE] ledger append fallback: %s", exc, exc_info=True)
                    # Store approve_debug and computed data on self for caller to retrieve
                    self._last_approve_debug = approve_debug
                    self._last_approve_targets = result_targets
                    self._last_approve_active_counts = result_active_counts
                    self._last_approve_needs = result_needs
            except Exception as exc:
                try:
                    self.system.ledger.append("AUTOPILOT_STEP_ERROR", error=str(exc))

                    # [2026-02-03] Autopilot 에러 시 Telegram 알림
                    try:
                        from app.notify.telegram import send_telegram
                        send_telegram(
                            f"⚠️ [Autopilot Error]\n"
                            f"Error: {str(exc)[:200]}\n"
                            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                            cooldown_key="autopilot_step_error"
                        )
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[APPROVE] Telegram notify failed: %s", exc, exc_info=True)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[APPROVE] ledger append on step error: %s", exc, exc_info=True)

        return approved
