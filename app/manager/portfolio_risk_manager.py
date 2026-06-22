# ============================================================
# File: app/manager/portfolio_risk_manager.py
# Autocoin OS v3-H — 포트폴리오 레벨 리스크 관리
# ------------------------------------------------------------
# 1. 일일 손실 한도 (Daily Loss Limit)
# 2. 코인 상관관계 체크 (Correlation Guard)
# 3. Circuit Breaker 시스템
# ============================================================

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict

from app.core.constants import env_bool, env_float, env_int

logger = logging.getLogger(__name__)

# ============================================================
# 환경변수 설정
# ============================================================
PORTFOLIO_RISK_ENABLED = env_bool("PORTFOLIO_RISK_ENABLED", default=True)
DAILY_LOSS_LIMIT_PCT = env_float("DAILY_LOSS_LIMIT_PCT", default=5.0)  # 일일 -5% 한도
CIRCUIT_BREAKER_LOSS_PCT = env_float("CIRCUIT_BREAKER_LOSS_PCT", default=10.0)  # -10% 전체 중단
CORRELATION_CHECK_ENABLED = env_bool("CORRELATION_CHECK_ENABLED", default=True)
MAX_CORRELATED_POSITIONS = env_int("MAX_CORRELATED_POSITIONS", default=5)  # 동일 방향 최대 5개
CORRELATION_THRESHOLD = env_float("CORRELATION_THRESHOLD", default=0.7)  # 상관계수 임계값


# ============================================================
# 데이터 클래스
# ============================================================

@dataclass
class DailyRiskStatus:
    """일일 리스크 상태"""
    date: str  # YYYY-MM-DD
    starting_capital: float
    current_capital: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    loss_pct: float
    is_paused: bool
    circuit_breaker_active: bool
    last_update: float


@dataclass
class CircuitBreakerState:
    """Circuit Breaker 상태"""
    active: bool
    triggered_at: Optional[float]
    trigger_reason: str
    loss_pct_at_trigger: float
    resume_at: Optional[float]  # 자동 재개 시각 (None = 수동 재개만)
    cooldown_minutes: int = 30


@dataclass
class CorrelationGuard:
    """상관관계 가드 상태"""
    enabled: bool
    correlated_groups: Dict[str, List[str]] = field(default_factory=dict)  # sector -> [markets]
    position_count_by_group: Dict[str, int] = field(default_factory=dict)
    max_positions_per_group: int = 5
    last_analysis: float = 0.0


# ============================================================
# PortfolioRiskManager
# ============================================================

