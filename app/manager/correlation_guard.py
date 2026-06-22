# ============================================================
# File: app/manager/correlation_guard.py
# Autocoin OS v3-H — Correlation Guard (상관관계 가드)
# ------------------------------------------------------------
# 형(兄) 서버가 동생(弟) 서버에게 주는 선물:
# 상관관계 높은 코인 동시 진입 시 conviction 페널티 부여.
#
# 두 계층:
#   Layer 1: 정적 섹터 그룹 (암호화폐 도메인 지식)
#   Layer 2: 동적 결과 상관관계 (저널 EXIT 승/패 동기화율)
#
# 핵심 철학:
#   - 진입을 "차단"하지 않는다 — conviction 감점으로 부드럽게 억제
#   - BTC+ETH+SOL 전부 LONG = 사실상 같은 베팅 3번
#   - GOLD(XAUTUSDT)는 크립토와 독립 → 항상 패널티 0
#   - 반대 방향 = 헤지 → 패널티 상쇄
# ============================================================
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.io_utils import safe_load_json, safe_write_json

logger = logging.getLogger(__name__)

# ── 경로 ────────────────────────────────────────────────────
JOURNAL_PATH = os.path.join("runtime", "focus_harpoon_journal.jsonl")
CACHE_PATH = os.path.join("runtime", "correlation_cache.json")

# ── 정적 상관관계 그룹 (Layer 1) ────────────────────────────
CORRELATION_GROUPS: Dict[str, Dict[str, Any]] = {
    "BTC_MAJORS": {
        "coins": ["BTCUSDT"],
        "label": "비트코인",
        "correlation": 1.0,
    },
    "ETH_ECOSYSTEM": {
        "coins": ["ETHUSDT"],
        "label": "이더리움 생태계",
        "correlation": 0.85,
    },
    "LARGE_L1": {
        "coins": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "label": "대형 L1",
        "correlation": 0.75,
    },
    "DEFI_BLUE": {
        "coins": ["LINKUSDT", "AAVEUSDT", "UNIUSDT"],
        "label": "DeFi 블루칩",
        "correlation": 0.70,
    },
    "ALT_HIGH_BETA": {
        "coins": ["HYPEUSDT", "RAVEUSDT", "ZECUSDT", "TAOUSDT"],
        "label": "고변동 알트",
        "correlation": 0.50,
    },
    "GOLD": {
        "coins": ["XAUTUSDT"],
        "label": "금 (독립)",
        "correlation": 0.0,  # 크립토와 비상관
    },
}

# ── 미등록 코인 기본 상관관계 ────────────────────────────────
_OTHER_GROUP_CORR = 0.30

# ── 동적 상관관계 임계값 ────────────────────────────────────
_DYNAMIC_SYNC_THRESHOLD = 0.80   # 80% 이상 동기화 시 상관 높음
_DYNAMIC_MIN_OVERLAPS = 3        # 최소 3건 겹침이 있어야 판정 가능
_DYNAMIC_LOOKBACK_DAYS = 7       # 최근 7일
_DYNAMIC_REFRESH_SEC = 3600      # 1시간마다 갱신

# ── 페널티 한도 ─────────────────────────────────────────────
# [2026-05-17 100점 ×10] _MAX_PENALTY 옛 -3 → -30
_MAX_PENALTY = -30
_LARGE_L1_EXTRA_THRESHOLD = 3    # LARGE_L1에 3개 이상 같은 방향 → 추가 -20 (×10)


