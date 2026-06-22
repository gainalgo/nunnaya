# ============================================================
# File: app/manager/recovery_policy.py
# Autocoin OS v3-H — Recovery Policy Engine (Policy Table + Reactor)
# ------------------------------------------------------------
# 목적:
# - 원장(ledger) 이벤트(ORDER_FINAL / EXIT_UNRESOLVED / SLIPPAGE_HARD_BREACH)를
#   트리거로 삼아, 시장을 RECOVERY(회수 모드)로 승격하고
#   (선택적으로) 자동/조건부 청산을 수행한다.
# - 최소 변경 원칙: 기존 엔진/전략 로직을 뜯지 않고,
#   "원장 이벤트 → 정책 판단 → 실행"으로 안전 레일을 추가한다.
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple, List

logger = logging.getLogger(__name__)

class RecoveryPolicyMode(str, Enum):
    """RECOVERY 정책 모드."""

    MANUAL = "MANUAL"  # 수동: RECOVERY로만 올리고 자동 매도 없음
    CONDITIONAL = "CONDITIONAL"  # 조건부: 특정 조건 충족 시 자동 청산
    AUTO = "AUTO"  # 자동: RECOVERY 진입 즉시 자동 청산 시도

# ------------------------------------------------------------
# 결정표(Policy Table)
# ------------------------------------------------------------
# 입력 이벤트(event):
# - SLIPPAGE_HARD_BREACH
# - EXIT_UNRESOLVED
# - ORDER_FINAL
#
# 출력 액션(action):
# - ENTER_RECOVERY(market)
# - SET_EMERGENCY_STOP(global)
# - ATTEMPT_LIQUIDATE(market)
#
# ====== Decision Table (요약) ======
#
# 1) SLIPPAGE_HARD_BREACH
#    - SELL(ask) 하드 브리치:
#         MANUAL       → ENTER_RECOVERY + SET_EMERGENCY_STOP
#         CONDITIONAL  → ENTER_RECOVERY + SET_EMERGENCY_STOP + (조건부) LIQUIDATE
#         AUTO         → ENTER_RECOVERY + SET_EMERGENCY_STOP + LIQUIDATE
#    - BUY(bid) 하드 브리치:
#         MANUAL       → ENTER_RECOVERY(진입 금지)  (전역 stop은 기본 OFF)
#         CONDITIONAL  → ENTER_RECOVERY (반복/조건 시 LIQUIDATE는 보통 불필요)
#         AUTO         → ENTER_RECOVERY (보유가 생겼다면 후속은 조건부)
#
# 2) EXIT_UNRESOLVED (청산 실패/교착)
#         모든 모드  → ENTER_RECOVERY + SET_EMERGENCY_STOP
#         CONDITIONAL/AUTO → (조건부/즉시) LIQUIDATE
#
# 3) ORDER_FINAL (done/cancel 포함한 주문 종료)
#    - SELL 주문이 cancel 이면서 executed_volume > 0 (부분 청산 후 잔량 남음)
#         MANUAL       → ENTER_RECOVERY + SET_EMERGENCY_STOP
#         CONDITIONAL  → ENTER_RECOVERY + SET_EMERGENCY_STOP + (조건부) LIQUIDATE
#         AUTO         → ENTER_RECOVERY + SET_EMERGENCY_STOP + LIQUIDATE
#    - 그 외: 주로 튜닝/감사 목적(즉시 액션 없음)
#

@dataclass
class PolicyDecision:
    enter_recovery: bool = False
    set_emergency_stop: bool = False
    attempt_liquidate: bool = False
    reason: str = ""
    meta: Dict[str, Any] = None

def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    if v is None:
        return default
    v = str(v).strip()
    return v if v else default

from app.core.constants import env_float as _env_float, env_int as _env_int

def _now_ts() -> float:
    return time.time()

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        logger.warning(f"_safe_float: failed to convert {x!r}, using default={default}", exc_info=True)
        return float(default)

