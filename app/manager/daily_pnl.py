"""
Daily PnL Report Module
- Save a daily PnL snapshot at midnight each day
- Query daily/weekly/monthly statistics
- Per-market and per-strategy performance analysis

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

# Storage path
DAILY_PNL_DIR = "runtime/daily_pnl"


@dataclass
class DailyReport:
    """Daily report"""
    date: str  # "2026-01-23"

    # Overall summary
    total_pnl_usdt: float = 0.0
    total_trades: int = 0
    buy_count: int = 0
    sell_count: int = 0
    total_fees_usdt: float = 0.0

    # Win rate (based on sells)
    win_count: int = 0
    lose_count: int = 0

    # Per-market detail
    markets: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Per-strategy detail (if any)
    strategies: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Meta
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
    """Daily PnL manager"""
    
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
    # Save / Load
    # --------------------------------------------------------
    def save_report(self, report: DailyReport) -> None:
        """Save a report"""
        from app.core.io_utils import safe_write_json
        path = self._date_to_path(report.date)
        report.updated_ts = time.time()
        if report.created_ts == 0:
            report.created_ts = report.updated_ts

        with self._lock:
            safe_write_json(path, report.to_dict())
    
    def load_report(self, date_str: str) -> Optional[DailyReport]:
        """Load the report for a specific date"""
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
        """Load today's report"""
        return self.load_report(self._today_str())

    # --------------------------------------------------------
    # Aggregation
    # --------------------------------------------------------
    def aggregate_from_ledger(
        self,
        ledger_records: List[Dict[str, Any]],
        date_str: Optional[str] = None,
    ) -> DailyReport:
        """Aggregate PnL for a specific date from the ledger"""
        if date_str is None:
            date_str = self._today_str()

        # Start/end timestamps for the given date
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            logger.warning("[DailyPnl] generate: invalid date_str=%r", date_str, exc_info=True)
            dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        start_ts = dt.timestamp()
        end_ts = (dt + timedelta(days=1)).timestamp()

        # Aggregate
        aggs = aggregate_fill_pnl(
            ledger_records,
            since_ts=start_ts,
            until_ts=end_ts,
        )

        # Build the report
        report = DailyReport(date=date_str)
        
        for market, agg in aggs.items():
            report.total_pnl_usdt += agg.net_cash_usdt
            report.total_trades += agg.trade_n
            report.buy_count += agg.buy_n
            report.sell_count += agg.sell_n
            report.total_fees_usdt += agg.fees_usdt

            # Per-market detail
            report.markets[market] = {
                "pnl_usdt": agg.net_cash_usdt,
                "trades": agg.trade_n,
                "buy_n": agg.buy_n,
                "sell_n": agg.sell_n,
                "fees_usdt": agg.fees_usdt,
            }

            # Simple win/loss verdict (based on per-market net_cash_usdt)
            if agg.sell_n > 0:
                if agg.net_cash_usdt > 0:
                    report.win_count += 1
                elif agg.net_cash_usdt < 0:
                    report.lose_count += 1
        
        return report
    
    def snapshot_today(self, ledger_records: List[Dict[str, Any]]) -> DailyReport:
        """Save today's snapshot

        Args:
            ledger_records: Ledger records (passed already filtered)
        """
        today = self._today_str()
        report = self.aggregate_from_ledger(ledger_records, today)
        self.save_report(report)
        logger.info(f"[DailyPnL] Saved snapshot: {report.date}, PnL={report.total_pnl_usdt:,.0f} USDT")
        return report
    
    # --------------------------------------------------------
    # Queries
    # --------------------------------------------------------
    def list_dates(self, limit: int = 30) -> List[str]:
        """List of stored dates (newest first)"""
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
        """Query reports over a date range"""
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
        """Summary of the last N days"""
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
    # Send daily report via Telegram
    # --------------------------------------------------------
    def send_daily_report_telegram(self, report: Optional[DailyReport] = None) -> bool:
        """Send the daily report via Telegram

        Args:
            report: Report to send (None means today's report)

        Returns:
            Whether the send succeeded
        """
        try:
            from app.notify.telegram import send_telegram
            
            if report is None:
                report = self.load_today()
            
            if report is None:
                logger.warning("[DailyPnL] No report to send")
                return False
            
            # Win rate calculation
            win_rate = report.win_rate * 100

            # PnL color / icon
            pnl = report.total_pnl_usdt
            pnl_icon = "📈" if pnl >= 0 else "📉"
            pnl_sign = "+" if pnl >= 0 else ""

            # 7-day summary
            summary = self.get_summary(7)
            weekly_pnl = summary.get("total_pnl_usdt", 0)
            weekly_sign = "+" if weekly_pnl >= 0 else ""
            weekly_win_rate = summary.get("win_rate", 0) * 100

            # Per-strategy performance (if any)
            strategy_lines = []
            if report.strategies:
                for strat, data in report.strategies.items():
                    strat_pnl = data.get("pnl_usdt", 0)
                    strat_trades = data.get("trades", 0)
                    strat_sign = "+" if strat_pnl >= 0 else ""
                    strategy_lines.append(f"  • {strat}: {strat_sign}{strat_pnl:,.2f} USDT ({strat_trades} trades)")

            strategy_section = ""
            if strategy_lines:
                strategy_section = "\n📊 *By strategy:*\n" + "\n".join(strategy_lines)

            # Build the message
            message = (
                f"📋 *Daily Report* ({report.date})\n\n"
                f"{pnl_icon} *Today's PnL:* {pnl_sign}{pnl:,.2f} USDT\n"
                f"🎯 *Win rate:* {win_rate:.1f}% ({report.win_count}W/{report.lose_count}L)\n"
                f"📦 *Trades:* {report.total_trades} (buys {report.buy_count} / sells {report.sell_count})\n"
                f"💸 *Fees:* {report.total_fees_usdt:,.2f} USDT\n"
                f"{strategy_section}\n\n"
                f"📅 *Last 7 days:*\n"
                f"  • Total PnL: {weekly_sign}{weekly_pnl:,.2f} USDT\n"
                f"  • Average win rate: {weekly_win_rate:.1f}%\n\n"
                f"_Automated report_"
            )
            
            send_telegram(message)
            logger.info(f"[DailyPnL] Telegram report sent: {report.date}")
            return True
            
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.error(f"[DailyPnL] Telegram send failed: {e}")
            return False


# Singleton
_daily_pnl_manager: Optional[DailyPnLManager] = None

def get_daily_pnl_manager() -> DailyPnLManager:
    global _daily_pnl_manager
    if _daily_pnl_manager is None:
        _daily_pnl_manager = DailyPnLManager()
    return _daily_pnl_manager