def _safe_float(val: Any, default: float = 0.0) -> float:
    """저널 필드 누락/타입 오류 방어."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class CorrelationGuard:
    """상관관계 기반 conviction 페널티 가드.

    사용:
        from app.manager.correlation_guard import correlation_guard
        result = correlation_guard.check_entry("ETHUSDT", "LONG", positions)
        # result = {"allowed": True, "penalty": -1, "warnings": [...], "overlap_groups": [...]}
    """

    def __init__(self, journal_path: str = JOURNAL_PATH):
        self._journal_path = journal_path
        self._lock = threading.Lock()

        # Layer 1: 정적 그룹 — 코인→그룹 역인덱스
        self._coin_to_groups: Dict[str, List[str]] = defaultdict(list)
        for group_name, info in CORRELATION_GROUPS.items():
            for coin in info["coins"]:
                self._coin_to_groups[coin.upper()].append(group_name)

        # Layer 2: 동적 상관관계 캐시
        self._dynamic_pairs: Dict[str, float] = {}  # "COINX|COINY" → sync_rate
        self._dynamic_ts: float = 0.0

        # 부팅 시 캐시 로드
        self._load_cache()

    # ================================================================
    # 캐시 I/O
    # ================================================================

    def _load_cache(self) -> None:
        """디스크에서 동적 상관관계 캐시 로드."""
        data = safe_load_json(CACHE_PATH, default={})
        self._dynamic_pairs = data.get("pairs", {})
        self._dynamic_ts = _safe_float(data.get("ts", 0))
        if self._dynamic_pairs:
            logger.info(
                "[CORR_GUARD] 캐시 로드: %d 페어, ts=%s",
                len(self._dynamic_pairs),
                time.strftime("%H:%M:%S", time.localtime(self._dynamic_ts)),
            )

    def _save_cache(self) -> None:
        """동적 상관관계 캐시를 디스크에 저장."""
        data = {
            "pairs": self._dynamic_pairs,
            "ts": self._dynamic_ts,
            "readable_ts": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self._dynamic_ts)
            ),
        }
        try:
            safe_write_json(CACHE_PATH, data)
        except Exception as exc:
            logger.warning("[CORR_GUARD] 캐시 저장 실패: %s", exc)

    # ================================================================
    # Layer 2: 동적 결과 상관관계 (저널 기반)
    # ================================================================

    def _read_journal_exits(self, lookback_days: int = _DYNAMIC_LOOKBACK_DAYS) -> List[Dict]:
        """최근 N일 EXIT 레코드 추출."""
        cutoff = time.time() - lookback_days * 86400
        exits: List[Dict] = []

        if not os.path.exists(self._journal_path):
            return exits

        try:
            with open(self._journal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("event") != "EXIT":
                        continue
                    if _safe_float(rec.get("ts")) < cutoff:
                        continue
                    exits.append(rec)
        except OSError as exc:
            logger.warning("[CORR_GUARD] 저널 읽기 실패: %s", exc)

        return exits

    def _read_journal_entries(self, lookback_days: int = _DYNAMIC_LOOKBACK_DAYS) -> List[Dict]:
        """최근 N일 ENTRY 레코드 추출 (겹침 시간 계산용)."""
        cutoff = time.time() - lookback_days * 86400
        entries: List[Dict] = []

        if not os.path.exists(self._journal_path):
            return entries

        try:
            with open(self._journal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("event") != "ENTRY":
                        continue
                    if _safe_float(rec.get("ts")) < cutoff:
                        continue
                    entries.append(rec)
        except OSError as exc:
            logger.warning("[CORR_GUARD] 저널 ENTRY 읽기 실패: %s", exc)

        return entries

    def refresh_dynamic(self) -> None:
        """동적 상관관계 재계산 (저널 EXIT 데이터 기반).

        겹침 판정:
        - 같은 코인의 ENTRY/EXIT 쌍으로 보유 구간 생성
        - 두 코인의 보유 구간이 겹치면 overlap
        - 겹친 건들 중 둘 다 승리 또는 둘 다 패배 = 동기화
        - sync_rate = 동기화 건수 / 전체 겹침 건수
        """
        with self._lock:
            now = time.time()
            exits = self._read_journal_exits()
            entries = self._read_journal_entries()

            if not exits:
                self._dynamic_pairs = {}
                self._dynamic_ts = now
                self._save_cache()
                return

            # ENTRY/EXIT 매칭 → 보유 구간 생성
            # {market → [(entry_ts, exit_ts, won: bool), ...]}
            holdings = self._build_holdings(entries, exits)

            # 코인 쌍별 겹침 분석
            coins = sorted(holdings.keys())
            new_pairs: Dict[str, float] = {}

            for i, coin_a in enumerate(coins):
                for coin_b in coins[i + 1:]:
                    sync_count, total_overlaps = self._calc_overlap_sync(
                        holdings[coin_a], holdings[coin_b]
                    )
                    if total_overlaps < _DYNAMIC_MIN_OVERLAPS:
                        continue  # 데이터 부족 — 판정 불가
                    sync_rate = sync_count / total_overlaps
                    pair_key = self._pair_key(coin_a, coin_b)
                    new_pairs[pair_key] = round(sync_rate, 4)

            self._dynamic_pairs = new_pairs
            self._dynamic_ts = now
            self._save_cache()

            high_corr = {k: v for k, v in new_pairs.items() if v >= _DYNAMIC_SYNC_THRESHOLD}
            if high_corr:
                logger.info(
                    "[CORR_GUARD] 동적 상관관계 갱신: %d 페어 분석, 고상관 %d건 %s",
                    len(new_pairs), len(high_corr), high_corr,
                )

    def _build_holdings(
        self,
        entries: List[Dict],
        exits: List[Dict],
    ) -> Dict[str, List[Tuple[float, float, bool]]]:
        """ENTRY/EXIT 매칭으로 보유 구간 생성.

        Returns:
            {market → [(entry_ts, exit_ts, won), ...]}
        """
        # exit 역참조: (market, direction) → [exit_records]
        exit_map: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
        for ex in exits:
            market = ex.get("market", "").upper()
            direction = ex.get("direction", "").upper()
            if market and direction:
                exit_map[(market, direction)].append(ex)

        # 각 exit 목록을 ts 기준 오름차순 정렬
        for key in exit_map:
            exit_map[key].sort(key=lambda r: _safe_float(r.get("ts")))

        # entry와 exit 매칭 (FIFO 방식)
        used_exits: Set[int] = set()  # exit record의 id(메모리 주소)로 중복 방지
        holdings: Dict[str, List[Tuple[float, float, bool]]] = defaultdict(list)

        for entry in sorted(entries, key=lambda r: _safe_float(r.get("ts"))):
            market = entry.get("market", "").upper()
            direction = entry.get("direction", "").upper()
            entry_ts = _safe_float(entry.get("ts"))
            if not market or not direction or entry_ts <= 0:
                continue

            key = (market, direction)
            for ex in exit_map.get(key, []):
                ex_id = id(ex)
                if ex_id in used_exits:
                    continue
                exit_ts = _safe_float(ex.get("ts"))
                if exit_ts < entry_ts:
                    continue  # 이 exit는 이전 entry의 것
                pnl = _safe_float(ex.get("pnl_net"))
                won = pnl > 0
                holdings[market].append((entry_ts, exit_ts, won))
                used_exits.add(ex_id)
                break  # FIFO: 첫 매칭 exit 사용

        return dict(holdings)

    @staticmethod
    def _calc_overlap_sync(
        spans_a: List[Tuple[float, float, bool]],
        spans_b: List[Tuple[float, float, bool]],
    ) -> Tuple[int, int]:
        """두 코인의 보유 구간 겹침 분석.

        Returns:
            (sync_count, total_overlaps)
            sync = 둘 다 승리 또는 둘 다 패배
        """
        total = 0
        sync = 0

        for (a_start, a_end, a_won) in spans_a:
            for (b_start, b_end, b_won) in spans_b:
                # 구간 겹침 체크: max(start) < min(end)
                if max(a_start, b_start) < min(a_end, b_end):
                    total += 1
                    if a_won == b_won:
                        sync += 1

        return sync, total

    @staticmethod
    def _pair_key(coin_a: str, coin_b: str) -> str:
        """정렬된 페어 키 생성 (순서 무관하게 동일 키)."""
        a, b = sorted([coin_a.upper(), coin_b.upper()])
        return f"{a}|{b}"

    def _ensure_dynamic_fresh(self) -> None:
        """동적 데이터가 오래되었으면 갱신."""
        if time.time() - self._dynamic_ts > _DYNAMIC_REFRESH_SEC:
            self.refresh_dynamic()

    # ================================================================
    # 그룹 조회 헬퍼
    # ================================================================

    def _get_groups_for_coin(self, coin: str) -> List[str]:
        """코인이 속한 정적 그룹 목록 반환. 미등록 시 빈 리스트."""
        return self._coin_to_groups.get(coin.upper(), [])

    def _is_gold(self, coin: str) -> bool:
        """금(XAUTUSDT)인지 확인."""
        return coin.upper() == "XAUTUSDT"

    # ================================================================
    # 핵심 API: check_entry
    # ================================================================

    def check_entry(
        self,
        new_coin: str,
        direction: str,
        current_positions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """진입 전 상관관계 검사.

        Args:
            new_coin: 진입하려는 코인 (예: "ETHUSDT")
            direction: "LONG" 또는 "SHORT"
            current_positions: 현재 보유 포지션 리스트
                각 dict에 최소 'market'과 'direction' 필드 필요

        Returns:
            {
                "allowed": True,        # 항상 True (가드는 차단하지 않음)
                "penalty": -2,          # conviction 감점 (0 ~ -3)
                "warnings": ["..."],    # 경고 메시지 리스트
                "overlap_groups": ["LARGE_L1"],  # 겹치는 그룹명
            }
        """
        new_coin = new_coin.upper()
        direction = direction.upper()

        result: Dict[str, Any] = {
            "allowed": True,
            "penalty": 0,
            "warnings": [],
            "overlap_groups": [],
        }

        # 빈 포지션이면 패널티 없음
        if not current_positions:
            return result

        # 금은 항상 독립 — 패널티 0
        if self._is_gold(new_coin):
            return result

        # 동적 데이터 신선도 확인
        self._ensure_dynamic_fresh()

        # ── Layer 1: 정적 그룹 기반 검사 ──
        new_groups = self._get_groups_for_coin(new_coin)
        penalty = 0
        warnings: List[str] = []
        overlap_groups: Set[str] = set()

        # 그룹별 같은 방향 포지션 카운트
        group_same_dir: Dict[str, int] = defaultdict(int)
        group_opposite_dir: Dict[str, int] = defaultdict(int)

        for pos in current_positions:
            pos_market = pos.get("market", "").upper()
            pos_dir = pos.get("direction", "").upper()

            if not pos_market or not pos_dir:
                continue

            # 자기 자신은 건너뜀 (이미 보유 중인 같은 코인)
            if pos_market == new_coin:
                continue

            # 금 포지션은 상관관계에서 제외
            if self._is_gold(pos_market):
                continue

            # 기존 포지션의 그룹
            pos_groups = self._get_groups_for_coin(pos_market)

            # 신규 코인과 기존 코인의 교집합 그룹 찾기
            common_groups = set(new_groups) & set(pos_groups)

            for grp in common_groups:
                overlap_groups.add(grp)
                if pos_dir == direction:
                    group_same_dir[grp] += 1
                else:
                    group_opposite_dir[grp] += 1

            # 미등록 코인끼리는 OTHER 그룹으로 묶지 않음
            # (서로 다른 미지의 코인은 독립으로 취급)

        # ── 정적 그룹 페널티 계산 ──
        for grp in overlap_groups:
            same = group_same_dir.get(grp, 0)
            opposite = group_opposite_dir.get(grp, 0)

            if same == 0:
                continue  # 같은 방향 없음 → 페널티 없음

            # 반대 방향이 있으면 헤지로 상쇄
            net_same = max(0, same - opposite)

            if net_same <= 0:
                warnings.append(
                    f"{CORRELATION_GROUPS[grp]['label']}({grp}): "
                    f"헤지 상쇄됨 (same={same}, opposite={opposite})"
                )
                continue

            # [2026-05-17 100점 ×10] 같은 그룹, 같은 방향: -10 per overlap (옛 -1)
            grp_penalty = -10 * net_same
            warnings.append(
                f"{CORRELATION_GROUPS[grp]['label']}({grp}): "
                f"같은 방향 {net_same}건 → penalty {grp_penalty}"
            )
            penalty += grp_penalty

            # LARGE_L1 특별 규칙: 3개 이상 같은 방향 → 추가 -20 (×10)
            if grp == "LARGE_L1" and net_same >= _LARGE_L1_EXTRA_THRESHOLD:
                penalty += -20
                warnings.append(
                    f"LARGE_L1 과밀집 ({net_same}개 {direction}) → 추가 penalty -2"
                )

        # ── Layer 2: 동적 결과 상관관계 추가 검사 ──
        dynamic_penalty = self._calc_dynamic_penalty(new_coin, direction, current_positions)
        if dynamic_penalty < 0:
            penalty += dynamic_penalty
            warnings.append(f"동적 상관관계(저널 분석) → 추가 penalty {dynamic_penalty}")

        # ── 페널티 상한 적용 ──
        penalty = max(penalty, _MAX_PENALTY)

        result["penalty"] = penalty
        result["warnings"] = warnings
        result["overlap_groups"] = sorted(overlap_groups)

        if penalty < 0:
            logger.info(
                "[CORR_GUARD] %s %s → penalty=%d, groups=%s | %s",
                direction, new_coin, penalty,
                result["overlap_groups"],
                "; ".join(warnings),
            )

        return result

    def _calc_dynamic_penalty(
        self,
        new_coin: str,
        direction: str,
        current_positions: List[Dict[str, Any]],
    ) -> int:
        """동적 상관관계 기반 추가 페널티.

        같은 방향의 기존 포지션 중 저널 승/패 동기화율이
        80% 이상인 코인이 있으면 -1.
        """
        if not self._dynamic_pairs:
            return 0

        new_coin = new_coin.upper()
        penalty = 0

        for pos in current_positions:
            pos_market = pos.get("market", "").upper()
            pos_dir = pos.get("direction", "").upper()

            if not pos_market or pos_market == new_coin:
                continue
            if pos_dir != direction:
                continue  # 반대 방향은 관심 없음

            pair_key = self._pair_key(new_coin, pos_market)
            sync_rate = self._dynamic_pairs.get(pair_key, 0.0)

            if sync_rate >= _DYNAMIC_SYNC_THRESHOLD:
                penalty -= 10  # [2026-05-17 100점 ×10] 고동기화 페어당 -10 (옛 -1)

        return penalty

    # ================================================================
    # 노출도 맵
    # ================================================================

    def get_exposure_map(
        self,
        positions: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """현재 포지션의 그룹별 노출도 분석.

        Returns:
            {
                "LARGE_L1": {
                    "count": 3,
                    "direction": "LONG",
                    "coins": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                    "risk_level": "HIGH",
                },
                ...
            }
        """
        # 그룹별 코인+방향 수집
        group_data: Dict[str, Dict[str, List[str]]] = defaultdict(
            lambda: defaultdict(list)
        )

        for pos in positions:
            market = pos.get("market", "").upper()
            direction = pos.get("direction", "").upper()
            if not market or not direction:
                continue

            groups = self._get_groups_for_coin(market)
            if not groups:
                # 미등록 코인은 OTHER로
                groups = ["OTHER"]

            for grp in groups:
                group_data[grp][direction].append(market)

        # 결과 구성
        exposure: Dict[str, Dict[str, Any]] = {}

        for grp, dir_map in group_data.items():
            long_coins = dir_map.get("LONG", [])
            short_coins = dir_map.get("SHORT", [])
            total = len(long_coins) + len(short_coins)

            # 주된 방향 판별
            if len(long_coins) > len(short_coins):
                main_dir = "LONG"
                same_count = len(long_coins)
            elif len(short_coins) > len(long_coins):
                main_dir = "SHORT"
                same_count = len(short_coins)
            else:
                main_dir = "MIXED"
                same_count = max(len(long_coins), len(short_coins))

            # 리스크 레벨 판정
            if long_coins and short_coins:
                risk_level = "HEDGED"
            elif same_count >= 3:
                risk_level = "HIGH"
            elif same_count == 2:
                risk_level = "MEDIUM"
            else:
                risk_level = "LOW"

            all_coins = sorted(set(long_coins + short_coins))

            exposure[grp] = {
                "count": total,
                "direction": main_dir,
                "coins": all_coins,
                "long_count": len(long_coins),
                "short_count": len(short_coins),
                "risk_level": risk_level,
            }

        return exposure

    # ================================================================
    # 상관관계 매트릭스 (API 표시용)
    # ================================================================

    def get_correlation_matrix(self) -> Dict[str, Any]:
        """정적 + 동적 상관관계 통합 매트릭스 반환.

        API 대시보드 표시용.
        """
        self._ensure_dynamic_fresh()

        # 정적 그룹 간 상관관계
        static_groups = {}
        for grp_name, info in CORRELATION_GROUPS.items():
            static_groups[grp_name] = {
                "label": info["label"],
                "coins": info["coins"],
                "base_correlation": info["correlation"],
            }

        # 동적 페어 상관관계 (상위 20개만)
        dynamic_top = dict(
            sorted(
                self._dynamic_pairs.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:20]
        )

        return {
            "static_groups": static_groups,
            "dynamic_pairs": dynamic_top,
            "dynamic_pair_count": len(self._dynamic_pairs),
            "dynamic_high_corr_count": sum(
                1 for v in self._dynamic_pairs.values()
                if v >= _DYNAMIC_SYNC_THRESHOLD
            ),
            "last_refresh": self._dynamic_ts,
            "last_refresh_readable": time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(self._dynamic_ts),
            ) if self._dynamic_ts > 0 else "never",
        }

    # ================================================================
    # 상태 조회 (API)
    # ================================================================

    def get_status(self) -> Dict[str, Any]:
        """전체 가드 상태 반환."""
        return {
            "static_groups": {
                grp: {
                    "label": info["label"],
                    "coins": info["coins"],
                    "correlation": info["correlation"],
                }
                for grp, info in CORRELATION_GROUPS.items()
            },
            "dynamic": {
                "pair_count": len(self._dynamic_pairs),
                "high_corr_pairs": {
                    k: v for k, v in self._dynamic_pairs.items()
                    if v >= _DYNAMIC_SYNC_THRESHOLD
                },
                "last_refresh_ts": self._dynamic_ts,
                "last_refresh": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(self._dynamic_ts),
                ) if self._dynamic_ts > 0 else "never",
                "refresh_interval_sec": _DYNAMIC_REFRESH_SEC,
            },
            "config": {
                "max_penalty": _MAX_PENALTY,
                "large_l1_extra_threshold": _LARGE_L1_EXTRA_THRESHOLD,
                "dynamic_sync_threshold": _DYNAMIC_SYNC_THRESHOLD,
                "dynamic_min_overlaps": _DYNAMIC_MIN_OVERLAPS,
                "dynamic_lookback_days": _DYNAMIC_LOOKBACK_DAYS,
                "other_group_correlation": _OTHER_GROUP_CORR,
            },
        }


# ── Singleton ────────────────────────────────────────────────
correlation_guard = CorrelationGuard()
