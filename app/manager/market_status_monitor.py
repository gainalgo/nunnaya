# -*- coding: utf-8 -*-
"""
Market Status Monitor - detect market status changes (pending delisting, new listing).

File: app/manager/market_status_monitor.py

Features:
1. Detect pending delisting among active markets → alert
2. Detect new listing (PREVIEW → ACTIVE) → alert
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

from app.integrations.bybit_markets import fetch_bybit_markets as fetch_bybit_markets
from app.core.currency import Q

# State file path
DEFAULT_STATE_PATH = "runtime/market_status_state.json"

def _load_state(path: str = DEFAULT_STATE_PATH) -> Dict[str, Any]:
    """Load previous market status."""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("[market_status_monitor] %s: %s", 'market_status_monitor._load_state fallback', exc, exc_info=True)
    return {"known_markets": {}, "last_check_ts": 0}

def _save_state(data: Dict[str, Any], path: str = DEFAULT_STATE_PATH) -> None:
    """Save market status."""
    from app.core.io_utils import safe_write_json
    try:
        safe_write_json(path, data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("[market_status_monitor] %s: %s", 'market_status_monitor._save_state fallback', exc, exc_info=True)

def check_market_status_changes(
    active_markets: Optional[Set[str]] = None,
    state_path: str = DEFAULT_STATE_PATH,
) -> Dict[str, Any]:
    """
    Detect market status changes.

    Args:
        active_markets: set of currently active markets (OMA ACTIVE state)

    Returns:
        {
            "delisting_alerts": [{market, delisting_date, korean_name}, ...],
            "new_listings": [{market, korean_name}, ...],
            "preview_markets": [{market, korean_name}, ...],  # awaiting listing
        }
    """
    result = {
        "delisting_alerts": [],
        "new_listings": [],
        "preview_markets": [],
        "checked_at": time.time(),
    }
    
    try:
        # Fetch Bybit market info (isDetails=true)
        markets = fetch_bybit_markets(is_details=True, timeout=10.0)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
        logger.warning("[MarketStatusMonitor] check_market_status: fetch failed", exc_info=True)
        return result

    # Load previous state
    prev_state = _load_state(state_path)
    prev_known = prev_state.get("known_markets", {})
    
    current_known: Dict[str, Dict[str, Any]] = {}
    active_markets = active_markets or set()
    
    for m in markets:
        code = str(m.get("market") or "").upper()
        if not code.startswith(Q.config.market_prefix):
            continue
        
        market_state = str(m.get("market_state") or "").upper()
        delist_date = m.get("delisting_date")
        korean_name = str(m.get("korean_name") or "").strip()
        
        current_known[code] = {
            "market_state": market_state,
            "delisting_date": str(delist_date) if delist_date else None,
            "korean_name": korean_name,
        }
        
        prev_info = prev_known.get(code, {})
        prev_market_state = prev_info.get("market_state", "")
        
        # 1. Detect pending delisting: active market is DELISTED, or a new delisting_date appeared
        is_delisting = (market_state == "DELISTED") or (delist_date is not None and str(delist_date).strip() != "")
        was_delisting = prev_info.get("delisting_date") is not None

        if is_delisting and not was_delisting:
            # Newly scheduled for delisting
            if code in active_markets:
                result["delisting_alerts"].append({
                    "market": code,
                    "delisting_date": str(delist_date) if delist_date else None,
                    "korean_name": korean_name,
                    "severity": "critical",  # active market
                })
            else:
                result["delisting_alerts"].append({
                    "market": code,
                    "delisting_date": str(delist_date) if delist_date else None,
                    "korean_name": korean_name,
                    "severity": "info",
                })
        
        # 2. Detect new listing: PREVIEW → ACTIVE
        if market_state == "ACTIVE" and prev_market_state == "PREVIEW":
            result["new_listings"].append({
                "market": code,
                "korean_name": korean_name,
                "listed_at": time.time(),
            })
        
        # 3. List of markets awaiting listing
        if market_state == "PREVIEW":
            result["preview_markets"].append({
                "market": code,
                "korean_name": korean_name,
            })
    
    # Save state
    _save_state({
        "known_markets": current_known,
        "last_check_ts": time.time(),
    }, state_path)
    
    return result

def get_active_delisting_markets(
    active_markets: Set[str],
) -> List[Dict[str, Any]]:
    """
    Query active markets that are scheduled for delisting.

    Can be called during reconcile to warn immediately.
    """
    alerts = []
    
    try:
        markets = fetch_bybit_markets(is_details=True, timeout=10.0)
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.warning("[MarketStatusMonitor] fetch failed (network): %s", e)
        return alerts
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError):
        logger.warning("[MarketStatusMonitor] check_active_market_alerts: fetch failed", exc_info=True)
        return alerts

    for m in markets:
        code = str(m.get("market") or "").upper()
        if code not in active_markets:
            continue
        
        market_state = str(m.get("market_state") or "").upper()
        delist_date = m.get("delisting_date")
        
        is_delisting = (market_state == "DELISTED") or (delist_date is not None and str(delist_date).strip() != "")
        if is_delisting:
            alerts.append({
                "market": code,
                "market_state": market_state,
                "delisting_date": str(delist_date) if delist_date else None,
                "korean_name": str(m.get("korean_name") or "").strip(),
            })
    
    return alerts