class PortfolioRiskManager:
    """
    포트폴리오 레벨 리스크 관리자
    
    기능:
    1. 일일 손실 한도 모니터링
    2. Circuit Breaker (과도한 손실 시 자동 중단)
    3. 코인 상관관계 체크 (한 방향 몰빵 방지)
    """
    
    def __init__(
        self,
        *,
        state_file: Optional[Path] = None,
        enabled: bool = PORTFOLIO_RISK_ENABLED
    ):
        self.enabled = enabled
        self.state_file = state_file or Path("runtime/portfolio_risk_state.json")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 상태
        self.daily_status: Optional[DailyRiskStatus] = None
        self.circuit_breaker: CircuitBreakerState = CircuitBreakerState(
            active=False,
            triggered_at=None,
            trigger_reason="",
            loss_pct_at_trigger=0.0,
            resume_at=None,
            cooldown_minutes=30
        )
        self.correlation_guard = CorrelationGuard(
            enabled=CORRELATION_CHECK_ENABLED,
            max_positions_per_group=MAX_CORRELATED_POSITIONS
        )
        
        # 임계값 — system 속성이 있으면 UI 설정값 우선, 없으면 .env/기본값
        self.daily_loss_limit_pct = DAILY_LOSS_LIMIT_PCT
        self.circuit_breaker_loss_pct = CIRCUIT_BREAKER_LOSS_PCT
        self._system_ref = None   # hyper_system 참조 (sync_from_system()으로 연결)

        # 상태 로드
        self._load_state()

        logger.info(
            f"PortfolioRiskManager initialized: enabled={self.enabled}, "
            f"daily_limit={self.daily_loss_limit_pct}%, "
            f"circuit_breaker={self.circuit_breaker_loss_pct}%"
        )

    def sync_from_system(self, system) -> None:
        """UI에서 설정한 값을 PRM에 동기화.

        hyper_system.__init__() 또는 guards_set() 후에 호출.
        - daily_loss_limit_pct: Guard Matrix → Daily Loss Limit
        - circuit_breaker_loss_pct: Demotion Rules → Circuit Breaker %
        - circuit_breaker cooldown: Demotion Rules → Circuit Breaker 쿨다운(분)
        """
        self._system_ref = system
        dl = getattr(system, "daily_loss_limit_pct", None)
        if dl is not None and float(dl) > 0:
            self.daily_loss_limit_pct = float(dl)
        cb = getattr(system, "circuit_breaker_loss_pct", None)
        if cb is not None and float(cb) > 0:
            self.circuit_breaker_loss_pct = float(cb)
        cb_cool = getattr(system, "circuit_breaker_cooldown_min", None)
        if cb_cool is not None and float(cb_cool) > 0:
            self.circuit_breaker.cooldown_minutes = float(cb_cool)
        logger.info(
            f"PRM synced from system: daily_limit={self.daily_loss_limit_pct}%, "
            f"circuit_breaker={self.circuit_breaker_loss_pct}%, "
            f"cooldown={self.circuit_breaker.cooldown_minutes}min"
        )
    
    # ============================================================
    # 일일 손실 한도
    # ============================================================
    
    def init_daily_status(self, total_capital: float) -> DailyRiskStatus:
        """일일 리스크 상태 초기화"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        # 이미 오늘 데이터가 있으면 유지
        if self.daily_status and self.daily_status.date == today:
            logger.info("Daily status already initialized for %s", today)
            return self.daily_status
        
        self.daily_status = DailyRiskStatus(
            date=today,
            starting_capital=total_capital,
            current_capital=total_capital,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_pnl=0.0,
            loss_pct=0.0,
            is_paused=False,
            circuit_breaker_active=False,
            last_update=time.time()
        )
        
        self._save_state()
        logger.info(f"Initialized daily status for {today}: capital={total_capital:,.0f}")
        return self.daily_status
    
    def update_portfolio_pnl(
        self,
        current_capital: float,
        realized_pnl: float,
        unrealized_pnl: float
    ) -> DailyRiskStatus:
        """포트폴리오 손익 업데이트 및 리스크 체크"""
        if not self.enabled:
            return self._get_dummy_status()
        
        # 날짜 체크 (자정 넘어가면 초기화)
        today = datetime.now().strftime("%Y-%m-%d")
        if not self.daily_status or self.daily_status.date != today:
            self.init_daily_status(current_capital)
            return self.daily_status
        
        # 손익 업데이트
        self.daily_status.current_capital = current_capital
        self.daily_status.realized_pnl = realized_pnl
        self.daily_status.unrealized_pnl = unrealized_pnl
        self.daily_status.total_pnl = realized_pnl + unrealized_pnl
        self.daily_status.loss_pct = (
            (self.daily_status.total_pnl / self.daily_status.starting_capital * 100)
            if self.daily_status.starting_capital > 0 else 0.0
        )
        self.daily_status.last_update = time.time()
        
        # 리스크 체크
        self._check_daily_loss_limit()
        self._check_circuit_breaker()
        
        self._save_state()
        return self.daily_status
    
    def _check_daily_loss_limit(self):
        """일일 손실 한도 체크"""
        if not self.daily_status:
            return
        
        # 손실이 한도 초과 시 일시정지
        if self.daily_status.loss_pct < -self.daily_loss_limit_pct:
            if not self.daily_status.is_paused:
                self.daily_status.is_paused = True
                logger.warning(
                    f"🛑 Daily loss limit exceeded: {self.daily_status.loss_pct:.2f}% "
                    f"(limit: -{self.daily_loss_limit_pct}%). New entries PAUSED."
                )
        else:
            # 손실이 완화되면 자동 재개
            if self.daily_status.is_paused:
                self.daily_status.is_paused = False
                logger.info(
                    f"✅ Daily loss recovered: {self.daily_status.loss_pct:.2f}%. "
                    f"New entries RESUMED."
                )
    
    def _check_circuit_breaker(self):
        """Circuit Breaker 체크"""
        if not self.daily_status:
            return
        
        # Circuit Breaker 트리거
        if self.daily_status.loss_pct < -self.circuit_breaker_loss_pct:
            if not self.circuit_breaker.active:
                self.circuit_breaker.active = True
                self.circuit_breaker.triggered_at = time.time()
                self.circuit_breaker.trigger_reason = (
                    f"Portfolio loss {self.daily_status.loss_pct:.2f}% "
                    f"exceeded circuit breaker threshold -{self.circuit_breaker_loss_pct}%"
                )
                self.circuit_breaker.loss_pct_at_trigger = self.daily_status.loss_pct
                self.circuit_breaker.resume_at = (
                    time.time() + self.circuit_breaker.cooldown_minutes * 60
                )
                
                self.daily_status.circuit_breaker_active = True
                
                logger.critical(
                    f"🚨 CIRCUIT BREAKER TRIGGERED: {self.daily_status.loss_pct:.2f}% loss. "
                    f"All trading HALTED for {self.circuit_breaker.cooldown_minutes} minutes."
                )
        
        # 자동 재개 체크
        if self.circuit_breaker.active and self.circuit_breaker.resume_at:
            if time.time() >= self.circuit_breaker.resume_at:
                self._resume_circuit_breaker(auto=True)
    
    def _resume_circuit_breaker(self, auto: bool = False):
        """Circuit Breaker 재개"""
        self.circuit_breaker.active = False
        self.circuit_breaker.resume_at = None
        
        if self.daily_status:
            self.daily_status.circuit_breaker_active = False
        
        reason = "Auto-resumed after cooldown" if auto else "Manual resume"
        logger.warning("⚡ CIRCUIT BREAKER RESUMED: %s", reason)
    
    def can_enter_new_position(self) -> Tuple[bool, str]:
        """신규 진입 가능 여부 체크"""
        if not self.enabled:
            return True, "Risk management disabled"

        if not self.daily_status:
            return True, "Daily status not initialized"

        # [FIX 2026-03-24] 자정 넘으면 자동 리셋 — 어제 손실이 오늘 매수를 막는 버그 방지
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_status.date != today:
            equity = self.daily_status.current_capital or self.daily_status.starting_capital
            self.init_daily_status(equity)
            if self.circuit_breaker.active:
                self._resume_circuit_breaker(auto=True)
            logger.info("PRM auto-reset: date changed to %s", today)
            return True, "Daily reset"

        # Circuit Breaker 쿨다운 자동 해제 (매 체크 시)
        if self.circuit_breaker.active and self.circuit_breaker.resume_at:
            if time.time() >= self.circuit_breaker.resume_at:
                self._resume_circuit_breaker(auto=True)

        # Circuit Breaker 체크
        if self.circuit_breaker.active:
            remaining = ""
            if self.circuit_breaker.resume_at:
                remaining_sec = max(0, self.circuit_breaker.resume_at - time.time())
                remaining = f" (resume in {remaining_sec/60:.1f} min)"
            return False, f"Circuit breaker active{remaining}"
        
        # 일일 손실 한도 체크
        if self.daily_status.is_paused:
            return False, (
                f"Daily loss limit exceeded: {self.daily_status.loss_pct:.2f}% "
                f"(limit: -{self.daily_loss_limit_pct}%)"
            )
        
        return True, "OK"

    def get_size_multiplier(self) -> float:
        """포트폴리오 PnL 기반 매수 규모 배수 반환 (0.0 ~ 1.0).

        OMA_SIZE_MULT_HI_PCT(기본 -2.0%) ~ daily_loss_limit_pct(-5.0%) 구간에서
        선형으로 감소. -2% 이상이면 1.0, -5% 이하이면 floor 반환.
        """
        import os
        if not self.enabled or not self.daily_status:
            return 1.0
        loss = self.daily_status.loss_pct                            # 음수 (예: -3.0)
        hi = float(os.getenv("OMA_SIZE_MULT_HI_PCT", "-2.0"))       # 감소 시작점
        lo = -float(self.daily_loss_limit_pct)                       # 완전 차단점 (-5.0)
        floor = float(os.getenv("OMA_SIZE_MULT_FLOOR", "0.4"))       # 최솟값
        if loss >= hi:
            return 1.0
        if loss <= lo:
            return floor
        ratio = (loss - lo) / (hi - lo)                              # 0.0 ~ 1.0
        return floor + ratio * (1.0 - floor)

    # ============================================================
    # 코인 상관관계 체크
    # ============================================================
    
    def update_correlation_groups(self, market_sectors: Dict[str, str]):
        """
        코인 섹터 정보 업데이트
        
        Args:
            market_sectors: {market: sector} 예) {"BTCUSDT": "L1", "ETHUSDT": "L1"}
        """
        if not self.correlation_guard.enabled:
            return
        
        # 섹터별 그룹 재구성
        groups = defaultdict(list)
        for market, sector in market_sectors.items():
            groups[sector].append(market)
        
        self.correlation_guard.correlated_groups = dict(groups)
        self.correlation_guard.last_analysis = time.time()
        
        logger.info(f"Updated correlation groups: {len(groups)} sectors")
    
    def check_correlation_limit(
        self,
        market: str,
        sector: str,
        active_markets: Set[str]
    ) -> Tuple[bool, str]:
        """
        상관관계 한도 체크
        
        Args:
            market: 진입하려는 마켓
            sector: 해당 마켓의 섹터
            active_markets: 현재 활성 포지션 마켓들
        
        Returns:
            (허용 여부, 사유)
        """
        if not self.correlation_guard.enabled:
            return True, "Correlation guard disabled"
        
        # 동일 섹터의 활성 포지션 수 카운트
        sector_positions = [
            m for m in active_markets
            if m in self.correlation_guard.correlated_groups.get(sector, [])
        ]
        
        # 한도 체크
        if len(sector_positions) >= self.correlation_guard.max_positions_per_group:
            return False, (
                f"Sector '{sector}' limit reached: {len(sector_positions)}/{self.correlation_guard.max_positions_per_group} "
                f"positions ({', '.join(sector_positions[:3])}...)"
            )
        
        return True, "OK"
    
    def get_sector_exposure(self, active_positions: Dict[str, dict]) -> Dict[str, float]:
        """
        섹터별 익스포저 계산
        
        Args:
            active_positions: {market: {"budget": float, "sector": str}}
        
        Returns:
            {sector: total_exposure_usdt}
        """
        exposure = defaultdict(float)
        
        for market, pos in active_positions.items():
            sector = pos.get("sector", "UNKNOWN")
            budget = pos.get("budget", 0.0)
            exposure[sector] += budget
        
        return dict(exposure)
    
    # ============================================================
    # 수동 제어
    # ============================================================
    
    def manual_resume(self):
        """수동으로 Circuit Breaker 재개"""
        if self.circuit_breaker.active:
            self._resume_circuit_breaker(auto=False)
            self._save_state()
    
    def manual_pause(self, reason: str = "Manual pause"):
        """수동으로 신규 진입 일시정지"""
        if self.daily_status:
            self.daily_status.is_paused = True
            logger.warning("📛 Manual pause activated: %s", reason)
            self._save_state()
    
    def manual_unpause(self):
        """수동으로 일시정지 해제"""
        if self.daily_status:
            self.daily_status.is_paused = False
            logger.info("✅ Manual unpause: new entries allowed")
            self._save_state()
    
    def reset_daily_status(self, new_capital: float):
        """일일 상태 강제 리셋 (운영자 전용)"""
        logger.warning(f"🔄 Daily status FORCE RESET: capital={new_capital:,.0f}")
        self.init_daily_status(new_capital)
        self.circuit_breaker.active = False
        self.circuit_breaker.resume_at = None
        self._save_state()
    
    # ============================================================
    # 상태 영속화
    # ============================================================
    
    def _save_state(self):
        """상태 파일 저장"""
        try:
            state = {
                "daily_status": asdict(self.daily_status) if self.daily_status else None,
                "circuit_breaker": asdict(self.circuit_breaker),
                "correlation_guard": {
                    "enabled": self.correlation_guard.enabled,
                    "correlated_groups": self.correlation_guard.correlated_groups,
                    "position_count_by_group": self.correlation_guard.position_count_by_group,
                    "max_positions_per_group": self.correlation_guard.max_positions_per_group,
                    "last_analysis": self.correlation_guard.last_analysis,
                }
            }
            
            self.state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.error("Failed to save portfolio risk state: %s", e)
    
    def _load_state(self):
        """상태 파일 로드"""
        if not self.state_file.exists():
            return
        
        try:
            state = json.loads(self.state_file.read_text())
            
            # Daily status
            if state.get("daily_status"):
                self.daily_status = DailyRiskStatus(**state["daily_status"])
            
            # Circuit breaker
            if state.get("circuit_breaker"):
                self.circuit_breaker = CircuitBreakerState(**state["circuit_breaker"])
            
            # Correlation guard
            if state.get("correlation_guard"):
                cg = state["correlation_guard"]
                self.correlation_guard.enabled = cg.get("enabled", True)
                self.correlation_guard.correlated_groups = cg.get("correlated_groups", {})
                self.correlation_guard.position_count_by_group = cg.get("position_count_by_group", {})
                self.correlation_guard.max_positions_per_group = cg.get("max_positions_per_group", 5)
                self.correlation_guard.last_analysis = cg.get("last_analysis", 0.0)
            
            logger.info(f"Loaded portfolio risk state from {self.state_file}")
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error("Failed to load portfolio risk state: %s", e)
    
    def _get_dummy_status(self) -> DailyRiskStatus:
        """비활성화 시 더미 상태 반환"""
        return DailyRiskStatus(
            date=datetime.now().strftime("%Y-%m-%d"),
            starting_capital=0.0,
            current_capital=0.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_pnl=0.0,
            loss_pct=0.0,
            is_paused=False,
            circuit_breaker_active=False,
            last_update=time.time()
        )
    
    # ============================================================
    # 상태 조회
    # ============================================================
    
    def get_status_summary(self) -> dict:
        """리스크 관리 상태 요약"""
        can_enter, entry_reason = self.can_enter_new_position()
        
        return {
            "enabled": self.enabled,
            "can_enter_new_position": can_enter,
            "entry_block_reason": entry_reason if not can_enter else None,
            "daily_status": asdict(self.daily_status) if self.daily_status else None,
            "circuit_breaker": {
                "active": self.circuit_breaker.active,
                "triggered_at": self.circuit_breaker.triggered_at,
                "trigger_reason": self.circuit_breaker.trigger_reason,
                "loss_pct_at_trigger": self.circuit_breaker.loss_pct_at_trigger,
                "resume_at": self.circuit_breaker.resume_at,
                "cooldown_minutes": self.circuit_breaker.cooldown_minutes,
            },
            "correlation_guard": {
                "enabled": self.correlation_guard.enabled,
                "max_positions_per_group": self.correlation_guard.max_positions_per_group,
                "groups": self.correlation_guard.correlated_groups,
                "last_analysis": self.correlation_guard.last_analysis,
            },
            "thresholds": {
                "daily_loss_limit_pct": self.daily_loss_limit_pct,
                "circuit_breaker_loss_pct": self.circuit_breaker_loss_pct,
            }
        }


# ============================================================
# 싱글톤 인스턴스
# ============================================================
_portfolio_risk_manager: Optional[PortfolioRiskManager] = None


def get_portfolio_risk_manager() -> PortfolioRiskManager:
    """포트폴리오 리스크 관리자 싱글톤 가져오기"""
    global _portfolio_risk_manager
    if _portfolio_risk_manager is None:
        _portfolio_risk_manager = PortfolioRiskManager()
    return _portfolio_risk_manager
