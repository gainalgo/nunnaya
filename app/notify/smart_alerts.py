# ============================================================
# File: app/notify/smart_alerts.py
# Autocoin OS v3-H — Smart Alert System
# ------------------------------------------------------------
# 1. 연속 손실 경고 (3연패 알림)
# 2. 이상 거래 탐지 (평균 대비 큰 손실)
# 3. 일일 요약 리포트 자동 발송
# ============================================================

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import deque

from app.notify.telegram import send_telegram
from app.manager.ledger_pnl import aggregate_fill_pnl

logger = logging.getLogger(__name__)


# ============================================================
# 데이터 클래스
# ============================================================

@dataclass
class LossStreak:
    """연속 손실 추적"""
    market: str
    strategy: str
    consecutive_losses: int = 0
    total_loss_usdt: float = 0.0
    last_loss_ts: float = 0.0
    alerted: bool = False


@dataclass
class AnomalyDetection:
    """이상 거래 탐지"""
    market: str
    loss_usdt: float
    avg_loss_usdt: float
    deviation_pct: float
    timestamp: float
    reason: str


@dataclass
class DailyReport:
    """일일 리포트"""
    date: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl_usdt: float = 0.0
    best_strategy: str = ""
    worst_strategy: str = ""
    top_markets: List[Tuple[str, float]] = field(default_factory=list)
    alerts_count: int = 0


# ============================================================
# SmartAlertManager
# ============================================================

