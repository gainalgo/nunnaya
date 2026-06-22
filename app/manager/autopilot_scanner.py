# ============================================================
# File: app/manager/autopilot_scanner.py
# Autocoin OS v3-H — Autopilot Scanner Mixin (Step 1)
# Phase 3-C: Extracted from autopilot_manager.py
# ============================================================

from __future__ import annotations
import asyncio
import functools
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from app.manager.autopilot_helpers import normalize_strategy_name as _normalize_strategy_name
from app.manager.reserved_queue import reserved_queue
from app.manager.reserved_selector import build_reserved_candidates
from app.manager.pp_al_rank_scorer import enrich_pp_al_rank_scores

logger = logging.getLogger(__name__)

# [2026-02-01] 전략별 추천 - 직접 함수 호출 (HTTP 제거)
# HTTP 호출은 서버 시작 시 타이밍 문제로 실패할 수 있음
_cached_system = None  # AutopilotManager.system 참조용


def _fetch_strategy_recommendations(strategy: str, n: int = 5) -> List[Dict[str, Any]]:
    """전략별 추천 후보 가져오기 (직접 함수 호출).

    [2026-02-01] HTTP 호출 대신 reserved_selector.build_reserved_candidates 직접 호출.
    서버 시작 시 타이밍 문제 해결.
    """
    try:
        from app.manager.reserved_selector import build_reserved_candidates

        if _cached_system is None:
            logger.warning("[Autopilot] _cached_system is None, cannot fetch %s", strategy)
            return []

        # 전략별로 해당 슬롯만 요청
        kwargs = {
            "pingpong_n": n if strategy.upper() == "PINGPONG" else 0,
            "autoloop_n": n if strategy.upper() == "AUTOLOOP" else 0,
            "ladder_n": n if strategy.upper() == "LADDER" else 0,
            "lightning_n": n if strategy.upper() == "LIGHTNING" else 0,
            "gazua_n": n if strategy.upper() == "GAZUA" else 0,
            "contrarian_n": n if strategy.upper() == "CONTRARIAN" else 0,
            "sniper_n": n if strategy.upper() == "SNIPER" else 0,
        }

        items, summary = build_reserved_candidates(_cached_system, **kwargs)

        # 해당 전략 후보만 필터링 (필드명 호환: recommended_strategy 또는 strategy)
        def _get_item_strategy(it: Dict[str, Any]) -> str:
            return str(it.get("recommended_strategy") or it.get("strategy") or "").strip().upper()

        result = [it for it in items if _get_item_strategy(it) == strategy.upper()]

        if result:
            logger.info(f"[Autopilot] Fetched {len(result)} {strategy} candidates (direct call)")
        else:
            # 디버그용: 후보가 0인 경우 로그
            logger.debug(f"[Autopilot] No {strategy} candidates found (items={len(items)}, kwargs={kwargs})")

        return result

    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("[Autopilot] Failed to fetch %s recommendations: %s", strategy, e)
    return []


# [2026-02-01] 급등/역행 스캐너 - 직접 함수 호출 (HTTP 제거)
def _fetch_surge_coins(
    min_surge_pct: float = 5.0,
    top_n: int = 5,
    timeframe: str = "1h",
    exclude_active: bool = True,
    mode: str = "both",  # absolute, relative, both
) -> List[Dict[str, Any]]:
    """실시간 급등/역행 코인 스캔 (SNIPER용) - reserved_selector 사용.

    [2026-02-01] HTTP 호출 대신 reserved_selector의 SNIPER 후보 사용.
    """
    try:
        from app.manager.reserved_selector import build_reserved_candidates

        if _cached_system is None:
            logger.warning("[Autopilot] _cached_system is None, cannot fetch surge coins")
            return []

        # SNIPER 후보 가져오기
        items, summary = build_reserved_candidates(_cached_system, sniper_n=top_n)

        # SNIPER 후보만 필터링하고 surge 형식으로 변환
        result = []
        for it in items:
            if it.get("recommended_strategy", "").upper() == "SNIPER":
                sniper_params = it.get("sniper_params", {})
                result.append({
                    "market": it.get("market"),
                    "surge_pct": it.get("change_24h", 0),
                    "relative_strength": it.get("relative_strength", 0),
                    "is_contrarian": it.get("rsi", 50) < 40,  # RSI 40 이하면 역행
                    "sniper_score": it.get("score", 0),
                    "ai_score": it.get("ai_score", 0.5),
                    "rsi": it.get("rsi", 50),
                    "price": it.get("price", 0),
                    "volume_24h": it.get("vol24_usdt", 0),
                    "reason": "sniper_candidate",
                })

        if result:
            logger.info(f"[Autopilot/SNIPER] Found {len(result)} surge/contrarian coins (direct call)")

        return result

    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("[Autopilot] Failed to fetch surge coins: %s", e)
    return []


