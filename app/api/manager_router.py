# ============================================================
# File: app/api/manager_router.py
# Autocoin OS v3-H — Manager (OMA) Router (RECOVERY aware)
# ============================================================

from __future__ import annotations

from fastapi import APIRouter, Request, Query
from pydantic import BaseModel
from typing import Dict, Any, List
from datetime import datetime, timezone

from app.notify.telegram import send_telegram
from app.manager.oma_market_registry import MarketState
from app.core.currency import Q

import logging
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/manager",
    tags=["manager"]
)

# ============================================================
# A. Market Administration (WATCH / ACTIVE / RECOVERY)
# ============================================================

@router.get(
    "/markets",
    summary="Get all managed markets by state",
    responses={
        200: {"description": "Markets grouped by WATCH, ACTIVE, and RECOVERY states"},
    },
)
def get_markets(request: Request):
    """
    Retrieve all OMA-managed markets grouped by state.

    - Excludes LongHold markets from the listing
    - Returns WATCH, ACTIVE, and RECOVERY market lists
    """
    system = request.app.state.system
    oma = system.oma

    snap = oma.snapshot()

    # [PATCH] Mutual Exclusion: Hide markets that are in LongHold list
    # Visually exclude LongHold coins from the OMA management lists (Active/Watch/Recovery).
    ladder = getattr(system, "ladder_manager", None)
    longhold_markets = set()
    if ladder:
        try:
            # list_longhold_configs reads from disk, but it's acceptable for admin polling
            cfgs = ladder.list_longhold_configs()
            for c in cfgs:
                if c.get("enabled"):
                    longhold_markets.add(str(c.get("market")).upper())
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[manager_router] %s: %s", "list_longhold_configs reads from disk, but it's acceptable for admin polling", exc, exc_info=True)

    def _filter(items):
        if not longhold_markets:
            return items
        return [x for x in items if str(x.get("market") if isinstance(x, dict) else x).upper() not in longhold_markets]

    return {
        "ok": True,
        "watch": _filter(snap.get("watch", [])),
        "active": _filter(snap.get("active", [])),
        "recovery": _filter(snap.get("recovery", [])),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@router.post(
    "/markets/set",
    summary="Set market state and configuration",
    responses={
        200: {"description": "Market state updated successfully"},
    },
)
def set_market_state(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    state: MarketState = Query(..., description="Target state (WATCH, ACTIVE, RECOVERY, DISABLED)"),
    reason: str | None = Query(None, description="Reason for state change"),
    budget_usdt: float | None = Query(None, description="Budget allocation in quote currency"),
    strategy: str | None = Query(None, description="Strategy mode to apply (LADDER, LIGHTNING, GAZUA, etc.)"),
):
    """
    Set the OMA state for a market with optional budget and strategy.

    - Changes market state in OMA registry
    - Optionally sets budget allocation
    - Optionally applies strategy controls
    """
    system = request.app.state.system
    
    # Check warning for markets scheduled for delisting
    delisting_warning = None
    if state == MarketState.ACTIVE:
        pass

    system.oma_set_market(
        market=market,
        state=state,
        reason=[reason] if reason else [],
        budget_usdt=budget_usdt,
    )

    # Apply strategy if provided (Fix for generic admin panel)
    if strategy and state == MarketState.ACTIVE:
        from app.manager.market_controls import apply_engine_controls
        apply_engine_controls(system, market, strategy)

    return {
        "ok": True,
        "market": market,
        "state": state,
        "reason": reason,
        "delisting_warning": delisting_warning,
    }

@router.post(
    "/markets/cleanup-watch",
    summary="Disable WATCH markets marked as deleted/stop",
    responses={200: {"description": "WATCH markets cleanup result"}},
)
def cleanup_watch_markets(
    request: Request,
    dry_run: bool = Query(False, description="If true, only report targets without changing state"),
    reason_match: str = Query("delete,stop_ui,stop_btn,user_disabled", description="Comma-separated substrings to match in reason"),
):
    system = request.app.state.system
    snap = system.oma.snapshot()
    tokens = [t.strip().lower() for t in (reason_match or "").split(",") if t.strip()]

    def _match(reasons: list) -> bool:
        if not tokens:
            return False
        for r in reasons or []:
            s = str(r).lower()
            if any(t in s for t in tokens):
                return True
        return False

    items: List[Dict[str, Any]] = []
    for it in snap.get("watch", []) or []:
        market = it.get("market")
        reasons = list(it.get("reason") or [])
        if not market or not _match(reasons):
            continue
        if dry_run:
            items.append({"market": market, "action": "would_disable", "reason": reasons})
            continue
        try:
            system.oma_set_market(
                market=market,
                state=MarketState.DISABLED,
                reason=reasons + ["user_disabled_cleanup"],
            )
            new_state = None
            try:
                st = system.oma_registry.get_state(market)
                new_state = st.value if hasattr(st, "value") else str(st)
            except (KeyError, AttributeError, TypeError):
                logger.warning("manager_router._match L170 except", exc_info=True)
                new_state = None
            items.append({"market": market, "action": "disabled", "state": new_state, "reason": reasons})
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("manager_router._match L173: %s", exc)
            items.append({"market": market, "action": "error", "error": str(exc), "reason": reasons})

    return {"ok": True, "count": len(items), "dry_run": dry_run, "items": items}

# ============================================================
# B. Risk Band View (READ ONLY)
# ============================================================

@router.get(
    "/risk-bands",
    summary="Get risk band status for all markets",
    responses={
        200: {"description": "Risk band information for each market"},
    },
)
def get_risk_bands(request: Request):
    """
    Retrieve risk band status for all markets.

    - Shows current risk band, unlock status, and capital ratio
    - Read-only view for OMA decision making
    """
    system = request.app.state.system
    coordinator = system.coordinator

    items: List[Dict[str, Any]] = []

    for market, ctx in coordinator.contexts.items():
        risk = getattr(ctx, "risk_state", None)
        if not isinstance(risk, dict) or not risk:
            continue

        items.append({
            "market": market,
            "band": risk.get("band"),
            "unlock": risk.get("unlock"),
            "cap_ratio": risk.get("cap_ratio"),
            "cap_usdt": risk.get("cap_usdt"),
            "reason": risk.get("reason"),
            "ts": risk.get("ts"),
        })

    return {
        "ok": True,
        "timestamp": datetime.utcnow().isoformat(),
        "items": items,
    }

# ============================================================
# C. OMA Execution Approval (WRITE)
# ============================================================

@router.post(
    "/approve",
    summary="Approve market execution in UNLOCK state",
    responses={
        200: {"description": "Market execution approved and engine started"},
        400: {"description": "Market is not in UNLOCK state"},
    },
)
def approve_execution(
    request: Request,
    market: str = Query(..., description="Market code to approve (e.g., BTCUSDT)"),
):
    """
    Approve execution for a market that has reached UNLOCK state.

    - Records approval in unlock history
    - Starts the engine for the market
    - Sends Telegram notification
    """
    system = request.app.state.system
    coordinator = system.coordinator

    ctx = coordinator.get_context(market)
    risk = getattr(ctx, "risk_state", None)

    if not isinstance(risk, dict) or not risk.get("unlock"):
        return {"ok": False, "reason": "Market is not UNLOCK state"}

    record = {
        "market": market,
        "band": risk.get("band"),
        "approved_at": datetime.utcnow().isoformat(),
    }
    system.oma.unlock_history.append(record)

    system.engine.start(market)

    send_telegram(
        f"✅ OMA Approved\n"
        f"Market: {market}\n"
        f"Risk: {risk.get('band')}\n"
        f"Time: {record['approved_at']}"
    )

    return {"ok": True, "record": record}

# ============================================================
# D. UNLOCK History (READ ONLY)
# ============================================================

@router.get(
    "/unlock-history",
    summary="Get unlock approval history",
    responses={
        200: {"description": "List of past unlock approvals"},
    },
)
def get_unlock_history(request: Request):
    """
    Retrieve the history of unlock approvals.

    - Read-only audit trail of approved executions
    """
    system = request.app.state.system
    return {"ok": True, "items": system.oma.unlock_history}

# ============================================================
# E. Recovery Manual Liquidation (WRITE)
# ============================================================

@router.post(
    "/recovery/liquidate",
    summary="Manually liquidate a RECOVERY market",
    responses={
        200: {"description": "Liquidation request processed"},
        400: {"description": "Order FSM not available"},
    },
)
def recovery_liquidate(
    request: Request,
    market: str = Query(..., description="Market code to liquidate (e.g., BTCUSDT)"),
    reason: str | None = Query(None, description="Reason for manual liquidation"),
):
    """
    Manually liquidate all positions for a RECOVERY market.

    - Executes full position sell via order FSM
    - Can be used with HOLD policy when manual intervention needed
    - Fails if order_fsm is not available
    """
    system = request.app.state.system
    res = system.request_recovery_liquidate(market=market, reason=reason or "manual")
    return {"ok": bool(res.get("ok")), "result": res}

# ============================================================
# F. Manual Trade (Convenience)
# ============================================================

class ManualTradeReq(BaseModel):
    market: str
    side: str           # 'buy' | 'sell'
    ord_type: str       # 'market' | 'limit'
    input_val: float    # User input value (Amount or Percent)
    input_unit: str     # 'pct' | 'abs'
    price: float | None = None  # Required for limit order

@router.post(
    "/trade/manual",
    summary="Execute a manual trade via Exchange",
    responses={
        200: {"description": "Trade executed successfully"},
        400: {"description": "Invalid trade parameters"},
    },
)
def manual_trade(request: Request, req: ManualTradeReq):
    """
    Execute a manual buy or sell trade directly via Exchange API.

    - **side**: "buy" or "sell"
    - **ord_type**: "market" or "limit"
    - **input_val**: Amount value (absolute or percentage)
    - **input_unit**: "pct" (percent of balance) or "abs" (absolute value)
    - **price**: Required for limit orders
    """
    system = request.app.state.system
    trade_client = system.trade_client

    # 1. Balance Check
    bal_quote = float(trade_client.get_balance(Q.symbol) or 0)
    bal_coin = float(trade_client.get_balance(req.market) or 0)

    # 2. Determine Order Parameters
    final_price = req.price
    final_vol = 0.0
    final_total_usdt = 0.0

    try:
        if req.side == 'buy':
            # BUY Logic
            budget = 0.0
            if req.input_unit == 'pct':
                budget = bal_quote * (req.input_val / 100.0)
            else: # abs
                # For Market Buy, input is quote currency. For Limit Buy, input is Qty usually, but let's assume quote currency for simplicity in this UI context?
                # Actually, standard is: Market Buy -> quote currency, Limit Buy -> Qty.
                # But to unify UI, let's assume 'abs' input for Limit Buy is Qty.
                if req.ord_type == 'limit':
                    final_vol = req.input_val
                else:
                    budget = req.input_val

            if req.ord_type == 'market':
                return {"ok": True, "result": trade_client.buy_market_order(req.market, budget)}
            else:
                # Limit Buy
                if not final_price: raise ValueError("Price required for limit buy")
                if req.input_unit == 'pct':
                    final_vol = budget / final_price
                return {"ok": True, "result": trade_client.buy_limit_order(req.market, final_price, final_vol)}

        else:
            # SELL Logic
            if req.input_unit == 'pct':
                final_vol = bal_coin * (req.input_val / 100.0)
            else:
                final_vol = req.input_val

            if req.ord_type == 'market':
                return {"ok": True, "result": trade_client.sell_market_order(req.market, final_vol)}
            else:
                if not final_price: raise ValueError("Price required for limit sell")
                return {"ok": True, "result": trade_client.sell_limit_order(req.market, final_price, final_vol)}

    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("manager_router.manual_trade L403: %s", e)
        return {"ok": False, "error": str(e)}

# ============================================================
# G. PnL Management
# ============================================================

@router.post(
    "/pnl/reset",
    summary="Reset PnL history",
    responses={
        200: {"description": "PnL history cleared"},
        400: {"description": "profit_store not available"},
    },
)
def reset_pnl(request: Request):
    """
    Clear the PnL (profit/loss) history.

    - Resets the profit_store to empty
    - Used for starting fresh tracking period
    """
    system = request.app.state.system
    # Assume profit_store is a list (closed trades) and reset it
    if hasattr(system, "profit_store") and isinstance(system.profit_store, list):
        system.profit_store.clear()
        return {"ok": True, "msg": "PnL history cleared"}
    
    return {"ok": False, "error": "profit_store not found or not a list"}

# ============================================================
# H. External Trade Sync
# ============================================================

@router.post(
    "/sync-external-trades",
    summary="Sync external trades from Exchange",
    responses={
        200: {"description": "External trades synced to ledger"},
        500: {"description": "Sync failed"},
    },
)
def sync_external_trades(
    request: Request,
    market: str | None = Query(None, description="Specific market to sync (e.g., BTCUSDT). If empty, syncs all holdings."),
    max_pages: int = Query(5, ge=1, le=20, description="Max pages to fetch from Exchange"),
    lookback_days: int = Query(30, ge=1, le=365, description="Days to look back"),
):
    """
    Sync external trades (executed outside this app) from Exchange to the ledger.

    - Queries exchange for completed orders
    - Records missing trades as FILL_SYNC_BUY/FILL_SYNC_SELL events
    - Enables accurate PnL calculation for externally executed trades
    
    Use cases:
    - Manual trades via Exchange app/web
    - Trades from other bots/tools
    - Initial setup when migrating to this system
    """
    from app.manager.external_trade_sync import ExternalTradeSync

    system = request.app.state.system
    
    try:
        trade_client = system.trade_client
        ledger = system.ledger
        
        syncer = ExternalTradeSync(trade_client=trade_client, ledger=ledger)
        
        if market:
            result = syncer.sync_market(
                market=market.upper(),
                max_pages=max_pages,
                lookback_days=lookback_days,
            )
        else:
            # Sync all current holdings from Exchange accounts
            accounts = trade_client.accounts(skip_currencies=[Q.symbol])
            holdings = {}
            for a in accounts:
                cur = str(a.get("currency") or "").upper()
                if not cur:
                    continue
                qty = float(a.get("balance") or 0) + float(a.get("locked") or 0)
                if qty <= 0:
                    continue
                mkt = Q.market(cur)
                holdings[mkt] = {
                    "qty": qty,
                    "avg_buy_price": float(a.get("avg_buy_price") or 0),
                }
            
            result = syncer.sync_all_holdings(
                holdings=holdings,
                max_pages=max_pages,
                lookback_days=lookback_days,
            )
        
        return {"ok": True, **result}
    
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("manager_router.sync_external_trades L506: %s", e)
        return {"ok": False, "error": str(e)}

@router.get(
    "/cost-basis/{market}",
    summary="Get cost basis for a market",
    responses={
        200: {"description": "Cost basis calculated from Exchange trade history"},
        500: {"description": "Failed to calculate"},
    },
)
def get_cost_basis(
    request: Request,
    market: str,
    max_pages: int = Query(10, ge=1, le=50, description="Max pages to fetch"),
):
    """
    Calculate the actual cost basis for a market from Exchange trade history.

    Returns:
    - Total buy/sell amounts
    - Net quantity held
    - Average buy price
    - Cost basis in USDT
    """
    system = request.app.state.system
    
    try:
        trade_client = system.trade_client
        result = trade_client.calculate_cost_basis(
            market=market.upper(),
            max_pages=max_pages,
        )
        return {"ok": True, **result}
    
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("manager_router.get_cost_basis L542: %s", e)
        return {"ok": False, "error": str(e)}

# ============================================================
# Legacy API Compatibility: /api/markets/candidate
# ------------------------------------------------------------
# Candidate market lookup endpoint for legacy dashboard compatibility
# ============================================================

legacy_markets_router = APIRouter(
    prefix="/api/markets",
    tags=["legacy"],
)

@legacy_markets_router.get(
    "/candidate",
    summary="Get candidate markets (legacy compatibility)",
    responses={
        200: {"description": "List of candidate markets from reserved queue"},
    },
)
def get_candidate_markets(
    request: Request,
    no: int = Query(0, description="Page number"),
    cacheStart: int = Query(0, description="Cache start timestamp"),
    s: int = Query(1, description="Sort order"),
):
    """
    Legacy endpoint for fetching candidate markets.
    
    Returns candidates from the reserved queue for backward compatibility
    with older dashboard versions.
    """
    from app.manager.reserved_queue import reserved_queue
    
    system = request.app.state.system
    
    try:
        # Get reserved queue items
        queue_snap = reserved_queue.snapshot()
        items = queue_snap.get("items", [])
        
        # Transform to legacy format
        candidates = []
        for item in items:
            candidates.append({
                "market": item.get("market", ""),
                "strategy": item.get("strategy", ""),
                "score": item.get("ai_score") or item.get("score", 0),
                "suggested_budget": item.get("suggested_budget_usdt") or item.get("suggested_budget_usdt", 0),
                "volume_24h": item.get("volume_24h", 0),
                "change_rate": item.get("change_rate", 0),
                "rid": item.get("rid", ""),
            })
        
        # Sort if requested
        if s == 1:
            candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        
        return {
            "ok": True,
            "candidates": candidates,
            "total": len(candidates),
            "page": no,
        }
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("manager_router.get_candidate_markets L609: %s", e)
        return {"ok": True, "candidates": [], "total": 0, "page": no, "error": str(e)}

# ============================================================
# Legacy API: /api/market/candles
# ------------------------------------------------------------
# Candle data lookup (legacy compatibility)
# ============================================================

legacy_market_router = APIRouter(
    prefix="/api/market",
    tags=["legacy"],
)

@legacy_market_router.get(
    "/candles",
    summary="Get market candles (legacy compatibility)",
    responses={
        200: {"description": "Candle data for the market"},
    },
)
def get_market_candles(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    interval: str = Query("15", description="Candle interval: 1,3,5,15,60,240 (minutes) or D,W,M (day/week/month)"),
    count: int = Query(100, ge=1, le=200, description="Number of candles"),
):
    """
    Legacy endpoint for fetching candle data.
    Supports: 1,3,5,10,15,30,60,240 (minute candles), D (daily), W (weekly), M (monthly)
    """
    from app.core.rate_limiter import bybit_get
    from app.core.constants import BYBIT_MARKET_KLINE, bybit_v5_rest_category, parse_bybit_list

    try:
        # Bybit V5 kline API: interval mapping
        # D=D, W=W, M=M, minutes=1,3,5,15,30,60,120,240,360,720
        interval_map = {"D": "D", "W": "W", "M": "M"}
        bybit_interval = interval_map.get(interval.upper(), interval)
        params = {"category": bybit_v5_rest_category(), "symbol": market, "interval": bybit_interval, "limit": count}

        resp = bybit_get(BYBIT_MARKET_KLINE, params=params, timeout=5.0)
        if resp.status_code == 200:
            raw = parse_bybit_list(resp.json())
            candles = []
            for k in raw:
                if isinstance(k, (list, tuple)) and len(k) >= 6:
                    candles.append({
                        "timestamp": int(k[0]),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    })
            # Bybit returns newest-first; reverse for chronological
            candles.reverse()
            return {"ok": True, "candles": candles, "count": len(candles)}
        else:
            return {"ok": False, "error": f"Bybit API error: {resp.status_code}"}
    except Exception as e:
        logger.warning("manager_router.get_market_candles L671: %s", e)
        return {"ok": False, "error": str(e)}

# ============================================================
# Legacy API: /api/longhold/*
# ------------------------------------------------------------
# LongHold API (legacy compatibility - alias of ladder_router)
# ============================================================

legacy_longhold_router = APIRouter(
    prefix="/api/longhold",
    tags=["legacy"],
)

@legacy_longhold_router.get(
    "/list",
    summary="List LongHold configurations (legacy)",
    responses={
        200: {"description": "List of LongHold configurations"},
    },
)
def longhold_list_legacy(request: Request):
    """
    Legacy endpoint for LongHold list.
    Proxies to /api/ladder/longhold/list
    """
    system = request.app.state.system
    mgr = getattr(system, "ladder_manager", None)
    
    if not mgr:
        return {"ok": True, "items": []}
    
    try:
        return {"ok": True, "items": mgr.list_longhold_configs()}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("manager_router.longhold_list_legacy L707: %s", e)
        return {"ok": False, "error": str(e), "items": []}

@legacy_longhold_router.get(
    "/recommend",
    summary="Get LongHold recommendations (legacy)",
    responses={
        200: {"description": "Recommended markets for LongHold"},
    },
)
def longhold_recommend_legacy(
    request: Request,
    n: int = Query(10, ge=1, le=50, description="Number of recommendations"),
):
    """
    Legacy endpoint for LongHold recommendations.
    """
    system = request.app.state.system
    mgr = getattr(system, "ladder_manager", None)
    
    if not mgr:
        return {"ok": True, "recommendations": [], "count": 0}
    
    try:
        result = mgr.scan_longhold_candidates(
            strategy="GAZUA",
            n=n,
            method="candles",
        )
        candidates = result.get("candidates", [])
        return {
            "ok": True,
            "recommendations": candidates,
            "count": len(candidates),
        }
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("manager_router.longhold_recommend_legacy L743: %s", e)
        return {"ok": False, "error": str(e), "recommendations": [], "count": 0}
