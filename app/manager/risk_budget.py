# ============================================================
# File: app/manager/risk_budget.py
# Autocoin OS v3-H — Risk Budget System
# ------------------------------------------------------------
# 목적:
# - 일일 최대 손실 한도 관리
# - 한도 도달 시 신규 진입 중단
# - 자본 보호 자동화
# ============================================================

from __future__ import annotations
import logging

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
logger = logging.getLogger(__name__)

class RiskMode(Enum):
    """리스크 모드."""
    NORMAL = "normal"           # 정상 운영
    CAUTION = "caution"         # 주의 (50% 소진)
    WARNING = "warning"         # 경고 (75% 소진)
    DEFENSE = "defense"         # 방어 (100% 소진, 진입 중단)
    RECOVERY = "recovery"       # 회복 중 (수익 발생 시)

@dataclass
class DailyRiskState:
    """일일 리스크 상태."""
    date: str  # YYYY-MM-DD
    
    # 손익
    realized_pnl_usdt: float = 0.0
    unrealized_pnl_usdt: float = 0.0
    total_pnl_usdt: float = 0.0
    
    # 리스크 버짓
    daily_loss_limit_usdt: float = 0.0
    used_budget_usdt: float = 0.0
    remaining_budget_usdt: float = 0.0
    
    # 상태
    mode: RiskMode = RiskMode.NORMAL
    new_entry_allowed: bool = True
    
    # 카운터
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    
    # 시간
    last_update_ts: float = 0.0

@dataclass
class RiskAction:
    """리스크 액션."""
    action: str  # "block_entry", "reduce_budget", "alert", "none"
    reason: str
    severity: str  # "info", "warning", "critical"
    details: Dict[str, Any] = field(default_factory=dict)