class SmartAlertManager:
    """스마트 알림 관리자"""
    
    def __init__(
        self,
        *,
        loss_streak_threshold: int = 3,
        anomaly_threshold_pct: float = 200.0,  # 평균 대비 200% 이상 손실
        daily_report_hour: int = 20,  # 오후 8시
    ):
        self.loss_streak_threshold = loss_streak_threshold
        self.anomaly_threshold_pct = anomaly_threshold_pct
        self.daily_report_hour = daily_report_hour
        
        # 상태
        self.loss_streaks: Dict[str, LossStreak] = {}  # market -> LossStreak
        self.recent_losses: deque = deque(maxlen=100)  # 최근 손실 기록 (이상 탐지용)
        self.last_daily_report_date: str = ""
        
        logger.info(
            f"SmartAlertManager initialized: "
            f"loss_streak={loss_streak_threshold}, "
            f"anomaly={anomaly_threshold_pct}%, "
            f"report_hour={daily_report_hour}"
        )
    
    # ============================================================
    # 연속 손실 감지
    # ============================================================
    
    def track_trade_result(
        self,
        market: str,
        strategy: str,
        pnl_usdt: float,
        timestamp: float = None
    ):
        """거래 결과 추적 (승/패)"""
        if timestamp is None:
            timestamp = time.time()
        
        # 손실인 경우
        if pnl_usdt < 0:
            if market not in self.loss_streaks:
                self.loss_streaks[market] = LossStreak(
                    market=market,
                    strategy=strategy
                )
            
            streak = self.loss_streaks[market]
            streak.consecutive_losses += 1
            streak.total_loss_usdt += pnl_usdt
            streak.last_loss_ts = timestamp
            streak.strategy = strategy
            
            # 연속 손실 임계값 도달 시 알림
            if (streak.consecutive_losses >= self.loss_streak_threshold 
                and not streak.alerted):
                self._alert_loss_streak(streak)
                streak.alerted = True
            
            # 이상 거래 탐지용 기록
            self.recent_losses.append({
                "market": market,
                "loss": abs(pnl_usdt),
                "timestamp": timestamp
            })
            
        # 승리인 경우 - 스트릭 리셋
        elif pnl_usdt > 0:
            if market in self.loss_streaks:
                streak = self.loss_streaks[market]
                if streak.consecutive_losses > 0:
                    logger.info(
                        f"Loss streak broken for {market}: "
                        f"{streak.consecutive_losses} losses → WIN"
                    )
                del self.loss_streaks[market]
    
    def _alert_loss_streak(self, streak: LossStreak):
        """연속 손실 알림"""
        msg = (
            f"⚠️ 연속 손실 경고!\n\n"
            f"📉 마켓: {streak.market}\n"
            f"🎯 전략: {streak.strategy}\n"
            f"🔴 연속 손실: {streak.consecutive_losses}회\n"
            f"💸 누적 손실: {streak.total_loss_usdt:,.0f} USDT\n\n"
            f"전략 재검토가 필요할 수 있습니다."
        )
        
        send_telegram(msg, cooldown_key=f"loss_streak_{streak.market}")
        logger.warning(
            f"Loss streak alert: {streak.market} "
            f"({streak.consecutive_losses} losses, {streak.total_loss_usdt:,.0f} USDT)"
        )
    
    # ============================================================
    # 이상 거래 탐지
    # ============================================================
    
    def check_anomaly(self, market: str, loss_usdt: float) -> Optional[AnomalyDetection]:
        """이상 거래 탐지 (평균 대비 큰 손실)"""
        if loss_usdt >= 0:  # 손실 아니면 무시
            return None
        
        loss_abs = abs(loss_usdt)
        
        # 최근 손실 평균 계산
        if len(self.recent_losses) < 5:  # 최소 5개 샘플 필요
            return None
        
        recent_avg = sum(l["loss"] for l in self.recent_losses) / len(self.recent_losses)
        
        if recent_avg <= 0:
            return None
        
        # 편차 계산
        deviation_pct = (loss_abs / recent_avg - 1.0) * 100
        
        # 임계값 초과 시 이상 탐지
        if deviation_pct >= self.anomaly_threshold_pct:
            anomaly = AnomalyDetection(
                market=market,
                loss_usdt=loss_usdt,
                avg_loss_usdt=recent_avg,
                deviation_pct=deviation_pct,
                timestamp=time.time(),
                reason=f"Loss {deviation_pct:.0f}% above average"
            )
            
            self._alert_anomaly(anomaly)
            return anomaly
        
        return None
    
    def _alert_anomaly(self, anomaly: AnomalyDetection):
        """이상 거래 알림"""
        msg = (
            f"🚨 이상 거래 탐지!\n\n"
            f"📊 마켓: {anomaly.market}\n"
            f"💥 손실: {anomaly.loss_usdt:,.0f} USDT\n"
            f"📈 평균 손실: {anomaly.avg_loss_usdt:,.0f} USDT\n"
            f"⚡ 편차: +{anomaly.deviation_pct:.0f}%\n\n"
            f"평소보다 훨씬 큰 손실이 발생했습니다."
        )
        
        send_telegram(msg, cooldown_key=f"anomaly_{anomaly.market}_{int(anomaly.timestamp)}")
        logger.warning(
            f"Anomaly detected: {anomaly.market} "
            f"loss={anomaly.loss_usdt:,.0f}, avg={anomaly.avg_loss_usdt:,.0f}, "
            f"deviation={anomaly.deviation_pct:.0f}%"
        )
    
    # ============================================================
    # 일일 리포트
    # ============================================================
    
    def generate_daily_report(
        self,
        ledger_records: List[Dict],
        since_ts: float = None
    ) -> DailyReport:
        """일일 리포트 생성"""
        if since_ts is None:
            # 지난 24시간
            since_ts = time.time() - 86400
        
        # 날짜
        date = datetime.now().strftime("%Y-%m-%d")
        
        # 원장 데이터 집계
        until_ts = time.time()
        aggs = aggregate_fill_pnl(ledger_records, since_ts=since_ts, until_ts=until_ts)
        
        # 전체 통계
        total_trades = 0
        wins = 0
        losses = 0
        total_pnl = 0.0
        
        strategy_pnl: Dict[str, float] = {}
        market_pnl: Dict[str, float] = {}
        
        for market, agg in aggs.items():
            total_trades += agg.trade_n
            total_pnl += agg.net_cash_usdt
            
            if agg.net_cash_usdt > 0:
                wins += 1
            elif agg.net_cash_usdt < 0:
                losses += 1
            
            # 전략별 집계 (간단히 마켓 이름에서 추정)
            strategy = "UNKNOWN"
            if "PINGPONG" in market.upper():
                strategy = "PINGPONG"
            elif "AUTOLOOP" in market.upper():
                strategy = "AUTOLOOP"
            # ... (실제로는 원장에서 가져와야 함)
            
            strategy_pnl[strategy] = strategy_pnl.get(strategy, 0.0) + agg.net_cash_usdt
            market_pnl[market] = agg.net_cash_usdt
        
        # 승률
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
        
        # 최고/최악 전략
        best_strategy = max(strategy_pnl.items(), key=lambda x: x[1])[0] if strategy_pnl else ""
        worst_strategy = min(strategy_pnl.items(), key=lambda x: x[1])[0] if strategy_pnl else ""
        
        # 상위 마켓
        top_markets = sorted(market_pnl.items(), key=lambda x: x[1], reverse=True)[:5]
        
        report = DailyReport(
            date=date,
            total_trades=total_trades,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            total_pnl_usdt=total_pnl,
            best_strategy=best_strategy,
            worst_strategy=worst_strategy,
            top_markets=top_markets,
            alerts_count=len([s for s in self.loss_streaks.values() if s.alerted])
        )
        
        return report
    
    def send_daily_report(self, report: DailyReport):
        """일일 리포트 발송"""
        # 이모지 선택
        if report.total_pnl_usdt > 0:
            emoji = "📈"
            status = "이익"
        elif report.total_pnl_usdt < 0:
            emoji = "📉"
            status = "손실"
        else:
            emoji = "➖"
            status = "손익 없음"
        
        msg = (
            f"{emoji} 일일 거래 리포트 ({report.date})\n"
            f"{'=' * 30}\n\n"
            f"📊 총 거래: {report.total_trades}회\n"
            f"✅ 승리: {report.wins}회\n"
            f"❌ 손실: {report.losses}회\n"
            f"🎯 승률: {report.win_rate:.1f}%\n\n"
            f"💰 총 손익: {report.total_pnl_usdt:+,.0f} USDT ({status})\n\n"
        )
        
        if report.best_strategy:
            msg += f"🏆 최고 전략: {report.best_strategy}\n"
        if report.worst_strategy:
            msg += f"⚠️ 최악 전략: {report.worst_strategy}\n\n"
        
        if report.top_markets:
            msg += "📈 상위 마켓:\n"
            for market, pnl in report.top_markets[:3]:
                msg += f"  • {market}: {pnl:+,.0f} USDT\n"
            msg += "\n"
        
        if report.alerts_count > 0:
            msg += f"⚠️ 오늘 발생한 경고: {report.alerts_count}건\n"
        
        send_telegram(msg, cooldown_key=f"daily_report_{report.date}")
        logger.info(f"Daily report sent: {report.date}")
    
    def check_and_send_daily_report(self, ledger_records: List[Dict]):
        """일일 리포트 자동 발송 체크"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        
        # 이미 오늘 발송했으면 스킵
        if self.last_daily_report_date == today:
            return
        
        # 설정된 시간이 되었는지 체크
        if now.hour >= self.daily_report_hour:
            report = self.generate_daily_report(ledger_records)
            self.send_daily_report(report)
            self.last_daily_report_date = today
    
    # ============================================================
    # 상태 조회
    # ============================================================
    
    def get_status(self) -> Dict:
        """알림 시스템 상태 조회"""
        return {
            "loss_streaks": {
                market: {
                    "consecutive_losses": s.consecutive_losses,
                    "total_loss_usdt": s.total_loss_usdt,
                    "strategy": s.strategy,
                    "alerted": s.alerted
                }
                for market, s in self.loss_streaks.items()
            },
            "recent_losses_count": len(self.recent_losses),
            "last_daily_report_date": self.last_daily_report_date,
            "config": {
                "loss_streak_threshold": self.loss_streak_threshold,
                "anomaly_threshold_pct": self.anomaly_threshold_pct,
                "daily_report_hour": self.daily_report_hour
            }
        }


# ============================================================
# 싱글톤 인스턴스
# ============================================================
_smart_alert_manager: Optional[SmartAlertManager] = None


def get_smart_alert_manager() -> SmartAlertManager:
    """스마트 알림 관리자 싱글톤"""
    global _smart_alert_manager
    if _smart_alert_manager is None:
        _smart_alert_manager = SmartAlertManager()
    return _smart_alert_manager
