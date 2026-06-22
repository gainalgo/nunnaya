# ============================================================
# File: app/manager/sniper_position_store.py
# SNIPER 포지션 영속화 관리자
# ------------------------------------------------------------
# 서버 재시작 시에도 활성 SNIPER 포지션을 유지합니다.
# [2026-01-31] 다중 SNIPER 지원: 동일 마켓에 여러 SNIPER 인스턴스 허용
# ============================================================

import json
import os
import time
import threading
import uuid
from typing import Dict, Any, Optional, List
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

SNIPER_POSITIONS_PATH = "runtime/sniper_positions.json"


def generate_sniper_id(market: str) -> str:
    """고유 SNIPER ID 생성.
    
    형식: {market}_sniper_{8자리 uuid}
    예: BTCUSDT_sniper_a1b2c3d4
    """
    return f"{market}_sniper_{uuid.uuid4().hex[:8]}"


def extract_market_from_id(sniper_id: str) -> Optional[str]:
    """SNIPER ID에서 마켓 코드 추출.
    
    예: BTCUSDT_sniper_a1b2c3d4 -> BTCUSDT
    """
    if "_sniper_" in sniper_id:
        return sniper_id.split("_sniper_")[0]
    return sniper_id  # Legacy: sniper_id가 market 자체인 경우