class RiskBudgetManager:
    """리스크 버짓 관리자.
    
    기능:
    1. 일일 최대 손실 한도 설정 (총 자본의 X%)
    2. 실시간 PnL 추적
    3. 한도 도달 시 자동 방어 모드
    4. 단계별 경고 시스템
    """

    def __init__(
        self,
        daily_loss_limit_pct: float = 2.0,  # 총 자본의 2%
        caution_threshold_pct: float = 50.0,
        warning_threshold_pct: float = 75.0,
        defense_threshold_pct: float = 100.0,
        recovery_required_pct: float = 25.0,  # 방어 모드 해제 위해 25% 회복 필요
        state_path: str = "runtime/risk_budget_state.json",
    ):
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.caution_threshold_pct = caution_threshold_pct
        self.warning_threshold_pct = warning_threshold_pct
        self.defense_threshold_pct = defense_threshold_pct
        self.recovery_required_pct = recovery_required_pct
        self.state_path = state_path
        
        self._state: Optional[DailyRiskState] = None
        self._load_state()

    def _get_today(self) -> str:
        """오늘 날짜 반환."""
        return time.strftime("%Y-%m-%d", time.localtime())

    def _load_state(self) -> None:
        """상태 로드."""
        if not self.state_path or not os.path.exists(self.state_path):
            return
        
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            if data.get("date") == self._get_today():
                self._state = DailyRiskState(
                    date=data.get("date", ""),
                    realized_pnl_usdt=float(data.get("realized_pnl_usdt") or 0),
                    unrealized_pnl_usdt=float(data.get("unrealized_pnl_usdt") or 0),
                    total_pnl_usdt=float(data.get("total_pnl_usdt") or 0),
                    daily_loss_limit_usdt=float(data.get("daily_loss_limit_usdt") or 0),
                    used_budget_usdt=float(data.get("used_budget_usdt") or 0),
                    remaining_budget_usdt=float(data.get("remaining_budget_usdt") or 0),
                    mode=RiskMode(data.get("mode", "normal")),
                    new_entry_allowed=bool(data.get("new_entry_allowed", True)),
                    trade_count=int(data.get("trade_count", 0)),
                    win_count=int(data.get("win_count", 0)),
                    loss_count=int(data.get("loss_count", 0)),
                    last_update_ts=float(data.get("last_update_ts", 0)),
                )
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[risk_budget] %s: %s", 'risk_budget._load_state fallback', exc, exc_info=True)

    def _save_state(self) -> None:
        """상태 저장."""
        if not self.state_path or not self._state:
            return

        try:
            from app.core.io_utils import safe_write_json
            data = {
                "date": self._state.date,
                "realized_pnl_usdt": self._state.realized_pnl_usdt,
                "unrealized_pnl_usdt": self._state.unrealized_pnl_usdt,
                "total_pnl_usdt": self._state.total_pnl_usdt,
                "daily_loss_limit_usdt": self._state.daily_loss_limit_usdt,
                "used_budget_usdt": self._state.used_budget_usdt,
                "remaining_budget_usdt": self._state.remaining_budget_usdt,
                "mode": self._state.mode.value,
                "new_entry_allowed": self._state.new_entry_allowed,
                "trade_count": self._state.trade_count,
                "win_count": self._state.win_count,
                "loss_count": self._state.loss_count,
                "last_update_ts": self._state.last_update_ts,
                "ts": time.time(),
            }
            safe_write_json(self.state_path, data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("[risk_budget] %s: %s", 'risk_budget._save_state fallback', exc, exc_info=True)

    def init_daily_budget(self, total_capital_usdt: float) -> DailyRiskState:
        """일일 버짓 초기화."""
        today = self._get_today()
        
        # 이미 오늘 상태가 있으면 반환
        if self._state and self._state.date == today:
            return self._state
        
        # 새로운 날 시작
        daily_limit = total_capital_usdt * (self.daily_loss_limit_pct / 100)
        
        self._state = DailyRiskState(
            date=today,
            daily_loss_limit_usdt=daily_limit,
            remaining_budget_usdt=daily_limit,
            last_update_ts=time.time(),
        )
        
        self._save_state()
        return self._state

    def update_pnl(
        self,
        realized_pnl_usdt: float,
        unrealized_pnl_usdt: float = 0.0,
        trade_result: Optional[str] = None,  # "win" or "loss"
    ) -> RiskAction:
        """PnL 업데이트 및 리스크 체크."""
        if not self._state:
            return RiskAction(action="none", reason="no_state", severity="info")
        
        # PnL 업데이트
        self._state.realized_pnl_usdt = realized_pnl_usdt
        self._state.unrealized_pnl_usdt = unrealized_pnl_usdt
        self._state.total_pnl_usdt = realized_pnl_usdt + unrealized_pnl_usdt
        
        # 거래 결과 카운트
        if trade_result == "win":
            self._state.win_count += 1
            self._state.trade_count += 1
        elif trade_result == "loss":
            self._state.loss_count += 1
            self._state.trade_count += 1
        
        # 손실 계산 (음수면 손실)
        if self._state.total_pnl_usdt < 0:
            self._state.used_budget_usdt = abs(self._state.total_pnl_usdt)
        else:
            self._state.used_budget_usdt = 0.0
        
        self._state.remaining_budget_usdt = max(
            0, self._state.daily_loss_limit_usdt - self._state.used_budget_usdt
        )
        
        # 모드 결정
        action = self._evaluate_risk()
        
        self._state.last_update_ts = time.time()
        self._save_state()
        
        return action

    def _evaluate_risk(self) -> RiskAction:
        """리스크 평가 및 모드 전환."""
        if not self._state or self._state.daily_loss_limit_usdt <= 0:
            return RiskAction(action="none", reason="no_limit", severity="info")
        
        usage_pct = (self._state.used_budget_usdt / self._state.daily_loss_limit_usdt) * 100
        
        old_mode = self._state.mode
        
        # 방어 모드에서 회복 체크
        if old_mode == RiskMode.DEFENSE:
            if self._state.total_pnl_usdt >= 0:
                # 손실 복구됨
                self._state.mode = RiskMode.NORMAL
                self._state.new_entry_allowed = True
                return RiskAction(
                    action="resume_entry",
                    reason="loss_recovered",
                    severity="info",
                    details={"pnl": self._state.total_pnl_usdt},
                )
            elif usage_pct < (self.defense_threshold_pct - self.recovery_required_pct):
                # 부분 회복
                self._state.mode = RiskMode.RECOVERY
                self._state.new_entry_allowed = False
                return RiskAction(
                    action="wait_recovery",
                    reason="partial_recovery",
                    severity="warning",
                )
        
        # 모드 전환
        if usage_pct >= self.defense_threshold_pct:
            self._state.mode = RiskMode.DEFENSE
            self._state.new_entry_allowed = False
            return RiskAction(
                action="block_entry",
                reason=f"daily_loss_limit_reached:{usage_pct:.1f}%",
                severity="critical",
                details={
                    "used_usdt": self._state.used_budget_usdt,
                    "limit_usdt": self._state.daily_loss_limit_usdt,
                    "usage_pct": usage_pct,
                },
            )
        elif usage_pct >= self.warning_threshold_pct:
            self._state.mode = RiskMode.WARNING
            self._state.new_entry_allowed = True
            return RiskAction(
                action="reduce_budget",
                reason=f"approaching_limit:{usage_pct:.1f}%",
                severity="warning",
                details={"budget_multiplier": 0.5},
            )
        elif usage_pct >= self.caution_threshold_pct:
            self._state.mode = RiskMode.CAUTION
            self._state.new_entry_allowed = True
            return RiskAction(
                action="alert",
                reason=f"caution_threshold:{usage_pct:.1f}%",
                severity="warning",
            )
        else:
            self._state.mode = RiskMode.NORMAL
            self._state.new_entry_allowed = True
            return RiskAction(action="none", reason="within_budget", severity="info")

    def is_entry_allowed(self) -> Tuple[bool, str]:
        """신규 진입 허용 여부."""
        if not self._state:
            return (True, "no_state")
        
        # 날짜 체크
        if self._state.date != self._get_today():
            return (True, "new_day")
        
        if not self._state.new_entry_allowed:
            return (False, f"blocked:{self._state.mode.value}")
        
        return (True, f"allowed:{self._state.mode.value}")

    def get_budget_multiplier(self) -> float:
        """현재 모드에 따른 예산 승수."""
        if not self._state:
            return 1.0
        
        if self._state.mode == RiskMode.DEFENSE:
            return 0.0
        elif self._state.mode == RiskMode.RECOVERY:
            return 0.3
        elif self._state.mode == RiskMode.WARNING:
            return 0.5
        elif self._state.mode == RiskMode.CAUTION:
            return 0.7
        else:
            return 1.0

    def get_state(self) -> Optional[DailyRiskState]:
        """현재 상태 반환."""
        return self._state

    def get_summary(self) -> Dict[str, Any]:
        """상태 요약."""
        if not self._state:
            return {"initialized": False}
        
        usage_pct = 0.0
        if self._state.daily_loss_limit_usdt > 0:
            usage_pct = (self._state.used_budget_usdt / self._state.daily_loss_limit_usdt) * 100
        
        win_rate = 0.0
        if self._state.trade_count > 0:
            win_rate = (self._state.win_count / self._state.trade_count) * 100
        
        return {
            "date": self._state.date,
            "mode": self._state.mode.value,
            "entry_allowed": self._state.new_entry_allowed,
            "total_pnl_usdt": round(self._state.total_pnl_usdt, 0),
            "realized_pnl_usdt": round(self._state.realized_pnl_usdt, 0),
            "unrealized_pnl_usdt": round(self._state.unrealized_pnl_usdt, 0),
            "daily_limit_usdt": round(self._state.daily_loss_limit_usdt, 0),
            "used_budget_usdt": round(self._state.used_budget_usdt, 0),
            "remaining_usdt": round(self._state.remaining_budget_usdt, 0),
            "usage_pct": round(usage_pct, 1),
            "budget_multiplier": self.get_budget_multiplier(),
            "trade_count": self._state.trade_count,
            "win_count": self._state.win_count,
            "loss_count": self._state.loss_count,
            "win_rate_pct": round(win_rate, 1),
        }

    def force_reset(self, total_capital_usdt: float) -> DailyRiskState:
        """강제 리셋 (관리자용)."""
        self._state = None
        return self.init_daily_budget(total_capital_usdt)
