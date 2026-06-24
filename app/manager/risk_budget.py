# ============================================================
# File: app/manager/risk_budget.py
# Autocoin OS v3-H — Risk Budget System
# ------------------------------------------------------------
# Purpose:
# - Manage the daily maximum loss limit
# - Halt new entries once the limit is reached
# - Automate capital protection
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
    """Risk mode."""
    NORMAL = "normal"           # normal operation
    CAUTION = "caution"         # caution (50% used)
    WARNING = "warning"         # warning (75% used)
    DEFENSE = "defense"         # defense (100% used, entries halted)
    RECOVERY = "recovery"       # recovering (when profit appears)

@dataclass
class DailyRiskState:
    """Daily risk state."""
    date: str  # YYYY-MM-DD

    # PnL
    realized_pnl_usdt: float = 0.0
    unrealized_pnl_usdt: float = 0.0
    total_pnl_usdt: float = 0.0

    # risk budget
    daily_loss_limit_usdt: float = 0.0
    used_budget_usdt: float = 0.0
    remaining_budget_usdt: float = 0.0

    # status
    mode: RiskMode = RiskMode.NORMAL
    new_entry_allowed: bool = True

    # counters
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0

    # time
    last_update_ts: float = 0.0

@dataclass
class RiskAction:
    """Risk action."""
    action: str  # "block_entry", "reduce_budget", "alert", "none"
    reason: str
    severity: str  # "info", "warning", "critical"
    details: Dict[str, Any] = field(default_factory=dict)

class RiskBudgetManager:
    """Risk budget manager.

    Features:
    1. Set the daily maximum loss limit (X% of total capital)
    2. Track PnL in real time
    3. Automatic defense mode when the limit is reached
    4. Tiered warning system
    """

    def __init__(
        self,
        daily_loss_limit_pct: float = 2.0,  # 2% of total capital
        caution_threshold_pct: float = 50.0,
        warning_threshold_pct: float = 75.0,
        defense_threshold_pct: float = 100.0,
        recovery_required_pct: float = 25.0,  # 25% recovery required to exit defense mode
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
        """Return today's date."""
        return time.strftime("%Y-%m-%d", time.localtime())

    def _load_state(self) -> None:
        """Load state."""
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
        """Save state."""
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
        """Initialize the daily budget."""
        today = self._get_today()

        # return if today's state already exists
        if self._state and self._state.date == today:
            return self._state

        # start of a new day
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
        """Update PnL and run the risk check."""
        if not self._state:
            return RiskAction(action="none", reason="no_state", severity="info")

        # update PnL
        self._state.realized_pnl_usdt = realized_pnl_usdt
        self._state.unrealized_pnl_usdt = unrealized_pnl_usdt
        self._state.total_pnl_usdt = realized_pnl_usdt + unrealized_pnl_usdt

        # count the trade result
        if trade_result == "win":
            self._state.win_count += 1
            self._state.trade_count += 1
        elif trade_result == "loss":
            self._state.loss_count += 1
            self._state.trade_count += 1

        # compute the loss (negative means a loss)
        if self._state.total_pnl_usdt < 0:
            self._state.used_budget_usdt = abs(self._state.total_pnl_usdt)
        else:
            self._state.used_budget_usdt = 0.0

        self._state.remaining_budget_usdt = max(
            0, self._state.daily_loss_limit_usdt - self._state.used_budget_usdt
        )

        # determine the mode
        action = self._evaluate_risk()
        
        self._state.last_update_ts = time.time()
        self._save_state()
        
        return action

    def _evaluate_risk(self) -> RiskAction:
        """Evaluate risk and switch mode."""
        if not self._state or self._state.daily_loss_limit_usdt <= 0:
            return RiskAction(action="none", reason="no_limit", severity="info")

        usage_pct = (self._state.used_budget_usdt / self._state.daily_loss_limit_usdt) * 100

        old_mode = self._state.mode

        # check for recovery while in defense mode
        if old_mode == RiskMode.DEFENSE:
            if self._state.total_pnl_usdt >= 0:
                # loss recovered
                self._state.mode = RiskMode.NORMAL
                self._state.new_entry_allowed = True
                return RiskAction(
                    action="resume_entry",
                    reason="loss_recovered",
                    severity="info",
                    details={"pnl": self._state.total_pnl_usdt},
                )
            elif usage_pct < (self.defense_threshold_pct - self.recovery_required_pct):
                # partial recovery
                self._state.mode = RiskMode.RECOVERY
                self._state.new_entry_allowed = False
                return RiskAction(
                    action="wait_recovery",
                    reason="partial_recovery",
                    severity="warning",
                )
        
        # mode transition
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
        """Whether new entries are allowed."""
        if not self._state:
            return (True, "no_state")

        # date check
        if self._state.date != self._get_today():
            return (True, "new_day")
        
        if not self._state.new_entry_allowed:
            return (False, f"blocked:{self._state.mode.value}")
        
        return (True, f"allowed:{self._state.mode.value}")

    def get_budget_multiplier(self) -> float:
        """Budget multiplier for the current mode."""
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
        """Return the current state."""
        return self._state

    def get_summary(self) -> Dict[str, Any]:
        """State summary."""
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
        """Force reset (for administrators)."""
        self._state = None
        return self.init_daily_budget(total_capital_usdt)
