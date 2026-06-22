# ============================================================
# File: app/manager/triage_manager.py
# Autocoin OS v3-H — 포트폴리오 트리아지 모드 매니저
# ------------------------------------------------------------
# 설계서: docs/TRAIGE MODE PLAN.md
# 결함 수정: ctx.position 필드명 (qty, entry — reconcile에서 세팅),
#            should_sell() DCA 후 신규 평균가 기준,
#            DCA 비동기 오프로드, TRIAGE_SELL 완료 감지
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================
# 환경변수 헬퍼 (hyper_system 방식과 동일)
# ============================================================

def _ef(key: str, default: float) -> float:
    try:
        v = os.getenv(key, "")
        return float(v) if v.strip() else default
    except (TypeError, ValueError):
        logger.warning("_ef suppressed exception", exc_info=True)
        return default

def _eb(key: str, default: bool) -> bool:
    v = str(os.getenv(key, "1" if default else "0")).strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _es(key: str, default: str) -> str:
    return os.getenv(key, default) or default


def _load_settings_from_env() -> Dict[str, Any]:
    return {
        "enabled": _eb("OMA_TRIAGE_ENABLED", False),
        "trigger_pnl_pct": _ef("OMA_TRIAGE_TRIGGER_PNL_PCT", -5.0),
        "trigger_loss_count": int(_ef("OMA_TRIAGE_TRIGGER_LOSS_COUNT", 5)),
        "max_dca_ratio": _ef("OMA_TRIAGE_MAX_DCA_RATIO", 2.0),
        "profit_target_pct": _ef("OMA_TRIAGE_PROFIT_TARGET_PCT", 0.3),
        "fee_pct": 0.15,  # 수수료 마진 (매수+매도 합산)
        "max_loss_exclude_pct": _ef("OMA_TRIAGE_MAX_LOSS_EXCLUDE_PCT", -30.0),
        "coin_timeout_hours": _ef("OMA_TRIAGE_COIN_TIMEOUT_HOURS", 48.0),
        "max_duration_hours": _ef("OMA_TRIAGE_MAX_DURATION_HOURS", 168.0),
        "dca_interval_sec": _ef("OMA_TRIAGE_DCA_INTERVAL_SEC", 300.0),
        "exit_pnl_pct": _ef("OMA_TRIAGE_EXIT_PNL_PCT", -2.0),
        "recovery_target": _es("OMA_TRIAGE_RECOVERY_TARGET", "ALL"),
        "notify": _eb("OMA_TRIAGE_NOTIFY", True),
        "state_path": _es("OMA_TRIAGE_STATE_PATH", "runtime/triage_state.json"),
        "sell_timeout_sec": 300.0,   # TRIAGE_SELL 5분 타임아웃
        "min_position_usdt": 10.0, # 먼지 제외 기준 (min_order_usdt * 2)
        # BUY 차단 면제 전략
        # CONTRARIAN: 하락장이 바로 진입 타이밍 (BTC Guard도 동일 정책)
        # SNIPER: 시간민감 급등신호, 포트 손실과 독립적 기회
        # WHALE: 고래 신호 추종, 시간민감
        "global_dca_cap_pct": _ef("OMA_TRIAGE_GLOBAL_DCA_CAP_PCT", 30.0),  # 전체 DCA 합산 포트폴리오 % 캡
        "exempt_strategies": ["CONTRARIAN", "SNIPER", "WHALE"],
        "focus_dca_allow": True,     # 포커스 마켓 PRM 우회 허용
        # BUY 모드 제어
        # "block_all"      : 기존 동작 — 면제 전략 외 모든 BUY 차단
        # "allow_non_loss" : 트리아지 진입 시점 손실 코인이 아닌 코인은 BUY 허용
        "buy_mode": "block_all",
        # 손실 코인 조건부 즉시 DCA
        # True: 손실 코인에 전략이 BUY 시그널을 내면 스케줄 기다리지 않고 즉시 DCA 허용
        #       현금 버퍼(triage_reserved_usdt) 체크 후 통과
        "opportunistic_dca": False,
        # 시장 회복 자동 해제
        # BTC Guard OFF + PnL 개선 + 최소 경과 시간 → 트리아지 자동 종료
        "market_recovery_exit_enabled": _eb("OMA_TRIAGE_MARKET_RECOVERY_EXIT", True),
        "market_recovery_min_hours": _ef("OMA_TRIAGE_MARKET_RECOVERY_MIN_HOURS", 2.0),
        # 매수 후 이 시간(분) 이내는 손실 코인으로 카운트 안 함
        # 매수 직후 수수료+스프레드 때문에 오발동 방지
        "loss_grace_min": _ef("OMA_TRIAGE_LOSS_GRACE_MIN", 30.0),
        # 병렬 복구: 동시에 DCA 복구할 최대 코인 수
        "max_concurrent_targets": int(_ef("OMA_TRIAGE_MAX_CONCURRENT", 3)),
        # 긴급 탈출: 시장 상황에 따라 수익 목표를 동적으로 낮춤
        "emergency_exit_enabled": _eb("OMA_TRIAGE_EMERGENCY_EXIT", True),
        "emergency_moderate_avg_loss_pct": _ef("OMA_TRIAGE_EMERGENCY_MODERATE_PCT", -10.0),
        "emergency_severe_avg_loss_pct": _ef("OMA_TRIAGE_EMERGENCY_SEVERE_PCT", -30.0),
    }


# ============================================================
# TriageManager
# ============================================================

