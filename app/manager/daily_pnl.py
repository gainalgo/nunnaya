"""
Daily PnL Report Module
- 매일 자정에 일별 손익 스냅샷 저장
- 일별/주별/월별 통계 조회
- 마켓별, 전략별 성과 분석

[CREATED 2026-01-23]
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from threading import Lock
import logging

from app.manager.ledger_pnl import aggregate_fill_pnl, MarketFillAgg

logger = logging.getLogger(__name__)

# 저장 경로
DAILY_PNL_DIR = "runtime/daily_pnl"


@dataclass
class DailyReport:
    """일별 리포트"""
    date: str  # "2026-01-23"
    
    # 전체 요약
    total_pnl_usdt: float = 0.0
    total_trades: int = 0
    buy_count: int = 0
    sell_count: int = 0
    total_fees_usdt: float = 0.0
    
    # 승률 (매도 기준)
    win_count: int = 0
    lose_count: int = 0
    
    # 마켓별 상세
    markets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    # 전략별 상세 (있으면)
    strategies: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    # 메타
    created_ts: float = 0.0
    updated_ts: float = 0.0
    
    @property
    def win_rate(self) -> float:
        total = self.win_count + self.lose_count
        if total == 0:
            return 0.0
        return self.win_count / total
    
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["win_rate"] = self.win_rate
        return d
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DailyReport":
        return cls(
            date=d.get("date", ""),
            total_pnl_usdt=float(d.get("total_pnl_usdt", 0.0)),
            total_trades=int(d.get("total_trades", 0)),
            buy_count=int(d.get("buy_count", 0)),
            sell_count=int(d.get("sell_count", 0)),
            total_fees_usdt=float(d.get("total_fees_usdt", 0.0)),
            win_count=int(d.get("win_count", 0)),
            lose_count=int(d.get("lose_count", 0)),
            markets=d.get("markets", {}),
            strategies=d.get("strategies", {}),
            created_ts=float(d.get("created_ts", 0.0)),
            updated_ts=float(d.get("updated_ts", 0.0)),
        )


class DailyPnLManager:
    """일별 손익 관리자"""
    
    def __init__(self, base_dir: str = DAILY_PNL_DIR):
        self.base_dir = base_dir
        self._lock = Lock()
        self._ensure_dir()
    
    def _ensure_dir(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
    
    def _date_to_path(self, date_str: str) -> str:
        return os.path.join(self.base_dir, f"{date_str}.json")
    
    def _today_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")
    
    # --------------------------------------------------------
    # 저장/로드
    # --------------------------------------------------------
    def save_report(self, report: DailyReport) -> None:
        """리포트 저장"""
        from app.core.io_utils import safe_write_json
        path = self._date_to_path(report.date)
        report.updated_ts = time.time()
        if report.created_ts == 0:
            report.created_ts = report.updated_ts

        with self._lock:
            safe_write_json(path, report.to_dict())
    
    def load_report(self, date_str: str) -> Optional[DailyReport]:
        """특정 날짜 리포트 로드"""
        path = self._date_to_path(date_str)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return DailyReport.from_dict(d)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"[DailyPnL] Failed to load {date_str}: {e}")
            return None
    
    def load_today(self) -> Optional[DailyReport]:
        """오늘 리포트 로드"""
        return self.load_report(self._today_str())
    
    # --------------------------------------------------------
    # 집계
    # --------------------------------------------------------
    def aggregate_from_ledger(
        self,
        ledger_records: List[Dict[str, Any]],
        date_str: Optional[str] = None,
    ) -> DailyReport:
        """원장에서 특정 날짜의 PnL 집계"""
        if date_str is None:
            date_str = self._today_str()
        
        # 해당 날짜의 시작/끝 타임스탬프
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            logger.warning("[DailyPnl] generate: invalid date_str=%r", date_str, exc_info=True)
            dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        start_ts = dt.timestamp()
        end_ts = (dt + timedelta(days=1)).timestamp()
        
        # 집계
        aggs = aggregate_fill_pnl(
            ledger_records,
            since_ts=start_ts,
            until_ts=end_ts,
        )
        
        # 리포트 생성
        report = DailyReport(date=date_str)
        
        for market, agg in aggs.items():
            report.total_pnl_usdt += agg.net_cash_usdt
            report.total_trades += agg.trade_n
            report.buy_count += agg.buy_n
            report.sell_count += agg.sell_n
            report.total_fees_usdt += agg.fees_usdt
            
            # 마켓별 상세
            report.markets[market] = {
                "pnl_usdt": agg.net_cash_usdt,
                "trades": agg.trade_n,
                "buy_n": agg.buy_n,
                "sell_n": agg.sell_n,
                "fees_usdt": agg.fees_usdt,
            }
            
            # 간단한 승패 판정 (마켓별 net_cash_usdt 기준)
            if agg.sell_n > 0:
                if agg.net_cash_usdt > 0:
                    report.win_count += 1
                elif agg.net_cash_usdt < 0:
                    report.lose_count += 1
        
        return report
    
    def snapshot_today(self, ledger_records: List[Dict[str, Any]]) -> DailyReport:
        """오늘 스냅샷 저장
        
        Args:
            ledger_records: 원장 레코드 (이미 필터링된 상태로 전달)
        """
        today = self._today_str()
        report = self.aggregate_from_ledger(ledger_records, today)
        self.save_report(report)
        logger.info(f"[DailyPnL] Saved snapshot: {report.date}, PnL={report.total_pnl_usdt:,.0f} USDT")
        return report
    
    # --------------------------------------------------------
    # 조회
    # --------------------------------------------------------
    def list_dates(self, limit: int = 30) -> List[str]:
        """저장된 날짜 목록 (최신순)"""
        try:
            files = os.listdir(self.base_dir)
            dates = []
            for f in files:
                if f.endswith(".json") and len(f) == 15:  # "2026-01-23.json"
                    dates.append(f[:-5])
            dates.sort(reverse=True)
            return dates[:limit]
        except (AttributeError, TypeError, ValueError):
            logger.warning("[DailyPnl] list_dates: listdir/parse failed", exc_info=True)
            return []
    
    def get_range(
        self,
        start_date: str,
        end_date: str,
    ) -> List[DailyReport]:
        """날짜 범위 리포트 조회"""
        reports = []
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            logger.warning("[DailyPnl] get_range: invalid date range %s ~ %s", start_date, end_date, exc_info=True)
            return reports

        current = start
        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            report = self.load_report(date_str)
            if report:
                reports.append(report)
            current += timedelta(days=1)
        
        return reports
    
    def get_summary(self, days: int = 7) -> Dict[str, Any]:
        """최근 N일 요약"""
        dates = self.list_dates(limit=days)
        
        total_pnl = 0.0
        total_trades = 0
        total_wins = 0
        total_loses = 0
        daily_pnls: List[Dict[str, Any]] = []
        
        for date_str in dates:
            report = self.load_report(date_str)
            if report:
                total_pnl += report.total_pnl_usdt
                total_trades += report.total_trades
                total_wins += report.win_count
                total_loses += report.lose_count
                daily_pnls.append({
                    "date": report.date,
                    "pnl_usdt": report.total_pnl_usdt,
                    "trades": report.total_trades,
                    "win_rate": report.win_rate,
                })
        
        win_total = total_wins + total_loses
        return {
            "days": len(dates),
            "total_pnl_usdt": total_pnl,
            "avg_daily_pnl_usdt": total_pnl / len(dates) if dates else 0.0,
            "total_trades": total_trades,
            "win_rate": total_wins / win_total if win_total > 0 else 0.0,
            "daily": daily_pnls,
        }


    # --------------------------------------------------------
    # 텔레그램 일일 리포트 발송
    # --------------------------------------------------------
    def send_daily_report_telegram(self, report: Optional[DailyReport] = None) -> bool:
        """일일 리포트를 텔레그램으로 발송
        
        Args:
            report: 발송할 리포트 (None이면 오늘 리포트)
        
        Returns:
            발송 성공 여부
        """
        try:
            from app.notify.telegram import send_telegram
            
            if report is None:
                report = self.load_today()
            
            if report is None:
                logger.warning("[DailyPnL] No report to send")
                return False
            
            # 승률 계산
            win_rate = report.win_rate * 100
            
            # 수익 색상/아이콘
            pnl = report.total_pnl_usdt
            pnl_icon = "📈" if pnl >= 0 else "📉"
            pnl_sign = "+" if pnl >= 0 else ""
            
            # 7일 요약
            summary = self.get_summary(7)
            weekly_pnl = summary.get("total_pnl_usdt", 0)
            weekly_sign = "+" if weekly_pnl >= 0 else ""
            weekly_win_rate = summary.get("win_rate", 0) * 100
            
            # 전략별 성과 (있으면)
            strategy_lines = []
            if report.strategies:
                for strat, data in report.strategies.items():
                    strat_pnl = data.get("pnl_usdt", 0)
                    strat_trades = data.get("trades", 0)
                    strat_sign = "+" if strat_pnl >= 0 else ""
                    strategy_lines.append(f"  • {strat}: {strat_sign}{strat_pnl:,.2f} USDT ({strat_trades}건)")
            
            strategy_section = ""
            if strategy_lines:
                strategy_section = "\n📊 *전략별:*\n" + "\n".join(strategy_lines)
            
            # 메시지 구성
            message = (
                f"📋 *일일 리포트* ({report.date})\n\n"
                f"{pnl_icon} *오늘 손익:* {pnl_sign}{pnl:,.2f} USDT\n"
                f"🎯 *승률:* {win_rate:.1f}% ({report.win_count}승/{report.lose_count}패)\n"
                f"📦 *거래:* {report.total_trades}건 (매수 {report.buy_count} / 매도 {report.sell_count})\n"
                f"💸 *수수료:* {report.total_fees_usdt:,.2f} USDT\n"
                f"{strategy_section}\n\n"
                f"📅 *최근 7일:*\n"
                f"  • 총 손익: {weekly_sign}{weekly_pnl:,.2f} USDT\n"
                f"  • 평균 승률: {weekly_win_rate:.1f}%\n\n"
                f"_자동 발송 리포트_"
            )
            
            send_telegram(message)
            logger.info(f"[DailyPnL] Telegram report sent: {report.date}")
            return True
            
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error(f"[DailyPnL] Telegram send failed: {e}")
            return False


# 싱글톤
_daily_pnl_manager: Optional[DailyPnLManager] = None

def get_daily_pnl_manager() -> DailyPnLManager:
    global _daily_pnl_manager
    if _daily_pnl_manager is None:
        _daily_pnl_manager = DailyPnLManager()
    return _daily_pnl_manager