class SniperPositionStore:
    """SNIPER 포지션 저장소 (다중 포지션 지원)."""

    def __init__(self):
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        """파일에서 포지션 로드."""
        try:
            if os.path.exists(SNIPER_POSITIONS_PATH):
                with open(SNIPER_POSITIONS_PATH, "r", encoding="utf-8") as f:
                    self._positions = json.load(f)
                logger.info(f"[SNIPER] Loaded {len(self._positions)} positions from {SNIPER_POSITIONS_PATH}")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[SNIPER] Failed to load positions: %s", e)
            self._positions = {}

    def _save(self):
        """포지션을 파일에 저장 (원자적 쓰기 - 크래시 시 깨진 JSON 방지)."""
        from app.core.io_utils import safe_write_json
        try:
            safe_write_json(SNIPER_POSITIONS_PATH, self._positions)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[SNIPER] Failed to save positions: %s", e)

    def save_position(self, sniper_id: str, data: Dict[str, Any]):
        """포지션 저장 (ID 기반).
        
        Args:
            sniper_id: 고유 SNIPER ID (예: BTCUSDT_sniper_a1b2c3d4) 또는 market (legacy)
            data: 포지션 데이터
        """
        with self._lock:
            # 마켓 정보 자동 추가
            market = extract_market_from_id(sniper_id)
            # [FIX] 동일 마켓 레거시 키(market 자체) 중복 방지
            # 새 형식(market_sniper_xxx) 저장 시 레거시 키가 남아있으면 제거
            if "_sniper_" in sniper_id and market in self._positions and market != sniper_id:
                del self._positions[market]
                logger.info("[SNIPER] Cleaned legacy key %s (replaced by %s)", market, sniper_id)
            self._positions[sniper_id] = {
                **data,
                "market": market,
                "sniper_id": sniper_id,
                "ts": time.time(),
            }
            self._save()
            logger.info("[SNIPER] Saved position %s", sniper_id)

    def get_position(self, sniper_id: str) -> Optional[Dict[str, Any]]:
        """포지션 조회 (ID 기반)."""
        with self._lock:
            return self._positions.get(sniper_id)

    def get_positions_by_market(self, market: str) -> List[Dict[str, Any]]:
        """특정 마켓의 모든 SNIPER 포지션 조회.
        
        Args:
            market: 마켓 코드 (예: BTCUSDT)
            
        Returns:
            해당 마켓의 모든 SNIPER 포지션 리스트
        """
        with self._lock:
            result = []
            for sniper_id, data in self._positions.items():
                # 새 형식: sniper_id에서 market 추출
                if sniper_id.startswith(f"{market}_sniper_"):
                    result.append({"sniper_id": sniper_id, **data})
                # Legacy 형식: sniper_id가 market 자체인 경우
                elif sniper_id == market:
                    result.append({"sniper_id": sniper_id, **data})
            return result

    def remove_position(self, sniper_id: str):
        """포지션 제거 (ID 기반)."""
        with self._lock:
            if sniper_id in self._positions:
                del self._positions[sniper_id]
                self._save()
                logger.info("[SNIPER] Removed position %s", sniper_id)
                return True
            return False

    def remove_positions_by_market(self, market: str) -> int:
        """특정 마켓의 모든 SNIPER 포지션 제거.
        
        Returns:
            제거된 포지션 개수
        """
        with self._lock:
            to_remove = []
            for sniper_id in self._positions.keys():
                if sniper_id.startswith(f"{market}_sniper_") or sniper_id == market:
                    to_remove.append(sniper_id)
            
            for sniper_id in to_remove:
                del self._positions[sniper_id]
            
            if to_remove:
                self._save()
                logger.info(f"[SNIPER] Removed {len(to_remove)} positions for {market}")
            
            return len(to_remove)

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """모든 포지션 조회."""
        with self._lock:
            return dict(self._positions)

    def get_all_as_list(self) -> List[Dict[str, Any]]:
        """모든 포지션을 리스트로 조회 (ID 포함)."""
        with self._lock:
            return [
                {"sniper_id": k, **v}
                for k, v in self._positions.items()
            ]

    def restore_to_system(self, system) -> int:
        """시스템에 포지션 복구. 복구된 개수 반환.
        
        다중 SNIPER 지원: 동일 마켓에 여러 포지션이 있을 수 있으므로
        마켓 단위로 그룹화하여 복구합니다.
        SNIPERS(precision_scope) 슬롯은 target 수를 초과하지 않도록 제한합니다.
        """
        restored = 0
        restored_markets = set()
        scope_target = max(0, int(getattr(system, "autopilot_scope_target_n",
                                          getattr(system, "reserved_sniper_n", 0)) or 0))
        scope_restored_count = 0
        
        with self._lock:
            # [FIX] 레거시/신규 중복 키 사전 정리 — 같은 마켓이 2슬롯 차지 방지
            _legacy_to_remove = []
            for sid in list(self._positions.keys()):
                market_of_sid = (self._positions[sid].get("market")
                                 or extract_market_from_id(sid))
                if "_sniper_" not in sid:  # legacy 키 (마켓명 자체)
                    new_key_exists = any(
                        k != sid and "_sniper_" in k
                        and (self._positions[k].get("market") or extract_market_from_id(k)) == market_of_sid
                        for k in self._positions
                    )
                    if new_key_exists:
                        _legacy_to_remove.append(sid)
            for sid in _legacy_to_remove:
                del self._positions[sid]
                logger.info("[SNIPER] Removed duplicate legacy key %s during restore", sid)
            if _legacy_to_remove:
                self._save()

            for sniper_id, data in self._positions.items():
                is_scope = False  # [FIX #3] try 밖에서 초기화 — 예외 시 NameError 방지
                try:
                    # 마켓 추출 (새 형식 또는 legacy)
                    market = data.get("market") or extract_market_from_id(sniper_id)

                    # SNIPERS(scope) 슬롯 target 수 제한: 보유 포지션은 항상 복구
                    # [FIX #1] profile=="SNIPERS" OR source=="precision_scope" 어느 한쪽이면 scope
                    # strategy_recommender 경로는 source 없이 profile만 설정하므로 OR 필요
                    params = data.get("params", {}) or {}
                    is_scope = (str(params.get("profile") or "").strip().upper() == "SNIPERS"
                                or str(params.get("source") or "").strip().lower() == "precision_scope")
                    if is_scope and scope_target > 0:
                        has_qty = False
                        try:
                            _ctx = system.coordinator.contexts.get(market)
                            if _ctx:
                                _pos = getattr(_ctx, "position", None) or {}
                                has_qty = float(_pos.get("qty", 0) or 0) > 0
                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[SNIPER_STORE] strategy_recommender 경로 OR 판정: %s", exc, exc_info=True)
                        if not has_qty and scope_restored_count >= scope_target:
                            logger.info("[SNIPER] Skip restore %s — scope target %d reached", market, scope_target)
                            continue

                    ctx = system.coordinator.get_context(market)
                    if not ctx:
                        ctx = system.coordinator.ensure_market(market)

                    # 전략 모드 복구 (마켓 당 한 번만)
                    # LADDER 전략이 이미 설정된 마켓은 SNIPER로 덮어쓰지 않음
                    if market not in restored_markets:
                        existing_mode = ""
                        try:
                            ctrls = getattr(ctx, "controls", None) or {}
                            sc = ctrls.get("strategy") or {}
                            if isinstance(sc, dict) and bool(sc.get("enabled")):
                                existing_mode = str(sc.get("mode") or "").upper()
                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[SNIPER_STORE] LADDER 전략 확인 중 오류: %s", exc, exc_info=True)
                        if not existing_mode:
                            try:
                                sm = str(getattr(ctx, "strategy_mode", "") or "").strip().upper()
                                if sm:
                                    existing_mode = sm
                            except (KeyError, AttributeError, TypeError) as exc:
                                logger.warning("[SNIPER_STORE] strategy_mode 조회 오류: %s", exc, exc_info=True)
                        if existing_mode and existing_mode != "SNIPER":
                            logger.info("[SNIPER] Skip restore %s — existing strategy %s", market, existing_mode)
                            restored_markets.add(market)
                            continue
                        if not existing_mode:
                            try:
                                lm = getattr(system, "ladder_manager", None)
                                if lm:
                                    lcfg = lm.get_config(market)
                                    if lcfg.get("enabled"):
                                        existing_mode = "LADDER"
                            except (KeyError, AttributeError, TypeError) as exc:
                                logger.warning("[SNIPER_STORE] ladder_manager 조회 오류: %s", exc, exc_info=True)
                        if existing_mode == "LADDER":
                            logger.info("[SNIPER] Skip restore %s — already LADDER", market)
                            restored_markets.add(market)
                            continue

                        ctx.update_controls({
                            "strategy": {
                                "enabled": True,
                                "mode": "SNIPER",
                                "params": data.get("params", {}),
                            }
                        })
                        ctx.strategy_mode = "SNIPER"

                        # 상태 복구
                        from app.manager.oma_market_registry import MarketState
                        system.oma_set_market(
                            market=market,
                            state=MarketState.ACTIVE,
                            reason=["sniper_restore"],
                        )
                        restored_markets.add(market)

                    # 예산 복구 (idempotent): 누적 합산 금지
                    if data.get("budget_usdt") and hasattr(system, 'oma_registry'):
                        stored_budget = float(data.get("budget_usdt") or 0.0)
                        current_budget = float(system.oma_registry.get_budget_usdt(market) or 0.0)
                        # 현재 상태 유지하면서 예산만 업데이트
                        current_state = system.oma_registry.get_state(market) or MarketState.ACTIVE
                        if current_budget <= 0.0:
                            system.oma_registry.set_state(
                                market,
                                current_state,
                                reason=["sniper_budget_restore"],
                                budget_usdt=stored_budget,
                            )
                        elif stored_budget > 0.0 and current_budget > (stored_budget * 1.5):
                            # 과거 합산 복구 버그로 부풀려진 예산을 정상화
                            system.oma_registry.set_state(
                                market,
                                current_state,
                                reason=["sniper_budget_restore_normalize"],
                                budget_usdt=stored_budget,
                            )

                    restored += 1
                    if is_scope:
                        scope_restored_count += 1
                    logger.info(f"[SNIPER] Restored {sniper_id} ({market}) with budget={data.get('budget_usdt')}")
                except Exception as e:
                    logger.warning("[SNIPER] Failed to restore %s: %s", sniper_id, e)
        return restored


# 싱글톤 인스턴스
sniper_store = SniperPositionStore()
