"""
Harpoon (작살) — FOCUS Sub-Strategy Scalper

FOCUS가 감지한 H4 존 근처에서 M1 PA 패턴으로 고배율 단타를 반복하는 전략.
FOCUS의 데이터를 읽기 전용으로 소비하며, FOCUS 상태를 절대 변경하지 않는다.

Architecture:
    FOCUS (낚시꾼 🎣) → zones, h4_sig, ATR, state
    Harpoon (작살 🔱) → M1 PA 패턴 감지 → 존 근처 고배율 단타

State Machine:
    STANDBY → ZONE_READY → STALKING → FIRED → COOLDOWN → STANDBY
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join("runtime", "harpoon_config.json")


# ---------------------------------------------------------------------------
# Enums & Dataclasses
# ---------------------------------------------------------------------------

class HarpoonState(str, Enum):
    STANDBY = "STANDBY"          # FOCUS 존 없음 or 가격 멀리
    ZONE_READY = "ZONE_READY"    # 가격이 존 ATR×0.5 이내
    STALKING = "STALKING"        # M1 PA 패턴 대기
    FIRED = "FIRED"              # 스캘프 진입 완료
    COOLDOWN = "COOLDOWN"        # 스캘프 종료 후 대기


@dataclass
class HarpoonConfig:
    enabled: bool = False               # ★ 2026-04-23 부모 결정 ① 번복: HARPOON 당분간 OFF (양쪽 서버 동일). runtime 파일 소실 시 True 기동 방지.
    leverage: int = 20
    budget_pct: float = 10.0            # FOCUS 예산의 10%
    budget_usdt: float = 0.0            # 직접 지정 시 (0=auto from FOCUS)
    tp_atr_mult: float = 0.15           # TP = ATR × 0.15
    sl_atr_mult: float = 0.10           # SL = ATR × 0.10
    risk_pct: float = 0.5              # 스캘프당 예산의 0.5% 리스크
    zone_proximity_atr: float = 0.5     # 존 근접 판정: ATR × 0.5
    max_scalps_per_hour: int = 5
    max_daily_scalps: int = 15
    max_consecutive_loss: int = 3       # 연속 손실 → 1시간 정지
    max_daily_loss_pct: float = 2.0     # 일일 최대 손실 %
    tick_interval_sec: float = 5.0      # STALKING/ZONE_READY 틱 간격
    standby_interval_sec: float = 30.0  # STANDBY 틱 간격
    cooldown_sec: float = 30.0          # 스캘프 후 쿨다운
    entry_tf: str = "1"                 # M1 타임프레임
    spread_max_atr_pct: float = 2.0     # 최대 스프레드 (ATR %)
    server_side_tpsl: bool = True       # 서버사이드 TP/SL 필수
    # ── ADX Filter (독립 임계값) ──
    min_adx: int = 0                       # 0=FOCUS dormant_adx_threshold 상속, >0=하푼 독립 판단
    # ── Dynamic Trailing SL (스캘퍼용 — FOCUS보다 타이트) ──
    dynamic_trailing: bool = False         # 동적 트레일링 ON/OFF
    breakeven_trigger_pct: float = 0.08    # 0.08% 수익 시 SL→손익분기 (스캘퍼용 빠른 잠금)
    trailing_preserve_pct: float = 50.0    # 최고수익의 50% 보존
    # ★ Stage 0 (2026-04-22 부모 B 결정, 동생 plan v3 통합) ★
    # paper_mode + 9 방어 통합. 형 letter #11 검수 기준 동일 패턴 (Phase K/L).
    # default OFF — 부모 승인 후 활성. paper_mode=True 면 진입 0건, 통계만.
    paper_mode: bool = False               # ★ 2026-04-23 부모 지시: paper 개념 제거 — ON=실거래, OFF=완전 정지
    # Stage 0-1: B11 regime_lock 통합 (FOCUS focus_mgr._get_btc_regime_lock_reason)
    respect_b11_regime_lock: bool = True   # BULL → SHORT 차단, BEAR → LONG 차단
    # Stage 0-2: min_adx 강화 (기존 min_adx field 의 default 만 0 → 20 으로)
    # ↑ 기존 필드 유지 (사용자가 0 설정 시 fallback 으로만 변경)
    min_adx_v2: int = 20                   # Stage 0-2 권장값 (기존 min_adx=0 일 때 사용)
    # Stage 0-3: J v2 ADX 하락 skip 공유 (FOCUS adx_slope_check_enabled)
    respect_focus_adx_slope: bool = True   # FOCUS J v2 켜져 있으면 같은 기준 적용
    # Stage 0-4: Morning Guard 시간 자동 standby
    respect_morning_guard: bool = True     # 06:50~morning_guard_end 구간 자동 standby
    # Stage 0-5: coin_loss_cap 공유
    respect_coin_loss_cap: bool = True     # FOCUS 의 24h 누적 손실 cap 공유
    # Stage 0-6: HARPOON 전용 Fast-Reject (forensic 04-14 peak 0% 패턴)
    fast_reject_v2_enabled: bool = True    # 진입 후 60초 peak 0% + 손실 → 즉시 컷
    fast_reject_v2_max_sec: float = 60.0
    fast_reject_v2_peak_threshold_pct: float = 0.05
    fast_reject_v2_pnl_pct: float = -0.05
    # Stage 0-7: 첫 SL 후 30분 재진입 cooldown
    post_sl_cooldown_min: float = 30.0     # 0 = 비활성
    # Stage 0-9: Morning Guard 시간 확장 (HARPOON 만, 옵션 B)
    morning_extended_end_hour_kst: float = 10.5  # FOCUS 09:30 → HARPOON 10:30
    # Stage 0-10: 진입 신호 강화 (PA 2건 합의)
    pa_double_confirm_enabled: bool = False         # default OFF (paper 1주 후 검토)
    pa_double_confirm_window_sec: float = 60.0

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M (2026-04-24) — Multi-Market + Budget 분리 완전 탈바꿈
    #   부모님 결정: HARPOON 을 "FOCUS 보조" 에서 "독립 멀티마켓 엔진" 으로
    #   편지 #23 plan 참조
    # ═══════════════════════════════════════════════════════════════════

    # [Phase M.A] Scan Universe — HARPOON 자체 스캔 대상
    scan_universe: str = "all"              # "all" / "top20" / "top50" / "custom"
    scan_blacklist: List[str] = field(default_factory=list)     # 제외 코인
    scan_whitelist: List[str] = field(default_factory=list)     # 전용 코인 (비면 all 또는 universe 사용)
    scan_min_volume_usdt_24h: float = 1_000_000.0  # 유동성 바닥 (최소 거래대금)

    # [Phase M.B] Multi-position 관리
    max_concurrent_scalps: int = 3          # 동시 보유 최대 스캘프 수
    max_same_direction_scalps: int = 2      # 같은 방향 최대
    cooldown_per_coin_sec: float = 300.0    # 같은 코인 재진입 쿨다운

    # [Phase M.C] FOCUS 조율
    respect_focus_coin_lock: bool = True    # FOCUS 보유 코인은 HARPOON skip
    respect_focus_direction_lock: bool = True  # FOCUS 반대 방향 스캘프 금지
    coin_exclusive_priority: str = "first_come"  # "first_come"/"focus"/"harpoon"
    focus_entry_freeze_sec: float = 30.0    # FOCUS 진입 직후 N초 HARPOON skip

    # [Phase M.D] HARPOON 자체 신호 임계
    min_adx_self: int = 20                   # HARPOON 자체 ADX 임계 (min_adx=0 inherit 과 별개)
    min_conviction_self: float = 50.0        # [2026-05-17 100점 ×10] 5→50. HARPOON 자체 conviction (FOCUS scanner_min 과 별개)
    pa_patterns_allowed: List[str] = field(default_factory=lambda: [
        "ENGULFING", "PIN_BAR", "BOS_BULLISH", "BOS_BEARISH",
        "STAR_V2", "SQUEEZE_BREAK"   # HARPOON 우대 — 빠른 reversal/breakout
    ])

    # ★ [2026-04-24 부모 지시] HARPOON-Specific PA Weight Override
    # 부모 직관: "하푼이 잡아야 할 신호 ≠ 포커스 먹이감"
    # FOCUS conviction 점수에 PA 가산할 때 HARPOON caller 이면 이 값 사용.
    # FOCUS pa_weight (focus_config.json) 와 별개.
    #
    # 패턴별 우대 (HARPOON 단기 스캘퍼 관점):
    #   PIN_BAR        — M5 짧은 wick reject : HARPOON 우대 (FOCUS 1 → 2)
    #   ENGULFING      — 강한 모멘텀 2봉     : HARPOON 우대 (FOCUS 2 → 3)
    #   STAR_V1        — H4 큰 reversal       : FOCUS 먹이 (HARPOON 1)
    #   STAR_V2        — 추세전환 3봉         : 둘 다 강 (3)
    #   SQUEEZE_BREAK  — 압축 → 폭발          : HARPOON 우대 (3)
    #   BOS_BULLISH/BEARISH — 지지/저항 돌파  : HARPOON 우대 (3)
    pa_weight_pin_bar: int = 2
    pa_weight_engulfing: int = 3
    pa_weight_star_v1: int = 1
    pa_weight_star_v2: int = 3
    pa_weight_squeeze_break: int = 3
    pa_weight_bos: int = 3
    pa_weight_zone_bonus: int = 1
    pa_zone_proximity_atr: float = 0.5
    pa_location_penalty_far: float = 0.5

    # [Phase M.E] Zone 계산 소스
    zone_source: str = "self"                # "self" (자체 계산) / "focus" (FOCUS zone 공유)
    zone_lookback_bars: int = 50             # zone 계산 lookback

    # [Phase M.F] ★ HARPOON Standalone Mode (2026-04-24 부모 직접 요청)
    #   기본 False: FOCUS Sub-scalper (FOCUS enabled=True 필요)
    #   True: FOCUS 무관 단독 가동 (FOCUS 꺼져도 HARPOON 동작)
    #   단점 (부모 인지): FOCUS Shared guards 무력화, 시장 맥락 정보 빈약
    #   장점: 24/7 풀가동, FOCUS 사각지대 메움, Phase M 순수 검증
    harpoon_standalone_mode: bool = False


@dataclass
class ScalpPosition:
    market: str = ""
    direction: str = ""         # "LONG" or "SHORT"
    entry_price: float = 0.0
    qty: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    atr_used: float = 0.0
    entry_ts: float = 0.0
    scalp_id: int = 0          # 일련번호
    # ── Dynamic Trailing SL ──
    peak_profit_price: float = 0.0   # 최고 수익 가격
    breakeven_locked: bool = False   # SL이 손익분기로 이동됨
    original_sl: float = 0.0        # 원본 SL
    # ★ Phase M.G (2026-04-24) — BE Stall Exit / Pre-BE Stall 용 타임스탬프
    last_peak_update_ts: float = 0.0  # peak_profit_price 최근 갱신 시각
    be_locked_ts: float = 0.0          # BE 락 시각


@dataclass
class ScalpRecord:
    """완료된 스캘프 기록."""
    scalp_id: int = 0
    market: str = ""
    direction: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    qty: float = 0.0
    pnl_usdt: float = 0.0
    result: str = ""            # "TP" / "SL" / "MANUAL"
    duration_sec: float = 0.0
    ts: float = 0.0


# ---------------------------------------------------------------------------
# FOCUS state whitelist — Harpoon이 활성화될 수 있는 FOCUS 상태
# ---------------------------------------------------------------------------
# v2: DORMANT 제거 — ADX<15 횡보장에서 스캘핑도 수수료 손실
# ALERT(횡보 끝 징조)부터 활성화
FOCUS_ACTIVE_STATES = {"ALERT", "HUNT"}


# ---------------------------------------------------------------------------
# HarpoonManager
# ---------------------------------------------------------------------------

class HarpoonManager:
    """
    FOCUS 하위 스캘핑 전략 매니저.

    FOCUS의 존/h4_sig/ATR을 읽기 전용으로 소비하며,
    존 근처에서 M1 PA 패턴으로 고배율 단타를 반복한다.
    """

    def __init__(self, focus_manager: Any = None, system: Any = None):
        self.focus = focus_manager      # FocusManager (읽기 전용)
        self.system = system
        self._lock = threading.RLock()

        # Config
        self.config = HarpoonConfig()

        # State
        self.state = HarpoonState.STANDBY
        self.current_scalp: Optional[ScalpPosition] = None
        self.target_zone: Optional[Dict] = None    # 현재 타겟 존
        self.target_direction: str = ""

        # Counters
        self.scalps_this_hour: int = 0
        self.scalps_today: int = 0
        self.consecutive_losses: int = 0
        self.daily_pnl: float = 0.0
        self.total_pnl: float = 0.0
        self._next_scalp_id: int = 1

        # Timing
        self.last_tick_ts: float = 0.0
        self.cooldown_start_ts: float = 0.0
        self.hour_reset_ts: float = 0.0
        self.daily_reset_ts: float = 0.0
        self.loss_pause_until: float = 0.0   # 연속 손실 시 정지 해제 시각

        # History
        self.recent_scalps: List[Dict] = []  # 최근 50개 기록

        # ═══════════════════════════════════════════════════════════════════
        # ★ Phase M.B (2026-04-24) — Multi-position management
        #   current_scalp 병렬 유지 (기존 로직 영향 X). Phase 4+ 에서 점진 전환.
        # ═══════════════════════════════════════════════════════════════════
        self.active_scalps: List[ScalpPosition] = []     # 동시 보유 스캘프 (신규)
        self.last_scalp_exit_by_coin: Dict[str, float] = {}  # {MARKET: last_exit_ts} — cooldown_per_coin
        self._post_sl_cooldown_by_coin: Dict[tuple, float] = {}  # {(MKT, DIR): expire_ts} — post_sl_cooldown_min 공유

        # Trade client (lazy init)
        self._client = None
        self._exit_retry_count: int = 0

        # Load persisted state
        self._load_config()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def effective_budget(self) -> float:
        """Harpoon 실효 예산 계산."""
        if self.config.budget_usdt > 0:
            return self.config.budget_usdt
        # FOCUS 예산에서 자동 배분
        if self.focus:
            focus_budget = getattr(self.focus, 'budget_usdt', 0) or 0
            if focus_budget > 0:
                return focus_budget * (self.config.budget_pct / 100.0)
            # budget=0이면 시스템 잔고에서 배분
            if self.system:
                try:
                    bal = getattr(self.system, '_cached_balance', {})
                    total = float(bal.get('totalWalletBalance', 0) or 0)
                    if total > 0:
                        return total * (self.config.budget_pct / 100.0)
                except (TypeError, ValueError, AttributeError):
                    pass
        return 50.0  # fallback $50

    # ------------------------------------------------------------------
    # Lazy client
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            from app.integrations.bybit_trade import BybitTradeClient
            self._client = BybitTradeClient(category="linear")
            logger.info("[HARPOON] Linear perpetual client initialized")
        return self._client

    # ------------------------------------------------------------------
    # Price access
    # ------------------------------------------------------------------

    def _get_current_price(self, market: str) -> Optional[float]:
        try:
            from app.core.hyper_price_store import price_store
            p = price_store.get_price(market)
            if p and p > 0:
                return float(p)
        except Exception:
            pass
        try:
            p = self._get_client()._linear_last_price(market)
            if p and p > 0:
                return float(p)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    def tick(self) -> Dict[str, Any]:
        """
        Harpoon 메인 틱. FocusManager의 _scan_loop 또는 별도 루프에서 호출.

        Returns:
            상태 정보 dict
        """
        if not self.config.enabled:
            return {"state": "DISABLED"}

        # E-STOP 체크
        if self.system and bool(getattr(self.system, 'emergency_stop', False)):
            self._emergency_close()
            return {"state": "EMERGENCY_STOP"}

        with self._lock:
            now = time.time()
            self._maybe_reset_counters(now)

            # Throttle by state
            interval = self.config.standby_interval_sec if self.state == HarpoonState.STANDBY \
                else self.config.tick_interval_sec
            if (now - self.last_tick_ts) < interval:
                return {"state": self.state.value, "skipped": True}
            self.last_tick_ts = now

            # ★ FOCUS 충돌 방어: HARPOON 포지션 보유 중 FOCUS가 실제 포지션 진입 시 즉시 청산
            if self.current_scalp and self._has_focus_conflict():
                logger.warning(
                    "[HARPOON] FOCUS conflict! Closing scalp %s %s immediately",
                    self.current_scalp.market, self.current_scalp.direction,
                )
                price = self._get_current_price(self.current_scalp.market) or 0
                self._execute_scalp_exit("FOCUS_CONFLICT", price)
                self.state = HarpoonState.STANDBY
                return {"state": "STANDBY", "reason": "focus_conflict_close"}

            # FOCUS 연동 체크
            if not self._is_focus_compatible():
                if self.state != HarpoonState.STANDBY:
                    self.state = HarpoonState.STANDBY
                return {"state": "STANDBY", "reason": "focus_incompatible"}

            # 안전장치 체크
            guard = self._check_guards(now)
            if guard:
                return {"state": self.state.value, "guard": guard}

            # State dispatch
            result = {}
            try:
                if self.state == HarpoonState.STANDBY:
                    result = self._handle_standby(now)
                elif self.state == HarpoonState.ZONE_READY:
                    result = self._handle_zone_ready(now)
                elif self.state == HarpoonState.STALKING:
                    result = self._handle_stalking(now)
                elif self.state == HarpoonState.FIRED:
                    result = self._handle_fired(now)
                elif self.state == HarpoonState.COOLDOWN:
                    result = self._handle_cooldown(now)
            except Exception as exc:
                logger.error("[HARPOON] tick error: %s", exc, exc_info=True)

            # ★ Phase M.F (2026-04-24): Multi-market / Multi-position 추가 처리
            #   기존 state dispatch 이후에 실행 — primary (current_scalp) 는 기존대로,
            #   additional scalps 는 아래에서 별도 처리.
            try:
                # 1) Additional scalps Bybit 서버 상태 확인 (secondary 청산 감지)
                cleared_count = self._monitor_additional_scalps(now)
                if cleared_count > 0:
                    result["multi_cleared"] = cleared_count

                # 2) 추가 진입 시도 (max_concurrent>1 이고 슬롯 남아있을 때만)
                #    paper_mode 또는 FOCUS 비호환이면 _try_extra_entries 내부에서 skip
                if self._is_focus_compatible() and self.state not in (HarpoonState.COOLDOWN,):
                    entered_count = self._try_extra_entries(now)
                    if entered_count > 0:
                        result["multi_entered"] = entered_count
            except Exception as exc:
                logger.debug("[HARPOON M.F] tick multi-processing exception: %s", exc)

            result["state"] = self.state.value
            result["scalps_today"] = self.scalps_today
            result["daily_pnl"] = round(self.daily_pnl, 4)
            result["active_scalps_count"] = self._count_active_scalps()
            return result

    # ------------------------------------------------------------------
    # FOCUS compatibility check
    # ------------------------------------------------------------------

    def _is_focus_compatible(self) -> bool:
        """FOCUS 상태가 Harpoon 활성화에 적합한지 확인.

        v2: ADX 필터 추가 — FOCUS의 ADX가 dormant_adx_threshold 미만이면
        Harpoon도 정지 (횡보장에서 스캘핑 = 수수료 손실).

        ★ Phase M.F (2026-04-24 부모 직접 요청): Standalone 모드 지원.
           harpoon_standalone_mode=True 면 FOCUS 체크 우회 → HARPOON 단독 가동.
        """
        # ★ Standalone 모드: FOCUS 체크 전부 우회
        if getattr(self.config, "harpoon_standalone_mode", False):
            return True

        if not self.focus:
            return False
        if not getattr(self.focus, 'enabled', False):
            return False

        focus_state = getattr(self.focus, 'state', None)
        if focus_state is None:
            return False

        state_val = focus_state.value if hasattr(focus_state, 'value') else str(focus_state)

        # FOCUS가 포지션 보유 중이면 레버리지 충돌 → 정지
        if state_val in ("POSITIONED", "CAUTION"):
            return False

        # FOCUS가 쿨다운이면 시장 불리 → 정지
        if state_val == "COOLDOWN":
            return False

        # v2→v3: ADX 필터 — 하푼 전용 min_adx > 0이면 독립 임계, 아니면 FOCUS 상속
        focus_config = getattr(self.focus, 'config', None)
        if focus_config and getattr(focus_config, 'adx_filter_enabled', False):
            last_adx = getattr(self.focus, '_last_adx_value', 0)
            # 하푼 독립 임계값 우선, 없으면(0) FOCUS dormant_adx_threshold 상속
            harpoon_min = self.config.min_adx
            threshold = harpoon_min if harpoon_min > 0 else getattr(focus_config, 'dormant_adx_threshold', 15)
            if last_adx > 0 and last_adx < threshold:
                return False  # 횡보장 — 스캘핑도 위험

        return state_val in FOCUS_ACTIVE_STATES

    def _has_focus_conflict(self) -> bool:
        """FOCUS가 실제로 포지션 보유 중 → 레버리지 충돌."""
        if not self.focus:
            return False  # FOCUS 없음 = 충돌 없음
        state_val = getattr(self.focus, 'state', None)
        if state_val and hasattr(state_val, 'value'):
            state_val = state_val.value
        return str(state_val) in ("POSITIONED", "CAUTION", "TRAILING")

    # ------------------------------------------------------------------
    # Guard checks
    # ------------------------------------------------------------------

    def _check_guards(self, now: float) -> Optional[str]:
        """안전장치 체크. 위반 시 사유 문자열 반환."""
        # 연속 손실 정지
        if self.loss_pause_until > now:
            return f"consecutive_loss_pause_until_{int(self.loss_pause_until - now)}s"

        # 시간당 스캘프 제한
        if self.scalps_this_hour >= self.config.max_scalps_per_hour:
            return "max_scalps_per_hour"

        # 일일 스캘프 제한
        if self.scalps_today >= self.config.max_daily_scalps:
            return "max_daily_scalps"

        # 일일 손실 제한
        budget = self.effective_budget
        if budget > 0:
            loss_pct = abs(min(0, self.daily_pnl)) / budget * 100
            if loss_pct >= self.config.max_daily_loss_pct:
                return f"max_daily_loss_{loss_pct:.1f}pct"

        return None

    # ------------------------------------------------------------------
    # ★ Stage 0 (2026-04-22 부모 B 결정, 동생 plan v3 통합) ★
    # 진입 직전 9 가드 + paper_mode JSONL 기록
    # B11/J v2/Morning Guard/coin_loss_cap = FOCUS 공유
    # Fast-Reject v2 / 재진입 cooldown / Morning Guard 확장 = HARPOON 독자
    # ★ 형 letter FAIL (2026-04-22 22:42) 수정:
    #   - _get_focus_adx 신규 메서드 (Stage 0-2 활성화)
    #   - 0-3 J v2 최소 구현 (stub → 활성)
    # ------------------------------------------------------------------

    def _get_focus_adx(self) -> float:
        """FOCUS 의 최근 H4 ADX 값 조회 (Stage 0-2/0-3 용).

        ★ 형 letter FAIL (2026-04-22 22:42) 수정 — 누락 메서드 추가.
        FOCUS 가 _last_adx_value 필드로 노출 (이미 L355 등에서 사용 중).
        """
        try:
            if not self.focus:
                return 0.0
            return float(getattr(self.focus, "_last_adx_value", 0) or 0)
        except Exception:
            return 0.0

    def _check_stage0_gates(self, market: str, direction: str,
                            atr: float, price: float) -> tuple:
        """진입 직전 Stage 0 9 가드 통합 검사.

        Returns: (blocked: bool, reason: str)
        """
        cfg = self.config
        focus_mgr = self.focus  # FocusManager reference (있을 때만)

        # Stage 0-1: B11 regime_lock 통합
        if getattr(cfg, "respect_b11_regime_lock", True) and focus_mgr is not None:
            try:
                if hasattr(focus_mgr, "_get_btc_regime_lock_reason"):
                    blocked, reason = focus_mgr._get_btc_regime_lock_reason(direction)
                    if blocked:
                        return True, f"b11_regime_lock: {reason}"
            except Exception as exc:
                logger.debug("[HARPOON] B11 check failed: %s", exc)

        # Stage 0-2: min_adx 강화 (기본 0 → 20 fallback)
        try:
            adx_threshold = cfg.min_adx if cfg.min_adx > 0 else getattr(cfg, "min_adx_v2", 20)
            current_adx = self._get_focus_adx() if hasattr(self, "_get_focus_adx") else 0
            if current_adx > 0 and current_adx < adx_threshold:
                return True, f"adx_below_threshold: {current_adx:.1f} < {adx_threshold}"
        except Exception:
            pass  # ADX fetch 실패 시 통과 (안전 fallback)

        # Stage 0-3: FOCUS J v2 공유 (★ 형 letter FAIL 4-4 적용)
        if getattr(cfg, "respect_focus_adx_slope", True) and focus_mgr is not None:
            try:
                if getattr(focus_mgr.config, "adx_slope_check_enabled", False):
                    # FOCUS J v2 가 ON 이면 HARPOON 도 시장 식어감 시 skip
                    # 최소 구현: ADX 가 J v2 기준 (FOCUS scanner_min_adx) 이하면 차단
                    adx = self._get_focus_adx()
                    j_threshold = float(getattr(focus_mgr.config, "scanner_min_adx", 18.0))
                    if adx > 0 and adx < j_threshold:
                        return True, f"focus_j_v2_adx_cooling: {adx:.1f} < {j_threshold}"
            except Exception:
                pass

        # Stage 0-4: Morning Guard 자동 standby + Stage 0-9 시간 확장 (HARPOON 만)
        if getattr(cfg, "respect_morning_guard", True) and focus_mgr is not None:
            try:
                from datetime import datetime, timezone, timedelta
                kst_now = datetime.now(tz=timezone(timedelta(hours=9)))
                hour = kst_now.hour + kst_now.minute / 60.0
                # FOCUS Morning Guard 시간대 (06:50 ~ end_hour) + HARPOON 확장 시간
                mg_enabled = getattr(focus_mgr.config, "morning_guard_enabled", True)
                if mg_enabled:
                    fg_end = float(getattr(focus_mgr.config, "morning_guard_end_hour_kst", 9.5))
                    hp_end = float(getattr(cfg, "morning_extended_end_hour_kst", fg_end))
                    end_hour = max(fg_end, hp_end)  # HARPOON 더 보수적 (긴 시간)
                    if 6.83 <= hour <= end_hour:  # 06:50~
                        return True, f"morning_guard_active (KST {hour:.2f}h, end={end_hour})"
            except Exception as exc:
                logger.debug("[HARPOON] Morning Guard check failed: %s", exc)

        # Stage 0-5: coin_loss_cap 공유
        if getattr(cfg, "respect_coin_loss_cap", True) and focus_mgr is not None:
            try:
                if (getattr(focus_mgr.config, "coin_loss_cap_enabled", True)
                        and hasattr(focus_mgr, "_get_coin_loss_total")):
                    loss_total = focus_mgr._get_coin_loss_total(market, direction)
                    cap = float(getattr(focus_mgr.config, "coin_loss_cap_amount", 30.0))
                    if loss_total < 0 and abs(loss_total) >= cap:
                        return True, f"coin_loss_cap_exceeded: ${loss_total:.2f} >= ${cap}"
            except Exception as exc:
                logger.debug("[HARPOON] coin_loss_cap check failed: %s", exc)

        # Stage 0-7: 재진입 30분 cooldown (HARPOON 자체 SL/Fast-Reject 후)
        cd_min = float(getattr(cfg, "post_sl_cooldown_min", 30.0))
        if cd_min > 0:
            cd_sec = cd_min * 60.0
            now_ts = time.time()
            cooldown_map = getattr(self, "_post_sl_cooldown_map", {})
            key = (market.upper(), direction.upper())
            last_sl_ts = cooldown_map.get(key, 0)
            if last_sl_ts > 0 and (now_ts - last_sl_ts) < cd_sec:
                remain = cd_sec - (now_ts - last_sl_ts)
                return True, f"post_sl_cooldown: {remain/60:.1f}min remain"

        return False, "all_passed"

    def _log_paper_entry(self, market: str, direction: str, price: float, atr: float) -> None:
        """paper_mode 진입 시도 JSONL 기록 (Phase K 패턴)."""
        try:
            log_path = os.path.join("runtime", "harpoon_paper_log.jsonl")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            entry = {
                "ts": time.time(),
                "market": market,
                "direction": direction,
                "price": price,
                "atr": atr,
                "tp_estimate": price + atr * self.config.tp_atr_mult * (1 if direction == "LONG" else -1),
                "sl_estimate": price - atr * self.config.sl_atr_mult * (1 if direction == "LONG" else -1),
                "leverage": self.config.leverage,
                "budget_estimate": self.effective_budget,
                "paper_mode": True,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("[HARPOON] paper log failed: %s", exc)

    def _record_post_sl(self, market: str, direction: str) -> None:
        """SL/Fast-Reject 발생 시 cooldown map 갱신 (Stage 0-7)."""
        if not hasattr(self, "_post_sl_cooldown_map"):
            self._post_sl_cooldown_map = {}
        self._post_sl_cooldown_map[(market.upper(), direction.upper())] = time.time()

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _handle_standby(self, now: float) -> Dict:
        """STANDBY: zone 에 가격이 근접하는지 감시.
        ★ Phase M.E + M.F: standalone 모드면 자체 zone 계산, 아니면 FOCUS zones."""
        market = self._get_focus_market()
        if not market:
            return {"reason": "no_market"}
        zones = self._get_zones_for_harpoon(market)
        if not zones:
            return {"reason": "no_zones"}

        price = self._get_current_price(market)
        if not price:
            return {"reason": "no_price"}

        atr = self._get_focus_atr()
        if not atr or atr <= 0:
            return {"reason": "no_atr"}

        proximity = atr * self.config.zone_proximity_atr

        # 가장 가까운 존 찾기 (양방향 — 존 타입이 방향 결정)
        best_zone = self._find_nearest_zone(price, "", zones, proximity)
        if best_zone:
            # ★ 존 타입에 따라 스캘프 방향 결정: SUPPORT→LONG, RESISTANCE→SHORT
            z_type = best_zone.get("type", "").upper()
            if "RESISTANCE" in z_type:
                scalp_dir = "SHORT"
            else:
                scalp_dir = "LONG"

            self.target_zone = best_zone
            self.target_direction = scalp_dir
            self.state = HarpoonState.ZONE_READY
            logger.info(
                "[HARPOON] STANDBY → ZONE_READY: %s %s zone %s $%.2f~$%.2f (price=$%.2f, dist=$%.2f)",
                market, scalp_dir, z_type,
                best_zone.get("price_low", 0), best_zone.get("price_high", 0),
                price, abs(price - best_zone.get("price_low", price)),
            )
            self._save_config()
            return {"transition": "ZONE_READY", "zone": best_zone}

        return {"reason": "no_nearby_zone"}

    def _handle_zone_ready(self, now: float) -> Dict:
        """ZONE_READY: 존 근처 도달, M1 PA 대기 시작."""
        market = self._get_focus_market()
        if not market:
            self.state = HarpoonState.STANDBY
            return {"reason": "lost_market"}

        price = self._get_current_price(market)
        if not price:
            return {"reason": "no_price"}

        atr = self._get_focus_atr()
        if not atr:
            self.state = HarpoonState.STANDBY
            return {"reason": "no_atr"}

        # 존에서 멀어졌는지 체크
        proximity = atr * self.config.zone_proximity_atr
        if self.target_zone:
            zone_mid = (self.target_zone.get("price_low", 0) + self.target_zone.get("price_high", 0)) / 2
            if abs(price - zone_mid) > proximity * 1.5:
                logger.info("[HARPOON] ZONE_READY → STANDBY: price moved away from zone")
                self.state = HarpoonState.STANDBY
                self.target_zone = None
                return {"reason": "price_moved_away"}

        # ── M5 추세 필터 (FOCUS의 H1 체크와 동일 역할) ──
        m5_trend = self._check_m5_trend(market, self.target_direction)
        if m5_trend == "opposed":
            logger.debug(
                "[HARPOON] ZONE_READY skip: M5 trend opposed to %s — waiting",
                self.target_direction,
            )
            return {"waiting": "m5_opposed"}

        # M1 PA 패턴 체크
        pa_signal = self._check_m1_pa(market, self.target_direction)
        if pa_signal:
            self.state = HarpoonState.STALKING
            logger.info("[HARPOON] ZONE_READY → STALKING: M1 %s detected (m5=%s)",
                        pa_signal.get("pattern"), m5_trend or "n/a")
            return {"transition": "STALKING", "pa": pa_signal}

        return {"waiting": "m1_pa"}

    def _handle_stalking(self, now: float) -> Dict:
        """STALKING: M1 PA 확인됨, 진입 실행."""
        market = self._get_focus_market()
        if not market:
            self.state = HarpoonState.STANDBY
            return {"reason": "lost_market"}

        price = self._get_current_price(market)
        if not price:
            return {"reason": "no_price"}

        atr = self._get_focus_atr()
        if not atr:
            self.state = HarpoonState.STANDBY
            return {"reason": "no_atr"}

        # ── M5 추세 재확인 (진입 직전) ──
        m5_trend = self._check_m5_trend(market, self.target_direction)
        if m5_trend == "opposed":
            logger.info("[HARPOON] STALKING → ZONE_READY: M5 trend opposed to %s", self.target_direction)
            self.state = HarpoonState.ZONE_READY
            return {"reason": "m5_opposed", "transition": "ZONE_READY"}

        # ★ M17 FIX: 진입 전 M1 PA 신선도 재확인 (stale signal 방지)
        pa_recheck = self._check_m1_pa(market, self.target_direction)
        if not pa_recheck:
            self.state = HarpoonState.ZONE_READY
            logger.info("[HARPOON] STALKING → ZONE_READY: M1 PA no longer valid")
            return {"reason": "pa_stale", "transition": "ZONE_READY"}

        # ★★★ Stage 0 (2026-04-22 부모 B 결정, plan v3 통합) ★★★
        # 진입 직전 9 가드 + paper_mode 마지막 게이트.
        # 형 letter #11 검수 기준 동일 패턴, default safe (paper_mode=True).
        stage0_blocked, stage0_reason = self._check_stage0_gates(market, self.target_direction, atr, price)
        if stage0_blocked:
            logger.info("[HARPOON] STAGE 0 BLOCK: %s %s — %s", market, self.target_direction, stage0_reason)
            self.state = HarpoonState.COOLDOWN
            self.cooldown_start_ts = now
            return {"reason": "stage0_blocked", "detail": stage0_reason}

        # ★ paper_mode 마지막 게이트 (Phase K 패턴: 진입 직전 차단 + JSONL 기록만)
        if getattr(self.config, "paper_mode", True):
            self._log_paper_entry(market, self.target_direction, price, atr)
            logger.info(
                "[HARPOON] PAPER skip: %s %s @ $%.4f atr=$%.4f (실거래 X, JSONL 기록만)",
                market, self.target_direction, price, atr,
            )
            self.state = HarpoonState.COOLDOWN
            self.cooldown_start_ts = now
            return {"reason": "paper_mode_skip", "market": market, "direction": self.target_direction}

        # 진입 실행
        try:
            result = self._execute_scalp_entry(market, self.target_direction, price, atr)
            if result and result.get("success"):
                self.state = HarpoonState.FIRED
                logger.info(
                    "[HARPOON] STALKING → FIRED: %s %s @ $%.4f qty=%.6f TP=$%.4f SL=$%.4f",
                    market, self.target_direction, price,
                    result.get("qty", 0), result.get("tp", 0), result.get("sl", 0),
                )
                self._save_config()
                return {"transition": "FIRED", **result}
            else:
                # 진입 실패 → 쿨다운
                self.state = HarpoonState.COOLDOWN
                self.cooldown_start_ts = now
                return {"reason": "entry_failed", "detail": result}
        except Exception as exc:
            logger.error("[HARPOON] entry execution error: %s", exc, exc_info=True)
            self.state = HarpoonState.COOLDOWN
            self.cooldown_start_ts = now
            return {"reason": "entry_error", "error": str(exc)}

    def _handle_fired(self, now: float) -> Dict:
        """FIRED: 포지션 보유 중, TP/SL 모니터링."""
        if not self.current_scalp:
            self.state = HarpoonState.STANDBY
            return {"reason": "no_scalp_position"}

        market = self.current_scalp.market
        price = self._get_current_price(market)
        if not price:
            return {"reason": "no_price"}

        # ★ Bybit 실포지션 확인 — 서버사이드 TP/SL로 이미 청산됐으면 고스트 방지
        if not hasattr(self, '_last_bybit_check_ts'):
            self._last_bybit_check_ts = 0.0
        if now - self._last_bybit_check_ts >= 10.0:  # 10초마다 체크
            self._last_bybit_check_ts = now
            try:
                from app.core.constants import BYBIT_POSITION_LIST
                client = self._get_client()
                resp = client._request("GET", BYBIT_POSITION_LIST,
                                       params={"category": "linear", "symbol": market})
                pos_list = resp.get("result", {}).get("list", [])
                has_position = any(float(p.get("size", 0)) > 0 for p in pos_list)
                if not has_position:
                    # Bybit에 포지션 없음 → 서버사이드 TP/SL로 이미 청산됨
                    scalp = self.current_scalp
                    entry = scalp.entry_price
                    # TP/SL 중 어느 쪽이 체결됐는지 추정 → 해당 가격을 exit_price로 사용
                    if scalp.direction == "LONG":
                        _tp_hit = price >= scalp.tp if scalp.tp else False
                        exit_price = scalp.tp if _tp_hit else (scalp.sl if scalp.sl else price)
                    else:
                        _tp_hit = price <= scalp.tp if scalp.tp else False
                        exit_price = scalp.tp if _tp_hit else (scalp.sl if scalp.sl else price)
                    exit_reason = "SERVER_TP" if _tp_hit else "SERVER_SL"

                    if scalp.direction == "LONG":
                        pnl = (exit_price - entry) * scalp.qty
                    else:
                        pnl = (entry - exit_price) * scalp.qty
                    fee = exit_price * scalp.qty * 0.00055 * 2
                    pnl -= fee
                    duration = now - scalp.entry_ts

                    logger.info(
                        "[HARPOON] FIRED → position gone (%s): %s %s @ $%.4f → $%.4f | PnL=$%.4f | %.1fs",
                        exit_reason, scalp.market, scalp.direction, entry, exit_price, pnl, duration,
                    )

                    # ── ScalpRecord + recent_scalps ──
                    record = ScalpRecord(
                        scalp_id=scalp.scalp_id, market=scalp.market,
                        direction=scalp.direction, entry_price=entry,
                        exit_price=exit_price, qty=scalp.qty,
                        pnl_usdt=round(pnl, 4), result=exit_reason,
                        duration_sec=round(duration, 1), ts=now,
                    )
                    self.recent_scalps.append(asdict(record))
                    if len(self.recent_scalps) > 50:
                        self.recent_scalps = self.recent_scalps[-50:]

                    # ── 카운터 업데이트 ──
                    self.scalps_today += 1
                    self.scalps_this_hour += 1
                    self.daily_pnl += pnl
                    self.total_pnl += pnl
                    if pnl < 0:
                        self.consecutive_losses += 1
                        # ★ Stage 0-7 hook (2026-04-22 형 letter FAIL 4-3 수정):
                        # SL/Fast-Reject 발생 시 (market, direction) 30분 cooldown 기록
                        try:
                            self._record_post_sl(scalp.market, scalp.direction)
                        except Exception as exc:
                            logger.debug("[HARPOON] _record_post_sl failed: %s", exc)
                    else:
                        self.consecutive_losses = 0

                    # ── 장부 기록 (journal + ledger) ──
                    try:
                        from app.manager.trade_journal import journal
                        _peak = 0.0
                        if scalp.peak_profit_price > 0 and entry > 0:
                            if scalp.direction == "LONG":
                                _peak = (scalp.peak_profit_price / entry - 1) * 100
                            else:
                                _peak = (1 - scalp.peak_profit_price / entry) * 100
                        journal.record_exit(
                            strategy="HARPOON", market=scalp.market,
                            direction=scalp.direction,
                            entry_price=entry, exit_price=exit_price, qty=scalp.qty,
                            reason=exit_reason, leverage=self.config.leverage,
                            hold_sec=duration,
                            dynamic_trailing=self.config.dynamic_trailing,
                            breakeven_locked=scalp.breakeven_locked,
                            peak_profit_pct=_peak,
                        )
                    except Exception:
                        pass
                    try:
                        if self.system and hasattr(self.system, 'ledger'):
                            self.system.ledger.append(
                                "HARPOON_EXIT", market=scalp.market,
                                direction=scalp.direction, qty=scalp.qty,
                                entry_price=entry, exit_price=exit_price,
                                pnl_usdt=round(pnl, 4), reason=exit_reason,
                                duration_sec=round(duration, 1),
                            )
                    except Exception:
                        pass

                    self._clear_current_scalp_from_active()  # Phase M.B sync
                    self.current_scalp = None
                    self.state = HarpoonState.COOLDOWN
                    self.cooldown_start_ts = now
                    self._save_config()
                    return {"exit": exit_reason, "pnl": round(pnl, 4)}
            except Exception as exc:
                logger.debug("[HARPOON] Bybit position check failed: %s", exc)

        # ── Dynamic Trailing SL (exit 체크 전에 SL 갱신) ──
        if self.config.dynamic_trailing:
            self._apply_scalp_trailing_sl(self.current_scalp, price)

        from app.strategy.greenpen.harpoon_tp import ScalpTargets, should_scalp_exit

        targets = ScalpTargets(
            tp=self.current_scalp.tp,
            sl=self.current_scalp.sl,  # dynamic trailing이 갱신했을 수 있음
            atr_used=self.current_scalp.atr_used,
        )

        exit_reason = should_scalp_exit(price, self.current_scalp.entry_price,
                                        self.current_scalp.direction, targets)
        if exit_reason:
            result = self._execute_scalp_exit(exit_reason, price)
            return {"exit": exit_reason, **result}

        # Timeout: 5분 이상 보유 시 시장가 청산
        hold_time = now - self.current_scalp.entry_ts
        if hold_time > 300:  # 5분
            logger.warning("[HARPOON] scalp timeout (%.0fs), closing at market", hold_time)
            result = self._execute_scalp_exit("TIMEOUT", price)
            return {"exit": "TIMEOUT", **result}

        # 현재 PnL 표시
        entry = self.current_scalp.entry_price
        if self.current_scalp.direction == "LONG":
            unrealized = (price - entry) * self.current_scalp.qty
        else:
            unrealized = (entry - price) * self.current_scalp.qty

        return {"holding": True, "unrealized_pnl": round(unrealized, 4),
                "hold_sec": round(hold_time, 1)}

    # ------------------------------------------------------------------
    # Dynamic Trailing SL (스캘퍼 전용)
    # ------------------------------------------------------------------

    def _apply_scalp_trailing_sl(self, scalp: ScalpPosition, price: float):
        """스캘퍼용 동적 트레일링 SL. FOCUS와 동일 구조, 파라미터만 타이트.

        Stage 1: profit >= breakeven_trigger_pct → SL → 진입가 (손익분기 잠금)
        Stage 2: 이후 최고수익의 trailing_preserve_pct%를 보존하도록 SL 추적
        """
        # 원본 SL 기록 (최초 1회)
        if scalp.original_sl <= 0:
            scalp.original_sl = scalp.sl

        # 수익률 계산
        if scalp.direction == "LONG":
            pnl_pct = (price / scalp.entry_price - 1) * 100 if scalp.entry_price > 0 else 0
        else:
            pnl_pct = (1 - price / scalp.entry_price) * 100 if scalp.entry_price > 0 else 0

        # 최고 수익 가격 추적
        if scalp.direction == "LONG":
            if price > scalp.peak_profit_price:
                scalp.peak_profit_price = price
        else:
            if scalp.peak_profit_price <= 0 or price < scalp.peak_profit_price:
                scalp.peak_profit_price = price

        trigger_pct = self.config.breakeven_trigger_pct
        preserve_ratio = self.config.trailing_preserve_pct / 100.0

        # ── Stage 1: 손익분기 잠금 ──
        if not scalp.breakeven_locked and pnl_pct >= trigger_pct:
            new_sl = scalp.entry_price
            should_lock = False
            if scalp.direction == "LONG" and new_sl > scalp.sl:
                should_lock = True
            elif scalp.direction == "SHORT" and new_sl < scalp.sl:
                should_lock = True

            if should_lock:
                old_sl = scalp.sl
                scalp.sl = new_sl
                scalp.breakeven_locked = True
                scalp.be_locked_ts = time.time()  # ★ Phase M.G: BE Stall Exit 용
                logger.info(
                    "[HARPOON] BREAKEVEN LOCK %s %s: SL $%.2f->$%.2f (profit +%.3f%%)",
                    scalp.direction, scalp.market, old_sl, scalp.sl, pnl_pct,
                )
                self._update_scalp_bybit_sl(scalp)
                self._save_config()
                return

        # ── Stage 2: 수익 추적 ──
        if scalp.breakeven_locked and scalp.peak_profit_price > 0:
            if scalp.direction == "LONG":
                peak_gain = scalp.peak_profit_price / scalp.entry_price - 1
                trail_offset = peak_gain * preserve_ratio
                new_sl = scalp.entry_price * (1 + trail_offset)
                if new_sl > scalp.sl:
                    old_sl = scalp.sl
                    scalp.sl = round(new_sl, 4)
                    logger.info(
                        "[HARPOON] TRAIL %s %s: SL $%.2f->$%.2f (peak +%.3f%%, locked +%.3f%%)",
                        scalp.direction, scalp.market, old_sl, scalp.sl,
                        peak_gain * 100, trail_offset * 100,
                    )
                    self._update_scalp_bybit_sl(scalp)
                    self._save_config()
            else:  # SHORT
                peak_gain = 1 - scalp.peak_profit_price / scalp.entry_price
                trail_offset = peak_gain * preserve_ratio
                new_sl = scalp.entry_price * (1 - trail_offset)
                if new_sl < scalp.sl:
                    old_sl = scalp.sl
                    scalp.sl = round(new_sl, 4)
                    logger.info(
                        "[HARPOON] TRAIL %s %s: SL $%.2f->$%.2f (peak +%.3f%%, locked +%.3f%%)",
                        scalp.direction, scalp.market, old_sl, scalp.sl,
                        peak_gain * 100, trail_offset * 100,
                    )
                    self._update_scalp_bybit_sl(scalp)
                    self._save_config()

    def _update_scalp_bybit_sl(self, scalp: ScalpPosition):
        """Bybit에 SL만 업데이트 (TP 유지). 실패 시 1회 재시도."""
        for _try in range(2):
            try:
                self._get_client().set_trading_stop(
                    scalp.market, take_profit=scalp.tp, stop_loss=scalp.sl,
                )
                return
            except Exception as exc:
                logger.warning("[HARPOON] Bybit SL update failed %s (attempt %d/2): %s",
                               scalp.market, _try + 1, exc)
                if _try == 0:
                    import time as _t; _t.sleep(0.5)

    def _handle_cooldown(self, now: float) -> Dict:
        """COOLDOWN: 스캘프 종료 후 대기."""
        elapsed = now - self.cooldown_start_ts
        if elapsed >= self.config.cooldown_sec:
            self.state = HarpoonState.STANDBY
            self.target_zone = None
            logger.info("[HARPOON] COOLDOWN → STANDBY (%.0fs)", elapsed)
            return {"transition": "STANDBY"}
        return {"cooldown_remaining": round(self.config.cooldown_sec - elapsed, 1)}

    # ------------------------------------------------------------------
    # Zone helpers
    # ------------------------------------------------------------------

    def _get_focus_zones(self) -> List[Dict]:
        # ★ A2 FIX: shallow copy — 레이스 방지
        if not self.focus:
            return []
        zones = getattr(self.focus, 'zones', []) or []
        return list(zones)

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.E (2026-04-24) — 자체 zone 계산 (Standalone 모드 지원)
    # ═══════════════════════════════════════════════════════════════════

    def _compute_self_zones(self, market: str) -> List[Dict]:
        """HARPOON 자체 M1 lookback zone 계산.

        FOCUS 의존 없이 M1 N봉 (zone_lookback_bars, default 50) 에서
        Swing High (저항) / Swing Low (지지) 클러스터링.

        Returns: FOCUS zones 와 동일 구조 List[Dict]
          [{type: "SUPPORT"/"RESISTANCE", price_low, price_high, strength}]
        """
        if not market:
            return []
        try:
            cfg = self.config
            lookback = int(getattr(cfg, "zone_lookback_bars", 50))
            client = self._get_client()
            klines = client.get_kline(market, interval=getattr(cfg, "entry_tf", "1"), limit=lookback)
            if not klines or len(klines) < 10:
                return []

            # OHLC 추출 (Bybit 형식: [ts, open, high, low, close, volume, ...])
            highs = []
            lows = []
            closes = []
            for k in klines:
                try:
                    highs.append(float(k[2]))
                    lows.append(float(k[3]))
                    closes.append(float(k[4]))
                except (IndexError, ValueError, TypeError):
                    continue
            if len(highs) < 10 or len(lows) < 10:
                return []

            # ATR 추정 (단순화: high-low 평균)
            ranges = [h - l for h, l in zip(highs, lows)]
            atr = sum(ranges) / len(ranges) if ranges else 0
            if atr <= 0:
                return []
            tolerance = atr * 0.3   # 가격 클러스터링 tolerance

            # Swing High / Low 추출 (앞뒤 2봉 비교)
            swing_highs = []
            swing_lows = []
            for i in range(2, len(highs) - 2):
                if highs[i] >= max(highs[i-2], highs[i-1], highs[i+1], highs[i+2]):
                    swing_highs.append(highs[i])
                if lows[i] <= min(lows[i-2], lows[i-1], lows[i+1], lows[i+2]):
                    swing_lows.append(lows[i])

            # 가격 클러스터링 (가까운 가격 묶기)
            def cluster(prices: list, tol: float) -> List[List[float]]:
                if not prices:
                    return []
                sorted_p = sorted(prices)
                clusters = [[sorted_p[0]]]
                for p in sorted_p[1:]:
                    if abs(p - clusters[-1][-1]) <= tol:
                        clusters[-1].append(p)
                    else:
                        clusters.append([p])
                # 큰 클러스터 우선 (자주 닿은 곳 = 강한 zone)
                clusters.sort(key=len, reverse=True)
                return clusters

            high_clusters = cluster(swing_highs, tolerance)
            low_clusters = cluster(swing_lows, tolerance)

            zones = []
            # 상위 3개 RESISTANCE
            for cl in high_clusters[:3]:
                if len(cl) < 2:  # 최소 2개 swing 모인 곳만
                    continue
                zones.append({
                    "type": "RESISTANCE",
                    "price_low": min(cl),
                    "price_high": max(cl),
                    "strength": min(1.0, len(cl) / 5.0),  # 5개 모이면 strength 1.0
                })
            # 상위 3개 SUPPORT
            for cl in low_clusters[:3]:
                if len(cl) < 2:
                    continue
                zones.append({
                    "type": "SUPPORT",
                    "price_low": min(cl),
                    "price_high": max(cl),
                    "strength": min(1.0, len(cl) / 5.0),
                })
            return zones
        except Exception as exc:
            logger.debug("[HARPOON M.E] self-zone compute failed for %s: %s", market, exc)
            return []

    def _get_zones_for_harpoon(self, market: str = "") -> List[Dict]:
        """Phase M.E + M.F (2026-04-24 부모 명시):
        Standalone 모드일 때만 자체 zone 계산. 기본은 FOCUS zones 사용.

        - harpoon_standalone_mode = True: 자체 M1 lookback 계산 (FOCUS 무관)
                                          fallback: 자체 결과 없으면 FOCUS zones
        - harpoon_standalone_mode = False (default): FOCUS zones 그대로 (기존 동작)
        """
        cfg = self.config
        standalone = bool(getattr(cfg, "harpoon_standalone_mode", False))
        if standalone:
            if not market:
                market = self._get_focus_market()
            zones = self._compute_self_zones(market)
            if not zones:
                # fallback: FOCUS zones 시도 (FOCUS 가 켜져 있다면)
                zones = self._get_focus_zones()
            return zones
        # 기본 (Sub-scalper) — FOCUS zones 사용
        return self._get_focus_zones()

    def _get_focus_market(self) -> str:
        if not self.focus:
            return ""
        market = getattr(self.focus, 'selected_market', "") or ""
        if not market:
            # ★ FOCUS IDLE일 때 lock_market fallback
            cfg = getattr(self.focus, 'config', None)
            if cfg:
                market = getattr(cfg, 'lock_market', "") or ""
        return market

    def _get_focus_direction(self) -> str:
        if not self.focus:
            return ""
        # [2026-05-15] FOCUS h4_sig→primary_sig 개명 — 구 속성 fallback 유지
        sig = getattr(self.focus, 'primary_sig', None) or getattr(self.focus, 'h4_sig', None)
        if sig and isinstance(sig, dict):
            return sig.get("direction", "")
        return ""

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.A (2026-04-24) — Multi-Market Scanner (독립 scan universe)
    # ═══════════════════════════════════════════════════════════════════

    def _get_scan_universe(self) -> List[str]:
        """HARPOON 스캔 대상 마켓 리스트 반환.

        Priority:
          1. whitelist 비어있지 않음 → whitelist 만 스캔
          2. scan_universe == "custom" → whitelist 필수 (비면 빈 리스트)
          3. scan_universe == "top20" / "top50" → FOCUS scanner_pool 또는 Bybit 상위 N
          4. scan_universe == "all" → FOCUS scanner 에 등록된 전체 pool

        blacklist 는 최종 단계에서 제외.
        """
        cfg = self.config
        universe_mode = (getattr(cfg, "scan_universe", "all") or "all").lower()
        whitelist = [m.upper() for m in getattr(cfg, "scan_whitelist", []) if m]
        blacklist_set = {m.upper() for m in getattr(cfg, "scan_blacklist", []) if m}

        candidates: List[str] = []

        # 1) whitelist 우선
        if whitelist:
            candidates = list(whitelist)
        elif universe_mode == "custom":
            # custom 인데 whitelist 비면 스캔 X
            return []
        else:
            # 2) FOCUS scanner pool 상속 (top_n 반영)
            # FOCUS 가 이미 유동성/등급 필터 적용한 리스트 — 재사용
            try:
                if self.focus:
                    # FOCUS scanner 가 평가하는 전체 풀 참조
                    pool = getattr(self.focus, "_scanner_market_pool", None)
                    if pool and isinstance(pool, (list, tuple)):
                        candidates = [str(m).upper() for m in pool]
                    else:
                        # fallback: scan_list endpoint 용 캐시
                        scan_cache = getattr(self.focus, "_last_scan_list", None)
                        if scan_cache and isinstance(scan_cache, (list, tuple)):
                            candidates = [str(item.get("market","")).upper() if isinstance(item, dict) else str(item).upper()
                                          for item in scan_cache if item]
            except Exception as exc:
                logger.debug("[HARPOON] scan universe fetch from focus failed: %s", exc)

            # top_n 제한
            if universe_mode == "top20":
                candidates = candidates[:20]
            elif universe_mode == "top50":
                candidates = candidates[:50]
            # "all" 은 제한 없음

        # 3) blacklist 제외
        if blacklist_set:
            candidates = [m for m in candidates if m not in blacklist_set]

        # 4) 중복 제거 + 빈값 제거
        seen = set()
        result = []
        for m in candidates:
            if m and m not in seen:
                seen.add(m)
                result.append(m)
        return result

    def _get_harpoon_candidates(self) -> List[Dict[str, Any]]:
        """HARPOON 스캔 후보 리스트 반환 (Phase 1 — 1차 필터만).

        각 후보는 dict 형태:
          {
            "market": str,
            "direction": "LONG"/"SHORT",
            "conviction": int,
            "adx": float,
            "pa_pattern": str,
            "price": float,
            "atr": float,
          }

        필터 순서:
          1. scan universe (blacklist/whitelist 포함) — _get_scan_universe
          2. 유동성 (min_volume_usdt_24h) — Phase 2 에서 Bybit API 통합
          3. ADX 바닥 (min_adx_self 또는 min_adx inherit)
          4. Conviction 바닥 (min_conviction_self)
          5. PA 패턴 허용 리스트 (pa_patterns_allowed)

        Phase 1 범위: universe + PA 패턴 필터만. 실제 신호 재사용 (FOCUS scanner 결과).
        Phase 2~3 에서: 자체 PA 감지 + 유동성 + TP/SL 계산.
        """
        cfg = self.config
        universe = self._get_scan_universe()
        if not universe:
            return []

        # FOCUS scanner 가 이미 평가한 최신 결과 재사용
        scan_list = []
        try:
            if self.focus:
                scan_list = getattr(self.focus, "_last_scan_list", None) or []
        except Exception:
            scan_list = []

        if not scan_list:
            return []

        min_adx = int(getattr(cfg, "min_adx_self", 20))
        # min_adx=0 이면 FOCUS dormant 상속
        if min_adx <= 0:
            min_adx = int(getattr(cfg, "min_adx", 0)) or 15
        min_conv = float(getattr(cfg, "min_conviction_self", 50.0))  # [2026-05-17 100점 ×10] 5→50
        pa_allowed = set(getattr(cfg, "pa_patterns_allowed", []) or [])

        candidates = []
        for item in scan_list:
            if not isinstance(item, dict):
                continue
            mkt = str(item.get("market", "")).upper()
            if mkt not in universe:
                continue
            # 신호 없으면 skip (HOLD)
            sig = str(item.get("signal", "")).upper()
            if sig not in ("BUY", "SELL"):
                continue
            direction = "LONG" if sig == "BUY" else "SHORT"
            # ADX / Conviction 필터
            adx = float(item.get("adx", 0) or 0)
            if adx < min_adx:
                continue
            conv = int(item.get("conviction", 0) or 0)

            # ★ [2026-04-24] HARPOON-Specific PA Weight Override
            # FOCUS conviction 은 caller="focus" 가중치로 계산됨.
            # HARPOON 우대 패턴 (PIN_BAR/ENGULFING/SQUEEZE/BOS) 은 추가 가산,
            # FOCUS 우대 패턴 (STAR_V1) 은 차감.
            # delta = HARPOON_w - FOCUS_w (이미 포함된 것과의 차이만 더함).
            pa_name = str(item.get("pa_pattern", "") or "").upper()
            if pa_name and self.focus:
                try:
                    fcfg = self.focus.config
                    focus_w_map = {
                        "PIN_BAR": int(getattr(fcfg, "pa_weight_pin_bar", 1)),
                        "ENGULFING": int(getattr(fcfg, "pa_weight_engulfing", 2)),
                        "STAR_V1": int(getattr(fcfg, "pa_weight_star_v1", 3)),
                        "STAR_V2": int(getattr(fcfg, "pa_weight_star_v2", 3)),
                        "SQUEEZE_BREAK": int(getattr(fcfg, "pa_weight_squeeze_break", 2)),
                        "BOS_BULLISH": int(getattr(fcfg, "pa_weight_bos", 2)),
                        "BOS_BEARISH": int(getattr(fcfg, "pa_weight_bos", 2)),
                    }
                    harpoon_w_map = {
                        "PIN_BAR": int(getattr(cfg, "pa_weight_pin_bar", 2)),
                        "ENGULFING": int(getattr(cfg, "pa_weight_engulfing", 3)),
                        "STAR_V1": int(getattr(cfg, "pa_weight_star_v1", 1)),
                        "STAR_V2": int(getattr(cfg, "pa_weight_star_v2", 3)),
                        "SQUEEZE_BREAK": int(getattr(cfg, "pa_weight_squeeze_break", 3)),
                        "BOS_BULLISH": int(getattr(cfg, "pa_weight_bos", 3)),
                        "BOS_BEARISH": int(getattr(cfg, "pa_weight_bos", 3)),
                    }
                    # [2026-05-17 100점 ×10] PA weight 자체는 0~6 (FOCUS _compute_pa_weight 와 동일 스케일).
                    # 신규 conviction 은 0~100 (PA_score × 5 가 conviction 의 PA 기여분).
                    # → delta 도 ×5 해서 100점 체계와 일치. (예: PA delta 1 → conv delta 5)
                    delta = (harpoon_w_map.get(pa_name, 0) - focus_w_map.get(pa_name, 0)) * 5
                    if delta != 0:
                        conv_adjusted = max(0.0, float(conv) + float(delta))
                        if conv_adjusted != conv:
                            logger.debug("[HARPOON] PA delta %s: %s %+d → conv %.1f→%.1f",
                                         mkt, pa_name, delta, conv, conv_adjusted)
                        conv = conv_adjusted
                except Exception as exc:
                    logger.debug("[HARPOON] PA delta calc error: %s", exc)

            if conv < min_conv:
                continue
            # PA 패턴 허용 리스트
            pa = pa_name
            if pa_allowed and pa not in pa_allowed:
                continue
            # 후보 수집
            candidates.append({
                "market": mkt,
                "direction": direction,
                "conviction": conv,
                "adx": adx,
                "pa_pattern": pa,
                "price": float(item.get("price", 0) or 0),
                "atr": float(item.get("atr", 0) or 0),
            })

        # conviction 내림차순 정렬 (강한 신호 우선)
        candidates.sort(key=lambda x: (-x["conviction"], -x["adx"]))

        # ★ Phase M.C: FOCUS 조율 필터 적용
        candidates = self._filter_candidates_by_focus_coordination(candidates)

        # ★ Phase M.B: Multi-position 체크 (신규 진입 가능한 후보만)
        filtered = []
        for c in candidates:
            can_open, reason = self._can_open_new_scalp(c.get("market", ""), c.get("direction", ""))
            if can_open:
                filtered.append(c)
            else:
                logger.debug("[HARPOON] candidate skip: %s %s — %s",
                             c.get("market"), c.get("direction"), reason)
        return filtered

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.C (2026-04-24) — FOCUS 조율 로직
    #   부모님 결정: HARPOON 완전 탈바꿈 + FOCUS 동시 작동 안전
    # ═══════════════════════════════════════════════════════════════════

    def _get_focus_held_markets(self) -> List[tuple]:
        """FOCUS 가 보유한 (market, direction) 리스트 조회.
        read-only helper on focus_manager side."""
        try:
            if not self.focus:
                return []
            fn = getattr(self.focus, "_get_held_markets_for_harpoon", None)
            if callable(fn):
                return fn() or []
        except Exception as exc:
            logger.debug("[HARPOON] _get_focus_held_markets failed: %s", exc)
        return []

    def _is_focus_coin_locked(self, market: str) -> bool:
        """FOCUS 가 이 코인 보유 중이면 True (respect_focus_coin_lock 시 HARPOON skip).
        코인 exclusive 규칙 — 한 코인 = 한 엔진."""
        if not market:
            return False
        if not getattr(self.config, "respect_focus_coin_lock", True):
            return False
        held = self._get_focus_held_markets()
        mkt_u = market.upper()
        return any(m == mkt_u for (m, _d) in held)

    def _is_focus_direction_conflict(self, market: str, direction: str) -> bool:
        """FOCUS 가 이 코인에 반대 방향 포지션 있으면 True.
        양방향 헤지 방지 (respect_focus_direction_lock)."""
        if not market or not direction:
            return False
        if not getattr(self.config, "respect_focus_direction_lock", True):
            return False
        held = self._get_focus_held_markets()
        mkt_u = market.upper()
        dir_u = direction.upper()
        for (m, d) in held:
            if m == mkt_u and d != dir_u:
                return True
        return False

    def _is_focus_entry_freeze_active(self) -> bool:
        """FOCUS 가 최근 진입한지 focus_entry_freeze_sec 이내면 True.
        FOCUS 진입 직후 HARPOON 잠시 skip — race condition 방지."""
        freeze_sec = float(getattr(self.config, "focus_entry_freeze_sec", 30.0))
        if freeze_sec <= 0:
            return False
        try:
            if not self.focus:
                return False
            fn = getattr(self.focus, "_get_most_recent_entry_ts_for_harpoon", None)
            if not callable(fn):
                return False
            last_ts = fn() or 0.0
            if last_ts <= 0:
                return False
            elapsed = time.time() - last_ts
            return elapsed < freeze_sec
        except Exception as exc:
            logger.debug("[HARPOON] _is_focus_entry_freeze_active failed: %s", exc)
            return False

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.B (2026-04-24) — Multi-position management methods
    # ═══════════════════════════════════════════════════════════════════

    def _get_active_scalps(self) -> List[ScalpPosition]:
        """현재 활성 스캘프 리스트.
        Phase 3 (병렬 유지): current_scalp 가 있으면 그것을 포함하여 반환.
        Phase 4+ 에서 active_scalps 직접 관리로 전환."""
        result = list(self.active_scalps) if self.active_scalps else []
        if self.current_scalp and not any(s.market == self.current_scalp.market for s in result):
            result.append(self.current_scalp)
        return result

    def _count_active_scalps(self) -> int:
        """현재 활성 스캘프 수."""
        return len(self._get_active_scalps())

    def _count_same_direction_scalps(self, direction: str) -> int:
        """같은 방향 활성 스캘프 수."""
        if not direction:
            return 0
        dir_u = direction.upper()
        return sum(1 for s in self._get_active_scalps() if (s.direction or "").upper() == dir_u)

    def _is_coin_on_cooldown(self, market: str) -> bool:
        """코인별 쿨다운 체크 (cooldown_per_coin_sec).
        같은 코인 재진입 방지 — FOCUS coin_repeat_brake 와 유사하지만 HARPOON 독립."""
        if not market:
            return False
        cooldown = float(getattr(self.config, "cooldown_per_coin_sec", 300.0))
        if cooldown <= 0:
            return False
        last_exit = self.last_scalp_exit_by_coin.get(market.upper(), 0)
        if last_exit <= 0:
            return False
        return (time.time() - last_exit) < cooldown

    def _is_coin_on_post_sl_cooldown(self, market: str, direction: str) -> bool:
        """Stage 0-7 post_sl_cooldown_min 체크 (SL 후 동일 setup 차단).
        기존 _record_post_sl 과 연동."""
        if not market or not direction:
            return False
        cooldown_min = float(getattr(self.config, "post_sl_cooldown_min", 30.0))
        if cooldown_min <= 0:
            return False
        key = (market.upper(), direction.upper())
        expire_ts = self._post_sl_cooldown_by_coin.get(key, 0)
        if expire_ts <= 0:
            return False
        return time.time() < expire_ts

    def _can_open_new_scalp(self, market: str, direction: str) -> tuple:
        """신규 스캘프 진입 가능 여부.
        Returns: (can_open: bool, reason: str)

        체크 순서:
          1. max_concurrent_scalps (동시 총 수)
          2. max_same_direction_scalps (같은 방향 수)
          3. cooldown_per_coin_sec (코인별 쿨다운)
          4. post_sl_cooldown (SL 후 동일 setup)
          5. 이미 같은 코인 보유 중 (중복 진입 방지)
        """
        cfg = self.config
        mkt_u = (market or "").upper()
        dir_u = (direction or "").upper()

        # 1) 총 동시 스캘프 수
        max_concurrent = int(getattr(cfg, "max_concurrent_scalps", 3))
        current_count = self._count_active_scalps()
        if current_count >= max_concurrent:
            return False, f"max_concurrent({current_count}/{max_concurrent})"

        # 2) 같은 방향 수
        max_same_dir = int(getattr(cfg, "max_same_direction_scalps", 2))
        same_dir_count = self._count_same_direction_scalps(dir_u)
        if same_dir_count >= max_same_dir:
            return False, f"max_same_direction({same_dir_count}/{max_same_dir})"

        # 3) 코인별 쿨다운
        if self._is_coin_on_cooldown(mkt_u):
            return False, "coin_cooldown"

        # 4) Post-SL 쿨다운 (Stage 0-7)
        if self._is_coin_on_post_sl_cooldown(mkt_u, dir_u):
            return False, "post_sl_cooldown"

        # 5) 중복 진입 방지 — 이미 보유 중인 코인
        for s in self._get_active_scalps():
            if (s.market or "").upper() == mkt_u:
                return False, f"coin_already_held({(s.direction or '').upper()})"

        # 6) ★ Phase M.E: 공유 가드 (FOCUS B8/B10/coin_loss_cap/manual_exit_penalty)
        shared_blocked, shared_reason = self._check_shared_guards(mkt_u, dir_u)
        if shared_blocked:
            return False, shared_reason

        return True, "ok"

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.D (2026-04-24) — Budget 분리 (FOCUS/HARPOON pool 분리)
    # ═══════════════════════════════════════════════════════════════════

    def _get_focus_total_margin(self) -> float:
        """FOCUS 가 사용 중인 총 margin 조회 (read-only)."""
        try:
            if not self.focus:
                return 0.0
            fn = getattr(self.focus, "_get_total_margin_for_harpoon", None)
            if callable(fn):
                return float(fn() or 0)
        except Exception as exc:
            logger.debug("[HARPOON] _get_focus_total_margin failed: %s", exc)
        return 0.0

    def _get_harpoon_used_margin(self) -> float:
        """HARPOON 활성 스캘프들이 사용 중인 총 margin 합산."""
        try:
            total = 0.0
            for s in self._get_active_scalps():
                entry_price = float(getattr(s, "entry_price", 0) or 0)
                qty = float(getattr(s, "qty", 0) or 0)
                lev = float(getattr(self.config, "leverage", 1) or 1)
                if entry_price > 0 and qty > 0 and lev > 0:
                    margin = (entry_price * qty) / lev
                    total += margin
            return total
        except Exception as exc:
            logger.debug("[HARPOON] _get_harpoon_used_margin failed: %s", exc)
            return 0.0

    def _get_harpoon_total_budget_pool(self) -> float:
        """HARPOON 자체 총 예산 pool (= 기존 effective_budget property).
        budget_pct 또는 budget_usdt 직접 지정값."""
        return float(self.effective_budget or 0)

    def _get_harpoon_available_budget(self) -> float:
        """HARPOON 잔여 가용 예산 = total_pool - used_margin."""
        pool = self._get_harpoon_total_budget_pool()
        used = self._get_harpoon_used_margin()
        return max(0.0, pool - used)

    def _compute_per_scalp_budget(self, market: str, conviction: int = 5) -> float:
        """신규 스캘프당 예산 계산 (Multi-position 고려).

        공식: 잔여 HARPOON 가용 예산 / 잔여 슬롯 수
          - 잔여 가용 = total_pool - used_margin (다른 스캘프들이 쓰는 것 차감)
          - 잔여 슬롯 = max_concurrent_scalps - 현재 active 수
          - risk_pct 고려한 micro-adjustment

        최소 $5 (Bybit 최소 주문) / 최대 잔여 가용
        """
        cfg = self.config
        available = self._get_harpoon_available_budget()
        if available < 5.0:
            return 0.0  # 진입 불가

        max_concurrent = int(getattr(cfg, "max_concurrent_scalps", 3))
        current_count = self._count_active_scalps()
        remaining_slots = max(1, max_concurrent - current_count)

        # 잔여 예산을 잔여 슬롯에 균등 분배
        per_scalp = available / remaining_slots

        # ★ [2026-04-24] PA Weight 통합 (FOCUS conviction 0~10 → 0~16 확장)
        #   부모 결정 "PA=공식, fee=변수" → 강한 PA 일수록 size ↑ 로 fee 희석
        #   tier 매핑 (full size 기준):
        #     conviction <  5  → skip (수수료 못 이김, return 0.0)
        #     [2026-05-17 100점 ×10 마이그] 옛 5/7/10/13/16 → 50/70/100/130/160 (단순 ×10)
        #     conviction 50~70   → 0.5x  (약한 신호: ADX/BB만, PA 없음)
        #     conviction 71~100  → 0.8x  (전통적 강신호: ADX+BB+MACD+trend)
        #     conviction 101~130 → 1.1x  (전통 + Pat 1/2 PA)
        #     conviction 131~160 → 1.4x  (전통 + Pat 3 PA + zone — full conviction)
        risk_pct = float(getattr(cfg, "risk_pct", 0.5))
        if conviction < 50:
            return 0.0  # 수수료 BEP 못 넘김 — skip
        elif conviction <= 70:
            conv_scale = 0.5
        elif conviction <= 100:
            conv_scale = 0.8
        elif conviction <= 130:
            conv_scale = 1.1
        else:  # 131~160
            conv_scale = 1.4
        per_scalp = per_scalp * conv_scale

        # 최종: 최소 $5, 최대 available
        return max(5.0, min(per_scalp, available))

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.E (2026-04-24) — Shared Guards 통합 (FOCUS 가드 공유)
    # ═══════════════════════════════════════════════════════════════════

    def _get_focus_shared_guard_state(self, market: str, direction: str) -> dict:
        """FOCUS 의 공유 가드 상태 조회 (read-only).
        FOCUS._get_shared_guard_state_for_harpoon() wrapper."""
        try:
            if not self.focus:
                return {}
            fn = getattr(self.focus, "_get_shared_guard_state_for_harpoon", None)
            if callable(fn):
                return fn(market, direction) or {}
        except Exception as exc:
            logger.debug("[HARPOON] _get_focus_shared_guard_state failed: %s", exc)
        return {}

    def _check_shared_guards(self, market: str, direction: str) -> tuple:
        """공유 가드 통합 체크 (Phase M.E).

        체크 항목 (FOCUS 상태 공유):
          1. profit_exit_block (B10) — 같은 방향 수익 streak 후 차단
          2. direction_exhaustion (B8) — 15분 내 2회 profit exit 후 차단
          3. coin_loss_cap — 24h 누적 -$20+ 코인 차단
          4. manual_exit_penalty — 부모님이 수동 청산한 코인 1.5h 차단
          5. (coin_repeat 정보는 참고만, 차단 결정 X — HARPOON 은 자체 cooldown 사용)

        부모님 결정: HARPOON 이 FOCUS 가드 우회하면 안 됨. 양쪽 합산.

        Returns: (blocked: bool, reason: str)
        """
        cfg = self.config

        # 부모님이 명시 OFF 한 경우 skip
        if not getattr(cfg, "respect_coin_loss_cap", True) \
           and not getattr(cfg, "respect_focus_adx_slope", True) \
           and not getattr(cfg, "respect_b11_regime_lock", True):
            # 가드 모두 OFF — shared guard 도 skip
            return (False, "")

        state = self._get_focus_shared_guard_state(market, direction)
        if not state:
            return (False, "")

        # 1) profit_exit_block (FOCUS B10)
        peb_blocked, peb_reason = state.get("profit_exit_blocked", (False, ""))
        if peb_blocked:
            return (True, f"shared_profit_exit_block: {peb_reason[:80]}")

        # 2) direction_exhaustion (FOCUS B8)
        de_blocked, de_reason = state.get("direction_exhaustion_blocked", (False, ""))
        if de_blocked:
            return (True, f"shared_direction_exhaustion: {de_reason[:80]}")

        # 3) coin_loss_cap (24h 누적, respect_coin_loss_cap 설정에 따름)
        if getattr(cfg, "respect_coin_loss_cap", True):
            cap_blocked, cap_reason = state.get("coin_loss_cap_blocked", (False, ""))
            if cap_blocked:
                return (True, f"shared_coin_loss_cap: {cap_reason[:80]}")

        # 4) manual_exit_penalty (부모 수동 청산 후)
        if state.get("manual_exit_penalty_active", False):
            return (True, f"shared_manual_exit_penalty: {market} 수동 청산 페널티 active")

        return (False, "")

    def _record_scalp_exit(self, market: str, direction: str, result: str):
        """스캘프 청산 시 cooldown timer 기록 + active_scalps 정리.
        result: "TP" / "SL" / "MANUAL" / "server_sl" / "pre_be_stall" 등"""
        if not market:
            return
        mkt_u = market.upper()
        now = time.time()
        # 코인별 cooldown 기록 (모든 청산 공통)
        self.last_scalp_exit_by_coin[mkt_u] = now
        # SL 계열은 post_sl_cooldown 추가
        result_lower = (result or "").lower()
        if direction and ("sl" in result_lower or "reject" in result_lower):
            cooldown_min = float(getattr(self.config, "post_sl_cooldown_min", 30.0))
            if cooldown_min > 0:
                key = (mkt_u, direction.upper())
                self._post_sl_cooldown_by_coin[key] = now + cooldown_min * 60
        # active_scalps 에서 제거
        self.active_scalps = [s for s in self.active_scalps if (s.market or "").upper() != mkt_u]

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.F (2026-04-24) — Multi-market / Multi-position 실진입
    #   설계 원칙:
    #     - current_scalp = "primary" — 기존 단일 path 유지 (dynamic trailing 등 full)
    #     - active_scalps[others] = "secondary" — Bybit 서버사이드 TP/SL 에만 의존
    #     - 기존 state machine 은 primary 관리용
    #     - 신규 함수는 tick 끝에서 추가 처리
    # ═══════════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════════
    # ★ Phase M.G (2026-04-24) — Multi-scalp 가드 3종 (generic helpers)
    #   Dynamic Trailing / Fast-Reject v2 / Pre-BE Stall Exit
    #   pos-generic 으로 설계 — Primary + Secondary 양쪽 적용 가능
    # ═══════════════════════════════════════════════════════════════════

    def _check_fast_reject_v2_for_scalp(self, scalp: ScalpPosition, price: float, now: float) -> tuple:
        """Fast-Reject v2: 진입 후 N초 내 peak 0% + PnL 손실 → 즉시 청산.
        Stage 0-6 forensic 04-14 peak 0% 패턴 직접 대응.
        Returns: (should_exit: bool, reason: str)"""
        cfg = self.config
        if not getattr(cfg, "fast_reject_v2_enabled", True):
            return (False, "")
        max_sec = float(getattr(cfg, "fast_reject_v2_max_sec", 60.0))
        peak_thr_pct = float(getattr(cfg, "fast_reject_v2_peak_threshold_pct", 0.05))
        pnl_pct_thr = float(getattr(cfg, "fast_reject_v2_pnl_pct", -0.05))
        elapsed = now - (scalp.entry_ts or 0)
        if elapsed <= 0 or elapsed > max_sec:
            return (False, "")
        # peak 수익 계산
        peak_pct = 0.0
        if scalp.peak_profit_price > 0 and scalp.entry_price > 0:
            if scalp.direction == "LONG":
                peak_pct = (scalp.peak_profit_price / scalp.entry_price - 1) * 100
            else:
                peak_pct = (1 - scalp.peak_profit_price / scalp.entry_price) * 100
        # 현재 pnl
        if scalp.direction == "LONG":
            pnl_pct = (price / scalp.entry_price - 1) * 100 if scalp.entry_price > 0 else 0
        else:
            pnl_pct = (1 - price / scalp.entry_price) * 100 if scalp.entry_price > 0 else 0
        # 조건: peak 아직 threshold 미달 AND pnl 손실
        if peak_pct < peak_thr_pct and pnl_pct <= pnl_pct_thr:
            return (True, f"fast_reject_v2_{int(elapsed)}s_peak{peak_pct:.2f}%_pnl{pnl_pct:.2f}%")
        return (False, "")

    def _check_pre_be_stall_exit_for_scalp(self, scalp: ScalpPosition, price: float, now: float) -> tuple:
        """Pre-BE Stall Exit (FOCUS 와 동일 개념, HARPOON 포팅).
        BE 락 전 + peak ≥ min_profit + N초 무갱신 → 청산.
        AUTO 모드: 진입 코인 ATR < 임계 일 때만 발동.

        HARPOON config 에 별도 필드 없으면 FOCUS 의 pre_be_stall_* 필드 참조하거나 기본값 적용.
        Returns: (should_exit: bool, reason: str)"""
        cfg = self.config
        # HARPOON 전용 pre_be_stall 설정이 없어서 default AUTO/0.10%/60s 기본 적용
        mode = (getattr(cfg, "pre_be_stall_exit_mode", "AUTO") or "AUTO").upper()
        if mode == "OFF":
            return (False, "")
        if scalp.breakeven_locked:
            return (False, "")  # BE 락 이후는 BE Stall Exit 담당
        min_profit_pct = float(getattr(cfg, "pre_be_stall_min_profit_pct", 0.10))
        stall_sec = float(getattr(cfg, "pre_be_stall_sec", 60.0))
        vol_thr_pct = float(getattr(cfg, "pre_be_stall_volatility_threshold_pct", 2.0))
        # peak 수익
        peak_pct = 0.0
        if scalp.peak_profit_price > 0 and scalp.entry_price > 0:
            if scalp.direction == "LONG":
                peak_pct = (scalp.peak_profit_price / scalp.entry_price - 1) * 100
            else:
                peak_pct = (1 - scalp.peak_profit_price / scalp.entry_price) * 100
        if peak_pct < min_profit_pct:
            return (False, "")
        # peak 갱신 후 경과 시간 — scalp 에 last_peak_update_ts 없으니 entry_ts 대체
        # (ScalpPosition 은 last_peak_update_ts 필드 없음 — 단순화)
        # entry_ts 로부터 일단 측정 (첫 peak 이후 stall_sec 지났는지)
        elapsed = now - (scalp.entry_ts or now)
        if elapsed < stall_sec:
            return (False, "")
        # AUTO 모드: ATR 기준 횡보 판정
        if mode == "AUTO":
            coin_atr_pct = (scalp.atr_used / scalp.entry_price * 100) if scalp.entry_price > 0 else 0
            if coin_atr_pct >= vol_thr_pct:
                return (False, "")  # 급변동 — 추세 따라감
        return (True, f"pre_be_stall_{int(elapsed)}s_peak+{peak_pct:.2f}%")

    def _update_scalp_peak_price(self, scalp: ScalpPosition, price: float, now: float = None) -> bool:
        """scalp 의 peak_profit_price 업데이트 + timestamp 기록.
        Returns: True if peak 갱신됨."""
        if now is None:
            now = time.time()
        updated = False
        if scalp.direction == "LONG":
            if price > scalp.peak_profit_price:
                scalp.peak_profit_price = price
                scalp.last_peak_update_ts = now
                updated = True
        else:
            if scalp.peak_profit_price <= 0 or price < scalp.peak_profit_price:
                scalp.peak_profit_price = price
                scalp.last_peak_update_ts = now
                updated = True
        return updated

    def _check_be_stall_exit_for_scalp(self, scalp: ScalpPosition, price: float, now: float) -> tuple:
        """BE Stall Exit (Phase M.G generic): BE 락 후 30초 무갱신 시 청산.
        수수료 가드: pnl ≥ 0.15% OR peak ≥ 0.30% 충족해야 발동.
        Returns: (should_exit: bool, reason: str)"""
        cfg = self.config
        if not getattr(cfg, "be_stall_exit_enabled", True):
            return (False, "")
        if not scalp.breakeven_locked or scalp.be_locked_ts <= 0:
            return (False, "")
        stall_sec = float(getattr(cfg, "be_stall_exit_sec", 30.0))
        ref_ts = max(scalp.be_locked_ts, scalp.last_peak_update_ts or scalp.be_locked_ts)
        elapsed = now - ref_ts
        if elapsed < stall_sec:
            return (False, "")
        # 수수료 가드
        if scalp.direction == "LONG":
            pnl_pct = (price / scalp.entry_price - 1) * 100 if scalp.entry_price > 0 else 0
            peak_pct = (scalp.peak_profit_price / scalp.entry_price - 1) * 100 if scalp.peak_profit_price > 0 and scalp.entry_price > 0 else 0
        else:
            pnl_pct = (1 - price / scalp.entry_price) * 100 if scalp.entry_price > 0 else 0
            peak_pct = (1 - scalp.peak_profit_price / scalp.entry_price) * 100 if scalp.peak_profit_price > 0 and scalp.entry_price > 0 else 0
        if pnl_pct < 0.15 and peak_pct < 0.30:
            return (False, "")  # 수수료 가드 미달
        return (True, f"be_stall_{int(elapsed)}s_peak+{peak_pct:.2f}%")

    def _check_timeout_for_scalp(self, scalp: ScalpPosition, now: float) -> tuple:
        """5분 Timeout 체크 (Phase M.G generic).
        Returns: (should_exit: bool, reason: str)"""
        TIMEOUT_SEC = 300.0  # 5분
        if not scalp.entry_ts or scalp.entry_ts <= 0:
            return (False, "")
        hold = now - scalp.entry_ts
        if hold > TIMEOUT_SEC:
            return (True, f"timeout_{int(hold)}s")
        return (False, "")

    def _close_secondary_scalp(self, scalp: ScalpPosition, reason: str, price: float) -> bool:
        """Secondary scalp 시장가 청산 (Primary 의 _execute_scalp_exit 간소화 버전).

        - Bybit market order 시장가 청산
        - active_scalps 에서 제거
        - Journal + recent_scalps 기록
        - Counters 업데이트
        """
        if not scalp or not scalp.market:
            return False
        market = scalp.market
        now = time.time()
        try:
            client = self._get_client()
            # 시장가 청산 주문 (direction 반대)
            close_side = "Sell" if scalp.direction == "LONG" else "Buy"
            _params = {
                "category": "linear", "symbol": market, "side": close_side,
                "orderType": "Market", "qty": str(scalp.qty),
                "reduceOnly": True,
            }
            client._request("POST", "/v5/order/create", params=None, body=_params)
        except Exception as exc:
            logger.warning("[HARPOON M.G] secondary close order failed %s: %s", market, exc)
            return False

        # PnL 계산
        if scalp.direction == "LONG":
            pnl = (price - scalp.entry_price) * scalp.qty
        else:
            pnl = (scalp.entry_price - price) * scalp.qty
        duration = now - scalp.entry_ts if scalp.entry_ts > 0 else 0.0

        # Journal
        try:
            from app.manager.trade_journal import journal as _journal
            _journal.append(
                strategy="HARPOON", event="EXIT", market=market,
                direction=scalp.direction, price=price, qty=scalp.qty,
                pnl_net=round(pnl, 4), exit_reason=reason, hold_sec=round(duration, 1),
            )
        except Exception as exc:
            logger.debug("[HARPOON M.G] journal append failed: %s", exc)

        # recent_scalps
        record = ScalpRecord(
            scalp_id=scalp.scalp_id, market=market, direction=scalp.direction,
            entry_price=scalp.entry_price, exit_price=price, qty=scalp.qty,
            pnl_usdt=round(pnl, 4), result=reason, duration_sec=round(duration, 1),
            ts=now,
        )
        self.recent_scalps.append(asdict(record))
        if len(self.recent_scalps) > 50:
            self.recent_scalps = self.recent_scalps[-50:]

        # Cooldown + active_scalps 제거 + counters
        self._record_scalp_exit(market, scalp.direction, reason)
        self.scalps_today += 1
        self.daily_pnl += pnl
        self.total_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        logger.info("[HARPOON M.G] SECONDARY close: %s %s @ $%.4f — %s pnl=$%.4f",
                    market, scalp.direction, price, reason, pnl)
        return True

    def _try_extra_entries(self, now: float) -> int:
        """추가 multi-market 진입 시도.
        Returns: 이번 tick 에 새로 진입한 스캘프 수.

        동작:
          1. Candidates 리스트 조회 (Phase M.A scanner)
          2. 각 후보마다 _can_open_new_scalp 체크 (Multi-position + Shared guards)
          3. 통과 후보는 _execute_scalp_entry 호출 (기존 함수 재사용)
          4. active_scalps 에 자동 등록 (이미 _execute_scalp_entry 내부에서)

        주의:
          - primary (current_scalp) 는 기존 state machine 이 진입 담당
          - 이 함수는 "추가" 진입만 (secondary)
          - scan_universe 가 "all" 이 아니거나 max_concurrent>1 일 때만 의미 있음
        """
        cfg = self.config
        max_concurrent = int(getattr(cfg, "max_concurrent_scalps", 3))
        if max_concurrent <= 1:
            return 0  # 단일 포지션 모드 — 추가 진입 없음

        # 이미 최대 보유 중이면 skip
        if self._count_active_scalps() >= max_concurrent:
            return 0

        # 후보 리스트 조회
        candidates = self._get_harpoon_candidates()
        if not candidates:
            return 0

        # paper_mode 시 skip (기존 _handle_stalking 의 paper 로직 재사용 필요하나 여기선 skip)
        if getattr(cfg, "paper_mode", False):
            # Paper: log 만 기록
            for c in candidates[:max_concurrent - self._count_active_scalps()]:
                self._log_paper_entry(c["market"], c["direction"], c.get("price", 0), c.get("atr", 0))
            return 0

        entered = 0
        for c in candidates:
            if self._count_active_scalps() >= max_concurrent:
                break
            market = c.get("market", "")
            direction = c.get("direction", "")
            price = float(c.get("price", 0) or 0)
            atr = float(c.get("atr", 0) or 0)
            if not (market and direction and price > 0 and atr > 0):
                continue
            # _can_open_new_scalp 재확인 (이미 candidates 필터에서 체크했지만 race condition 방지)
            can_open, reason = self._can_open_new_scalp(market, direction)
            if not can_open:
                logger.debug("[HARPOON M.F] extra entry skip: %s %s — %s", market, direction, reason)
                continue
            try:
                result = self._execute_scalp_entry(market, direction, price, atr)
                if result and result.get("success"):
                    entered += 1
                    logger.info("[HARPOON M.F] EXTRA entry: %s %s @ $%.4f (active=%d/%d)",
                                market, direction, price, self._count_active_scalps(), max_concurrent)
                else:
                    logger.debug("[HARPOON M.F] extra entry failed: %s %s — %s",
                                 market, direction, result.get("reason", "?") if result else "?")
            except Exception as exc:
                logger.warning("[HARPOON M.F] extra entry exception: %s %s — %s", market, direction, exc)
                continue
        return entered

    def _monitor_additional_scalps(self, now: float) -> int:
        """Additional scalps (current_scalp 외 active_scalps) Bybit 서버 상태 확인.
        Returns: 이번 tick 에 정리된 스캘프 수.

        동작:
          - current_scalp 은 _handle_fired 에서 담당 (skip)
          - 나머지 active_scalps 는 Bybit 포지션 조회
          - Bybit 에 포지션 없으면 → 서버사이드 TP/SL 로 청산됨 → active_scalps 제거 + journal 기록
          - 서버사이드 TP/SL 기준이라 peak_profit / trailing 등은 못 함 (secondary 제약)
        """
        if not self.active_scalps:
            return 0

        current_id = getattr(self.current_scalp, "scalp_id", 0) if self.current_scalp else 0
        additional = [s for s in self.active_scalps if s.scalp_id != current_id]
        if not additional:
            return 0

        cleared = 0
        try:
            from app.core.constants import BYBIT_POSITION_LIST
            client = self._get_client()
        except Exception as exc:
            logger.debug("[HARPOON M.F] monitor additional: client init failed: %s", exc)
            return 0

        for s in additional:
            market = s.market
            if not market:
                continue
            try:
                resp = client._request("GET", BYBIT_POSITION_LIST,
                                       params={"category": "linear", "symbol": market})
                pos_list = resp.get("result", {}).get("list", [])
                has_position = any(float(p.get("size", 0)) > 0 for p in pos_list)
                if has_position:
                    # ★ Phase M.G: Secondary 가드 완전 적용 (6종 모두 Primary 와 동일)
                    current_price = self._get_current_price(market)
                    if current_price:
                        # 1) peak 갱신 (last_peak_update_ts 자동 기록)
                        self._update_scalp_peak_price(s, current_price, now)
                        # 2) 5분 Timeout
                        to_exit, to_reason = self._check_timeout_for_scalp(s, now)
                        if to_exit:
                            self._close_secondary_scalp(s, to_reason, current_price)
                            cleared += 1
                            continue
                        # 3) Fast-Reject v2 (60초 peak 0% + 손실)
                        fr_exit, fr_reason = self._check_fast_reject_v2_for_scalp(s, current_price, now)
                        if fr_exit:
                            self._close_secondary_scalp(s, fr_reason, current_price)
                            cleared += 1
                            continue
                        # 4) Pre-BE Stall Exit (BE 전, peak +0.10%+60초 무갱신)
                        pb_exit, pb_reason = self._check_pre_be_stall_exit_for_scalp(s, current_price, now)
                        if pb_exit:
                            self._close_secondary_scalp(s, pb_reason, current_price)
                            cleared += 1
                            continue
                        # 5) BE Stall Exit (BE 락 후 30초 무갱신 + 수수료 가드)
                        be_exit, be_reason = self._check_be_stall_exit_for_scalp(s, current_price, now)
                        if be_exit:
                            self._close_secondary_scalp(s, be_reason, current_price)
                            cleared += 1
                            continue
                        # 6) Dynamic Trailing SL (server SL 업데이트, BE 락 자동 처리)
                        if getattr(self.config, "dynamic_trailing", False):
                            try:
                                self._apply_scalp_trailing_sl(s, current_price)
                            except Exception as exc:
                                logger.debug("[HARPOON M.G] trailing secondary %s failed: %s", market, exc)
                    continue  # 포지션 유지 중 — 서버사이드 TP/SL 대기

                # 포지션 없음 → 서버사이드 TP/SL 로 청산됨 → 정리
                price = self._get_current_price(market) or s.entry_price
                if s.direction == "LONG":
                    tp_hit = price >= s.tp if s.tp else False
                else:
                    tp_hit = price <= s.tp if s.tp else False
                exit_price = (s.tp if tp_hit else s.sl) if s.sl else price
                exit_reason = "SERVER_TP_MULTI" if tp_hit else "SERVER_SL_MULTI"

                # PnL 계산
                if s.direction == "LONG":
                    pnl = (exit_price - s.entry_price) * s.qty
                else:
                    pnl = (s.entry_price - exit_price) * s.qty
                duration = now - s.entry_ts if s.entry_ts > 0 else 0.0

                # Journal + record_scalp_exit
                try:
                    from app.manager.trade_journal import journal as _journal
                    _journal.append(
                        strategy="HARPOON",
                        event="EXIT",
                        market=market,
                        direction=s.direction,
                        price=exit_price,
                        qty=s.qty,
                        pnl_net=round(pnl, 4),
                        exit_reason=exit_reason,
                        hold_sec=round(duration, 1),
                    )
                except Exception as exc:
                    logger.debug("[HARPOON M.F] journal append failed: %s", exc)

                # recent_scalps 기록
                record = ScalpRecord(
                    scalp_id=s.scalp_id, market=market, direction=s.direction,
                    entry_price=s.entry_price, exit_price=exit_price, qty=s.qty,
                    pnl_usdt=round(pnl, 4), result=exit_reason, duration_sec=round(duration, 1),
                    ts=now,
                )
                self.recent_scalps.append(asdict(record))
                if len(self.recent_scalps) > 50:
                    self.recent_scalps = self.recent_scalps[-50:]

                # Cooldown 기록
                self._record_scalp_exit(market, s.direction, exit_reason)

                # Counters 업데이트
                self.scalps_today += 1
                self.daily_pnl += pnl
                self.total_pnl += pnl
                if pnl < 0:
                    self.consecutive_losses += 1
                else:
                    self.consecutive_losses = 0

                logger.info("[HARPOON M.F] SECONDARY exit: %s %s @ $%.4f — %s pnl=$%.4f dur=%.0fs",
                            market, s.direction, exit_price, exit_reason, pnl, duration)
                cleared += 1

            except Exception as exc:
                logger.debug("[HARPOON M.F] monitor %s exception: %s", market, exc)
                continue
        return cleared

    def _clear_current_scalp_from_active(self):
        """current_scalp 을 active_scalps 에서 제거 (id 또는 market 기준).
        Phase M.B: exit 경로 4곳에서 호출 — active_scalps 정합성 유지."""
        if not self.current_scalp:
            return
        sc_id = getattr(self.current_scalp, "scalp_id", 0)
        sc_mkt = (self.current_scalp.market or "").upper()
        self.active_scalps = [
            s for s in self.active_scalps
            if (sc_id and s.scalp_id != sc_id) or (not sc_id and (s.market or "").upper() != sc_mkt)
        ]

    def _filter_candidates_by_focus_coordination(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """HARPOON 후보 리스트에서 FOCUS 조율 조건 통과한 것만 반환.

        필터 순서:
          1. focus_entry_freeze_sec 활성 시: 전체 후보 차단 (방금 FOCUS 진입함)
          2. respect_focus_coin_lock: FOCUS 보유 코인 skip
          3. respect_focus_direction_lock: FOCUS 반대 방향 skip
          4. coin_exclusive_priority: 향후 Phase 3+ 에서 HARPOON↔HARPOON 충돌 처리
        """
        if not candidates:
            return candidates
        cfg = self.config

        # 1) Focus entry freeze (최근 FOCUS 진입 직후)
        if self._is_focus_entry_freeze_active():
            logger.debug("[HARPOON] focus_entry_freeze active — all candidates skipped")
            return []

        filtered = []
        skip_log = []
        for c in candidates:
            mkt = c.get("market", "")
            direction = c.get("direction", "")

            # 2) Coin lock — FOCUS 가 같은 코인 보유 중
            if self._is_focus_coin_locked(mkt):
                skip_log.append(f"{mkt}:focus_holds")
                continue

            # 3) Direction conflict — FOCUS 가 같은 코인에 반대 방향 보유 (중복이지만 명시적)
            if self._is_focus_direction_conflict(mkt, direction):
                skip_log.append(f"{mkt}:focus_dir_conflict")
                continue

            filtered.append(c)

        if skip_log:
            logger.debug("[HARPOON] FOCUS coordination filter: %s", " | ".join(skip_log))
        return filtered

    def _get_focus_atr(self) -> float:
        """FOCUS의 H4 ATR 값 가져오기. 모든 소스에서 시도."""
        # 1) FOCUS primary_sig [2026-05-15 h4_sig→primary_sig, 구 속성 fallback]
        if self.focus:
            sig = getattr(self.focus, 'primary_sig', None) or getattr(self.focus, 'h4_sig', None)
            if sig and isinstance(sig, dict):
                atr = sig.get("atr", 0) or 0
                if atr > 0:
                    return float(atr)
            # 2) FOCUS positions
            positions = getattr(self.focus, 'positions', []) or []
            for p in positions:
                atr = getattr(p, 'atr_used', 0) or 0
                if atr > 0:
                    return float(atr)
        # 3) ★ Fallback: 직접 kline에서 ATR 계산 (FOCUS h4_sig 없어도 독립 동작)
        try:
            market = self._get_focus_market()
            if market and self.focus:
                client = getattr(self.focus, '_client', None)
                if client:
                    raw = client.get_kline(market, interval="240", limit=20)
                    closes = [float(r[4]) for r in raw if len(r) >= 5]
                    if len(closes) >= 14:
                        from app.strategy.greenpen import _simple_atr
                        from app.strategy.greenpen.pa_detector import OHLCV
                        candles = [OHLCV(float(r[1]),float(r[2]),float(r[3]),float(r[4])) for r in raw if len(r)>=5]
                        atr = _simple_atr(candles)
                        if atr > 0:
                            logger.debug("[HARPOON] ATR fallback from kline: %.4f", atr)
                            return float(atr)
        except Exception as exc:
            logger.debug("[HARPOON] ATR fallback calc failed: %s", exc)
        return 0.0

    def _find_nearest_zone(
        self, price: float, direction: str, zones: List[Dict], proximity: float,
    ) -> Optional[Dict]:
        """가장 가까운 존 찾기 — 스캘퍼는 존 타입에 따라 방향 결정.

        SUPPORT 존 → LONG, RESISTANCE 존 → SHORT.
        direction 인자는 레거시 호환용 (빈 문자열이면 양방향 탐색).
        """
        best = None
        best_dist = float("inf")

        for z in zones:
            z_type = z.get("type", "")
            z_low = z.get("price_low", 0)
            z_high = z.get("price_high", 0)
            z_mid = (z_low + z_high) / 2 if z_low and z_high else 0

            if z_mid <= 0:
                continue

            # 존 타입이 없으면 건너뛰기
            if "SUPPORT" not in z_type.upper() and "RESISTANCE" not in z_type.upper():
                continue

            dist = abs(price - z_mid)
            if dist <= proximity and dist < best_dist:
                best = z
                best_dist = dist

        return best

    # ------------------------------------------------------------------
    # M1 PA detection
    # ------------------------------------------------------------------

    def _check_m1_pa(self, market: str, direction: str) -> Optional[Dict]:
        """M1 타임프레임에서 PA 패턴 감지."""
        try:
            client = self._get_client()
            klines_raw = client.get_kline(market, interval=self.config.entry_tf, limit=50)
            if not klines_raw or len(klines_raw) < 10:
                return None

            from app.strategy.greenpen.pa_detector import OHLCV, detect_pa_patterns
            from app.strategy.greenpen.market_structure import analyze_structure

            candles = []
            for k in klines_raw:
                try:
                    candles.append(OHLCV(
                        open=float(k[1]),
                        high=float(k[2]),
                        low=float(k[3]),
                        close=float(k[4]),
                        volume=float(k[5]) if len(k) > 5 else 0.0,
                    ))
                except (IndexError, ValueError, TypeError):
                    continue

            if len(candles) < 10:
                return None

            # ── M1 추세+모멘텀 필터 (역추세 PA 차단) ──
            m1_trend_opposed = False
            m1_momentum_opposed = False
            try:
                m1_struct = analyze_structure(candles, lookback=3)
                m1_trend = m1_struct.trend.value if hasattr(m1_struct.trend, 'value') else str(m1_struct.trend)
                if direction == "LONG" and m1_trend == "DOWNTREND":
                    m1_trend_opposed = True
                elif direction == "SHORT" and m1_trend == "UPTREND":
                    m1_trend_opposed = True
            except Exception:
                pass

            # M1 모멘텀: 최근 5봉 중 4봉+ 반대 방향이면 모멘텀 역행
            recent5 = candles[-5:]
            bearish_count = sum(1 for c in recent5 if c.close < c.open)
            if direction == "LONG" and bearish_count >= 4:
                m1_momentum_opposed = True
            elif direction == "SHORT" and (5 - bearish_count) >= 4:
                m1_momentum_opposed = True

            # 추세+모멘텀 모두 역행이면 PA 신호 무시
            if m1_trend_opposed and m1_momentum_opposed:
                logger.debug("[HARPOON] M1 PA blocked: trend+momentum opposed to %s", direction)
                return None

            # ★ zone_prices=None — Harpoon은 이미 zone proximity를 별도 검증하므로
            # PA 패턴에서 위치 필터링 생략 (zone_tup=(low,high) 전달 시
            # detect_pa_patterns가 (support,resistance)로 오해석하여 유효 신호 차단됨)
            signals = detect_pa_patterns(candles, zone_prices=None)

            # 방향 일치하는 최신 신호 찾기
            for sig in reversed(signals):
                sig_dir = getattr(sig, 'direction', '') or ''
                if isinstance(sig, dict):
                    sig_dir = sig.get('direction', '')

                if sig_dir.upper() == direction.upper():
                    conf = getattr(sig, 'confidence', 0) if not isinstance(sig, dict) else sig.get('confidence', 0)
                    pattern = getattr(sig, 'pattern', '') if not isinstance(sig, dict) else sig.get('pattern', '')
                    if conf >= 0.4:  # 스캘프는 신뢰도 문턱 낮춤
                        return {
                            "pattern": str(pattern),
                            "direction": direction,
                            "confidence": float(conf),
                        }

            return None
        except Exception as exc:
            logger.debug("[HARPOON] M1 PA check failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # M5 trend filter (Harpoon의 상위 TF 필터 — FOCUS의 H1 역할)
    # ------------------------------------------------------------------

    def _check_m5_trend(self, market: str, direction: str) -> Optional[str]:
        """M5 추세 확인 — Harpoon 상위 TF 필터.

        FOCUS가 H1으로 추세를 확인하듯, Harpoon은 M5로 확인.
        LONG 시도 중 M5 DOWNTREND → 차단, SHORT 시도 중 M5 UPTREND → 차단.

        Returns: "aligned" | "neutral" | "opposed" | None (API 실패)
        """
        try:
            client = self._get_client()
            raw = client.get_kline(market, interval="5", limit=20)
            if not raw or len(raw) < 10:
                return None

            from app.strategy.greenpen.pa_detector import OHLCV
            from app.strategy.greenpen.market_structure import analyze_structure

            candles = []
            for k in raw:
                try:
                    candles.append(OHLCV(
                        open=float(k[1]), high=float(k[2]),
                        low=float(k[3]), close=float(k[4]),
                        volume=float(k[5]) if len(k) > 5 else 0.0,
                    ))
                except (IndexError, ValueError, TypeError):
                    continue

            if len(candles) < 10:
                return None

            struct = analyze_structure(candles, lookback=3)
            trend = struct.trend.value if hasattr(struct.trend, 'value') else str(struct.trend)

            if direction == "LONG":
                if trend == "UPTREND":
                    return "aligned"
                elif trend == "DOWNTREND":
                    return "opposed"
            else:  # SHORT
                if trend == "DOWNTREND":
                    return "aligned"
                elif trend == "UPTREND":
                    return "opposed"

            return "neutral"
        except Exception as exc:
            logger.debug("[HARPOON] M5 trend check failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Entry execution
    # ------------------------------------------------------------------

    def _execute_scalp_entry(
        self, market: str, direction: str, price: float, atr: float,
    ) -> Dict[str, Any]:
        """스캘프 진입 실행."""
        # ★ 진입 직전 FOCUS 충돌 재확인 (STALKING 진행 중 FOCUS가 포지션 잡았을 수 있음)
        if self._has_focus_conflict():
            return {"success": False, "reason": "focus_conflict_at_entry"}

        from app.strategy.greenpen.harpoon_tp import compute_scalp_targets, compute_scalp_size

        # TP/SL 계산
        targets = compute_scalp_targets(
            entry_price=price,
            direction=direction,
            atr=atr,
            tp_atr_mult=self.config.tp_atr_mult,
            sl_atr_mult=self.config.sl_atr_mult,
        )

        if targets.tp <= 0 or targets.sl <= 0:
            return {"success": False, "reason": "invalid_targets"}

        # 포지션 사이징
        budget = self.effective_budget
        sizing = compute_scalp_size(
            budget_usdt=budget,
            risk_pct=self.config.risk_pct,
            sl_distance=targets.sl_dist,
            current_price=price,
            leverage=float(self.config.leverage),
        )

        if sizing.qty <= 0:
            return {"success": False, "reason": "zero_qty"}

        # ★ Bybit qty step/min 보정 (Qty invalid 방지)
        # ★ category="linear" — Harpoon은 USDT Perpetual 사용, spot qty_step과 다름!
        try:
            from app.integrations.bybit_instrument_cache import BybitInstrumentCache
            _cat = "linear"
            BybitInstrumentCache.load(category=_cat)  # linear 캐시 보장
            raw_qty = sizing.qty
            sizing.qty = BybitInstrumentCache.adjust_qty(market, sizing.qty, category=_cat)
            min_q = BybitInstrumentCache.get_min_qty(market, category=_cat)
            max_q = float((BybitInstrumentCache.get(market, category=_cat) or {}).get("max_qty", 0) or 0)
            if sizing.qty <= 0 or sizing.qty < min_q:
                logger.warning("[HARPOON] qty %.6f (raw %.6f) < min %.6f for %s — skip",
                               sizing.qty, raw_qty, min_q, market)
                return {"success": False, "reason": f"qty_below_min_{sizing.qty}"}
            if max_q > 0 and sizing.qty > max_q:
                logger.warning("[HARPOON] qty %.6f > maxOrderQty %.6f for %s — clamping",
                               sizing.qty, max_q, market)
                sizing.qty = max_q
            sizing.notional = sizing.qty * price
            logger.debug("[HARPOON] sizing: raw=%.6f adj=%.6f min=%.6f step=%s market=%s",
                         raw_qty, sizing.qty, min_q,
                         BybitInstrumentCache.get_qty_step(market, category=_cat), market)
        except Exception as exc:
            logger.warning("[HARPOON] instrument cache adjust FAILED for %s: %s — aborting entry",
                           market, exc)
            return {"success": False, "reason": f"cache_adjust_failed_{market}"}

        # 최소 주문 체크
        min_notional = 5.0  # USDT
        if sizing.notional < min_notional:
            return {"success": False, "reason": f"notional_too_small_{sizing.notional}"}

        # ★ 마진 캡: notional/leverage가 가용 잔고 초과 시 스킵
        margin_needed = sizing.notional / max(1, float(self.config.leverage))
        try:
            equity = float(getattr(self.system, '_last_equity_usdt', 0) or 0)
            if equity > 0 and margin_needed > equity * 0.30:
                logger.warning(
                    "[HARPOON] MARGIN CAP: margin=$%.0f > equity $%.0f × 30%% — skip %s",
                    margin_needed, equity, market)
                return {"success": False, "reason": f"margin_cap_{market}"}
        except Exception:
            pass

        try:
            client = self._get_client()

            # 레버리지 설정
            try:
                client.set_leverage(market, self.config.leverage)
            except Exception as exc:
                # 이미 설정된 경우 무시 (110043 에러)
                if "110043" not in str(exc):
                    logger.warning("[HARPOON] set_leverage failed: %s", exc)

            # 시장가 주문
            side = "Buy" if direction.upper() == "LONG" else "Sell"
            order = client.place_order(
                market=market,
                side=side,
                ord_type="Market",
                volume=sizing.qty,
            )

            if not order:
                return {"success": False, "reason": "order_failed"}

            # 서버사이드 TP/SL 설정
            if self.config.server_side_tpsl:
                try:
                    client.set_trading_stop(
                        market,
                        take_profit=targets.tp,
                        stop_loss=targets.sl,
                    )
                except Exception as exc:
                    logger.warning("[HARPOON] set_trading_stop failed: %s", exc)

            # 포지션 기록
            self.current_scalp = ScalpPosition(
                market=market,
                direction=direction,
                entry_price=price,
                qty=sizing.qty,
                tp=targets.tp,
                sl=targets.sl,
                atr_used=atr,
                entry_ts=time.time(),
                scalp_id=self._next_scalp_id,
            )
            self._next_scalp_id += 1
            # ★ Phase M.B: active_scalps 동기화 (중복 방지)
            if not any(s.scalp_id == self.current_scalp.scalp_id for s in self.active_scalps):
                self.active_scalps.append(self.current_scalp)
            # ★ Phase M.G: last_peak_update_ts 초기화 (Pre-BE/BE Stall Exit 기준 시각)
            self.current_scalp.last_peak_update_ts = time.time()

            logger.info(
                "[HARPOON] Entry: %s %s qty=%.6f @ $%.4f | TP=$%.4f SL=$%.4f | budget=$%.2f lev=%dx",
                market, direction, sizing.qty, price, targets.tp, targets.sl,
                budget, self.config.leverage,
            )
            try:
                if self.system and hasattr(self.system, 'ledger'):
                    self.system.ledger.append(
                        "HARPOON_ENTRY", market=market,
                        direction=direction, qty=sizing.qty, entry_price=price,
                        tp=targets.tp, sl=targets.sl,
                        leverage=self.config.leverage, budget_usdt=budget,
                    )
            except Exception:
                pass
            # 🔱 텔레그램 진입 알림 (부모님 "하푼 연결" · OMA_HARPOON_ALERTS=0 으로 끔)
            try:
                if self.system and hasattr(self.system, '_send_telegram_safe') and os.getenv("OMA_HARPOON_ALERTS", "1").strip().lower() not in ("0", "false", "no", "off"):
                    self.system._send_telegram_safe(f"🔱 [HARPOON] 진입 {direction} {market}\n@ ${price:.4f} qty={sizing.qty:.4f}\nTP=${targets.tp:.4f} SL=${targets.sl:.4f} | {self.config.leverage}x ${budget:.0f}")
            except Exception:
                pass
            # ── 장부 기록 ──
            try:
                from app.manager.trade_journal import journal
                journal.record_entry(
                    strategy="HARPOON", market=market, direction=direction,
                    price=price, qty=sizing.qty, leverage=self.config.leverage,
                )
            except Exception:
                pass

            return {
                "success": True,
                "qty": sizing.qty,
                "tp": targets.tp,
                "sl": targets.sl,
                "rr": targets.rr_ratio,
                "notional": sizing.notional,
            }

        except Exception as exc:
            logger.error("[HARPOON] entry order error: %s", exc, exc_info=True)
            return {"success": False, "reason": str(exc)}

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------

    def _execute_scalp_exit(self, reason: str, price: float) -> Dict[str, Any]:
        """스캘프 청산 실행."""
        if not self.current_scalp:
            self.state = HarpoonState.COOLDOWN
            self.cooldown_start_ts = time.time()
            return {"success": False, "reason": "no_position"}

        scalp = self.current_scalp
        try:
            client = self._get_client()
            close_side = "Sell" if scalp.direction == "LONG" else "Buy"

            # ★ C5: reduceOnly + reduce_only 모두 전달 (API 호환성 보장)
            order = client.place_order(
                market=scalp.market,
                side=close_side,
                ord_type="Market",
                volume=scalp.qty,
                reduceOnly=True,
                reduce_only=True,
            )

            # PnL 계산
            if scalp.direction == "LONG":
                pnl = (price - scalp.entry_price) * scalp.qty
            else:
                pnl = (scalp.entry_price - price) * scalp.qty

            # Fee 차감 (approximate)
            fee = price * scalp.qty * 0.00055 * 2  # taker fee round trip
            pnl -= fee

            duration = time.time() - scalp.entry_ts
            is_loss = pnl < 0

            # Record
            record = ScalpRecord(
                scalp_id=scalp.scalp_id,
                market=scalp.market,
                direction=scalp.direction,
                entry_price=scalp.entry_price,
                exit_price=price,
                qty=scalp.qty,
                pnl_usdt=round(pnl, 4),
                result=reason,
                duration_sec=round(duration, 1),
                ts=time.time(),
            )
            self.recent_scalps.append(asdict(record))
            if len(self.recent_scalps) > 50:
                self.recent_scalps = self.recent_scalps[-50:]

            # Update counters
            self.scalps_today += 1
            self.scalps_this_hour += 1
            self.daily_pnl += pnl
            self.total_pnl += pnl

            if is_loss:
                self.consecutive_losses += 1
                # ★ Stage 0-7 hook (2026-04-22 형 letter FAIL 4-3 수정):
                # SL/Fast-Reject 발생 시 (market, direction) 30분 cooldown 기록
                try:
                    _mkt = scalp.market if hasattr(scalp, 'market') else market
                    _dir = scalp.direction if hasattr(scalp, 'direction') else direction
                    self._record_post_sl(_mkt, _dir)
                except Exception as exc:
                    logger.debug("[HARPOON] _record_post_sl failed: %s", exc)
                if self.consecutive_losses >= self.config.max_consecutive_loss:
                    self.loss_pause_until = time.time() + 3600  # 1시간 정지
                    logger.warning(
                        "[HARPOON] %d consecutive losses → paused 1 hour",
                        self.consecutive_losses,
                    )
            else:
                self.consecutive_losses = 0

            logger.info(
                "[HARPOON] Exit (%s): %s %s @ $%.4f → $%.4f | PnL=$%.4f | %.1fs | today=%d",
                reason, scalp.market, scalp.direction,
                scalp.entry_price, price, pnl, duration, self.scalps_today,
            )
            # ── 장부 기록 ──
            try:
                from app.manager.trade_journal import journal
                _peak = 0.0
                if scalp.peak_profit_price > 0 and scalp.entry_price > 0:
                    if scalp.direction == "LONG":
                        _peak = (scalp.peak_profit_price / scalp.entry_price - 1) * 100
                    else:
                        _peak = (1 - scalp.peak_profit_price / scalp.entry_price) * 100
                journal.record_exit(
                    strategy="HARPOON", market=scalp.market, direction=scalp.direction,
                    entry_price=scalp.entry_price, exit_price=price, qty=scalp.qty,
                    reason=reason, leverage=self.config.leverage, hold_sec=duration,
                    dynamic_trailing=self.config.dynamic_trailing,
                    breakeven_locked=scalp.breakeven_locked,
                    peak_profit_pct=_peak,
                )
            except Exception:
                pass
            try:
                if self.system and hasattr(self.system, 'ledger'):
                    self.system.ledger.append(
                        "HARPOON_EXIT", market=scalp.market,
                        direction=scalp.direction, qty=scalp.qty,
                        entry_price=scalp.entry_price, exit_price=price,
                        pnl_usdt=round(pnl, 4), reason=reason,
                        duration_sec=round(duration, 1),
                    )
            except Exception:
                pass
            # 🔱 텔레그램 청산 알림 (PnL+사유 · OMA_HARPOON_ALERTS=0 으로 끔)
            try:
                if self.system and hasattr(self.system, '_send_telegram_safe') and os.getenv("OMA_HARPOON_ALERTS", "1").strip().lower() not in ("0", "false", "no", "off"):
                    self.system._send_telegram_safe(f"🔱 [HARPOON] 청산 {scalp.direction} {scalp.market}\n진입 ${scalp.entry_price:.4f} → 청산 ${price:.4f}\nPnL ${pnl:+.2f} | {reason} | {duration:.0f}초")
            except Exception:
                pass

            # Cleanup
            self._clear_current_scalp_from_active()  # Phase M.B sync
            self.current_scalp = None
            self.state = HarpoonState.COOLDOWN
            self.cooldown_start_ts = time.time()
            self._exit_retry_count = 0  # ★ H12: 성공 시 리셋
            self._save_config()

            return {"success": True, "pnl": round(pnl, 4), "duration": round(duration, 1)}

        except Exception as exc:
            logger.error("[HARPOON] exit order error: %s", exc, exc_info=True)
            # ★ H12 FIX: 재시도 cap — 5회 초과 시 포지션 포기
            self._exit_retry_count = getattr(self, '_exit_retry_count', 0) + 1
            if self._exit_retry_count >= 5:
                logger.critical("[HARPOON] Exit failed 5x — abandoning scalp, forcing COOLDOWN")
                self._clear_current_scalp_from_active()  # Phase M.B sync
                self.current_scalp = None
                self.state = HarpoonState.COOLDOWN
                self.cooldown_start_ts = time.time()
                self._exit_retry_count = 0
                self._save_config()
            return {"success": False, "reason": str(exc)}

    # ------------------------------------------------------------------
    # Emergency
    # ------------------------------------------------------------------

    def _emergency_close(self):
        """긴급 청산."""
        if self.current_scalp:
            price = self._get_current_price(self.current_scalp.market)
            # ★ C3 FIX: price=0이면 entry_price fallback (PnL 오염 방지)
            if not price or price <= 0:
                price = self.current_scalp.entry_price
            self._execute_scalp_exit("EMERGENCY", price)

    # ------------------------------------------------------------------
    # Counter resets
    # ------------------------------------------------------------------

    def _maybe_reset_counters(self, now: float):
        """시간/일 카운터 리셋."""
        # 시간 리셋
        if now - self.hour_reset_ts >= 3600:
            self.scalps_this_hour = 0
            self.hour_reset_ts = now

        # 일일 리셋 (07:00 KST = 22:00 UTC)
        # ★ H11 FIX: utcnow() deprecated → now(UTC) 사용
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        reset_hour_utc = 22
        today_reset = now_utc.replace(hour=reset_hour_utc, minute=0, second=0, microsecond=0)
        if now_utc.hour < reset_hour_utc:
            today_reset -= datetime.timedelta(days=1)
        reset_ts = today_reset.timestamp()
        if self.daily_reset_ts < reset_ts:
            self.scalps_today = 0
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self.daily_reset_ts = reset_ts
            logger.info("[HARPOON] Daily counters reset")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_config(self):
        """설정 + 상태 저장."""
        data = {
            "config": asdict(self.config),
            "state": {
                "harpoon_state": self.state.value,
                "scalps_today": self.scalps_today,
                "scalps_this_hour": self.scalps_this_hour,
                "consecutive_losses": self.consecutive_losses,
                "daily_pnl": self.daily_pnl,
                "total_pnl": self.total_pnl,
                "next_scalp_id": self._next_scalp_id,
                "daily_reset_ts": self.daily_reset_ts,
                "hour_reset_ts": self.hour_reset_ts,
                "loss_pause_until": self.loss_pause_until,
                "current_scalp": asdict(self.current_scalp) if self.current_scalp else None,
                "target_zone": self.target_zone,
                "target_direction": self.target_direction,
                "recent_scalps": self.recent_scalps[-20:],  # 최근 20개만 저장
            },
        }
        try:
            from app.core.io_utils import safe_write_json
            safe_write_json(CONFIG_PATH, data)
        except (OSError, ImportError) as exc:
            logger.warning("[HARPOON] save config failed: %s", exc)

    def _load_config(self):
        """설정 + 상태 복원."""
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Config — ★ M19 FIX: bool 필드 특별 처리 (bool("false")==True 방지)
            cfg = data.get("config", {})
            for k, v in cfg.items():
                if hasattr(self.config, k):
                    try:
                        cur = getattr(self.config, k)
                        if isinstance(cur, bool):
                            setattr(self.config, k, v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes"))
                        else:
                            setattr(self.config, k, type(cur)(v))
                    except (TypeError, ValueError):
                        pass

            # State
            st = data.get("state", {})
            self.state = HarpoonState(st.get("harpoon_state", "STANDBY"))
            self.scalps_today = int(st.get("scalps_today", 0))
            self.scalps_this_hour = int(st.get("scalps_this_hour", 0))
            self.consecutive_losses = int(st.get("consecutive_losses", 0))
            self.daily_pnl = float(st.get("daily_pnl", 0))
            self.total_pnl = float(st.get("total_pnl", 0))
            self._next_scalp_id = int(st.get("next_scalp_id", 1))
            self.daily_reset_ts = float(st.get("daily_reset_ts", 0))
            self.hour_reset_ts = float(st.get("hour_reset_ts", 0))
            self.loss_pause_until = float(st.get("loss_pause_until", 0))
            self.target_zone = st.get("target_zone")
            self.target_direction = st.get("target_direction", "")
            self.recent_scalps = st.get("recent_scalps", [])

            # Restore current scalp
            sp = st.get("current_scalp")
            if sp and isinstance(sp, dict) and sp.get("market"):
                self.current_scalp = ScalpPosition(**{
                    k: v for k, v in sp.items()
                    if k in ScalpPosition.__dataclass_fields__
                })
            else:
                self._clear_current_scalp_from_active()  # Phase M.B sync
                self.current_scalp = None

            # ★ 부팅 시 TP/SL 복구 — 재시작하면 Bybit 서버사이드 TP/SL 증발 방지
            if self.current_scalp and self.current_scalp.market:
                try:
                    from app.integrations.bybit_trade import BybitTradeClient
                    _hc = BybitTradeClient(category="linear")
                    _tp = self.current_scalp.tp
                    _sl = self.current_scalp.sl
                    if _tp > 0 and _sl > 0:
                        _hc.set_trading_stop(self.current_scalp.market, take_profit=_tp, stop_loss=_sl)
                        logger.info("[HARPOON] BOOT: TP/SL restored — %s TP=$%.4f SL=$%.4f",
                                    self.current_scalp.market, _tp, _sl)
                except Exception as _boot_err:
                    logger.warning("[HARPOON] BOOT: TP/SL restore failed — %s (will retry in tick)",
                                   _boot_err)

            logger.info(
                "[HARPOON] Config loaded: state=%s enabled=%s scalps_today=%d pnl=$%.2f",
                self.state.value, self.config.enabled, self.scalps_today, self.daily_pnl,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("[HARPOON] load config failed: %s", exc)

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """API/대시보드용 상태 반환."""
        with self._lock:
            focus_state = ""
            focus_market = ""
            focus_adx = 0.0
            adx_ok = True
            adx_threshold = 0
            adx_source = "inherit"
            if self.focus:
                fs = getattr(self.focus, 'state', None)
                focus_state = fs.value if hasattr(fs, 'value') else str(fs or "")
                focus_market = getattr(self.focus, 'selected_market', "") or ""
                focus_adx = round(getattr(self.focus, '_last_adx_value', 0), 1)
                fc = getattr(self.focus, 'config', None)
                if fc and getattr(fc, 'adx_filter_enabled', False):
                    focus_thr = getattr(fc, 'dormant_adx_threshold', 15)
                    harpoon_min = self.config.min_adx
                    if harpoon_min > 0:
                        adx_threshold = harpoon_min
                        adx_source = "harpoon"
                    else:
                        adx_threshold = focus_thr
                        adx_source = "focus"
                    if focus_adx > 0 and focus_adx < adx_threshold:
                        adx_ok = False

            return {
                "enabled": self.config.enabled,
                "state": self.state.value,
                "focus_state": focus_state,
                "focus_market": focus_market,
                "focus_adx": focus_adx,
                "adx_ok": adx_ok,
                "adx_threshold": adx_threshold,
                "adx_source": adx_source,
                "current_scalp": asdict(self.current_scalp) if self.current_scalp else None,
                "target_zone": self.target_zone,
                "target_direction": self.target_direction,
                "scalps_today": self.scalps_today,
                "scalps_this_hour": self.scalps_this_hour,
                "max_daily_scalps": self.config.max_daily_scalps,
                "consecutive_losses": self.consecutive_losses,
                "daily_pnl": round(self.daily_pnl, 4),
                "total_pnl": round(self.total_pnl, 4),
                "budget": round(self.effective_budget, 2),
                "recent_scalps": self.recent_scalps[-10:],
                "config": asdict(self.config),
            }

    def update_config(self, patch: Dict) -> Dict:
        """API로 설정 업데이트."""
        with self._lock:
            for k, v in patch.items():
                if hasattr(self.config, k):
                    try:
                        cur = getattr(self.config, k)
                        if isinstance(cur, bool):
                            setattr(self.config, k, v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes"))
                        else:
                            setattr(self.config, k, type(cur)(v))
                    except (TypeError, ValueError) as exc:
                        logger.warning("[HARPOON] config update failed for %s=%s: %s", k, v, exc)
            self._save_config()
            return asdict(self.config)