class TriageManager:
    """
    포트폴리오 트리아지 모드 상태 머신.

    7상태: NORMAL → TRIAGE_INIT → TRIAGE_SCAN → TRIAGE_DCA
           → TRIAGE_WAIT → TRIAGE_SELL → TRIAGE_EXIT → NORMAL

    핵심 원리:
      - 손실 코인 1개씩 DCA 물타기 → 본전+α 매도로 순차 복구
      - 복구 중 신규 BUY 전면 차단 (hyper_system._triage_entry_blocked)
    """

    STATE_NORMAL = "NORMAL"
    STATE_INIT   = "TRIAGE_INIT"
    STATE_SCAN   = "TRIAGE_SCAN"
    STATE_DCA    = "TRIAGE_DCA"
    STATE_WAIT   = "TRIAGE_WAIT"
    STATE_SELL   = "TRIAGE_SELL"
    STATE_EXIT   = "TRIAGE_EXIT"

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings: Dict[str, Any] = settings or _load_settings_from_env()
        self.state: str = self.STATE_NORMAL
        self.start_ts: float = 0.0
        self.trigger_reason: str = ""
        self.active_targets: List[Dict[str, Any]] = []  # 병렬 복구 대상 (각 타겟에 state 필드)
        self.recovered: List[Dict[str, Any]] = []              # 복구 완료
        self.skipped: List[Dict[str, Any]] = []                # 스킵
        self.excluded: List[Dict[str, Any]] = []               # 제외
        self.initial_snapshot: Dict[str, Any] = {}
        self._entry_equity_usdt: float = 0.0      # 진입 시점 전체 자산 (성과 측정)

    # ============================================================
    # current_target 하위 호환 프로퍼티
    # ============================================================

    @property
    def current_target(self) -> Optional[Dict[str, Any]]:
        """Backward compat: 첫 번째 active target 반환."""
        return self.active_targets[0] if self.active_targets else None

    @current_target.setter
    def current_target(self, val: Optional[Dict[str, Any]]) -> None:
        """Backward compat setter (테스트/외부 코드용)."""
        if val is None:
            self.active_targets = []
        else:
            val.setdefault("state", "DCA")
            val.setdefault("last_dca_ts", 0.0)
            val.setdefault("sell_submitted_ts", 0.0)
            val.setdefault("dca_confirmed_funds", 0.0)
            if self.active_targets:
                self.active_targets[0] = val
            else:
                self.active_targets = [val]

    # 하위 호환 프로퍼티: 이전 인스턴스 변수를 current_target 내부로 위임
    @property
    def _dca_confirmed_funds(self) -> float:
        t = self.current_target
        return t.get("dca_confirmed_funds", 0.0) if t else 0.0

    @_dca_confirmed_funds.setter
    def _dca_confirmed_funds(self, val: float) -> None:
        t = self.current_target
        if t:
            t["dca_confirmed_funds"] = val

    @property
    def _last_dca_ts(self) -> float:
        t = self.current_target
        return t.get("last_dca_ts", 0.0) if t else 0.0

    @_last_dca_ts.setter
    def _last_dca_ts(self, val: float) -> None:
        t = self.current_target
        if t:
            t["last_dca_ts"] = val

    @property
    def _sell_submitted_ts(self) -> float:
        t = self.current_target
        return t.get("sell_submitted_ts", 0.0) if t else 0.0

    @_sell_submitted_ts.setter
    def _sell_submitted_ts(self, val: float) -> None:
        t = self.current_target
        if t:
            t["sell_submitted_ts"] = val

    # ============================================================
    # DCA 체결 확인 (order_fsm buy-fill callback에서 호출)
    # ============================================================

    def on_dca_fill_confirmed(self, *, market: str, entry_price: float,
                               qty: float, funds: float, fee: float) -> None:
        """order_fsm buy-fill callback → DCA 실제 체결 확인."""
        target = self._find_target(market)
        if target is None:
            return
        target["dca_confirmed_funds"] = target.get("dca_confirmed_funds", 0.0) + funds
        # dca_invested를 실제 체결 기준으로 보정 (ACK 시점 추정값 → 실제값)
        target["dca_invested"] = target["dca_confirmed_funds"]
        self.save_state()
        logger.info("[TriageManager] DCA fill CONFIRMED market=%s funds=%.0f total_confirmed=%.0f",
                    market, funds, target["dca_confirmed_funds"])

    # ============================================================
    # 타겟 검색 헬퍼
    # ============================================================

    def _find_target(self, market: str) -> Optional[Dict[str, Any]]:
        """active_targets에서 market으로 타겟 검색."""
        for t in self.active_targets:
            if t.get("market") == market:
                return t
        return None

    # ============================================================
    # 자본 예약 계산
    # ============================================================

    def calc_reserved_capital(self, system: Any) -> float:
        """모든 active target의 잔여 DCA 예산 합산 → system._triage_reserved_usdt에 반영."""
        if not self.is_active() or not self.active_targets:
            return 0.0
        total = 0.0
        for target in self.active_targets:
            try:
                ctx = system.coordinator.get_context(target["market"])
                if not ctx or not getattr(ctx, "position", None):
                    continue
                avg_buy = float(ctx.position.get("entry", 0.0) or 0.0)
                qty = float(ctx.position.get("qty", 0.0) or 0.0)
                if avg_buy <= 0 or qty <= 0:
                    continue
                current_invested = avg_buy * qty
                max_additional = current_invested * float(self.settings["max_dca_ratio"])
                already = target.get("dca_invested", 0.0)
                remaining = max(0.0, max_additional - already)
                total += remaining
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] calc_reserved_capital: %s", exc, exc_info=True)
                continue
        return total

    # ============================================================
    # 진입 조건
    # ============================================================

    def should_enter(self, system: Any) -> Tuple[bool, str]:
        """트리아지 진입 여부 판단."""
        if self.state != self.STATE_NORMAL:
            return False, "already_in_triage"

        # 전체 PnL: PRM에서 재사용 (이미 30초마다 업데이트됨)
        prm = getattr(system, "portfolio_risk_manager", None)
        total_pnl_pct: Optional[float] = None
        if prm and getattr(prm, "daily_status", None):
            total_pnl_pct = prm.daily_status.loss_pct

        # per-market 손실 코인 계산 (count 트리거 + 스코어링 재료)
        loss_coins = self._gather_loss_coins(system)

        trigger_pnl = float(self.settings["trigger_pnl_pct"])   # 예: -5.0
        trigger_count = int(self.settings["trigger_loss_count"])  # 예: 5

        # [FIX 2026-03-23] 트리아지 진입 조건 정상화
        # 두 조건 모두 손실 코인이 trigger_loss_count 이상 존재해야 발동
        # PnL만 나쁘고 포지션이 없거나 적으면 트리아지가 할 일이 없음
        _n_loss = len(loss_coins)
        if _n_loss < trigger_count:
            return False, f"ok: pnl={total_pnl_pct:.1f}%, loss_coins={_n_loss} < {trigger_count}"

        # 손실 코인 >= trigger_count 확인됨 → PnL 조건 OR 코인 수 조건
        if total_pnl_pct is not None and total_pnl_pct <= trigger_pnl:
            return True, f"total_pnl={total_pnl_pct:.2f}% <= {trigger_pnl}% (loss_coins={_n_loss})"

        return True, f"loss_coins={_n_loss} >= {trigger_count}개"

    def _gather_loss_coins(self, system: Any) -> List[Dict[str, Any]]:
        """현재 포지션에서 손실 코인 목록을 수집."""
        from app.core.hyper_price_store import price_store

        result = []
        try:
            coordinator = getattr(system, "coordinator", None)
            if not coordinator:
                return result
            # [FIX] lock 보호 + dict 복사로 순회 중 RuntimeError 방지
            contexts = coordinator.get_contexts() if hasattr(coordinator, "get_contexts") else dict(getattr(coordinator, "contexts", {}))
            for market, ctx in contexts.items():
                pos = getattr(ctx, "position", None)
                if not pos:
                    continue
                qty = float(pos.get("qty", 0.0) or 0.0)
                avg_buy = float(pos.get("entry", 0.0) or 0.0)
                if qty <= 0 or avg_buy <= 0:
                    continue
                # [2026-03-30] 먼지코인 제외: 투자금 5 USDT 미만은 loss_coin 집계 안 함
                if avg_buy * qty < 5.0:
                    continue
                # [FIX 2026-03-22] price_store.get()는 오더북 dict를 반환하므로
                # get_price()로 float을 가져와야 함.
                # 가격이 없으면(재시작 직후 등) avg_buy 폴백 시 pnl=0%로 오분류되므로
                # 스킵하여 가격이 들어올 때까지 대기 (_handle_scan 재시도 로직과 연동)
                current_price = price_store.get_price(market)
                if current_price is None:
                    continue
                invested = avg_buy * qty
                current_val = current_price * qty
                pnl_pct = (current_price - avg_buy) / avg_buy * 100
                # [FIX 2026-03-24] 매수 후 N분 이내는 손실 카운트 제외
                # 매수 직후 수수료+스프레드로 트리아지 오발동 방지
                import time as _time
                _grace_min = float(self.settings.get("loss_grace_min", 30.0))
                _entry_ts = float(getattr(ctx, "_last_buy_fill_ts", 0) or
                                  getattr(ctx, "_entry_ts", 0) or 0)
                if _entry_ts > 0 and (_time.time() - _entry_ts) < _grace_min * 60:
                    continue
                if pnl_pct < 0:
                    result.append({
                        "market": market,
                        "pnl_pct": pnl_pct,
                        "invested": invested,
                        "current_val": current_val,
                        "qty": qty,
                        "avg_buy": avg_buy,
                        "current_price": current_price,
                    })
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            logger.warning("[triage] _gather_loss_coins error: %s", exc)
        return result

    # ============================================================
    # 긴급 탈출: 동적 수익 목표 계산
    # ============================================================

    def get_effective_profit_target(self, system: Any) -> Tuple[float, str]:
        """시장 상황에 따른 동적 수익 목표 반환.

        Returns:
            (profit_target_pct, reason)
            - 정상: (settings.profit_target_pct, "normal")
            - 경고: (profit_target_pct * 0.5, "moderate")
            - 긴급: (0.0, "emergency")  ← 수수료만 커버하면 즉시 탈출
        """
        base_target = float(self.settings["profit_target_pct"])

        if not self.settings.get("emergency_exit_enabled", True):
            return base_target, "normal"

        # 손실 코인 평균 깊이 계산
        avg_loss = self._calc_avg_loss_depth(system)
        if avg_loss is None:
            return base_target, "normal"

        moderate_threshold = float(self.settings.get("emergency_moderate_avg_loss_pct", -10.0))
        severe_threshold = float(self.settings.get("emergency_severe_avg_loss_pct", -30.0))

        btc_guard = bool(getattr(system, "btc_guard_mode", False))

        # 긴급: avg_loss가 severe 임계값 이하 OR (BTC Guard ON + avg_loss > moderate 수준)
        if avg_loss <= severe_threshold or (btc_guard and avg_loss <= moderate_threshold * 2):
            return 0.0, f"emergency(avg={avg_loss:.1f}%,btc_guard={btc_guard})"

        # 경고: avg_loss가 moderate 임계값 이하
        if avg_loss <= moderate_threshold:
            reduced = round(base_target * 0.5, 3)
            return reduced, f"moderate(avg={avg_loss:.1f}%)"

        return base_target, "normal"

    def _calc_avg_loss_depth(self, system: Any) -> Optional[float]:
        """전체 손실 코인의 평균 손실률(%) 계산."""
        try:
            loss_coins = self._gather_loss_coins(system)
            if not loss_coins:
                return None
            total_pnl = sum(c["pnl_pct"] for c in loss_coins)
            return total_pnl / len(loss_coins)
        except (KeyError, AttributeError, TypeError):
            logger.warning("TriageManager._calc_avg_loss_depth suppressed exception", exc_info=True)
            return None

    # ============================================================
    # 트리아지 진입
    # ============================================================

    def enter_triage(self, system: Any, reason: str) -> None:
        """트리아지 모드 진입. BUY 차단 + 스냅샷 저장."""
        self.state = self.STATE_SCAN
        self.start_ts = time.time()
        self.trigger_reason = reason
        self.recovered = []
        self.skipped = []
        self.excluded = []
        self.active_targets = []

        # 성과 측정: 진입 시점 전체 자산
        self._entry_equity_usdt = float(getattr(system, "_last_equity_usdt", 0.0) or 0.0)

        # 초기 스냅샷
        loss_coins = self._gather_loss_coins(system)
        prm = getattr(system, "portfolio_risk_manager", None)
        total_pnl = (prm.daily_status.loss_pct if prm and prm.daily_status else None)
        # 시장 회복 해제용 스냅샷: BTC Guard 상태 + 총 PnL
        _btc_guard_at_entry = bool(getattr(system, "btc_guard_mode", False))

        self.initial_snapshot = {
            "total_pnl_pct": total_pnl,
            "loss_coin_count": len(loss_coins),
            "loss_coins": [c["market"] for c in loss_coins],
            "entry_equity_usdt": self._entry_equity_usdt,
            "ts": self.start_ts,
            "btc_guard_at_entry": _btc_guard_at_entry,
        }

        system._triage_entry_blocked = True
        self.save_state()

        # 원장 기록
        try:
            system.ledger.append(
                "TRIAGE_ENTERED",
                reason=reason,
                total_pnl_pct=total_pnl,
                loss_coin_count=len(loss_coins),
            )
        except (AttributeError, TypeError) as exc:
            logger.warning("[TRIAGE] 원장 기록: %s", exc, exc_info=True)

        # 텔레그램
        if self.settings.get("notify"):
            try:
                system._send_telegram_safe(
                    f"🏥 [TRIAGE] 진입\n사유: {reason}\n"
                    f"손실 코인: {len(loss_coins)}개\n신규 매수 차단됨"
                )
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] 텔레그램: %s", exc, exc_info=True)

        logger.info("[TriageManager] ENTERED reason=%s loss_coins=%d", reason, len(loss_coins))

    # ============================================================
    # 복구 대상 선정
    # ============================================================

    def select_recovery_target(self, system: Any) -> Optional[Dict[str, Any]]:
        """score 기반으로 복구 가능성이 가장 높은 코인 1개 선정."""
        from app.core.hyper_price_store import price_store
        from app.strategy import indicators

        loss_coins = self._gather_loss_coins(system)
        exclude_pct = float(self.settings["max_loss_exclude_pct"])  # 예: -30.0
        min_val = float(self.settings["min_position_usdt"])

        done_markets = set(
            [r["market"] for r in self.recovered] +
            [s["market"] for s in self.skipped] +
            [t["market"] for t in self.active_targets]
        )

        scored = []
        new_excluded = []

        for coin in loss_coins:
            market = coin["market"]
            if market in done_markets:
                continue

            pnl_pct = coin["pnl_pct"]
            invested = coin["invested"]
            current_val = coin["current_val"]

            # 제외 조건
            ctx = None
            try:
                ctx = system.coordinator.get_context(market)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[TRIAGE] 제외 조건: %s", exc, exc_info=True)

            if getattr(ctx, "longhold", False) or getattr(ctx, "is_longhold", False):
                new_excluded.append({"market": market, "reason": "longhold"})
                continue
            if pnl_pct < exclude_pct:
                new_excluded.append({"market": market, "reason": f"too_deep:{pnl_pct:.1f}%"})
                continue
            if current_val < min_val:
                new_excluded.append({"market": market, "reason": "dust"})
                continue

            # 스코어링
            closeness = max(0.0, 100.0 / max(1.0, abs(pnl_pct))) * 3.0
            capital = min(50.0, invested / 100_000.0)

            # RSI (과매도일수록 반등 기대)
            rsi_score = 0.0
            try:
                tick_prices = getattr(ctx, "_tick_prices", None) if ctx else None
                if tick_prices and len(tick_prices) >= 15:
                    rsi_val = indicators.rsi(tick_prices, 14)
                    if rsi_val is not None and rsi_val < 40:
                        rsi_score = (40.0 - rsi_val) * 2.5
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[TRIAGE] RSI (과매도일수록 반등 기대): %s", exc, exc_info=True)

            # 거래량 (24h — tick_prices 변동성으로 근사)
            vol_score = 0.0
            try:
                if tick_prices and len(tick_prices) >= 5:
                    # 최근 가격 변동 범위로 유동성 근사
                    hi = max(tick_prices[-20:]) if len(tick_prices) >= 20 else max(tick_prices)
                    lo = min(tick_prices[-20:]) if len(tick_prices) >= 20 else min(tick_prices)
                    if lo > 0:
                        range_pct = (hi - lo) / lo * 100
                        vol_score = min(50.0, range_pct * 2.0) * 1.5
            except (TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] 최근 가격 변동 범위로 유동성 근사: %s", exc, exc_info=True)

            total_score = closeness + capital + rsi_score + vol_score
            scored.append({**coin, "score": round(total_score, 1), "rsi_score": rsi_score})

        # 신규 excluded 업데이트 (중복 없이)
        ex_markets = {e["market"] for e in self.excluded}
        for e in new_excluded:
            if e["market"] not in ex_markets:
                self.excluded.append(e)
                ex_markets.add(e["market"])

        if not scored:
            return None

        scored.sort(key=lambda x: x["score"], reverse=True)
        best = scored[0]
        logger.info("[TriageManager] TARGET selected: %s score=%.1f pnl=%.2f%%",
                    best["market"], best["score"], best["pnl_pct"])
        return best

    # ============================================================
    # DCA 시작
    # ============================================================

    def start_recovery(self, target: Dict[str, Any], system: Any = None) -> None:
        """선정된 코인에 대해 DCA 복구 계획 수립 → active_targets에 추가."""
        new_target = {
            "market": target["market"],
            "state": "DCA",
            "started_ts": time.time(),
            "original_avg_buy": target["avg_buy"],
            "original_qty": target["qty"],
            "original_pnl_pct": target["pnl_pct"],
            "dca_splits_total": self._calc_splits(
                target["pnl_pct"],
                ctx=system.coordinator.get_context(target["market"]) if system else None,
                system=system,
            ),
            "dca_splits_done": 0,
            "dca_invested": 0.0,
            "dca_confirmed_funds": 0.0,
            "last_dca_ts": 0.0,
            "sell_submitted_ts": 0.0,
            "score": target.get("score", 0.0),
        }
        self.active_targets.append(new_target)
        self.state = self.STATE_DCA
        self.save_state()

        try:
            if system:
                system.ledger.append(
                    "TRIAGE_TARGET_SELECTED",
                    market=target["market"],
                    score=target.get("score", 0),
                    pnl_pct=target["pnl_pct"],
                    concurrent=len(self.active_targets),
                )
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[TRIAGE] start_recovery fallback: %s", exc, exc_info=True)
        logger.info("[TriageManager] START_RECOVERY market=%s splits=%d concurrent=%d",
                    target["market"], new_target["dca_splits_total"], len(self.active_targets))

    def _calc_splits(self, pnl_pct: float, ctx: Any = None, system: Any = None) -> int:
        """손실 깊이 + 시장 상황 기반 DCA 분할 횟수 결정.

        기본: 손실 깊이 → 2/3/4
        동적 조정: RSI 과매도 → +1, 고변동성 → +1, BTC 하락 → +1
        범위: 2~6 (분할 많을수록 보수적)
        """
        # 기본 분할
        if pnl_pct > -5:
            base = 2
        elif pnl_pct > -10:
            base = 3
        else:
            base = 4

        adjust = 0
        try:
            from app.strategy import indicators
            tick_prices = getattr(ctx, "_tick_prices", None) if ctx else None
            if tick_prices and len(tick_prices) >= 15:
                rsi_val = indicators.rsi(tick_prices, 14)
                vol = indicators.volatility(tick_prices, 14)

                # RSI 과매도(< 30): 반등 기대 높음 → 분할 줄여 초기 비중 확대
                if rsi_val is not None and rsi_val < 30:
                    adjust -= 1

                # 고변동성(> 8%): 추가 하락 가능 → 분할 늘려 보수적
                if vol is not None and vol > 8.0:
                    adjust += 1

            # BTC 하락 추세: 전체 시장 약세 → 분할 늘려 보수적
            if system and getattr(system, "btc_guard_mode", False):
                adjust += 1
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[TRIAGE] BTC 하락 추세: 전체 시장 약세 → 분할 늘려 보수적: %s", exc, exc_info=True)

        return max(2, min(6, base + adjust))

    # ============================================================
    # DCA 실행 (system에서 bg_executor로 오프로드)
    # ============================================================

    def execute_dca_step(self, target_or_system, system=None) -> None:
        """DCA 분할 매수 한 스텝 실행 (executor에서 호출).

        중복 방지는 order_fsm의 ctx.order_state 체크가 담당.
        하위 호환: execute_dca_step(system) → current_target 사용
        """
        if system is None:
            system = target_or_system
            target = self.current_target
            if target is None:
                return
        else:
            target = target_or_system
        now = time.time()
        interval = float(self.settings["dca_interval_sec"])
        if (now - target.get("last_dca_ts", 0.0)) < interval:
            return

        market = target["market"]
        splits_total = target["dca_splits_total"]
        splits_done = target["dca_splits_done"]

        if splits_done >= splits_total:
            # DCA 완료 → WAIT 상태로 전환
            target["state"] = "WAIT"
            self._update_display_state()
            self.save_state()
            logger.info("[TriageManager] DCA complete → WAIT market=%s", market)
            return

        # 예산 계산
        try:
            from app.core.hyper_price_store import price_store
            ctx = system.coordinator.get_context(market)
            if not ctx or not getattr(ctx, "position", None):
                return

            qty = float(ctx.position.get("qty", 0.0) or 0.0)
            avg_buy = float(ctx.position.get("entry", 0.0) or 0.0)
            _raw_price = price_store.get_price(market)
            if _raw_price is None or float(_raw_price or 0) <= 0:
                logger.debug("[TriageManager] DCA skip: price unavailable for %s", market)
                return
            current_price = float(_raw_price)
            if avg_buy <= 0 or qty <= 0:
                return

            current_invested = avg_buy * qty
            max_additional = current_invested * float(self.settings["max_dca_ratio"])
            already_invested = target["dca_invested"]
            remaining_budget = max(0.0, max_additional - already_invested)

            # ★ 글로벌 DCA 캡: 전체 포트폴리오의 N%까지만 DCA 허용 (업비트 동기화 2026-04-05)
            global_cap_pct = float(self.settings.get("global_dca_cap_pct", 30.0))
            total_equity = float(getattr(system, "_last_equity_usdt", 0.0) or getattr(system, "_last_cash_usdt", 0.0) or 0.0)
            if total_equity > 0:
                global_cap = total_equity * (global_cap_pct / 100.0)
                total_dca_all = sum(t.get("dca_invested", 0.0) for t in self.active_targets)
                if total_dca_all >= global_cap:
                    logger.info("[TriageManager] DCA GLOBAL CAP reached: total_dca=%.0f >= cap=%.0f (%.0f%%) market=%s",
                                total_dca_all, global_cap, global_cap_pct, market)
                    return
                remaining_budget = min(remaining_budget, global_cap - total_dca_all)

            remaining_splits = splits_total - splits_done
            per_split = remaining_budget / max(1, remaining_splits)

            # 동적 예산 조정: RSI 과매도 시 비중 확대, BTC 하락 시 축소
            try:
                from app.strategy import indicators
                tick_prices = getattr(ctx, "_tick_prices", None)
                if tick_prices and len(tick_prices) >= 15:
                    rsi_val = indicators.rsi(tick_prices, 14)
                    if rsi_val is not None:
                        if rsi_val < 30:       # 강한 과매도 → 20% 확대
                            per_split *= 1.2
                        elif rsi_val > 60:     # 반등 진행 중 → 15% 축소
                            per_split *= 0.85
                if getattr(system, "btc_guard_mode", False):
                    per_split *= 0.7           # BTC 하락 추세 → 30% 축소
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[TRIAGE] 동적 예산 조정: RSI 과매도 시 비중 확대, BTC 하락 시 축소: %s", exc, exc_info=True)

            # 최소 주문 체크
            min_order = float(getattr(system, "min_order_usdt", 5.0))
            if per_split < min_order:
                logger.info("[TriageManager] DCA skip: per_split=%.0f < min_order=%.0f market=%s",
                            per_split, min_order, market)
                # 자본 부족 → WAIT으로 전환
                target["state"] = "WAIT"
                self._update_display_state()
                self.save_state()
                return

            # 가용 현금 체크
            avail_usdt = float(getattr(system, "_last_cash_usdt", 0.0) or 0.0)
            per_split = min(per_split, avail_usdt * 0.95)
            if per_split < min_order:
                logger.info("[TriageManager] DCA skip: insufficient cash avail=%.0f market=%s", avail_usdt, market)
                return

            target["last_dca_ts"] = now

            ok, msg = system.order_fsm.submit_market_buy(
                ctx=ctx,
                market=market,
                usdt_amount=per_split,
                expected_price=current_price,
                reason="triage:dca",
            )

            if ok:
                target["dca_splits_done"] += 1
                target["dca_invested"] += per_split
                self.save_state()
                try:
                    system.ledger.append(
                        "TRIAGE_DCA_STEP",
                        market=market,
                        split=target["dca_splits_done"],
                        of=splits_total,
                        usdt=round(per_split),
                        current_price=round(current_price, 2),
                        note="ack_not_fill",
                    )
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[TRIAGE] 가용 현금 체크: %s", exc, exc_info=True)
                logger.info("[TriageManager] DCA step %d/%d market=%s usdt=%.0f (ACK, fill pending)",
                            target["dca_splits_done"], splits_total, market, per_split)
                try:
                    reserved = self.calc_reserved_capital(system)
                    system._triage_reserved_usdt = reserved
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[TRIAGE] 가용 현금 체크: %s", exc, exc_info=True)
            else:
                logger.warning("[TriageManager] DCA submit failed market=%s: %s", market, msg)

        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.error("[TriageManager] execute_dca_step error market=%s: %s", market, exc)

    # ============================================================
    # 매도 조건 체크
    # ============================================================

    def check_sell_condition(self, target_or_system, system=None) -> Tuple[bool, str]:
        """DCA 후 신규 평균가 기준으로 본전+α 도달 여부 확인."""
        if system is None:
            system = target_or_system
            target = self.current_target
            if target is None:
                return False, "no_target"
        else:
            target = target_or_system
        market = target["market"]
        try:
            from app.core.hyper_price_store import price_store
            ctx = system.coordinator.get_context(market)
            if not ctx or not getattr(ctx, "position", None):
                return False, "no_position"

            # DCA 후 신규 평균가 (거래소 reconcile로 업데이트됨)
            avg_buy = float(ctx.position.get("entry", 0.0) or 0.0)
            current_price = price_store.get_price(market)
            if current_price is None:
                return False, "price_not_ready"
            if avg_buy <= 0 or current_price <= 0:
                return False, "invalid_price"

            # 목표: (현재가 - DCA 후 평균가) / DCA 후 평균가 >= profit_target + fee
            # 긴급 탈출: 시장 상황에 따라 profit_target을 동적으로 낮춤
            effective_target, severity = self.get_effective_profit_target(system)
            self._last_emergency_info = {"target_pct": effective_target, "severity": severity}
            target_pct = effective_target + float(self.settings["fee_pct"])
            pnl_pct = (current_price - avg_buy) / avg_buy * 100

            if pnl_pct >= target_pct:
                return True, f"target_reached: {pnl_pct:.2f}% >= {target_pct:.2f}% ({severity})"
            return False, f"waiting: {pnl_pct:.2f}% < {target_pct:.2f}% ({severity})"

        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("TriageManager.check_sell_condition except: %s", exc, exc_info=True)
            return False, f"error: {exc}"

    def is_coin_timeout(self, target=None) -> bool:
        """해당 코인 복구 시도 시간 초과 여부."""
        if target is None:
            target = self.current_target
            if target is None:
                return False
        elapsed = time.time() - target.get("started_ts", time.time())
        timeout_sec = float(self.settings["coin_timeout_hours"]) * 3600
        return elapsed > timeout_sec

    # ============================================================
    # 매도 실행
    # ============================================================

    def execute_sell(self, target_or_system, system=None) -> None:
        """타겟 마켓 전량 시장가 매도."""
        if system is None:
            system = target_or_system
            target = self.current_target
            if target is None:
                return
        else:
            target = target_or_system
        market = target["market"]
        try:
            from app.core.hyper_price_store import price_store
            ctx = system.coordinator.get_context(market)
            if not ctx or not getattr(ctx, "position", None):
                logger.warning("[TriageManager] execute_sell: no position market=%s", market)
                return

            qty = float(ctx.position.get("qty", 0.0) or 0.0)
            expected_price = price_store.get_price(market) or float(
                ctx.position.get("entry", 0.0) or 0.0
            )
            if qty <= 0:
                logger.warning("[TriageManager] execute_sell: qty=0 market=%s", market)
                self._remove_target(target, system, recovered=False)
                return

            avg_buy = float(ctx.position.get("entry", 0.0) or 0.0)
            target["sell_snapshot_qty"] = qty
            target["sell_snapshot_avg"] = avg_buy

            ok, msg = system.order_fsm.submit_market_sell(
                ctx=ctx,
                market=market,
                qty=qty,
                expected_price=expected_price,
                reason="triage:tp_hit",
            )

            if ok:
                target["state"] = "SELL"
                target["sell_submitted_ts"] = time.time()
                self._update_display_state()
                self.save_state()
                logger.info("[TriageManager] SELL submitted market=%s qty=%.6f avg=%.2f", market, qty, avg_buy)
            else:
                logger.warning("[TriageManager] SELL failed market=%s: %s", market, msg)
                try:
                    system.ledger.append("TRIAGE_SELL_FAILED", market=market, error=str(msg))
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[TRIAGE] triage_manager fallback: %s", exc, exc_info=True)
                target["state"] = "WAIT"
                self._update_display_state()
                self.save_state()
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            logger.error("[TriageManager] execute_sell error market=%s: %s", market, exc)
            target["state"] = "WAIT"
            self._update_display_state()
            self.save_state()

    # ============================================================
    # 매도 완료 감지
    # ============================================================

    def is_sell_complete(self, target_or_system, system=None) -> bool:
        """매도 완료 여부: position qty ≈ 0 이거나 5분 타임아웃."""
        if system is None:
            system = target_or_system
            target = self.current_target
            if target is None:
                return False
        else:
            target = target_or_system
        market = target["market"]

        # 타임아웃 체크 (부분 체결 등으로 무한 대기 방지)
        sell_timeout = float(self.settings.get("sell_timeout_sec", 300.0))
        if (time.time() - target.get("sell_submitted_ts", 0.0)) > sell_timeout:
            # 잔량 확인 후 재매도 시도 (1회만)
            try:
                ctx = system.coordinator.get_context(market)
                remaining_qty = float(ctx.position.get("qty", 0.0) or 0.0) if ctx and getattr(ctx, "position", None) else 0.0
                if remaining_qty > 1e-8 and not target.get("_sell_retry_done", False):
                    target["_sell_retry_done"] = True
                    logger.warning("[TriageManager] SELL timeout market=%s remaining=%.6f — retrying sell", market, remaining_qty)
                    try:
                        from app.core.hyper_price_store import price_store
                        expected_price = price_store.get_price(market) or float(ctx.position.get("entry", 0.0) or 0.0)
                        system.order_fsm.submit_market_sell(
                            ctx=ctx, market=market, qty=remaining_qty,
                            expected_price=expected_price, reason="triage:tp_hit_retry",
                        )
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[TRIAGE] 잔량 확인 후 재매도 시도 (1회만): %s", exc, exc_info=True)
                    target["sell_submitted_ts"] = time.time()
                    return False
            except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                logger.warning("[TRIAGE] 잔량 확인 후 재매도 시도 (1회만): %s", exc, exc_info=True)
            logger.warning("[TriageManager] SELL timeout market=%s — forcing advance", market)
            target["_sell_retry_done"] = False
            return True

        # position qty 체크
        try:
            ctx = system.coordinator.get_context(market)
            if not ctx or not getattr(ctx, "position", None):
                return True
            qty = float(ctx.position.get("qty", 0.0) or 0.0)
            return qty < 1e-8
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("TriageManager.is_sell_complete suppressed exception", exc_info=True)
            return False

    # ============================================================
    # 복구 완료 처리
    # ============================================================

    def on_recovery_complete(self, target_or_system, system=None) -> None:
        """마켓 복구 완료 → recovered 추가, active_targets에서 제거."""
        if system is None:
            system = target_or_system
            target = self.current_target
            if target is None:
                return
        else:
            target = target_or_system
        market = target["market"]

        # 수익 계산: execute_sell() 시점 스냅샷 우선, fallback 순서
        profit_usdt = 0.0
        try:
            from app.core.hyper_price_store import price_store
            sell_price = price_store.get_price(market) or 0.0

            actual_avg = float(target.get("sell_snapshot_avg", 0.0) or 0.0)
            actual_qty = float(target.get("sell_snapshot_qty", 0.0) or 0.0)

            if actual_avg <= 0 or actual_qty <= 0:
                ctx = system.coordinator.get_context(market) if system else None
                if ctx and getattr(ctx, "position", None):
                    actual_avg = float(ctx.position.get("entry", 0.0) or 0.0)
                    actual_qty = float(ctx.position.get("qty", 0.0) or 0.0)

            if actual_avg <= 0 or actual_qty <= 0:
                actual_avg = target.get("original_avg_buy", 0.0)
                actual_qty = target.get("original_qty", 0.0)

            if sell_price > 0 and actual_avg > 0 and actual_qty > 0:
                total_cost = actual_avg * actual_qty
                total_revenue = sell_price * actual_qty
                fee = (total_cost + total_revenue) * 0.001
                profit_usdt = total_revenue - total_cost - fee
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[TRIAGE] 수익 계산: execute_sell() 시점 스냅샷 우선, fallback 순서: %s", exc, exc_info=True)

        self.recovered.append({
            "market": market,
            "recovered_ts": time.time(),
            "profit_usdt": round(profit_usdt, 2),
            "dca_invested": target.get("dca_invested", 0.0),
        })
        self._remove_target_from_list(target)
        self._update_display_state()
        self.save_state()

        try:
            system.ledger.append(
                "TRIAGE_RECOVERED",
                market=market,
                profit_usdt=round(profit_usdt, 2),
                total_recovered=len(self.recovered),
                remaining_targets=len(self.active_targets),
            )
        except (TypeError, ValueError) as exc:
            logger.warning("[TRIAGE] triage_manager fallback: %s", exc, exc_info=True)

        if self.settings.get("notify"):
            try:
                system._send_telegram_safe(
                    f"✅ [TRIAGE] {market} 복구 완료!\n"
                    f"복구 수익(추산): {profit_usdt:+,.2f} USDT\n"
                    f"완료: {len(self.recovered)}건 / 진행중: {len(self.active_targets)}건"
                )
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] triage_manager fallback: %s", exc, exc_info=True)

        logger.info("[TriageManager] RECOVERED market=%s profit≈%.0f remaining=%d",
                    market, profit_usdt, len(self.active_targets))

    # ============================================================
    # 스킵
    # ============================================================

    def skip_target(self, target: Dict[str, Any], system: Any, reason: str = "manual") -> None:
        """복구 대상 건너뜀 → active_targets에서 제거."""
        market = target["market"]
        self.skipped.append({"market": market, "reason": reason, "ts": time.time()})
        self._remove_target_from_list(target)
        self._update_display_state()
        self.save_state()

        try:
            system.ledger.append("TRIAGE_SKIPPED", market=market, reason=reason,
                                 remaining_targets=len(self.active_targets))
        except (AttributeError, TypeError) as exc:
            logger.warning("[TRIAGE] skip_target fallback: %s", exc, exc_info=True)
        logger.info("[TriageManager] SKIPPED market=%s reason=%s remaining=%d",
                    market, reason, len(self.active_targets))

    def skip_current_target(self, system: Any, reason: str = "manual") -> None:
        """하위 호환: 첫 번째 active target 스킵."""
        if self.active_targets:
            self.skip_target(self.active_targets[0], system, reason)

    def _remove_target_from_list(self, target: Dict[str, Any]) -> None:
        """active_targets에서 타겟 제거 (참조 비교)."""
        self.active_targets = [t for t in self.active_targets if t is not target]

    def _remove_target(self, target: Dict[str, Any], system: Any, recovered: bool) -> None:
        if recovered:
            self.on_recovery_complete(target, system)
        else:
            self._remove_target_from_list(target)
            self._update_display_state()
            self.save_state()

    def _update_display_state(self) -> None:
        """active_targets 상태로 글로벌 display state 갱신."""
        if not self.active_targets:
            if self.is_active():
                self.state = self.STATE_SCAN
            return
        # 우선순위: SELL > WAIT > DCA
        states = {t.get("state", "DCA") for t in self.active_targets}
        if "SELL" in states:
            self.state = self.STATE_SELL
        elif "WAIT" in states:
            self.state = self.STATE_WAIT
        else:
            self.state = self.STATE_DCA

    # ============================================================
    # 종료 조건
    # ============================================================

    def should_exit_triage(self, system: Optional[Any] = None) -> Tuple[bool, str]:
        """트리아지 종료 여부 판단 (4가지 조건).

        [FIX 2026-03-22] 즉시 종료 버그 3건 수정 (2차 리뷰):
          1) min_duration_guard: 60초 미만에서는 종료 불가
             - 이전: 진입 후 5초 만에 poll() 첫 호출 시 즉시 종료 가능했음
          2) ALL 조건: initial_count=0이면 스킵
             - 이전: 진입 직후 가격 미확보 → loss_coin_count=0 →
                     _count_remaining_loss_coins()=0 → "all_recovered" 즉시 반환
          3) PnL 조건: initial_pnl < exit_pnl일 때만 체크
             - 이전: prm.daily_status.loss_pct(0.0) >= exit_pnl(-2.0) →
                     진입 당시 손실이 없어도 "pnl_recovered" 즉시 반환
        """
        elapsed = time.time() - self.start_ts

        # [FIX] 최소 60초 보호 — 진입 직후 오판 방지
        if elapsed < 60:
            return False, f"min_duration_guard: {elapsed:.0f}s < 60s"

        # 조건 1: 복구 목표 달성
        target_str = str(self.settings.get("recovery_target", "ALL")).strip()
        initial_count = self.initial_snapshot.get("loss_coin_count", 0)
        recovered_count = len(self.recovered)

        if target_str == "ALL":
            # [FIX] initial_count=0이면 스킵 — 스냅샷이 미확정 상태
            # (_handle_scan에서 첫 타겟 발견 시 보정됨)
            if initial_count > 0:
                loss_coins_remaining = self._count_remaining_loss_coins(system)
                if loss_coins_remaining == 0:
                    return True, f"all_recovered: {recovered_count} recovered"
        elif target_str.replace(".", "").isdigit():
            # [FIX 2026-03-24] "."포함 → 비율(0.0~1.0), 정수 → 개수
            # "0.8" → 80% 복구, "3" → 3개 복구, "1.0" → 100% 복구, "1" → 1개 복구
            if "." in target_str:
                val = max(0.0, min(1.0, float(target_str)))
                if initial_count > 0 and recovered_count / initial_count >= val:
                    return True, f"ratio_target: {recovered_count}/{initial_count} >= {val:.0%}"
            else:
                val = int(float(target_str))
                if recovered_count >= val:
                    return True, f"count_target: {recovered_count}>={val}"

        # 조건 2: 전체 PnL 회복
        # [FIX] 진입 당시 PnL이 exit_pnl보다 나빴을 때만 체크
        # (진입 시 0.0%였는데 0.0% >= -2.0% 즉시 참이 되던 버그 수정)
        if system:
            prm = getattr(system, "portfolio_risk_manager", None)
            if prm and getattr(prm, "daily_status", None):
                exit_pnl = float(self.settings["exit_pnl_pct"])
                initial_pnl = self.initial_snapshot.get("total_pnl_pct", 0.0)
                current_pnl = prm.daily_status.loss_pct
                if initial_pnl < exit_pnl and current_pnl >= exit_pnl:
                    return True, f"pnl_recovered: {current_pnl:.2f}% >= {exit_pnl}%"

        # 조건 3: 시장 회복 자동 해제
        # BTC Guard OFF(회복) + 총 PnL이 진입 시점보다 개선 + 최소 경과 시간
        # 전체 시장이 동반 하락 → 트리아지 진입 → 시장 회복 시 자동 해제하여 정상 매매 재개
        if self.settings.get("market_recovery_exit_enabled", True) and system:
            _min_h = float(self.settings.get("market_recovery_min_hours", 2.0))
            if elapsed >= _min_h * 3600:
                _btc_guard_now = bool(getattr(system, "btc_guard_mode", False))
                if not _btc_guard_now:
                    # BTC Guard OFF 확인 — PnL 개선도 확인
                    _prm = getattr(system, "portfolio_risk_manager", None)
                    _cur_pnl = (_prm.daily_status.loss_pct
                                if _prm and getattr(_prm, "daily_status", None) else None)
                    _entry_pnl = self.initial_snapshot.get("total_pnl_pct")
                    if _cur_pnl is not None and _entry_pnl is not None:
                        if _cur_pnl > _entry_pnl:
                            return True, (
                                f"market_recovery: btc_guard=OFF, "
                                f"pnl={_cur_pnl:.2f}% > entry={_entry_pnl:.2f}%, "
                                f"elapsed={elapsed/3600:.1f}h"
                            )

        # 조건 4: 최대 지속 시간 초과
        max_hours = float(self.settings["max_duration_hours"])
        if elapsed > max_hours * 3600:
            return True, f"max_duration: {elapsed/3600:.1f}h > {max_hours}h"

        return False, f"continuing: recovered={recovered_count}, initial={initial_count}"

    def _count_remaining_loss_coins(self, system: Optional[Any]) -> int:
        """현재 손실 코인 수 (recovered + skipped 제외)."""
        if not system:
            return 1  # 알 수 없으면 계속
        done = {r["market"] for r in self.recovered} | {s["market"] for s in self.skipped}
        loss_coins = self._gather_loss_coins(system)
        return sum(1 for c in loss_coins if c["market"] not in done)

    # ============================================================
    # 트리아지 종료
    # ============================================================

    def exit_triage(self, system: Any, reason: str) -> None:
        """트리아지 모드 종료 → 정상 복귀."""
        elapsed_hours = (time.time() - self.start_ts) / 3600

        # 성과 측정
        exit_equity = float(getattr(system, "_last_equity_usdt", 0.0) or 0.0)
        entry_equity = self._entry_equity_usdt or self.initial_snapshot.get("entry_equity_usdt", 0.0)
        equity_change = exit_equity - entry_equity if entry_equity > 0 else 0.0
        equity_change_pct = (equity_change / entry_equity * 100) if entry_equity > 0 else 0.0
        total_profit = sum(r.get("profit_usdt", 0) for r in self.recovered)
        total_dca_invested = sum(r.get("dca_invested", 0) for r in self.recovered)

        self.state = self.STATE_NORMAL
        self.active_targets = []  # 모든 진행 중 타겟 정리
        system._triage_entry_blocked = False
        system._triage_reserved_usdt = 0.0  # 자본 예약 해제
        self.save_state()

        # 세션 히스토리 기록 (runtime/triage_history.jsonl)
        self._save_session_history(
            reason=reason,
            elapsed_hours=elapsed_hours,
            entry_equity=entry_equity,
            exit_equity=exit_equity,
            equity_change=equity_change,
            equity_change_pct=equity_change_pct,
            total_profit=total_profit,
            total_dca_invested=total_dca_invested,
        )

        try:
            system.ledger.append(
                "TRIAGE_EXITED",
                reason=reason,
                recovered_count=len(self.recovered),
                skipped_count=len(self.skipped),
                elapsed_hours=round(elapsed_hours, 2),
                entry_equity=round(entry_equity),
                exit_equity=round(exit_equity),
                equity_change=round(equity_change),
                equity_change_pct=round(equity_change_pct, 2),
            )
        except (TypeError, ValueError) as exc:
            logger.warning("[TRIAGE] session history record: %s", exc, exc_info=True)

        if self.settings.get("notify"):
            try:
                system._send_telegram_safe(
                    f"🎉 [TRIAGE] 정상 모드 복귀\n"
                    f"사유: {reason}\n"
                    f"복구: {len(self.recovered)}건 / 스킵: {len(self.skipped)}건\n"
                    f"추산 수익: {total_profit:+,.2f} USDT\n"
                    f"자산 변동: {equity_change:+,.2f} USDT ({equity_change_pct:+.2f}%)\n"
                    f"DCA 투입: {total_dca_invested:,.0f}원\n"
                    f"소요: {elapsed_hours:.1f}시간"
                )
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] telegram notify on exit: %s", exc, exc_info=True)

        logger.info("[TriageManager] EXITED reason=%s recovered=%d equity_change=%.0f(%.2f%%) elapsed=%.1fh",
                    reason, len(self.recovered), equity_change, equity_change_pct, elapsed_hours)

    # ============================================================
    # 상태 조회
    # ============================================================

    def is_active(self) -> bool:
        """트리아지 활성 여부 (NORMAL 아닌 모든 상태)."""
        return self.state != self.STATE_NORMAL

    def get_status_dict(self) -> Dict[str, Any]:
        """API 응답용 상태 딕셔너리."""
        elapsed_sec = (time.time() - self.start_ts) if self.start_ts else 0.0
        elapsed_hours = elapsed_sec / 3600
        return {
            "enabled": bool(self.settings.get("enabled", False)),
            "state": self.state,
            "active": self.is_active(),
            "started_at": self.start_ts or None,
            "elapsed_sec": round(elapsed_sec, 1),
            "elapsed_hours": round(elapsed_hours, 2),
            "trigger_reason": self.trigger_reason,
            "initial_snapshot": self.initial_snapshot,
            "current_target": self.current_target,  # 하위 호환 (첫 번째 타겟)
            "active_targets": self.active_targets,   # 병렬 복구 전체 목록
            "recovered": self.recovered,
            "skipped": self.skipped,
            "excluded": self.excluded,
            "recovered_count": len(self.recovered),
            "skipped_count": len(self.skipped),
            "active_target_count": len(self.active_targets),
            "emergency_exit": getattr(self, "_last_emergency_info", None),
            "settings": {k: v for k, v in self.settings.items() if k != "fee_pct"},
        }

    # ============================================================
    # 메인 상태머신 폴 (tick_loop에서 bg_executor로 호출)
    # ============================================================

    def poll(self, system: Any) -> None:
        """
        tick_loop에서 주기적으로 호출되는 상태머신 디스패처.

        병렬 복구: active_targets 각각의 state(DCA/WAIT/SELL)를 처리하고,
        빈 슬롯이 있으면 SCAN으로 추가 타겟을 배정한다.
        """
        try:
            # 자동 진입 (NORMAL 상태 + enabled + 아직 진입 전)
            if self.state == self.STATE_NORMAL:
                if self.settings.get("enabled"):
                    ok, reason = self.should_enter(system)
                    if ok:
                        self.enter_triage(system, reason)
                return

            # 종료 조건 체크 (모든 활성 상태에서)
            should_exit, exit_reason = self.should_exit_triage(system)
            if should_exit:
                self.exit_triage(system, reason=exit_reason)
                return

            # 자본 예약 갱신 (매 poll 주기)
            try:
                system._triage_reserved_usdt = self.calc_reserved_capital(system)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[TRIAGE] reserved capital refresh: %s", exc, exc_info=True)

            # 각 active target 처리 (copy로 순회 — 처리 중 제거 가능)
            for target in list(self.active_targets):
                tstate = target.get("state", "DCA")
                if tstate == "DCA":
                    self._handle_target_dca(target, system)
                elif tstate == "WAIT":
                    self._handle_target_wait(target, system)
                elif tstate == "SELL":
                    self._handle_target_sell(target, system)

            # 빈 슬롯 채우기 (SCAN)
            max_targets = int(self.settings.get("max_concurrent_targets", 3))
            self._fill_target_slots(max_targets, system)

            # active_targets가 비고 더 이상 타겟도 없으면 종료 체크
            if not self.active_targets:
                # should_exit_triage에서 all_recovered 등으로 처리됨 — 다음 poll에서 종료
                pass

        except Exception as exc:
            now = time.time()
            if (now - getattr(self, "_last_poll_error_ts", 0.0)) >= 60.0:
                self._last_poll_error_ts = now
                logger.error("[TriageManager] poll error state=%s: %s", self.state, exc, exc_info=True)
                try:
                    system.ledger.append("TRIAGE_POLL_ERROR", state=self.state, error=str(exc))
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[TRIAGE] poll error ledger append: %s", exc, exc_info=True)

    def _fill_target_slots(self, max_targets: int, system: Any) -> None:
        """빈 슬롯을 SCAN으로 채운다."""
        while len(self.active_targets) < max_targets:
            prev_count = len(self.active_targets)
            target = self.select_recovery_target(system)
            if target is None:
                # 타겟 없음
                if not self.active_targets:
                    no_target_count = getattr(self, "_no_target_count", 0) + 1
                    self._no_target_count = no_target_count
                    elapsed = time.time() - self.start_ts
                    if no_target_count >= 24 or elapsed > float(self.settings.get("max_duration_hours", 168)) * 3600:
                        logger.info("[TriageManager] SCAN: no eligible target after %d retries → exiting", no_target_count)
                        self.exit_triage(system, reason="no_eligible_target")
                    else:
                        logger.debug("[TriageManager] SCAN: no eligible target, retry %d/24", no_target_count)
                break
            else:
                self._no_target_count = 0
                # 첫 타겟 발견 시 스냅샷 보정
                if self.initial_snapshot.get("loss_coin_count", 0) == 0:
                    loss_coins = self._gather_loss_coins(system)
                    self.initial_snapshot["loss_coin_count"] = len(loss_coins)
                    self.initial_snapshot["loss_coins"] = [c["market"] for c in loss_coins]
                    logger.info("[TriageManager] initial_snapshot 보정: loss_coin_count=%d", len(loss_coins))
                    self.save_state()
                self.start_recovery(target, system=system)
                logger.info("[TriageManager] SCAN → DCA: %s (concurrent=%d)",
                            target["market"], len(self.active_targets))
                # 안전장치: start_recovery가 실제로 타겟을 추가하지 않았으면 무한루프 방지
                if len(self.active_targets) <= prev_count:
                    break

    def _handle_target_dca(self, target: Dict[str, Any], system: Any) -> None:
        """DCA 스텝 실행."""
        splits_total = target.get("dca_splits_total", 1)
        splits_done = target.get("dca_splits_done", 0)
        if splits_done < splits_total:
            self.execute_dca_step(target, system)
        else:
            target["state"] = "WAIT"
            self._update_display_state()
            self.save_state()

    def _handle_target_wait(self, target: Dict[str, Any], system: Any) -> None:
        """목표 수익률 도달 감지."""
        market = target.get("market", "?")

        # 포지션이 외부에 의해 소멸된 경우 자동 스킵
        try:
            ctx = system.coordinator.get_context(market)
            if ctx is not None and not getattr(ctx, "position", None):
                logger.info("[TriageManager] WAIT → SKIP: %s position externally cleared", market)
                self.skip_target(target, system, reason="position_externally_cleared")
                return
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[TRIAGE] position externally cleared check: %s", exc, exc_info=True)

        reached, reason = self.check_sell_condition(target, system)
        if reached:
            logger.info("[TriageManager] WAIT → SELL: %s (%s)", market, reason)
            self.execute_sell(target, system)
        elif self.is_coin_timeout(target):
            dca_invested = target.get("dca_invested", 0.0)
            logger.warning("[TriageManager] WAIT timeout → hold(skip): %s dca_invested=%.0f",
                           market, dca_invested)
            self.skip_target(target, system, reason=f"coin_timeout_hold(dca={dca_invested:.0f})")

    def _handle_target_sell(self, target: Dict[str, Any], system: Any) -> None:
        """매도 완료 감지."""
        if self.is_sell_complete(target, system):
            market = target.get("market", "?")
            logger.info("[TriageManager] SELL complete → RECOVERED: %s", market)
            self.on_recovery_complete(target, system)

    # ============================================================
    # 하위 호환: 이전 _handle_* 메서드 (테스트 및 외부 코드용)
    # ============================================================

    def _handle_scan(self, system: Any) -> None:
        """하위 호환: _fill_target_slots + 첫 타겟 처리."""
        max_targets = int(self.settings.get("max_concurrent_targets", 3))
        self._fill_target_slots(max_targets, system)

    def _handle_dca(self, system: Any) -> None:
        """하위 호환: current_target의 DCA 처리."""
        target = self.current_target
        if target is None:
            self.state = self.STATE_SCAN
            return
        self._handle_target_dca(target, system)

    def _handle_wait(self, system: Any) -> None:
        """하위 호환: current_target의 WAIT 처리."""
        target = self.current_target
        if target is None:
            self.state = self.STATE_SCAN
            return
        self._handle_target_wait(target, system)

    def _handle_sell(self, system: Any) -> None:
        """하위 호환: current_target의 SELL 처리."""
        target = self.current_target
        if target is None:
            self.state = self.STATE_SCAN
            return
        self._handle_target_sell(target, system)

    # ============================================================
    # 세션 히스토리 (성과 측정)
    # ============================================================

    def _save_session_history(self, **kwargs) -> None:
        """트리아지 세션 결과를 runtime/triage_history.jsonl에 추가."""
        try:
            history_path = Path("runtime/triage_history.jsonl")
            history_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": time.time(),
                "start_ts": self.start_ts,
                "trigger_reason": self.trigger_reason,
                "recovered_count": len(self.recovered),
                "skipped_count": len(self.skipped),
                "excluded_count": len(self.excluded),
                "recovered_markets": [r["market"] for r in self.recovered],
                "skipped_markets": [s["market"] for s in self.skipped],
                **kwargs,
            }
            with open(history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            # 파일 크기 제한 (1MB 초과 시 앞부분 삭제)
            try:
                if history_path.stat().st_size > 1_000_000:
                    lines = history_path.read_text(encoding="utf-8").strip().split("\n")
                    history_path.write_text("\n".join(lines[-100:]) + "\n", encoding="utf-8")
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("[TRIAGE] history file truncation: %s", exc, exc_info=True)
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[TriageManager] _save_session_history error: %s", exc)

    # ============================================================
    # 영속화
    # ============================================================

    def save_state(self) -> None:
        """runtime/triage_state.json에 원자적 저장."""
        try:
            path = Path(self.settings.get("state_path", "runtime/triage_state.json"))
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "state": self.state,
                "start_ts": self.start_ts,
                "trigger_reason": self.trigger_reason,
                "active_targets": self.active_targets,
                "recovered": self.recovered,
                "skipped": self.skipped,
                "excluded": self.excluded,
                "initial_snapshot": self.initial_snapshot,
                "settings": self.settings,
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.error("[TriageManager] save_state failed: %s", exc)

    def load_state(self) -> None:
        """triage_state.json 복원 (재시작 후 상태 유지)."""
        path = Path(self.settings.get("state_path", "runtime/triage_state.json"))
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.state = data.get("state", self.STATE_NORMAL)
            self.start_ts = float(data.get("start_ts", 0.0))
            self.trigger_reason = data.get("trigger_reason", "")

            # 하위 호환: 이전 포맷의 current_target → active_targets 변환
            if "active_targets" in data:
                self.active_targets = data["active_targets"] or []
            elif data.get("current_target"):
                old_target = data["current_target"]
                # 이전 포맷 → 새 포맷 변환: state 필드 추가
                if "state" not in old_target:
                    # 글로벌 state에서 per-target state 유추
                    _gs = data.get("state", "")
                    if "SELL" in _gs:
                        old_target["state"] = "SELL"
                    elif "WAIT" in _gs:
                        old_target["state"] = "WAIT"
                    else:
                        old_target["state"] = "DCA"
                    # per-target 타이밍 필드 추가
                    old_target.setdefault("last_dca_ts", 0.0)
                    old_target.setdefault("sell_submitted_ts", 0.0)
                    old_target.setdefault("dca_confirmed_funds", 0.0)
                self.active_targets = [old_target]
                logger.info("[TriageManager] migrated current_target → active_targets[0]")
            else:
                self.active_targets = []

            self.recovered = data.get("recovered", [])
            self.skipped = data.get("skipped", [])
            self.excluded = data.get("excluded", [])
            self.initial_snapshot = data.get("initial_snapshot", {})
            # settings 복원: state 파일에 저장된 PATCH 변경분 우선, ENV는 폴백
            saved_settings = data.get("settings")
            if saved_settings and isinstance(saved_settings, dict):
                for k, v in saved_settings.items():
                    if k in self.settings and v is not None:
                        self.settings[k] = v
            logger.info("[TriageManager] state loaded: state=%s active_targets=%d recovered=%d",
                        self.state, len(self.active_targets), len(self.recovered))
        except (OSError, json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            logger.error("[TriageManager] load_state failed: %s", exc)
