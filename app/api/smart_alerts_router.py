# ============================================================
# File: app/api/smart_alerts_router.py
# Autocoin OS v3-H — Smart Alerts API
# ------------------------------------------------------------
# Smart alert system API endpoints
# ============================================================

from __future__ import annotations

import time
from typing import Dict, List
from fastapi import APIRouter, Request, HTTPException

from app.notify.smart_alerts import get_smart_alert_manager

router = APIRouter(prefix="/api/smart-alerts", tags=["smart-alerts"])


@router.get("/status")
def get_smart_alerts_status(request: Request) -> Dict:
    """Get smart alert system status"""
    
    system = request.app.state.system
    if not hasattr(system, "smart_alert_manager") or not system.smart_alert_manager:
        return {"enabled": False, "error": "Smart alerts not initialized"}
    
    return {
        "enabled": True,
        **system.smart_alert_manager.get_status()
    }


@router.get("/loss-streaks")
def get_loss_streaks(request: Request) -> Dict:
    """Consecutive loss status"""
    
    system = request.app.state.system
    if not hasattr(system, "smart_alert_manager") or not system.smart_alert_manager:
        raise HTTPException(status_code=503, detail="Smart alerts not available")
    
    manager = system.smart_alert_manager
    
    return {
        "loss_streaks": {
            market: {
                "consecutive_losses": s.consecutive_losses,
                "total_loss_usdt": s.total_loss_usdt,
                "strategy": s.strategy,
                "alerted": s.alerted,
                "last_loss_ts": s.last_loss_ts
            }
            for market, s in manager.loss_streaks.items()
        }
    }


@router.post("/daily-report/send")
def send_daily_report_now(request: Request) -> Dict:
    """Send daily report immediately"""
    
    system = request.app.state.system
    if not hasattr(system, "smart_alert_manager") or not system.smart_alert_manager:
        raise HTTPException(status_code=503, detail="Smart alerts not available")
    
    manager = system.smart_alert_manager
    ledger_records = system.ledger.tail(2000)
    
    report = manager.generate_daily_report(ledger_records)
    manager.send_daily_report(report)
    
    return {
        "success": True,
        "report": {
            "date": report.date,
            "total_trades": report.total_trades,
            "wins": report.wins,
            "losses": report.losses,
            "win_rate": report.win_rate,
            "total_pnl_usdt": report.total_pnl_usdt,
            "best_strategy": report.best_strategy,
            "worst_strategy": report.worst_strategy,
            "alerts_count": report.alerts_count
        }
    }


@router.get("/daily-report")
def get_daily_report(request: Request) -> Dict:
    """Get daily report (without sending)"""
    
    system = request.app.state.system
    if not hasattr(system, "smart_alert_manager") or not system.smart_alert_manager:
        raise HTTPException(status_code=503, detail="Smart alerts not available")
    
    manager = system.smart_alert_manager
    ledger_records = system.ledger.tail(2000)
    
    report = manager.generate_daily_report(ledger_records)
    
    return {
        "date": report.date,
        "total_trades": report.total_trades,
        "wins": report.wins,
        "losses": report.losses,
        "win_rate": report.win_rate,
        "total_pnl_usdt": report.total_pnl_usdt,
        "best_strategy": report.best_strategy,
        "worst_strategy": report.worst_strategy,
        "top_markets": [{"market": m, "pnl_usdt": p} for m, p in report.top_markets],
        "alerts_count": report.alerts_count
    }


@router.post("/test-alert")
def test_alert(request: Request) -> Dict:
    """Send test alert"""
    
    from app.notify.telegram import send_telegram
    
    msg = "🔔 Test Alert\n\nAutocoin OS v3-H Smart Alert System is working normally."
    send_telegram(msg, cooldown_key="test_alert")
    
    return {"success": True, "message": "Test alert sent"}