def _market_currency(market: str) -> str:
    # 'BTCUSDT' -> 'BTC'
    try:
        return market.split("-", 1)[1].strip().upper()
    except (AttributeError, TypeError, IndexError) as e:
        logger.warning(f"_market_currency: failed to parse {market!r}: {e}", exc_info=True)
        return ""

class RecoveryPolicyEngine:
    """원장 이벤트를 기반으로 RECOVERY 정책을 실행한다."""

    def __init__(self) -> None:
        # 모드
        raw = _env_str("OMA_RECOVERY_POLICY", "MANUAL").upper()
        # 호환: HOLD=MANUAL
        if raw in ("HOLD", "MANUAL"):
            self.mode = RecoveryPolicyMode.MANUAL
        elif raw in ("COND", "CONDITIONAL"):
            self.mode = RecoveryPolicyMode.CONDITIONAL
        elif raw in ("AUTO",):
            self.mode = RecoveryPolicyMode.AUTO
        else:
            self.mode = RecoveryPolicyMode.MANUAL

        # 조건부/자동 청산 파라미터
        # NOTE: 환경변수 이름은 호환성을 위해 OMA_RECOVERY_MIN_VALUE_USDT 유지
        self.min_value_usdt = _env_float("OMA_RECOVERY_MIN_VALUE_USDT", 10.0)
        self.cond_max_hold_sec = _env_int("OMA_RECOVERY_COND_MAX_HOLD_SEC", 1800)
        self.cond_stoploss_pct = _env_float("OMA_RECOVERY_COND_STOPLOSS_PCT", 3.0)  # 손실 % 기준(양수)

        # 슬리피지 하드 브리치 기준(bps) — 이벤트에 값이 없을 때 fallback
        self.hard_slippage_bps = _env_float("OMA_SLIPPAGE_HARD_BPS", 80.0)

        # 파일 기반 상태(재부팅 후에도 recovery_since 유지)
        self.state_path = _env_str("OMA_RECOVERY_STATE_PATH", "runtime/recovery_state.json")
        self._state: Dict[str, Dict[str, Any]] = {}
        self._load_state()

        # 내부 주기 체크(조건부)
        self._last_periodic_ts = 0.0
        self.periodic_interval_sec = _env_float("OMA_RECOVERY_PERIODIC_SEC", 1.0)

    # --------------------------------------------------------
    # Persistence
    # --------------------------------------------------------
    def _load_state(self) -> None:
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._state = data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"RecoveryPolicyEngine._load_state: failed to read {self.state_path}: {e}")
            self._state = {}

    def _save_state(self) -> None:
        try:
            from app.core.io_utils import safe_write_json
            safe_write_json(self.state_path, self._state)
        except OSError as e:
            logger.warning(f"RecoveryPolicyEngine._save_state: failed to save {self.state_path}: {e}")

    # --------------------------------------------------------
    # Public entrypoint
    # --------------------------------------------------------
    def on_ledger_event(self, system: Any, record: Dict[str, Any]) -> None:
        """원장 레코드 1건을 받아 정책을 적용한다."""
        event = str(record.get("event") or "").strip()
        if not event:
            return

        # 관심 이벤트만 처리
        if event not in ("ORDER_FINAL", "EXIT_UNRESOLVED", "SLIPPAGE_HARD_BREACH", "ORPHAN_DETECTED"):
            return

        market = str(record.get("market") or "").strip()
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if not market:
            # 일부 원장 구현은 market이 data에 있을 수 있음
            market = str((data or {}).get("market") or "").strip()
        if not market:
            return

        decision = self.decide(system=system, event=event, market=market, data=data, record=record)
        if decision.enter_recovery or decision.set_emergency_stop or decision.attempt_liquidate:
            self.execute(system=system, market=market, event=event, decision=decision, record=record)

    def snapshot(self) -> Dict[str, Any]:
        """현재 RECOVERY 정책/임계값 스냅샷(READ-ONLY)."""
        markets: List[str] = []
        try:
            for m, st in (self._state or {}).items():
                if isinstance(st, dict) and st.get("status") == "RECOVERY":
                    markets.append(m)
        except (TypeError, AttributeError) as e:
            logger.warning("snapshot: failed to iterate state: %s", e, exc_info=True)
            markets = []

        return {
            "policy": self.mode.value,
            "min_value_usdt": float(self.min_value_usdt),
            "cond_max_hold_sec": int(self.cond_max_hold_sec),
            "cond_stoploss_pct": float(self.cond_stoploss_pct),
            "hard_slippage_bps": float(self.hard_slippage_bps),
            "state_path": self.state_path,
            "markets_in_recovery": sorted(markets),
        }

    def manual_enter_recovery(self, system: Any, market: str, *, reason: str = "manual") -> None:
        """수동으로 RECOVERY 진입(자동 청산은 하지 않음)."""
        self._enter_recovery(system, market, reason=reason, meta={"event": "MANUAL_RECOVERY"})

    def manual_liquidate(self, system: Any, market: str, *, reason: str = "manual_liquidate") -> None:
        """수동으로 RECOVERY 전량 청산을 시도한다."""
        self._enter_recovery(system, market, reason=reason, meta={"event": "MANUAL_LIQUIDATE"})
        self._attempt_liquidate(system, market, reason=reason)

    def periodic(self, system: Any) -> None:
        """조건부 정책에서 시간/손실 조건을 주기적으로 평가한다."""
        now = _now_ts()
        if (now - self._last_periodic_ts) < self.periodic_interval_sec:
            return
        self._last_periodic_ts = now

        if self.mode == RecoveryPolicyMode.MANUAL:
            return

        # RECOVERY로 들어가 있는 마켓들 중 조건 충족 시 회수
        for market, st in list(self._state.items()):
            if not isinstance(st, dict):
                continue
            if st.get("status") != "RECOVERY":
                continue
            since = _safe_float(st.get("since"), 0.0)
            if since <= 0:
                continue

            # OMA 시장 상태가 이미 RECOVERY가 아니라면(운영자 수동 변경/오탐 정정 등),
            # RecoveryPolicyEngine의 로컬 RECOVERY 상태를 자동 해제해서 로그 스팸/무한 청산 루프를 방지한다.
            try:
                reg = getattr(system, "oma_registry", None)
                if reg is not None and hasattr(reg, "get_state"):
                    cur = reg.get_state(market)
                    cur_name = cur.name if hasattr(cur, "name") else str(cur)
                    if "RECOVERY" not in str(cur_name).upper():
                        st["status"] = "CLEARED"
                        st["cleared_at"] = now
                        st["updated_at"] = now
                        self._state[market] = st
                        self._save_state()
                        self._ledger_append(system, "RECOVERY_EXIT", market, {
                            "reason": "oma_state_not_recovery",
                            "oma_state": cur_name,
                            "since": since,
                            "now": now,
                        })
                        continue
            except (AttributeError, KeyError, TypeError) as e:
                logger.warning("periodic: failed to check OMA state for %s: %s", market, e, exc_info=True)

            # 시간 조건
            if self.mode in (RecoveryPolicyMode.CONDITIONAL, RecoveryPolicyMode.AUTO):
                if self.cond_max_hold_sec > 0 and (now - since) >= float(self.cond_max_hold_sec):
                    self.execute(
                        system=system,
                        market=market,
                        event="RECOVERY_TICK",
                        decision=PolicyDecision(
                            enter_recovery=True,
                            set_emergency_stop=False,
                            attempt_liquidate=True,
                            reason=f"recovery_hold_timeout>={self.cond_max_hold_sec}s",
                            meta={"since": since, "now": now},
                        ),
                        record={"event": "RECOVERY_TICK", "market": market, "data": {}},
                    )
                    continue

            # 손실 조건(가능하면 컨텍스트 entry/현재가로 계산)
            pnl_pct = self._estimate_pnl_pct(system, market)
            if pnl_pct is not None and pnl_pct <= -abs(self.cond_stoploss_pct):
                self.execute(
                    system=system,
                    market=market,
                    event="RECOVERY_TICK",
                    decision=PolicyDecision(
                        enter_recovery=True,
                        set_emergency_stop=False,
                        attempt_liquidate=True,
                        reason=f"recovery_stoploss_breach pnl={pnl_pct:.2f}%",
                        meta={"pnl_pct": pnl_pct},
                    ),
                    record={"event": "RECOVERY_TICK", "market": market, "data": {}},
                )

    # --------------------------------------------------------
    # Decision
    # --------------------------------------------------------
    def decide(
        self,
        *,
        system: Any,
        event: str,
        market: str,
        data: Dict[str, Any],
        record: Dict[str, Any],
    ) -> PolicyDecision:
        event = event.upper()

        # --- SLIPPAGE_HARD_BREACH ---
        if event == "SLIPPAGE_HARD_BREACH":
            side = str(data.get("side") or "").lower()  # 'ask'|'bid'
            slp = _safe_float(data.get("slippage_bps"), _safe_float(data.get("slippage"), self.hard_slippage_bps))

            if side == "ask":
                # 청산측 하드 브리치: 전역 정지 + 회수
                return PolicyDecision(
                    enter_recovery=True,
                    set_emergency_stop=True,
                    attempt_liquidate=(self.mode != RecoveryPolicyMode.MANUAL),
                    reason=f"slippage_hard_breach_sell bps={slp:.1f}",
                    meta={"side": side, "slippage_bps": slp},
                )

            # buy 하드 브리치: 시장 단위 회수로 진입 차단(전역 stop 기본 off)
            return PolicyDecision(
                enter_recovery=True,
                set_emergency_stop=False,
                attempt_liquidate=False,
                reason=f"slippage_hard_breach_buy bps={slp:.1f}",
                meta={"side": side, "slippage_bps": slp},
            )

        # --- EXIT_UNRESOLVED ---
        if event == "EXIT_UNRESOLVED":
            # 청산 교착은 치명적: 전역 stop + 회수
            return PolicyDecision(
                enter_recovery=True,
                set_emergency_stop=True,
                attempt_liquidate=(self.mode != RecoveryPolicyMode.MANUAL),
                reason="exit_unresolved",
                meta={"data": data},
            )

        # --- ORPHAN_DETECTED ---
        if event == "ORPHAN_DETECTED":
            # orphan은 회수 대상이며 entry 금지
            # auto/conditional이면 (min_value 이상) 자동 청산 시도
            return PolicyDecision(
                enter_recovery=True,
                set_emergency_stop=False,
                attempt_liquidate=(self.mode == RecoveryPolicyMode.AUTO),
                reason="orphan_detected",
                meta={"data": data},
            )

        # --- ORDER_FINAL ---
        if event == "ORDER_FINAL":
            # 주문이 cancel로 끝났는데 sell 주문이 일부 체결된 경우,
            # 잔량이 남아 있을 확률이 높음 → RECOVERY 승격.
            state = str(data.get("state") or "").lower()
            side = str(data.get("side") or "").lower()
            executed = _safe_float(data.get("executed_volume"), 0.0)

            if side == "ask" and state == "cancel" and executed > 0:
                return PolicyDecision(
                    enter_recovery=True,
                    set_emergency_stop=True,
                    attempt_liquidate=(self.mode != RecoveryPolicyMode.MANUAL),
                    reason="sell_partial_then_cancel",
                    meta={"executed_volume": executed},
                )

            # 기타 ORDER_FINAL은 튜닝용 기록(즉시 액션 없음)
            return PolicyDecision(
                enter_recovery=False,
                set_emergency_stop=False,
                attempt_liquidate=False,
                reason="order_final_no_action",
                meta={},
            )

        # default: do nothing
        return PolicyDecision(reason="ignored")

    # --------------------------------------------------------
    # Execute
    # --------------------------------------------------------
    def execute(
        self,
        *,
        system: Any,
        market: str,
        event: str,
        decision: PolicyDecision,
        record: Dict[str, Any],
    ) -> None:
        meta = decision.meta or {}

        if decision.enter_recovery:
            self._enter_recovery(system, market, reason=decision.reason, meta=meta)

        if decision.set_emergency_stop:
            self._set_emergency_stop(system, True, reason=decision.reason, meta=meta)

        if decision.attempt_liquidate:
            # 조건부에서는 "가치 최소" 기준을 만족할 때만 수행 (방벽 최소화)
            if self.mode == RecoveryPolicyMode.CONDITIONAL:
                ok, val = self._estimate_position_value_usdt(system, market)
                if ok and val is not None and val >= float(self.min_value_usdt):
                    self._attempt_liquidate(system, market, reason=decision.reason)
                else:
                    self._ledger_append(system, "RECOVERY_LIQUIDATE_SKIP", market, {
                        "reason": decision.reason,
                        "estimated_value_usdt": val,
                        "min_value_usdt": self.min_value_usdt,
                    })
            else:
                self._attempt_liquidate(system, market, reason=decision.reason)

    # --------------------------------------------------------
    # Actions
    # --------------------------------------------------------
    def _enter_recovery(self, system: Any, market: str, *, reason: str, meta: Dict[str, Any]) -> None:
        now = _now_ts()

        # Skip if already in RECOVERY (avoid log spam)
        st = self._state.get(market) if isinstance(self._state.get(market), dict) else {}
        if st.get("status") == "RECOVERY":
            return

        # OMA state: RECOVERY로 승격(존재하지 않으면 WATCH로 폴백)
        try:
            from app.manager.oma_market_registry import MarketState
            desired_state = getattr(MarketState, "RECOVERY", None)
        except ImportError as e:
            logger.warning("_enter_recovery: failed to import MarketState: %s", e, exc_info=True)
            desired_state = None

        set_ok = False
        if desired_state is not None:
            # system.oma_set_market 우선
            if hasattr(system, "oma_set_market"):
                try:
                    system.oma_set_market(market=market, state=desired_state, reason=[f"RECOVERY:{reason}"])
                    set_ok = True
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    logger.warning("_enter_recovery: oma_set_market failed for %s: %s", market, e)
                    set_ok = False
            if not set_ok:
                # direct registry
                reg = getattr(system, "oma", None) or getattr(system, "oma_registry", None)
                if reg and hasattr(reg, "set_state"):
                    try:
                        reg.set_state(market=market, state=desired_state, reason=[f"RECOVERY:{reason}"])
                        set_ok = True
                    except (KeyError, AttributeError, TypeError, ValueError) as e:
                        logger.warning("_enter_recovery: reg.set_state failed for %s: %s", market, e)
                        set_ok = False

        if not set_ok:
            # fallback: WATCH
            try:
                from app.manager.oma_market_registry import MarketState
                if hasattr(system, "oma_set_market"):
                    system.oma_set_market(market=market, state=MarketState.WATCH, reason=[f"RECOVERY_FALLBACK:{reason}"])
                else:
                    reg = getattr(system, "oma", None) or getattr(system, "oma_registry", None)
                    if reg and hasattr(reg, "set_state"):
                        reg.set_state(market=market, state=MarketState.WATCH, reason=[f"RECOVERY_FALLBACK:{reason}"])
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.warning("_enter_recovery: fallback to WATCH failed for %s: %s", market, e)

        # local persistent recovery state
        st = self._state.get(market) if isinstance(self._state.get(market), dict) else {}
        st = dict(st)
        st.update({
            "status": "RECOVERY",
            "since": st.get("since") or now,
            "reason": reason,
            "last_event": event if isinstance((event := str(meta.get("event") or "")), str) else "",
            "updated_at": now,
        })
        self._state[market] = st
        self._save_state()

        self._ledger_append(system, "RECOVERY_ENTER", market, {"reason": reason, **meta})

    def _set_emergency_stop(self, system: Any, value: bool, *, reason: str, meta: Dict[str, Any]) -> None:
        # 우선 system 메서드 사용
        if hasattr(system, "set_emergency_stop"):
            try:
                system.set_emergency_stop(bool(value), reason=reason)
                self._ledger_append(system, "EMERGENCY_STOP_SET", "", {"value": bool(value), "reason": reason, **meta})
                return
            except (AttributeError, TypeError) as e:
                logger.warning("_set_emergency_stop: system.set_emergency_stop failed: %s", e)

        # 폴백: 속성 세팅
        try:
            setattr(system, "emergency_stop", bool(value))
        except (AttributeError, TypeError) as e:
            logger.warning("_set_emergency_stop: failed to set emergency_stop attr: %s", e, exc_info=True)

        self._ledger_append(system, "EMERGENCY_STOP_SET", "", {"value": bool(value), "reason": reason, **meta})

    def _attempt_liquidate(self, system: Any, market: str, *, reason: str) -> None:
        """Bybit 잔고를 기준으로 해당 마켓 코인을 전량 시장가 매도 시도."""
        
        # [PROTECTED] LongHold 마켓은 자동 청산 차단 (2026-02-01)
        # DO NOT MODIFY: 이 로직은 사용자 지시로 보호됨
        is_longhold = False
        user_sell_only = False
        try:
            ladder_mgr = getattr(system, "ladder_manager", None)
            if ladder_mgr:
                lh_cfg = ladder_mgr.get_longhold_config(market)
                if lh_cfg and lh_cfg.get("enabled"):
                    is_longhold = True
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[recovery_policy] %s: %s", 'DO NOT MODIFY: 이 로직은 사용자 지시로 보호됨', exc, exc_info=True)
        
        try:
            coord = getattr(system, "coordinator", None)
            if coord:
                ctx = coord.contexts.get(market)
                if ctx:
                    ctrls = getattr(ctx, "controls", {}) or {}
                    sp = ctrls.get("strategy", {}).get("params", {}) or {}
                    user_sell_only = bool(sp.get("user_sell_only", False))
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[recovery_policy] %s: %s", 'DO NOT MODIFY: 이 로직은 사용자 지시로 보호됨', exc, exc_info=True)
        
        if is_longhold or user_sell_only:
            self._ledger_append(system, "RECOVERY_LIQUIDATE_BLOCKED", market, {
                "reason": reason,
                "blocked_by": "longhold_protected",
                "is_longhold": is_longhold,
                "user_sell_only": user_sell_only,
            })
            return
        
        trade = self._resolve_trade_client(system)
        if trade is None:
            self._ledger_append(system, "RECOVERY_LIQUIDATE_FAIL", market, {"reason": reason, "error": "no_trade_client"})
            return

        currency = _market_currency(market)
        if not currency:
            self._ledger_append(system, "RECOVERY_LIQUIDATE_FAIL", market, {"reason": reason, "error": "bad_market"})
            return

        qty = self._accounts_qty(trade, currency)
        if qty <= 0:
            self._ledger_append(system, "RECOVERY_LIQUIDATE_SKIP", market, {"reason": reason, "qty": qty})

            # 더 이상 청산할 보유분이 없으면 RECOVERY 상태를 자동 해제(로그 스팸/무한 루프 방지)
            try:
                now = time.time()
                st = self._state.get(market)
                if isinstance(st, dict) and str(st.get("status") or "").upper() == "RECOVERY":
                    st["status"] = "CLEARED"
                    st["cleared_at"] = now
                    st["updated_at"] = now
                    self._state[market] = st
                    self._save_state()
                    self._ledger_append(system, "RECOVERY_EXIT", market, {
                        "reason": "no_holding",
                        "qty": qty,
                        "now": now,
                    })
            except (KeyError, TypeError) as e:
                logger.warning("_attempt_liquidate: failed to clear RECOVERY state for %s: %s", market, e)
            return

        ok, value_usdt = self._estimate_position_value_usdt(system, market)
        if ok and value_usdt is not None and value_usdt < float(self.min_value_usdt):
            self._ledger_append(system, "RECOVERY_LIQUIDATE_SKIP", market, {
                "reason": reason,
                "qty": qty,
                "estimated_value_usdt": value_usdt,
                "min_value_usdt": self.min_value_usdt,
            })
            return

        # 시장가 매도
        try:
            resp = trade.market_sell_qty(market, qty)
            oid = str(resp.get("uuid") or "")
            self._ledger_append(system, "RECOVERY_LIQUIDATE_SUBMIT", market, {
                "reason": reason,
                "uuid": oid,
                "qty": qty,
            })

            # 결과 확인(짧게)
            timeout = _env_float("BYBIT_ORDER_WAIT_SEC_SELL", _env_float("BYBIT_ORDER_WAIT_SEC", 3.0))
            poll = _env_float("BYBIT_ORDER_POLL_SEC", 0.2)
            final = trade.wait_order(uuid=oid, timeout_sec=timeout, poll_interval=poll) if oid else resp

            # NOTE: ledger에 구조화 필드를 남기기 위해 dict 기반으로 요약한다.
            summ = self._summarize_order_for_ledger(final)
            if oid and not summ.get("uuid"):
                summ["uuid"] = oid

            self._ledger_append(system, "RECOVERY_LIQUIDATE_FINAL", market, {
                "reason": reason,
                **summ,
            })

        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[RecoveryPolicy] liquidate failed for %s reason=%s", market, reason, exc_info=True)
            self._ledger_append(system, "RECOVERY_LIQUIDATE_FAIL", market, {"reason": reason, "error": str(exc)})

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------
    def _summarize_order_for_ledger(self, order: Any) -> Dict[str, Any]:
        """주문 응답(dict)을 원장 기록용으로 안전하게 요약한다.

        - 거래소 응답은 숫자들이 문자열로 들어오는 경우가 많다.
        - trades가 포함되어 있으면 funds/avg_price를 더 정확히 계산한다.
        - order가 dict가 아니면 raw 문자열로 남긴다(예외 방지).
        """
        if not isinstance(order, dict):
            return {"raw": str(order)}

        def sf(x: Any, default: float = 0.0) -> float:
            try:
                return float(x)
            except (TypeError, ValueError):
                logger.warning("[RecoveryPolicy] sf: conversion failed for %r", x, exc_info=True)
                return float(default)

        uuid = str(order.get("uuid") or "")
        state = str(order.get("state") or "")
        side = str(order.get("side") or "")

        executed_volume = sf(order.get("executed_volume"), 0.0)
        paid_fee = sf(order.get("paid_fee"), 0.0)

        # funds / avg_price best-effort
        funds = 0.0
        avg_price = None

        trades = order.get("trades")
        if isinstance(trades, list) and trades:
            vol_sum = 0.0
            funds_sum = 0.0
            for t in trades:
                if not isinstance(t, dict):
                    continue
                vol_sum += sf(t.get("volume") or t.get("executed_volume"), 0.0)
                funds_sum += sf(t.get("funds"), 0.0)

            if executed_volume <= 0.0 and vol_sum > 0.0:
                executed_volume = vol_sum

            funds = funds_sum
            if executed_volume > 0.0 and funds > 0.0:
                avg_price = funds / executed_volume

        else:
            # fallback: if price is present (limit order), approximate
            px = sf(order.get("price"), 0.0)
            if px > 0.0 and executed_volume > 0.0:
                funds = px * executed_volume
                avg_price = px

        return {
            "uuid": uuid,
            "state": state,
            "side": side,
            "executed_volume": executed_volume,
            "funds": funds,
            "avg_price": avg_price,
            "paid_fee": paid_fee,
        }

    def _resolve_trade_client(self, system: Any) -> Any:
        # 흔한 필드명들
        for name in ("trade_client", "trade"):
            obj = getattr(system, name, None)
            if obj is not None:
                return obj
        # 메서드 기반
        for name in ("get_trade_client",):
            fn = getattr(system, name, None)
            if callable(fn):
                try:
                    obj = fn()
                    if obj is not None:
                        return obj
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[recovery_policy] %s: %s", f"_resolve_trade_client: {name}() failed", exc, exc_info=True)
                    continue
        return None

    def _accounts_qty(self, trade: Any, currency: str) -> float:
        try:
            acc = trade.accounts()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("_accounts_qty: trade.accounts() failed for %s: %s", currency, e)
            return 0.0

        qty = 0.0
        for a in acc or []:
            if str(a.get("currency") or "").upper() != currency.upper():
                continue
            qty += _safe_float(a.get("balance"), 0.0)
            qty += _safe_float(a.get("locked"), 0.0)
        return float(qty)

    def _estimate_position_value_usdt(self, system: Any, market: str) -> Tuple[bool, Optional[float]]:
        """가능하면 현재가 기반으로 보유 가치(USDT)를 추정."""
        try:
            # price_store가 있으면 사용
            from app.core.hyper_price_store import price_store
            px = price_store.get_price(market)
        except (ImportError, AttributeError, TypeError):
            logger.warning("[RecoveryPolicy] _estimate_position_value_usdt: price_store import failed", exc_info=True)
            px = None

        if px is None:
            return False, None

        trade = self._resolve_trade_client(system)
        if trade is None:
            return False, None

        currency = _market_currency(market)
        qty = self._accounts_qty(trade, currency)
        return True, float(qty) * float(px)

    def _estimate_pnl_pct(self, system: Any, market: str) -> Optional[float]:
        """컨텍스트 position(entry)와 현재가로 PnL%를 추정."""
        try:
            from app.core.hyper_price_store import price_store
            px = price_store.get_price(market)
        except (ImportError, AttributeError, TypeError):
            logger.warning("[RecoveryPolicy] _estimate_pnl_pct: price_store import failed for %s", market, exc_info=True)
            return None
        if px is None:
            return None

        # coordinator/context에서 entry 확인
        entry = None
        try:
            coord = getattr(system, "coordinator", None)
            if coord and hasattr(coord, "get_context"):
                ctx = coord.get_context(market)
                pos = getattr(ctx, "position", None)
                if isinstance(pos, dict):
                    entry = pos.get("entry")
                elif pos is not None and isinstance(pos, dict):
                    entry = pos.get("entry")
        except (KeyError, AttributeError, TypeError):
            logger.warning("[RecoveryPolicy] _entry_price_from_ctx: context read failed for %s", market, exc_info=True)
            entry = None

        if entry is None:
            return None

        e = _safe_float(entry, 0.0)
        if e <= 0:
            return None

        return (float(px) - e) / e * 100.0

    def _ledger_append(self, system: Any, event: str, market: str, data: Dict[str, Any]) -> None:
        """시스템에 원장 writer가 있으면 사용, 없으면 파일로 직접 append."""
        # system.ledger.append(...) 형태 우선
        ledger = getattr(system, "ledger", None)
        if ledger and hasattr(ledger, "append"):
            try:
                ledger.append(event=event, market=market or None, data=data)
                return
            except (AttributeError, TypeError) as e:
                logger.warning("_ledger_append: ledger.append failed: %s", e, exc_info=True)

        # 폴백: JSONL 직접 append
        path = _env_str("OMA_LEDGER_PATH", "runtime/trade_ledger.jsonl")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            rec = {
                "ts": time.time(),
                "event": event,
                "market": market,
                "data": data,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("_ledger_append: failed to write to %s: %s", path, e)