class ScannerMixin:
    """Step 1: 시장 스캐닝 + 예비 큐 관리 Mixin.

    Expects: self.system, self._last_refresh_scan_ts
    """

    def _infer_strategy_from_market(self, market: str) -> str:
        """컨텍스트에서 마켓의 전략 추론."""
        mkt = str(market or "").strip().upper()
        if not mkt:
            return ""
        try:
            ctx = self.system.coordinator.contexts.get(mkt)
            ctrls = getattr(ctx, "controls", {}) or {}
            sc = ctrls.get("strategy") or {}
            if isinstance(sc, dict) and bool(sc.get("enabled")):
                mode = _normalize_strategy_name(sc.get("mode"))
                if mode:
                    return mode
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[AP_SCANNER] infer strategy from controls: %s", exc, exc_info=True)
        try:
            ctx = self.system.coordinator.contexts.get(mkt)
            sel = _normalize_strategy_name(getattr(ctx, "selected_strategy", ""))
            if sel:
                return sel
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[AP_SCANNER] infer strategy from selected_strategy: %s", exc, exc_info=True)
        return ""

    async def _step_scan(
        self,
        *,
        target_by_strategy: Dict[str, int],
        scan_only: bool,
        reason: str,
        round_strategies: Optional[List[str]],
    ) -> Tuple[Dict[str, Any], Dict[str, int], Set[str]]:
        """Step 1: 스캔 실행.

        Returns:
            (scan_summary, desired_by_strategy, longhold_markets)
        """
        manual_scan = bool(scan_only) or (str(reason).lower() in ("manual", "api", "debug"))
        should_scan = manual_scan
        shortage_probe: Dict[str, Any] = {}

        # [2026-03-14] LongHold 전환된 마켓은 슬롯 카운트에서 제외
        from app.strategy.strategy_plugins import _longhold_write_lock
        _longhold_markets: Set[str] = set()
        try:
            _lh_path = os.path.join("runtime", "longhold_config.json")
            if os.path.exists(_lh_path):
                with _longhold_write_lock:
                    with open(_lh_path, "r", encoding="utf-8") as _lhf:
                        _lh_store = json.load(_lhf)
                for _lh_mkt, _lh_cfg in (_lh_store.get("markets") or {}).items():
                    if isinstance(_lh_cfg, dict) and _lh_cfg.get("enabled", True):
                        _longhold_markets.add(str(_lh_mkt).strip().upper())
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[AP_SCANNER] longhold config read: %s", exc, exc_info=True)

        desired_by_strategy: Dict[str, int] = dict(target_by_strategy)

        if not should_scan:
            active_counts: Dict[str, int] = {k: 0 for k in target_by_strategy.keys()}
            try:
                pre_snap = self.system.oma_registry.snapshot()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.warning("[Autopilot] OMA 스냅샷 조회 실패 → active_counts 전체 0으로 초기화 위험", exc_info=True)
                pre_snap = {}

            longhold_counts_by_strategy: Dict[str, int] = {}
            for row in (pre_snap.get("active") or []):
                market = ""
                strategy = ""
                if isinstance(row, dict):
                    market = str(row.get("market") or "").strip().upper()
                    strategy = _normalize_strategy_name(row.get("strategy"))
                    if not strategy:
                        rs = row.get("reason")
                        if isinstance(rs, list):
                            for r in rs:
                                if isinstance(r, str) and r.upper().startswith("STRATEGY:"):
                                    strategy = _normalize_strategy_name(r.split(":", 1)[1].strip())
                                    break
                elif isinstance(row, str):
                    market = str(row).strip().upper()
                if not strategy:
                    strategy = self._infer_strategy_from_market(market)
                # [2026-03-23] LongHold 코인은 ACTIVE 슬롯에서 빼되 전략별 LH 쿼터로 집계
                if market in _longhold_markets:
                    if strategy:
                        longhold_counts_by_strategy[strategy] = longhold_counts_by_strategy.get(strategy, 0) + 1
                    continue
                if strategy in active_counts:
                    active_counts[strategy] = int(active_counts.get(strategy, 0) + 1)

            queue_counts: Dict[str, int] = {k: 0 for k in target_by_strategy.keys()}
            try:
                queue_items = (reserved_queue.snapshot() or {}).get("items") or []
                for it in queue_items:
                    st = _normalize_strategy_name((it or {}).get("strategy") or (it or {}).get("recommended_strategy"))
                    if st in queue_counts:
                        queue_counts[st] = int(queue_counts.get(st, 0) + 1)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[Autopilot] 예비 큐 집계 실패 → queue_shortage 미감지 가능", exc_info=True)

            _bench_n = max(0, int(os.getenv("OMA_AUTOPILOT_BENCH_N", "2")))

            need_by_strategy: Dict[str, int] = {}
            desired_by_strategy = {}  # need + bench
            queue_shortage_by_strategy: Dict[str, int] = {}
            for st, target_n in target_by_strategy.items():
                lh_n = int(longhold_counts_by_strategy.get(st, 0))
                need_n = max(0, int(target_n) - int(active_counts.get(st, 0)) - lh_n)
                need_by_strategy[st] = int(need_n)
                desired_n = max(need_n, _bench_n) if int(target_n) > 0 else need_n
                desired_by_strategy[st] = int(desired_n)
                queue_shortage_by_strategy[st] = max(0, int(desired_n) - int(queue_counts.get(st, 0)))

            should_scan = any(v > 0 for v in queue_shortage_by_strategy.values())

            if round_strategies and not manual_scan:
                round_set = {s.upper() for s in round_strategies}
                should_scan = any(v > 0 for k, v in queue_shortage_by_strategy.items() if k in round_set)

            if not should_scan:
                _refresh_interval = float(os.getenv("OMA_AUTOPILOT_REFRESH_SEC", "900"))
                _last_refresh = float(getattr(self, "_last_refresh_scan_ts", 0.0) or 0.0)
                if (time.time() - _last_refresh) >= _refresh_interval:
                    should_scan = True
                    logger.info("[Autopilot] periodic refresh scan (no shortage)")

            shortage_probe = {
                "targets": target_by_strategy,
                "active_counts": active_counts,
                "queue_counts": queue_counts,
                "need_by_strategy": need_by_strategy,
                "desired_by_strategy": desired_by_strategy,
                "bench_n": _bench_n,
                "queue_shortage_by_strategy": queue_shortage_by_strategy,
            }

        scan_summary: Dict[str, Any] = {}

        if should_scan:
            try:
                scan_t0 = time.time()

                pp_target = target_by_strategy.get("PINGPONG", 0)
                al_target = target_by_strategy.get("AUTOLOOP", 0)
                ld_target = target_by_strategy.get("LADDER", 0)
                lt_target = target_by_strategy.get("LIGHTNING", 0)
                gz_target = target_by_strategy.get("GAZUA", 0)
                ct_target = target_by_strategy.get("CONTRARIAN", 0)
                sn_target = target_by_strategy.get("SNIPER", 0)

                _desired = desired_by_strategy if not manual_scan else target_by_strategy
                if round_strategies and not manual_scan:
                    round_set = {s.upper() for s in round_strategies}
                    _pp = _desired.get("PINGPONG", 0) if "PINGPONG" in round_set else 0
                    _al = _desired.get("AUTOLOOP", 0) if "AUTOLOOP" in round_set else 0
                    _ld = _desired.get("LADDER", 0) if "LADDER" in round_set else 0
                    _lt = _desired.get("LIGHTNING", 0) if "LIGHTNING" in round_set else 0
                    _gz = _desired.get("GAZUA", 0) if "GAZUA" in round_set else 0
                    _ct = _desired.get("CONTRARIAN", 0) if "CONTRARIAN" in round_set else 0
                    _sn = _desired.get("SNIPER", 0) if "SNIPER" in round_set else 0
                    logger.info("[Autopilot/RoundRobin] Scanning: %s", round_strategies)
                else:
                    _pp = _desired.get("PINGPONG", pp_target)
                    _al = _desired.get("AUTOLOOP", al_target)
                    _ld = _desired.get("LADDER", ld_target)
                    _lt = _desired.get("LIGHTNING", lt_target)
                    _gz = _desired.get("GAZUA", gz_target)
                    _ct = _desired.get("CONTRARIAN", ct_target)
                    _sn = _desired.get("SNIPER", sn_target)

                _gate = getattr(self.system, '_scan_gate', None)
                async with (_gate if _gate is not None else asyncio.Lock()):
                    items, summary = await asyncio.to_thread(
                        functools.partial(build_reserved_candidates, self.system, pingpong_n=_pp, autoloop_n=_al, ladder_n=_ld, lightning_n=_lt, gazua_n=_gz, contrarian_n=_ct, sniper_n=_sn)
                    )
                summary = dict(summary or {})
                summary["elapsed_sec"] = round(time.time() - scan_t0, 3)
                summary["trigger"] = "manual" if manual_scan else "slot_shortage"

                # [2026-03-30] to_thread + timeout 30초 — 이벤트 루프 블로킹 + 스레드 고갈 방지
                try:
                    items = await asyncio.wait_for(
                        asyncio.to_thread(enrich_pp_al_rank_scores, items),
                        timeout=30.0)
                except asyncio.TimeoutError:
                    logger.warning("[Autopilot] enrich_pp_al_rank_scores timeout 30s — using raw scores")
                except Exception as _enrich_err:
                    logger.warning("[Autopilot] rank_score enrichment failed: %s", _enrich_err)

                reserved_queue.merge_round(items, round_strategies, summary=summary) if round_strategies else reserved_queue.replace(items, summary=summary)
                scan_summary = summary
                self._last_refresh_scan_ts = time.time()

                # ── WHALE 예비후보 스캔 (Step 1) ──────────────────────────────
                _wh_desired_n = int(_desired.get("WHALE", 0) or 0)
                if _wh_desired_n > 0:
                    try:
                        _wt0 = time.time()
                        from app.strategy.strategy_plugins import get_plugin as _get_wpl_fn
                        _wpl = _get_wpl_fn("WHALE")
                        _wh_all_markets = [
                            str(k).strip().upper()
                            for k in self.system.coordinator.contexts.keys()
                            if str(k).strip().upper().endswith("USDT")
                        ]
                        _wh_exclude_now: set = set()
                        try:
                            for _wh_row in (self.system.oma_registry.snapshot().get("active") or []):
                                if isinstance(_wh_row, dict):
                                    _wh_exclude_now.add(str(_wh_row.get("market") or "").strip().upper())
                                elif isinstance(_wh_row, str):
                                    _wh_exclude_now.add(str(_wh_row).strip().upper())
                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[AP_SCANNER] WHALE exclude list: %s", exc, exc_info=True)
                        _wh_scan_results = await asyncio.to_thread(
                            _wpl.scan_markets, _wh_all_markets, {}, _wh_exclude_now
                        )
                        _ts_now = time.time()
                        _wh_items = [
                            {
                                "market": _h["market"],
                                "strategy": "WHALE",
                                "recommended_strategy": "WHALE",
                                "ts": _ts_now,
                                "confidence": min(90.0, 65.0 + float(_h.get("score", 1.0)) * 10.0),
                                "score": _h.get("score", 1.0),
                                "whale_reason": _h.get("reason", ""),
                            }
                            for _h in _wh_scan_results
                        ]
                        if _wh_items:
                            reserved_queue.merge_round(_wh_items, None)
                            logger.info(
                                f"[WHALE/Step1] 🐋 예비후보 {len(_wh_items)}개 "
                                f"(elapsed={round(time.time()-_wt0,1)}s): "
                                f"{[x['market'] for x in _wh_items]}"
                            )
                        else:
                            logger.debug(f"[WHALE/Step1] 예비후보 없음 (elapsed={round(time.time()-_wt0,1)}s)")
                        summary["picked_whale"] = len(_wh_items)
                    except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as _wh_exc:
                        logger.warning("[WHALE/Step1] 스캔 오류: %s", _wh_exc, exc_info=True)
                # ─────────────────────────────────────────────────────────────

                try:
                    reserved_queue.add_history({
                        "kind": "SCAN",
                        "source": "autopilot",
                        "picked_pingpong": int(summary.get("picked_pingpong") or 0),
                        "picked_autoloop": int(summary.get("picked_autoloop") or 0),
                        "picked_ladder": int(summary.get("picked_ladder") or 0),
                        "picked_lightning": int(summary.get("picked_lightning") or 0),
                        "picked_gazua": int(summary.get("picked_gazua") or 0),
                        "picked_contrarian": int(summary.get("picked_contrarian") or 0),
                        "picked_sniper": int(summary.get("picked_sniper") or 0),
                        "picked_whale": int(summary.get("picked_whale") or 0),
                        "elapsed_sec": summary.get("elapsed_sec"),
                    })
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[AP_SCANNER] scan summary extraction failed", exc_info=True)
            except Exception as exc:
                logger.warning("[AutopilotScanner] scan failed: %s", exc, exc_info=True)
                scan_summary = {"ok": False, "error": str(exc)}
        else:
            scan_summary = {
                "skipped": True,
                "trigger": "no_slot_shortage",
                "probe": shortage_probe,
            }

        return scan_summary, desired_by_strategy, _longhold_markets
