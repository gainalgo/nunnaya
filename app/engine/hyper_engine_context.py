# ============================================================
# File: app/engine/hyper_engine_context.py
# Autocoin OS v3-H — Engine Context (LIVE-ready)
# ------------------------------------------------------------
# - Warm-up(Readiness)
# - Strategy snapshot / Risk snapshot (UI + Manager Router contract)
# - Position (qty 기반) + PnL
# - Order state (pending) persisted for crash recovery
# - Context state는 runtime/context_state.json으로 저장/복원
# ============================================================

from __future__ import annotations

import logging
import os
import time
import math
import copy
from collections import deque
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger(__name__)

from app.core.currency import Q


class HyperEngineContext:
    """단일 마켓의 상태 저장소(State).

    LIVE 설계 포인트:
    - 포지션은 qty 기반으로 저장 (실거래 체결량과 일치)
    - order_state를 dict로 저장하여 서버 리셋 후에도 pending 복구 가능
    - RECOVERY 모드에서는 '진입 금지'이되, 청산/회수는 허용한다.
    - 부팅/리셋에도 안전하게 재개할 수 있도록 최소한의 가격 tail을 보존한다.
    """

    def __init__(
        self,
        allocated: float = 0.0,
        base_policy: Optional[Dict[str, Any]] = None,
        *,
        market: Optional[str] = None,
        engine_name: Optional[str] = None,
    ) -> None:
        # -----------------------------
        # Market identity
        # -----------------------------
        # NOTE:
        # - market/engine_name are accepted as optional keyword arguments to keep
        #   compatibility with some legacy tick helpers.
        # - engine_name is intentionally unused (the system is single-engine);
        #   we accept it so callers don't crash.
        self.market: Optional[str] = market
        self.market_state: str = "DISABLED"  # ACTIVE/WATCH/RECOVERY/DISABLED

        # -----------------------------
        # Runtime mode
        # -----------------------------
        self.trading_mode: str = "PAPER"  # LIVE/PAPER

        # -----------------------------
        # Capital (soft accounting / gating)
        # -----------------------------
        self.allocated_capital: float = float(allocated)
        self.usable_capital: float = float(allocated)

        # PATCH 2025-12-26: per-market wallet mode (no cross-subsidize)
        self.wallet_mode: bool = False
        self.last_wallet_update: Optional[Dict[str, Any]] = None

        # -----------------------------
        # Policy / Position
        # -----------------------------
        self.policy: Dict[str, Any] = dict(base_policy or {})

        # Position schema (권장)
        # {
        #   entry: float,
        #   qty: float,
        #   usdt: float,          # (선택) 누적 투입금
        #   source: 'paper'|'bybit'|'orphan'
        # }
        self.position: Optional[Dict[str, Any]] = None

        # -----------------------------
        # Price history (volatile, in-memory)
        # -----------------------------
        self.price_history: Deque[float] = deque(maxlen=2000)
        self.price_buffer = self.price_history  # compatibility
        self.volume_history: Deque[float] = deque(maxlen=2000)
        self.last_price_ts: Optional[float] = None
        # 타임스탬프 포함 가격 히스토리 — 5분봉 저점 계산용 (maxlen=6000 ≈ 1틱/초 × 100분)
        self._ts_price_history: Deque[tuple] = deque(maxlen=6000)
        # NOTE: warmup timer anchor (set on first valid price)
        # 🚨 DO NOT REMOVE: some components reference ctx.first_price_ts directly.
        self.first_price_ts: Optional[float] = None

        # -----------------------------
        # Warm-up / Readiness
        # -----------------------------
        self.created_at: float = time.time()
        self.min_ticks: int = 100
        self.min_seconds: int = 300  # 5 minutes
        # [2026-02-02] 워밍업 완료 플래그 - True면 is_ready() 체크 스킵
        self._warmup_done: bool = False

        # -----------------------------
        # PnL
        # -----------------------------
        self.unrealized_profit: float = 0.0
        self.total_profit: float = 0.0

        # -----------------------------
        # State
        # -----------------------------
        self.last_signal: Optional[str] = None

        # -----------------------------
        # Strategy snapshot
        # -----------------------------
        self.selected_strategy: Optional[str] = None
        self.strategy_scores: Dict[str, float] = {}
        self.strategy_reason: Dict[str, Any] = {}
        self.strategy_ts: Optional[float] = None

        # Generic strategy state container (replaces loose attributes)
        self.strategy_vars: Dict[str, Any] = {}

        self.ema_scores: Dict[str, float] = {}
        self.bias: Optional[str] = None
        self.bias_ts: Optional[float] = None
        self.confidence: Optional[float] = None

        # UI/API contract object
        self.strategy_state: Dict[str, Any] = {}

        # -----------------------------
        # Risk snapshot
        # -----------------------------
        self.risk_band: str = "L0"
        self.risk_unlock: bool = False
        self.risk_cap_usdt: float = 0.0
        self.risk_cap_ratio: float = 0.0
        self.risk_reason: Dict[str, Any] = {}
        self.risk_ts: Optional[float] = None

        self.risk_state: Dict[str, Any] = {}

        # -----------------------------
        # Order state (pending)
        # -----------------------------
        # dict schema from PendingOrder.to_dict()
        self.order_state: Optional[Dict[str, Any]] = None
        self.last_order_ts: Optional[float] = None

        # -----------------------------
        # PingPong Cycle (repeatable loop)
        # -----------------------------
        # NOTE:
        # - Engine(HyperNunnaya)에서 BUY/SELL intent 생성에 사용
        # - System(HyperSystem)에서 체결 완료 후 IDLE로 복구
        self.cycle: str = "IDLE"              # IDLE | HOLDING | EXITING
        self.entry_tick: int = -1             # BUY 시점 tick
        self.entry_ts: float = 0.0            # BUY 시점 time.time()
        self.exit_pending: bool = False       # SELL intent 생성 후 중복 방지
        self.exit_submit_ts: float = 0.0      # SELL intent 생성 시각(옵션)
        self.reentry_block_until_ts: float = 0.0  # SELL 후 재진입 쿨다운(옵션)

        # Last exit (for entry-ceiling guard)
        self.last_exit_price: Optional[float] = None
        self.last_exit_ts: float = 0.0
        self.engine_started_ts: float = 0.0   # 엔진 start 직후 BUY 폭주 방지용(옵션)

        # -----------------------------
        # Entry guard (local cooldown)
        # -----------------------------
        # 슬리피지/지연 등으로 시장 단위로 잠깐 진입을 막고 싶을 때 사용
        self.entry_block_until_ts: Optional[float] = None
        self.entry_block_reason: Optional[str] = None
        self.exit_block_until_ts: Optional[float] = None
        self.exit_block_reason: Optional[str] = None

        # -----------------------------
        # Recovery
        # -----------------------------
        self.recovery: bool = False
        self.recovery_reason: Optional[str] = None
        self.recovery_since_ts: Optional[float] = None

        # -----------------------------
        # Suspicion / Risk Memory (v1)
        # -----------------------------
        # NOTE:
        # '의심을 숫자로 만들기' 위한 상태 기억 영역.
        # Context는 계산을 하지 않고, RiskClassifier가 계산한 결과만 저장한다.
        # 기존 RiskBand(L0/L1/L2) 구조를 대체하지 않으며, 병행 운용을 전제로 한다.

        # 현재 의심 점수 (0~100)
        # 50 = 중립(default), 높을수록 의심
        self.suspicion_score: float = 50.0

        # 내부 세분화된 Risk Level (L0~L5)
        # UI에는 신호등 그룹(RED/YELLOW/GREEN)으로 매핑됨
        self.suspicion_level: str = "L3"

        # UI용 신호등 그룹
        # RED / YELLOW / GREEN
        self.suspicion_group: str = "YELLOW"

        # 동일 그룹 내 감도 (0.0 ~ 1.0)
        # 색의 밝기/채도/펄스 강도에 사용
        self.suspicion_intensity: float = 0.5

        # 마지막 suspicion 갱신 시각
        self.suspicion_ts: Optional[float] = None

        # -----------------------------
        # Suspicion / Confidence History
        # -----------------------------
        # NOTE:
        # Risk 판단의 근거 추적용 히스토리.
        # '지금 값'이 아니라 '어떻게 변해왔는지'를 보기 위함.
        # 의심이 해소되기 전까지는 제거하지 않는다.

        # 최근 confidence 변화 기록
        self.confidence_history: Deque[float] = deque(maxlen=30)

        # 최근 suspicion score 변화 기록
        self.suspicion_history: Deque[float] = deque(maxlen=30)

        # -----------------------------
        # Trade Attempt Tracking
        # -----------------------------
        # NOTE:
        # BUY 신호가 발생했으나 실제 포지션 오픈으로 이어지지 않은 횟수
        self.attempt_count: int = 0

        # Win/Loss Tracking for Self-Optimizer
        self.win_count: int = 0
        self.loss_count: int = 0

        # Recent Trade History (PnL %) for Trend Analysis
        # Stores tuples: (ts, profit_usdt, profit_pct)
        self.trade_history: Deque[tuple[float, float, float]] = deque(maxlen=50)

        # Notification Queue (for Telegram/Slack integration)
        # Stores dicts: {"ts": float, "level": str, "message": str}
        self.notifications: Deque[Dict[str, Any]] = deque(maxlen=20)

        # -----------------------------
        # Engine Controls (UI Toggles & Sliders)
        # -----------------------------
        # NOTE:
        # - 버튼/슬라이더 기반 제어를 위한 상태 저장소
        # - 엔진이 '어떤 판단을 얼마나 반영할지'를 조절하기 위한 용도
        # - 기본값은 기존 엔진 동작을 최대한 유지하도록 설정
        #
        # enabled : ON / OFF (버튼)
        # level   : 0~10 (슬라이더, 신뢰도/강도)
        #
        # 이 구조는 엔진에서 "읽기 전용"으로 사용한다.
        # 계산/판단은 Context가 하지 않는다.
        self.controls: Dict[str, Dict[str, Any]] = {
            "baseline": {"enabled": False,  "level": 10},  # 최후 안전핀
            "ai":       {"enabled": True,  "level": 10},  # 기존 AI 판단
            "strategy": {"enabled": False, "level": 5, "mode": "PINGPONG", "params": {
                "pp_exit_enabled": True,
                "pp_exit_lookback": 60,
                "pp_exit_dampen_need": 2,
                "pp_exit_trail_min_pct": 0.4,
                "pp_exit_trail_max_pct": 1.0,
                "pp_exit_trail_vol_mult": 0.6,
                "pp_exit_trail_vol_window": 30,
                "pp_exit_rsi_len": 14,
                "pp_exit_rsi_drop_ratio": 0.08,
                "pp_exit_macd_fast": 12,
                "pp_exit_macd_slow": 26,
                "pp_exit_macd_signal": 9,
                "pp_exit_macd_down_streak": 2,
                "pp_exit_band_len": 20,
                "pp_exit_band_k": 2.0,
                "pp_exit_min_profit_pct": 0.0
            }},   # 전략 보조 (초기 OFF)
            "risk":     {"enabled": True,  "level": 10},  # 리스크 차단
            "tp_sl":    {"enabled": True,  "level": 10},
            "manual":   {"enabled": False},  # Market isolation (no engine orders)  # TP/SL
        }
 
    # --------------------------------------------------
    # Generic Strategy State Accessors
    # --------------------------------------------------
    def get_var(self, key: str, default: Any = None) -> Any:
        """Get a strategy-specific variable."""
        return self.strategy_vars.get(key, default)

    def set_var(self, key: str, value: Any) -> None:
        """Set a strategy-specific variable."""
        self.strategy_vars[key] = value

    def clear_vars(self, *, prefixes: Optional[tuple[str, ...]] = None) -> None:
        """Clear strategy_vars.

        - prefixes=None: clear all keys.
        - prefixes=("pp_",): clear only keys that start with any of the given prefixes.

        Why:
        - Some state is per-position (e.g., PingPong exit trackers) and must reset on entry.
        - Some state must survive the first BUY fill (e.g., AUTOLOOP staged-entry markers)
          because the stage is advanced when the BUY intent is created.
        """

        if not prefixes:
            self.strategy_vars.clear()
            return

        try:
            keys = list(self.strategy_vars.keys())
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[CTX] clear_vars keys: %s", exc, exc_info=True)
            return

        for k in keys:
            try:
                ks = str(k)
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[CTX] clear_vars str(k): %s", exc, exc_info=True)
                continue
            if any(ks.startswith(p) for p in prefixes):
                try:
                    self.strategy_vars.pop(k, None)
                except (KeyError, AttributeError, TypeError) as exc:
                    logger.warning("[CTX] clear_vars pop: %s", exc, exc_info=True)

    # --------------------------------------------------
    # Price
    # --------------------------------------------------
    def record_price(self, price: float) -> None:
        """Record a new price tick (sanitized).

        Why:
        - External feed / restore can inject 0, negative, NaN/inf.
        - Those values can cause division-by-zero or invalid indicator math.
        - We filter here to keep the tick loop stable.

        IMPORTANT:
        - Updates last_price_ts for staleness checks.
        - Sets first_price_ts once (first valid price) for warmup elapsed calculation.
        """
        try:
            p = float(price)
        except (TypeError, ValueError) as exc:
            logger.warning("[CTX] record_price: %s", exc, exc_info=True)
            return

        # 1st line of defense
        if (not math.isfinite(p)) or p <= 0.0:
            return

        now = time.time()

        # warmup anchor: first valid price time
        if getattr(self, "first_price_ts", None) is None:
            self.first_price_ts = now

        # [PERF] 중복 가격 price_history 저장 방지 (2026-03-18)
        # 동일 가격 반복 기록 시 deque(maxlen=2000) 윈도우가 짧아짐
        # 중복 제거 → 유효 lookback 확대 → 인디케이터 품질 향상
        # _ts_price_history는 항상 기록 (시간 기반 rolling low/high 계산 필요)
        if not self.price_history or self.price_history[-1] != p:
            self.price_history.append(p)
        self._ts_price_history.append((now, p))
        self.last_price_ts = now

    def get_rolling_low(self, minutes: float = 5.0) -> float:
        """최근 N분 내 최저가를 반환. 타임스탬프 기반이라 5분봉 저점과 동일한 축."""
        cutoff = time.time() - minutes * 60.0
        prices = [p for ts, p in self._ts_price_history if ts >= cutoff]
        return min(prices) if prices else 0.0

    def get_rolling_high(self, minutes: float = 5.0) -> float:
        """최근 N분 내 최고가를 반환. 저점 계산과 동일한 시간축을 사용."""
        cutoff = time.time() - minutes * 60.0
        prices = [p for ts, p in self._ts_price_history if ts >= cutoff]
        return max(prices) if prices else 0.0

    def record_volume(self, volume: float) -> None:
        try:
            v = float(volume)
            if v >= 0:
                self.volume_history.append(v)
        except (TypeError, ValueError) as exc:
            logger.warning("[CTX] record_volume: %s", exc, exc_info=True)

    @property
    def ticks(self) -> int:
        """Compatibility: some loggers/coordinators reference ctx.ticks."""
        try:
            return len(self.price_history)
        except (AttributeError, TypeError):
            logger.warning("[Context] price_history len() failed", exc_info=True)
            return 0

    @property
    def warmup_ticks(self) -> int:
        """Legacy alias."""
        return self.ticks

    def add_price(self, price: float) -> None:
        """DEPRECATED: keep for backwards compatibility."""
        self.record_price(price)


    # --------------------------------------------------
    # Warm-up / Readiness
    # --------------------------------------------------
    def is_ready(self) -> bool:
        """워밍업 완료 여부 확인.
        
        [2026-02-02] 최적화: _warmup_done 플래그가 True면 즉시 반환.
        ACTIVE 마켓은 매 tick마다 len() + time() 계산을 스킵한다.
        """
        # 플래그가 이미 True면 추가 계산 없이 즉시 반환
        if self._warmup_done:
            return True
        
        # 실제 조건 체크
        ready = (
            len(self.price_history) >= self.min_ticks
            and (time.time() - self.created_at) >= self.min_seconds
        )
        
        # 조건 충족 시 플래그 설정 (이후 호출에서 스킵)
        if ready:
            self._warmup_done = True
        
        return ready

    def readiness_status(self) -> Dict[str, Any]:
        return {
            "ready": self.is_ready(),
            "ticks": len(self.price_history),
            "min_ticks": self.min_ticks,
            "elapsed_sec": int(time.time() - self.created_at),
            "min_seconds": self.min_seconds,
            "warmup_done": self._warmup_done,
        }

    def readiness(self) -> Dict[str, Any]:
        return self.readiness_status()

    def reset_warmup(self) -> None:
        """가격 히스토리/웜업 기준을 리셋한다."""
        self._warmup_done = False  # 플래그도 리셋
        self.created_at = time.time()
        self.price_history.clear()
        self.volume_history.clear()
        self.last_price_ts = None
        self.first_price_ts = None

    def force_ready(self) -> None:
        """[2026-02-02] 워밍업을 강제 완료시킨다.

        ACTIVE로 전환될 때 호출하여 워밍업 대기 없이 즉시 거래 가능하게 함.
        [FIX 2026-03-24] 포지션 없고 데이터 부족하면 force하지 않음.
        포지션 있으면 기존 보유 코인이므로 즉시 거래 가능해야 함.
        """
        # 포지션이 있으면 무조건 ready (기존 보유 코인 관리용)
        has_position = bool(self.position and float(self.position.get("qty", 0) or 0) > 0)
        if has_position:
            self._warmup_done = True
            return
        # 포지션 없으면 데이터 충분할 때만 ready
        if len(self.price_history) >= self.min_ticks:
            self._warmup_done = True

    def pre_seed_prices(self, prices: list) -> None:
        """Pre-populate price_history from candle data (e.g. 5-min candles).

        Used by SniperFastLane to bypass warmup delay.
        Each price becomes a synthetic tick in both price_history
        and _ts_price_history (spaced 5 min apart, ending at now).
        """
        if not prices:
            return
        now = time.time()
        interval = 300.0  # 5-min spacing
        start_ts = now - interval * len(prices)
        for i, p in enumerate(prices):
            try:
                pf = float(p)
            except (TypeError, ValueError):
                logger.warning("[Context] pre_seed_prices: invalid price %r, skipping", p, exc_info=True)
                continue
            if not math.isfinite(pf) or pf <= 0:
                continue
            ts = start_ts + interval * (i + 1)
            self.price_history.append(pf)
            self._ts_price_history.append((ts, pf))
        if self.price_history:
            self.last_price_ts = now
            if getattr(self, "first_price_ts", None) is None:
                self.first_price_ts = start_ts + interval
        self._warmup_done = True

    # --------------------------------------------------
    # EMA
    # --------------------------------------------------
    def update_ema(self, scores: Dict[str, float], alpha: float) -> None:
        if not self.ema_scores:
            self.ema_scores = dict(scores)
            return

        for k, v in scores.items():
            prev = self.ema_scores.get(k, v)
            self.ema_scores[k] = float(alpha) * float(v) + (1.0 - float(alpha)) * float(prev)

    # --------------------------------------------------
    # Strategy snapshot
    # --------------------------------------------------
    def set_strategy_snapshot(
        self,
        selected: Optional[str],
        scores: Dict[str, float],
        reason: Dict[str, Any],
        ts: Optional[float] = None,
    ) -> None:
        self.selected_strategy = selected
        self.strategy_scores = dict(scores or {})
        self.strategy_reason = dict(reason or {})
        self.strategy_ts = ts or time.time()

        self.strategy_state = {
            "selected": self.selected_strategy,
            "scores": dict(self.strategy_scores),
            "reason": dict(self.strategy_reason),
            "ts": self.strategy_ts,
            "ema": dict(self.ema_scores),
            "bias": self.bias,
            "confidence": self.confidence,
        }

    # --------------------------------------------------
    # Risk snapshot
    # --------------------------------------------------
    def set_risk_snapshot(
        self,
        *,
        band: str,
        unlock: bool,
        cap_usdt: float,
        reason: Dict[str, Any],
        cap_ratio: float = 0.0,
        ts: Optional[float] = None,
    ) -> None:
        self.risk_band = str(band)
        self.risk_unlock = bool(unlock)
        self.risk_cap_usdt = float(cap_usdt)
        self.risk_cap_ratio = float(cap_ratio)
        self.risk_reason = dict(reason or {})
        self.risk_ts = ts or time.time()

        self.risk_state = {
            "band": self.risk_band,
            "unlock": self.risk_unlock,
            "cap_ratio": self.risk_cap_ratio,
            "cap_usdt": self.risk_cap_usdt,
            "reason": dict(self.risk_reason),
            "ts": self.risk_ts,
        }

    # --------------------------------------------------
    # Capital / Position helpers
    # --------------------------------------------------
    def request_capital(self, amount: float) -> bool:
        """(PAPER) 내부 자본 차감."""
        amount = float(amount)
        if self.usable_capital >= amount:
            self.usable_capital -= amount
            return True
        return False

    def open_position(self, entry_price: float, usdt_amount: float, *, source: str = "paper") -> None:
        """(PAPER) USDT 금액 기반 포지션 오픈 → qty로 환산."""
        entry = float(entry_price)
        usdt = float(usdt_amount)
        qty = (usdt / entry) if entry > 0 else 0.0

        self.position = {
            "entry": entry,
            "qty": float(qty),
            "usdt": usdt,
            "source": source,
        }

    def close_position(self, exit_price: float) -> float:
        """(PAPER) 포지션 전량 청산."""
        if not self.position:
            return 0.0

        entry = float(self.position.get("entry") or 0.0)
        qty = float(self.position.get("qty") or 0.0)
        if qty <= 0 or entry <= 0:
            self.position = None
            return 0.0

        # 수수료 반영 (매수 0.1% + 매도 0.1%)
        FEE_RATE = 0.001  # 0.1%
        buy_fee = entry * qty * FEE_RATE
        sell_fee = float(exit_price) * qty * FEE_RATE
        gross_profit = (float(exit_price) - entry) * qty
        profit = gross_profit - buy_fee - sell_fee
        self.total_profit += profit
        self.unrealized_profit = 0.0

        # 원금(=entry*qty) + profit 반환
        principal = entry * qty
        # PATCH 2025-12-26: wallet-mode => profit extracted, loss reflected
        if getattr(self, 'wallet_mode', False):
            if profit < 0:
                self.usable_capital += principal + profit
            else:
                self.usable_capital += principal
        else:
            self.usable_capital += principal + profit

        if profit > 0:
            self.win_count += 1
        elif profit < 0:
            self.loss_count += 1

        # Record trade history
        entry = float(self.position.get("entry") or 0.0)
        if entry > 0:
            pct = (float(exit_price) - entry) / entry * 100.0
            self.trade_history.append((time.time(), profit, pct))

        self.position = None
        return float(profit)

    def compute_unrealized(self, price: float) -> None:
        if not self.position:
            self.unrealized_profit = 0.0
            return
        entry = float(self.position.get("entry") or 0.0)
        qty = float(self.position.get("qty") or 0.0)
        self.unrealized_profit = (float(price) - entry) * qty

    def update_policy(self, refined: Dict[str, Any]) -> None:
        if not isinstance(refined, dict):
            return

        pol = dict(refined)
        params = pol.get("params")
        if not isinstance(params, dict):
            params = {}

        mode = ""
        try:
            st = (self.controls or {}).get("strategy") if isinstance(self.controls, dict) else {}
            if isinstance(st, dict):
                mode = str(st.get("mode") or "").strip().upper()
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Context] update_policy: strategy mode read failed", exc_info=True)
            mode = ""

        # Legacy default hardening: avoid ultra-tight historical baseline SL.
        try:
            sl_cur = params.get("sl")
            if sl_cur is None:
                params["sl"] = self._default_sl_for_mode(mode, fallback=-2.5)
            else:
                params["sl"] = self._normalize_policy_sl(sl_cur, mode=mode, fallback=-2.5)
        except (KeyError, AttributeError, TypeError):
            logger.warning("[Context] update_policy: SL normalization failed", exc_info=True)
            params["sl"] = self._default_sl_for_mode(mode, fallback=-2.5)

        pol["params"] = params
        self.policy = pol

    # --------------------------------------------------
    # LIVE fill apply
    # --------------------------------------------------
    def apply_fill_buy(self, *, avg_price: float, qty: float, funds: float, fee: float, source: str = "bybit") -> None:
        """체결(매수) 반영.

        - qty: 체결 수량
        - funds: 사용한 USDT
        """
        avg_price = float(avg_price)
        qty = float(qty)
        funds = float(funds)
        if qty <= 0 or avg_price <= 0:
            return

        if self.position is None:
            self.position = {
                "entry": avg_price,
                "qty": qty,
                "usdt": funds,
                "source": source,
                "entry_ts": time.time(),
                # PATCH 2025-12-26: cash/fee accounting
                "buy_fee_usdt": float(fee or 0.0),
                "buy_cost_usdt": float(funds or 0.0) + float(fee or 0.0),
                "sell_fee_usdt": 0.0,
                "sell_proceeds_usdt": 0.0,
            }

            # reset pingpong exit state on fresh entry
            # NOTE:
            # - Do NOT clear all strategy vars here.
            #   AUTOLOOP staged-entry advances its stage marker at BUY intent time,
            #   and clearing everything on the first fill would erase that marker,
            #   preventing staged add-buys from working.
            # - We only reset PingPong-specific trackers (pp_*).
            self.clear_vars(prefixes=("pp_",))
        else:
            old_entry = float(self.position.get("entry") or avg_price)
            old_qty = float(self.position.get("qty") or 0.0)
            old_usdt = float(self.position.get("usdt") or (old_entry * old_qty))

            new_qty = old_qty + qty
            if new_qty > 0:
                new_entry = (old_entry * old_qty + avg_price * qty) / new_qty
            else:
                new_entry = avg_price

            self.position["entry"] = float(new_entry)
            self.position["qty"] = float(new_qty)
            self.position["usdt"] = float(old_usdt + funds)
            
            # PATCH 2025-12-26: cash/fee accounting for wallet settlement on exit
            old_buy_fee = float(self.position.get("buy_fee_usdt") or 0.0)
            old_buy_cost = float(self.position.get("buy_cost_usdt") or old_usdt)
            self.position["buy_fee_usdt"] = old_buy_fee + float(fee or 0.0)
            self.position["buy_cost_usdt"] = old_buy_cost + float(funds or 0.0) + float(fee or 0.0)
            self.position.setdefault("sell_fee_usdt", 0.0)
            self.position.setdefault("sell_proceeds_usdt", 0.0)
            self.position["source"] = source

        # realized profit는 매수에서 발생하지 않는다.

    def apply_fill_sell(self, *, avg_price: float, qty: float, funds: float, fee: float, source: str = "bybit") -> None:
        pos = self.position
        if not pos:
            return

        entry = float(pos.get("entry") or 0.0)
        cur_qty = float(pos.get("qty") or 0.0)
        sell_qty = min(cur_qty, float(qty))
        remain = max(0.0, cur_qty - sell_qty)
        pos["qty"] = remain
        pos["ts"] = time.time()

        # PATCH 2025-12-26: cash/fee accounting
        sell_fee = float(fee or 0.0)
        sell_net = float(funds or 0.0) - sell_fee
        pos["sell_fee_usdt"] = float(pos.get("sell_fee_usdt") or 0.0) + sell_fee
        pos["sell_proceeds_usdt"] = float(pos.get("sell_proceeds_usdt") or 0.0) + sell_net

        if remain <= 0.0:
            # position fully closed -> reset all per-position strategy state
            self.clear_vars()
            buy_cost = float(pos.get("buy_cost_usdt") or 0.0)
            total_proceeds = float(pos.get("sell_proceeds_usdt") or 0.0)
            net_pnl = total_proceeds - buy_cost

            # store last exit price for entry-ceiling guard (dynamic attribute)
            self.last_exit_price = float(avg_price)
            self.last_exit_ts = time.time()

            if getattr(self, "wallet_mode", False):
                before = float(self.usable_capital)
                if net_pnl < 0:
                    self.usable_capital = max(0.0, float(self.usable_capital) + float(net_pnl))
                # never auto top-up beyond allocated_capital
                if float(self.allocated_capital) > 0:
                    self.usable_capital = min(float(self.usable_capital), float(self.allocated_capital))
                after = float(self.usable_capital)
                self.last_wallet_update = {
                    "ts": time.time(),
                    "net_pnl_usdt": float(net_pnl),
                    "usable_before": before,
                    "usable_after": after,
                }

            # track net profit
            self.total_profit += float(net_pnl)

            if net_pnl > 0:
                self.win_count += 1
            elif net_pnl < 0:
                self.loss_count += 1

            # Record trade history
            entry = float(pos.get("entry") or 0.0)
            if entry > 0:
                pct = (float(avg_price) - entry) / entry * 100.0
                self.trade_history.append((time.time(), net_pnl, pct))

            self.position = None

    def finalize_tick(
        self,
        signal: str,
        price: float,
        *,
        override: bool = False
    ) -> None:
        """
        Tick 종료 후 상태 정리.

        기본 동작:
        - signal을 last_signal로 기록
        - 미실현 손익 계산

        override=True 인 경우:
        - 엔진(arbiter)에서 확정한 signal을 그대로 신뢰
        - 외부에서 조정된 판단을 덮어쓰지 않음
        """

        # -----------------------------
        # signal 기록
        # -----------------------------
        if override:
            # NOTE:
            # 엔진에서 최종 확정한 신호.
            # Context 내부 판단으로 재해석하지 않는다.
            self.last_signal = signal
        else:
            # NOTE:
            # 기존 동작 유지 (호환성)
            self.last_signal = signal

        # -----------------------------
        # 손익 계산 (항상 수행)
        # -----------------------------
        self.compute_unrealized(price)


    # --------------------------------------------------
    # Persistence helpers (context_state.json)
    # --------------------------------------------------
    def _clean_strategy_vars(self) -> Dict[str, Any]:
        """직렬화 전 strategy_vars에서 오래된 날짜별 키를 제거.

        lt_shots_YYYYMMDD, sniper_shots_YYYYMMDD 등이 매일 새로 생겨
        무한 누적되는 문제 방지. 오늘 + 어제 키만 보존.
        """
        import re
        today = time.strftime("%Y%m%d")
        # 어제 날짜 계산 (간단히 -1일)
        try:
            from datetime import datetime, timedelta
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        except (OSError, TypeError, ValueError, OverflowError):
            logger.warning("[Context] _clean_strategy_vars: yesterday date calculation failed", exc_info=True)
            yesterday = today
        keep_dates = {today, yesterday}
        date_key_re = re.compile(r"^(.+?)_(\d{8})$")
        cleaned = {}
        for k, v in self.strategy_vars.items():
            m = date_key_re.match(k)
            if m and m.group(2) not in keep_dates:
                continue  # 오래된 날짜별 키 제거
            cleaned[k] = v
        return cleaned

    def to_state(self, *, max_prices: int = 500) -> Dict[str, Any]:
        prices = list(self.price_history)
        if max_prices > 0:
            prices = prices[-int(max_prices):]

        return {
            "market": self.market,
            "market_state": self.market_state,
            "trading_mode": self.trading_mode,
            "allocated_capital": self.allocated_capital,
            "usable_capital": self.usable_capital,
            "policy": dict(self.policy),
            "position": dict(self.position) if isinstance(self.position, dict) else None,
            "created_at": self.created_at,
            "min_ticks": self.min_ticks,
            "min_seconds": self.min_seconds,
            "_warmup_done": self._warmup_done,  # [2026-02-02] 워밍업 완료 플래그
            "price_tail": prices,
            "last_price_ts": self.last_price_ts,
            "first_price_ts": self.first_price_ts,
            "ema_scores": dict(self.ema_scores),
            "bias": self.bias,
            "confidence": self.confidence,
            "risk_state": dict(self.risk_state),
            "strategy_state": dict(self.strategy_state),
            "order_state": dict(self.order_state) if isinstance(self.order_state, dict) else None,
            "last_order_ts": self.last_order_ts,
            "entry_block_until_ts": self.entry_block_until_ts,
            "recovery": self.recovery,
            "recovery_reason": self.recovery_reason,
            "recovery_since_ts": self.recovery_since_ts,
            # -----------------------------
            # Cycle persistence (optional)
            # -----------------------------
            "cycle": getattr(self, "cycle", "IDLE"),
            "entry_tick": getattr(self, "entry_tick", -1),
            "entry_ts": getattr(self, "entry_ts", 0.0),
            "exit_pending": getattr(self, "exit_pending", False),
            "exit_submit_ts": getattr(self, "exit_submit_ts", 0.0),
            "reentry_block_until_ts": getattr(self, "reentry_block_until_ts", 0.0),
            "last_exit_price": getattr(self, "last_exit_price", None),
            "last_exit_ts": getattr(self, "last_exit_ts", 0.0),
            "engine_started_ts": getattr(self, "engine_started_ts", 0.0),
            "strategy_vars": self._clean_strategy_vars(),
            "controls": dict(self.controls),
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            # trade_history/notifications are volatile, usually not persisted or just tail
        }

    def apply_state(self, state: Dict[str, Any], *, stale_reset_sec: int = 900, max_prices: int = 500, is_active: bool = False) -> None:
        if not isinstance(state, dict):
            return

        self.market = state.get("market") or self.market
        self.market_state = str(state.get("market_state") or self.market_state)
        self.trading_mode = str(state.get("trading_mode") or self.trading_mode)

        try:
            self.allocated_capital = float(state.get("allocated_capital") or self.allocated_capital)
            self.usable_capital = float(state.get("usable_capital") or self.usable_capital)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[CTX] apply_state capital: %s", exc, exc_info=True)

        pol = state.get("policy")
        if isinstance(pol, dict):
            self.policy = dict(pol)

        pos = state.get("position")
        if isinstance(pos, dict):
            self.position = dict(pos)
            # [FIX 2026-02-19] 기존 포지션에 entry_ts가 없으면 백필 (Grace Period 작동 보장)
            if self.position.get("qty", 0) > 0 and not self.position.get("entry_ts"):
                self.position["entry_ts"] = time.time()
        else:
            self.position = None

        try:
            self.created_at = float(state.get("created_at") or self.created_at)
            self.min_ticks = int(state.get("min_ticks") or self.min_ticks)
            self.min_seconds = int(state.get("min_seconds") or self.min_seconds)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[CTX] apply_state entry_ts backfill: %s", exc, exc_info=True)

        # [2026-02-02] 워밍업 완료 플래그 복원
        # [FIX 2026-03-24] min_ticks 이상 데이터가 실제로 있을 때만 warmup 완료
        # 이전: ACTIVE면 무조건 True → min_ticks 변경 무력화
        _has_enough_data = len(self.price_history) >= self.min_ticks
        if state.get("_warmup_done") and _has_enough_data:
            self._warmup_done = True
        elif is_active and _has_enough_data:
            self._warmup_done = True

        # -----------------------------
        # Cycle restore (optional)
        # -----------------------------
        try:
            self.cycle = str(state.get("cycle") or getattr(self, "cycle", "IDLE"))
            self.entry_tick = int(state.get("entry_tick") if state.get("entry_tick") is not None else getattr(self, "entry_tick", -1))
            self.entry_ts = float(state.get("entry_ts") if state.get("entry_ts") is not None else getattr(self, "entry_ts", 0.0))
            self.exit_pending = bool(state.get("exit_pending") if state.get("exit_pending") is not None else getattr(self, "exit_pending", False))
            self.exit_submit_ts = float(state.get("exit_submit_ts") if state.get("exit_submit_ts") is not None else getattr(self, "exit_submit_ts", 0.0))
            self.reentry_block_until_ts = float(state.get("reentry_block_until_ts") if state.get("reentry_block_until_ts") is not None else getattr(self, "reentry_block_until_ts", 0.0))
            self.last_exit_price = (float(state.get("last_exit_price")) if state.get("last_exit_price") is not None else getattr(self, "last_exit_price", None))
            self.last_exit_ts = float(state.get("last_exit_ts") if state.get("last_exit_ts") is not None else getattr(self, "last_exit_ts", 0.0))
            self.engine_started_ts = float(state.get("engine_started_ts") if state.get("engine_started_ts") is not None else getattr(self, "engine_started_ts", 0.0))
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[CTX] apply_state timestamps: %s", exc, exc_info=True)

        # --------------------------------------------------
        # Strategy state restore
        # --------------------------------------------------
        sv = state.get("strategy_vars")
        if isinstance(sv, dict):
            self.strategy_vars = dict(sv)
        else:
            # Backward-compat migration: older context_state.json stored
            # some strategy state as top-level keys.
            migrated: Dict[str, Any] = {}

            # AUTOLOOP staged-entry (legacy)
            if "autoloop_entry_stage" in state:
                try:
                    migrated["autoloop_entry_stage"] = int(state.get("autoloop_entry_stage") or 0)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[Context] apply_state: autoloop_entry_stage migration failed", exc_info=True)
                    migrated["autoloop_entry_stage"] = 0
            if "autoloop_last_add_ts" in state:
                try:
                    migrated["autoloop_last_add_ts"] = float(state.get("autoloop_last_add_ts") or 0.0)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[Context] apply_state: autoloop_last_add_ts migration failed", exc_info=True)
                    migrated["autoloop_last_add_ts"] = 0.0
            if "autoloop_entry_ref" in state:
                try:
                    migrated["autoloop_entry_ref"] = float(state.get("autoloop_entry_ref") or 0.0)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[Context] apply_state: autoloop_entry_ref migration failed", exc_info=True)
                    migrated["autoloop_entry_ref"] = 0.0

            # PingPong exit trackers (legacy; some builds persisted these)
            for k in (
                "pp_high_since_entry",
                "pp_rsi_peak_since_entry",
                "pp_macd_hist_prev",
                "pp_macd_down_streak",
                "pp_band_was_above",
            ):
                if k not in state:
                    continue
                v = state.get(k)
                if v is None:
                    continue
                try:
                    if k == "pp_macd_down_streak":
                        migrated[k] = int(v)
                    elif k == "pp_band_was_above":
                        migrated[k] = bool(v)
                    elif k == "pp_macd_hist_prev":
                        migrated[k] = float(v)
                    else:
                        migrated[k] = float(v)
                except (TypeError, ValueError) as exc:
                    # best-effort; skip invalid
                    logger.warning("[CTX] PingPong exit tracker migrate: %s", exc, exc_info=True)

            if migrated:
                # Only set if we actually have migrated keys.
                self.strategy_vars = migrated

        # 가격 tail 복원
        tail = state.get("price_tail")
        prices: list[float] = []
        if isinstance(tail, list):
            seq = tail[-int(max_prices):] if max_prices and max_prices > 0 else tail
            for p in seq:
                try:
                    prices.append(float(p))
                except (TypeError, ValueError) as exc:
                    logger.warning("[CTX] price tail restore: %s", exc, exc_info=True)
                    continue

        self.price_history.clear()
        for p in prices:
            self.price_history.append(p)

        try:
            self.last_price_ts = float(state.get("last_price_ts")) if state.get("last_price_ts") is not None else None
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Context] apply_state: last_price_ts restore failed", exc_info=True)
            self.last_price_ts = None

        # restore warmup anchor if present
        try:
            self.first_price_ts = (
                float(state.get("first_price_ts")) if state.get("first_price_ts") is not None else None
            )
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Context] apply_state: first_price_ts restore failed", exc_info=True)
            self.first_price_ts = None

        # Backward-compat: if we have prices but no first_price_ts, fall back to last_price_ts
        if self.first_price_ts is None and len(self.price_history) > 0 and self.last_price_ts is not None:
            self.first_price_ts = float(self.last_price_ts)

        ema = state.get("ema_scores")
        if isinstance(ema, dict):
            try:
                self.ema_scores = {str(k): float(v) for k, v in ema.items()}
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[Context] apply_state: ema_scores float conversion failed", exc_info=True)
                self.ema_scores = dict(ema)

        self.bias = state.get("bias")
        try:
            self.confidence = float(state.get("confidence")) if state.get("confidence") is not None else None
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Context] apply_state: confidence restore failed", exc_info=True)
            self.confidence = None

        rs = state.get("risk_state")
        if isinstance(rs, dict):
            self.risk_state = dict(rs)
            self.risk_band = str(rs.get("band") or self.risk_band)
            self.risk_unlock = bool(rs.get("unlock") or False)
            try:
                self.risk_cap_ratio = float(rs.get("cap_ratio") or 0.0)
                self.risk_cap_usdt = float(rs.get("cap_usdt") or 0.0)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[CTX] risk state restore: %s", exc, exc_info=True)
            rr = rs.get("reason")
            if isinstance(rr, dict):
                self.risk_reason = dict(rr)

        ss = state.get("strategy_state")
        if isinstance(ss, dict):
            self.strategy_state = dict(ss)

        osd = state.get("order_state")
        if isinstance(osd, dict):
            self.order_state = dict(osd)
        else:
            self.order_state = None

        try:
            self.last_order_ts = float(state.get("last_order_ts")) if state.get("last_order_ts") is not None else None
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Context] apply_state: last_order_ts restore failed", exc_info=True)
            self.last_order_ts = None

        try:
            self.entry_block_until_ts = float(state.get("entry_block_until_ts")) if state.get("entry_block_until_ts") is not None else None
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Context] apply_state: entry_block_until_ts restore failed", exc_info=True)
            self.entry_block_until_ts = None

        self.recovery = bool(state.get("recovery") or False)
        self.recovery_reason = state.get("recovery_reason")
        try:
            self.recovery_since_ts = float(state.get("recovery_since_ts")) if state.get("recovery_since_ts") is not None else None
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Context] apply_state: recovery_since_ts restore failed", exc_info=True)
            self.recovery_since_ts = None

        try:
            self.win_count = int(state.get("win_count") or 0)
            self.loss_count = int(state.get("loss_count") or 0)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[CTX] win/loss count restore: %s", exc, exc_info=True)

        # stale guard: 너무 오래된 tail이면 warmup 리셋
        # [2026-02-02] ACTIVE/RECOVERY 마켓은 워밍업 리셋 대신 force_ready()
        try:
            if stale_reset_sec > 0 and self.last_price_ts is not None:
                if (time.time() - float(self.last_price_ts)) > float(stale_reset_sec):
                    if is_active:
                        self.force_ready()
                    else:
                        self.reset_warmup()
        except (TypeError, ValueError) as exc:
            logger.warning("[CTX] stale guard check: %s", exc, exc_info=True)

        # controls 복원(세팅-표시-실행 일치)
        # - 과거에는 안전을 위해 재시작 시 기본값으로 리셋했지만,
        #   전략 모드/파라미터가 재가동 후 PINGPONG/기본값으로 돌아가는 문제를 유발.
        # - baseline 은 강제/테스트 성격이 강해 기본적으로는 복원하지 않고,
        #   필요 시 env OMA_PERSIST_BASELINE=1 일 때만 복원한다.
        ctrls = state.get("controls")
        if isinstance(ctrls, dict) and ctrls:
            try:
                persist_baseline = str(os.getenv("OMA_PERSIST_BASELINE", "0")).strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "y",
                    "on",
                )
                if not persist_baseline and "baseline" in ctrls:
                    ctrls = dict(ctrls)
                    ctrls.pop("baseline", None)
                self.update_controls(ctrls)
            except (KeyError, AttributeError, TypeError) as exc:
                # 복원 실패는 치명적일 필요가 없고, 기본값으로 동작하면 된다.
                logger.warning("[CTX] controls restore: %s", exc, exc_info=True)

        # Legacy policy normalization:
        # some persisted contexts kept baseline sl=-0.8 even when strategy SL existed.
        # Normalize immediately on load to avoid early-stop regressions.
        try:
            self._sync_policy_tp_sl_from_controls(
                normalize_legacy=True,
                default_tp=1.2,
                default_sl=-2.5,
            )
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[CTX] legacy policy normalize: %s", exc, exc_info=True)

    def update_controls(self, patch: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Safely deep-merge control patches into current controls.

        - Unknown top-level keys are ignored.
        - Supports backward-compatible alias: strategy.config -> strategy.params
        - Ensures strategy.params is a dict.
        """

        if not isinstance(patch, dict):
            return self.controls

        # NOTE
        # - 'guards' is a UI-level operational override bucket.
        #   It is intentionally kept in ctx.controls so that:
        #   - dashboard can show the exact effective runtime values per market
        #   - HyperSystem can apply per-market safety overrides deterministically
        #   - controls persist via runtime/context_state.json
        allow = {"baseline", "ai", "strategy", "risk", "tp_sl", "guards", "manual"}
        prev_mode = ""
        try:
            prev_strat = self.controls.get("strategy")
            if isinstance(prev_strat, dict):
                prev_mode = str(prev_strat.get("mode") or "").strip().upper()
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Context] update_controls: prev_mode read failed", exc_info=True)
            prev_mode = ""
        sanitized: Dict[str, Any] = {}
        for k, v in patch.items():
            if k not in allow:
                continue
            if isinstance(v, dict):
                sanitized[k] = dict(v)

        # Backward-compat: some clients used strategy.config
        if "strategy" in sanitized:
            s = sanitized["strategy"]
            if isinstance(s, dict) and "config" in s and "params" not in s:
                s["params"] = s.get("config")
            if "params" in s and not isinstance(s.get("params"), dict):
                s["params"] = {}

        # Special: allow UI to clear guard overrides without needing a separate endpoint.
        # Usage: { "guards": { "__clear__": true } }
        # Effect: resets ctx.controls.guards to an empty dict.
        if "guards" in sanitized:
            g = sanitized.get("guards")
            if isinstance(g, dict) and bool(g.get("__clear__")):
                try:
                    self.controls["guards"] = {}
                except (KeyError, AttributeError, TypeError):
                    logger.warning("[Context] update_controls: guards clear failed, using setdefault", exc_info=True)
                    self.controls.setdefault("guards", {})
                try:
                    g = dict(g)
                    g.pop("__clear__", None)
                    sanitized["guards"] = g
                except (KeyError, AttributeError, TypeError):
                    logger.warning("[Context] update_controls: guards sanitize failed", exc_info=True)
                    sanitized["guards"] = {}

        def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
            for kk, vv in src.items():
                if isinstance(vv, dict) and isinstance(dst.get(kk), dict):
                    _deep_merge(dst[kk], vv)  # type: ignore[index]
                else:
                    dst[kk] = vv

        # Atomic update using deepcopy to prevent race conditions
        next_controls = copy.deepcopy(self.controls)

        # 모드 전환 시 이전 전략 params를 클린 교체
        # deep-merge만 하면 이전 모드의 파라미터가 잔존해 "풀림" 현상 발생
        try:
            new_mode = str((sanitized.get("strategy") or {}).get("mode") or "").strip().upper()
            if new_mode and new_mode != prev_mode and prev_mode:
                existing_strat = next_controls.get("strategy")
                if isinstance(existing_strat, dict):
                    existing_strat.pop("params", None)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[CTX] mode-change param cleanup: %s", exc, exc_info=True)

        _deep_merge(next_controls, sanitized)

        # --------------------------------------------------
        # Guard override normalization
        # --------------------------------------------------
        # UI/JSON may encode "inherit" as:
        #   - null  (preferred)
        #   - ""    (legacy / some clients)
        # Keep the stored controls lean and deterministic by removing
        # null/blank-string values after merge.
        try:
            gcur = next_controls.get("guards")
            if isinstance(gcur, dict) and gcur:
                for kk in list(gcur.keys()):
                    if kk == "__clear__":
                        gcur.pop(kk, None)
                        continue

                    vv = gcur.get(kk)
                    if vv is None:
                        gcur.pop(kk, None)
                        continue
                    if isinstance(vv, str) and vv.strip() == "":
                        gcur.pop(kk, None)
                        continue

                # drop empty dict to keep storage lean
                if not gcur:
                    try:
                        next_controls.pop("guards", None)
                    except (KeyError, AttributeError, TypeError) as exc:
                        logger.warning("[CTX] drop empty dict to keep storage lean: %s", exc, exc_info=True)
                else:
                    next_controls["guards"] = gcur
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[CTX] drop empty dict to keep storage lean: %s", exc, exc_info=True)

        # If strategy is enabled but mode is missing, restore prior mode if possible,
        # otherwise infer from params to avoid drifting into an unintended default.
        # drifting into an unintended default on reboot/partial patches.
        try:
            strat = next_controls.get("strategy")
            if isinstance(strat, dict):
                enabled = bool(strat.get("enabled"))
                mode = str(strat.get("mode") or "").strip().upper()
                if enabled and not mode:
                    if prev_mode:
                        strat["mode"] = prev_mode
                        next_controls["strategy"] = strat
                        self.controls = next_controls
                        return self.controls
                    params = strat.get("params")
                    if not isinstance(params, dict):
                        params = {}
                    inferred = ""
                    if any(k in params for k in ("entry_lookback_min", "entry_threshold_pct", "exit_lookback_min", "exit_threshold_pct")):
                        inferred = "SNIPER"
                    elif any(k in params for k in ("burst_window", "burst_threshold")):
                        inferred = "LIGHTNING"
                    elif any(k in params for k in ("step_pct", "max_steps")):
                        inferred = "LADDER"
                    elif any(k in params for k in ("bootstrap", "bar_sec", "max_bars", "z_len", "anchor_len")):
                        inferred = "AUTOLOOP"
                    elif any(k in params for k in ("manual_exit", "sl_price", "tp_price")):
                        inferred = "GAZUA"
                    elif any(k in params for k in ("trail_tp_enabled", "cooldown_sec")):
                        inferred = "CONTRARIAN"
                    elif any(k in params for k in ("rsi_buy", "rsi_sell", "pp_exit_enabled")):
                        inferred = "PINGPONG"
                    if inferred:
                        strat["mode"] = inferred
                        next_controls["strategy"] = strat
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[CTX] drifting into an unintended default on reboot/partial patches: %s", exc, exc_info=True)

        self.controls = next_controls
        try:
            self._sync_policy_tp_sl_from_controls(
                normalize_legacy=True,
                default_tp=1.2,
                default_sl=-2.5,
            )
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[CTX] hyper_engine_context fallback: %s", exc, exc_info=True)
        return self.controls

    def _sync_policy_tp_sl_from_controls(
        self,
        *,
        normalize_legacy: bool = True,
        default_tp: float = 1.2,
        default_sl: float = -2.5,
    ) -> None:
        """Align policy tp/sl with strategy params when available.

        - If strategy tp/sl exists, prefer those.
        - If only legacy sl=-0.8 exists, replace with default_sl.
        """
        ctrls = self.controls if isinstance(self.controls, dict) else {}
        st = ctrls.get("strategy", {}) if isinstance(ctrls.get("strategy"), dict) else {}
        mode = str(st.get("mode") or "").strip().upper()
        sp = st.get("params", {}) if isinstance(st.get("params"), dict) else {}

        policy = self.policy if isinstance(self.policy, dict) else {"name": "nunnaya", "params": {}}
        pp = policy.get("params")
        if not isinstance(pp, dict):
            pp = {}

        tp_val = sp.get("tp", sp.get("tp_pct"))
        sl_val = sp.get("sl", sp.get("sl_pct"))

        if tp_val is not None:
            try:
                pp["tp"] = float(tp_val)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[CTX] _sync_policy_tp_sl_from_controls tp fallback: %s", exc, exc_info=True)
        elif "tp" not in pp:
            pp["tp"] = float(default_tp)

        if sl_val is not None:
            try:
                pp["sl"] = self._normalize_policy_sl(sl_val, mode=mode, fallback=default_sl)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[CTX] _sync_policy_tp_sl_from_controls sl fallback: %s", exc, exc_info=True)
        else:
            cur_sl = pp.get("sl")
            if cur_sl is None:
                pp["sl"] = self._default_sl_for_mode(mode, fallback=default_sl)
            elif normalize_legacy:
                try:
                    pp["sl"] = self._normalize_policy_sl(cur_sl, mode=mode, fallback=default_sl)
                except (KeyError, AttributeError, TypeError):
                    logger.warning("_normalize_policy_sl failed for mode=%s, using default", mode, exc_info=True)
                    pp["sl"] = self._default_sl_for_mode(mode, fallback=default_sl)

        policy["name"] = str(policy.get("name") or "nunnaya")
        policy["params"] = pp
        self.policy = policy

    @staticmethod
    def _default_sl_for_mode(mode: str, *, fallback: float = -2.5) -> float:
        m = str(mode or "").strip().upper()
        defaults = {
            "LADDER": -5.0,
            "GAZUA": -8.0,
            "CONTRARIAN": -50.0,
            "LIGHTNING": -2.0,
            "PINGPONG": -2.0,
            "AUTOLOOP": -2.5,
            "SNIPER": -1.5,
        }
        base = float(defaults.get(m, fallback))
        floor = HyperEngineContext._global_min_sl_floor(fallback=fallback)
        if base > floor:
            base = floor
        return float(base)

    @staticmethod
    def _global_min_sl_floor(*, fallback: float = -2.5) -> float:
        """Return global SL safety floor (negative percent).

        - Reads OMA_GLOBAL_MIN_SL_PCT for runtime overrides from UI/API.
        - Ensures a sane negative range to avoid accidental zero/positive SL.
        """
        try:
            raw = float(os.getenv("OMA_GLOBAL_MIN_SL_PCT", str(fallback)) or fallback)
        except (TypeError, ValueError):
            logger.warning("[Context] OMA_GLOBAL_MIN_SL_PCT parse failed, using fallback %.1f", fallback, exc_info=True)
            raw = float(fallback)
        if raw > 0:
            raw = -abs(raw)
        return float(max(-95.0, min(-0.1, raw)))

    @classmethod
    def _normalize_policy_sl(cls, sl_raw: Any, *, mode: str, fallback: float = -2.5) -> float:
        sl_num = float(sl_raw)
        if sl_num > 0:
            sl_num = -abs(sl_num)

        # Sentinel recovery: historical stale baseline value should be replaced.
        if sl_num == -0.8:
            sl_num = cls._default_sl_for_mode(mode, fallback=fallback)

        # Global safety floor: prevent ultra-tight SL values across strategies.
        global_floor = cls._global_min_sl_floor(fallback=fallback)
        if sl_num > global_floor:
            sl_num = global_floor

        # LADDER must not run with overly tight SL.
        if str(mode or "").strip().upper() == "LADDER" and sl_num > -5.0:
            sl_num = -5.0

        return float(sl_num)
