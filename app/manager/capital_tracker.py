"""Capital Tracker — 입출금 추적 + 순수 트레이딩 성과 계산.

입금/출금과 무관하게 순수 트레이딩 실력을 측정하기 위한 모듈.
TWR (Time-Weighted Return) 방식으로 자본 변동을 보정.

사용법:
  - 입금 시: POST /api/strategy/focus/capital/deposit {amount: 500}
  - 출금 시: POST /api/strategy/focus/capital/withdraw {amount: 200}
  - 성과 조회: GET /api/strategy/focus/capital/performance

파일:
  - runtime/capital_events.jsonl — 입출금 이벤트 로그
  - runtime/capital_baseline.json — 초기 자본 + 현재 기준선
"""
import json
import os
import time
import logging
from typing import Dict, Any, List, Optional

from app.core.io_utils import safe_write_json

logger = logging.getLogger(__name__)

EVENTS_PATH = os.path.join("runtime", "capital_events.jsonl")
BASELINE_PATH = os.path.join("runtime", "capital_baseline.json")


class CapitalTracker:
    """입출금 추적 + 순수 트레이딩 ROI 계산."""

    def __init__(self):
        self._baseline = self._load_baseline()

    def _load_baseline(self) -> Dict:
        """기준선 로드. 없으면 빈 상태."""
        if os.path.exists(BASELINE_PATH):
            try:
                with open(BASELINE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "initial_capital": 0.0,     # 최초 입금 시 설정
            "total_deposited": 0.0,     # 누적 입금액
            "total_withdrawn": 0.0,     # 누적 출금액
            "net_invested": 0.0,        # 순 투자금 (입금 - 출금)
            "created_ts": 0.0,
        }

    def _save_baseline(self):
        safe_write_json(BASELINE_PATH, self._baseline)

    def _append_event(self, event: Dict):
        """이벤트를 JSONL에 추가."""
        os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def get_events(self) -> List[Dict]:
        """모든 자본 이벤트 조회."""
        events = []
        if not os.path.exists(EVENTS_PATH):
            return events
        try:
            with open(EVENTS_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return events

    # ── 입금 / 출금 ──

    def deposit(self, amount: float, memo: str = "") -> Dict:
        """입금 기록."""
        if amount <= 0:
            return {"ok": False, "error": "Amount must be positive"}

        event = {
            "type": "DEPOSIT",
            "amount": round(amount, 2),
            "memo": memo,
            "ts": time.time(),
        }
        self._append_event(event)

        if self._baseline["initial_capital"] == 0:
            self._baseline["initial_capital"] = amount
            self._baseline["created_ts"] = time.time()

        self._baseline["total_deposited"] = round(
            self._baseline["total_deposited"] + amount, 2
        )
        self._baseline["net_invested"] = round(
            self._baseline["total_deposited"] - self._baseline["total_withdrawn"], 2
        )
        self._save_baseline()

        logger.info("[Capital] DEPOSIT $%.2f (memo: %s) → net_invested=$%.2f",
                     amount, memo, self._baseline["net_invested"])
        return {"ok": True, "event": event, "baseline": self._baseline}

    def withdraw(self, amount: float, memo: str = "") -> Dict:
        """출금 기록."""
        if amount <= 0:
            return {"ok": False, "error": "Amount must be positive"}

        event = {
            "type": "WITHDRAW",
            "amount": round(amount, 2),
            "memo": memo,
            "ts": time.time(),
        }
        self._append_event(event)

        self._baseline["total_withdrawn"] = round(
            self._baseline["total_withdrawn"] + amount, 2
        )
        self._baseline["net_invested"] = round(
            self._baseline["total_deposited"] - self._baseline["total_withdrawn"], 2
        )
        self._save_baseline()

        logger.info("[Capital] WITHDRAW $%.2f (memo: %s) → net_invested=$%.2f",
                     amount, memo, self._baseline["net_invested"])
        return {"ok": True, "event": event, "baseline": self._baseline}

    def set_initial(self, amount: float) -> Dict:
        """초기 자본 설정 (첫 시작 시)."""
        self._baseline["initial_capital"] = round(amount, 2)
        if self._baseline["total_deposited"] == 0:
            self._baseline["total_deposited"] = round(amount, 2)
            self._baseline["net_invested"] = round(amount, 2)
        if self._baseline["created_ts"] == 0:
            self._baseline["created_ts"] = time.time()
        self._save_baseline()

        event = {
            "type": "INITIAL",
            "amount": round(amount, 2),
            "memo": "Initial capital set",
            "ts": time.time(),
        }
        self._append_event(event)

        logger.info("[Capital] Initial capital set to $%.2f", amount)
        return {"ok": True, "baseline": self._baseline}

    # ── 성과 계산 ──

    def get_performance(self, current_equity: float, trading_pnl: float) -> Dict[str, Any]:
        """순수 트레이딩 성과 계산.

        Args:
            current_equity: 현재 Bybit 지갑 잔고 (USDT)
            trading_pnl: journal에서 계산한 총 실현 PnL

        Returns:
            순수 성과 지표들
        """
        net_invested = self._baseline.get("net_invested", 0)
        initial = self._baseline.get("initial_capital", 0)

        # 1. 절대 수익 = 현재 잔고 - 순 투자금
        absolute_profit = round(current_equity - net_invested, 2) if net_invested > 0 else 0

        # 2. 순수 트레이딩 수익률 = 실현 PnL / 순 투자금 × 100
        #    입출금과 무관 — journal의 pnl_net 합계만 사용
        trading_roi_pct = round(trading_pnl / net_invested * 100, 2) if net_invested > 0 else 0

        # 3. 전체 수익률 = (현재 잔고 - 순 투자금) / 순 투자금 × 100
        total_roi_pct = round(absolute_profit / net_invested * 100, 2) if net_invested > 0 else 0

        # 4. 일별 스냅샷에서 TWR 계산
        daily_snapshots = self._get_daily_roi_series()

        # 5. TWR (Time-Weighted Return) — 입출금 보정
        twr = 1.0
        for d in daily_snapshots:
            twr *= (1 + d.get("roi_pct", 0) / 100)
        twr_pct = round((twr - 1) * 100, 2)

        # 6. 운영 기간
        created = self._baseline.get("created_ts", 0)
        days_active = round((time.time() - created) / 86400, 1) if created > 0 else 0

        # 7. 일 평균 수익률
        daily_avg_roi = round(twr_pct / max(days_active, 1), 2)

        return {
            "baseline": self._baseline,
            "current_equity": round(current_equity, 2),
            "net_invested": net_invested,
            # 절대 수치
            "absolute_profit": absolute_profit,
            "trading_pnl": round(trading_pnl, 2),
            # 수익률
            "trading_roi_pct": trading_roi_pct,
            "total_roi_pct": total_roi_pct,
            "twr_pct": twr_pct,
            # 일별 통계
            "days_active": days_active,
            "daily_avg_roi_pct": daily_avg_roi,
            "daily_snapshots_count": len(daily_snapshots),
        }

    def _get_daily_roi_series(self) -> List[Dict]:
        """일별 스냅샷에서 ROI% 시리즈 추출."""
        from app.manager.focus_daily_snapshot import get_all_snapshots
        snapshots = get_all_snapshots()
        series = []
        for snap in snapshots:
            # 스냅샷에 equity_start가 있으면 정밀 ROI 계산
            equity_start = snap.get("equity_start", 0)
            pnl = snap.get("total_pnl", 0)
            if equity_start > 0:
                roi = pnl / equity_start * 100
            elif self._baseline.get("net_invested", 0) > 0:
                roi = pnl / self._baseline["net_invested"] * 100
            else:
                roi = 0
            series.append({
                "date": snap.get("date", ""),
                "pnl": round(pnl, 2),
                "roi_pct": round(roi, 2),
            })
        return series

    def get_status(self) -> Dict:
        """현재 자본 추적 상태."""
        return {
            "baseline": self._baseline,
            "events_count": len(self.get_events()),
        }


# Singleton
capital_tracker = CapitalTracker()
